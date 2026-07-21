# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Filesystem bridge for Codex-in-Pier Guardian integration.

Pier's Codex harness can register MCP servers, but Codex also has a reliable
normal shell surface.  This bridge keeps Guardian's LLM-backed findings in a
well-known file so Codex can read them with ``cat`` at task checkpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Optional

# Stub codeminer's top-level __init__ so its eager litellm/tree-sitter imports
# don't run in the container (those deps may not be installed).
# __path__ must be set so Python can still find codeminer.guardian submodules.
if "codeminer" not in sys.modules:
    _stub = types.ModuleType("codeminer")
    _stub.__package__ = "codeminer"
    # Find the codeminer package directory on sys.path
    for _p in sys.path:
        _candidate = os.path.join(_p, "codeminer")
        if os.path.isdir(_candidate):
            _stub.__path__ = [_candidate]
            break
    sys.modules["codeminer"] = _stub

from codeminer.guardian.cycle import GuardianConfig, run_cycle
from codeminer.guardian.report import GuardianReport, render_markdown


def _apply_vertex_token_patch() -> None:
    """Patch LiteLLM to use GOOGLE_OAUTH_ACCESS_TOKEN instead of ADC refresh.

    LiteLLM's Vertex AI handler always calls oauth2.googleapis.com to refresh
    the ADC credentials, even when a valid access token is available in the
    environment.  Inside Pier's sandboxed container, oauth2.googleapis.com DNS
    is blocked; the host pre-fetches a short-lived token and passes it via this
    env var.  The patch bypasses the token-refresh network call entirely.
    """
    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if not token:
        return

    import datetime

    try:
        from google.oauth2.credentials import Credentials as _Creds
        from litellm.llms.vertex_ai.vertex_llm_base import VertexBase
    except ImportError:
        return

    # Read project_id from ADC so LiteLLM knows which GCP project to bill.
    _project_id: Optional[str] = None
    _adc = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    try:
        with open(_adc) as _f:
            _project_id = json.load(_f).get("quota_project_id")
    except Exception:
        pass

    _token = token  # capture for closure

    def _load_auth(self, credentials, project_id):  # type: ignore[override]
        creds = _Creds(token=_token)
        creds.expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        return creds, project_id or _project_id or ""

    VertexBase.load_auth = _load_auth  # type: ignore[method-assign]
    VertexBase.refresh_auth = lambda self, credentials: None  # type: ignore[method-assign]


def _head(repo_path: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_json_atomic(path: Path, data: object) -> None:
    _write_text_atomic(path, json.dumps(data, indent=2, default=str) + "\n")


def _write_report(out_dir: Path, report: GuardianReport) -> None:
    _write_text_atomic(out_dir / "findings.md", render_markdown(report))
    _write_json_atomic(out_dir / "findings.json", report.to_dict())

    def _tok(usage: object) -> dict:
        return {
            "prompt": getattr(usage, "prompt_tokens", 0) or 0,
            "completion": getattr(usage, "completion_tokens", 0) or 0,
            "total": getattr(usage, "total_tokens", 0) or 0,
        }

    _write_json_atomic(
        out_dir / "status.json",
        {
            "commit": report.commit,
            "generated_at": report.generated_at,
            "findings": len(report.findings),
            "llm_model": report.llm_model,
            "llm_backend": report.llm_backend,
            "llm_transport_history": report.llm_transport_history,
            "llm_tokens": _tok(report.llm_usage) if report.llm_usage else _tok(None),
            "outer_llm_tokens": (
                _tok(report.outer_llm_usage) if report.outer_llm_usage else _tok(None)
            ),
            "inner_llm_tokens": (
                _tok(report.inner_llm_usage) if report.inner_llm_usage else _tok(None)
            ),
            "running": False,
            "error": "",
        },
    )


def _write_status(
    out_dir: Path,
    *,
    commit: str,
    running: bool,
    error: str = "",
    llm_model: str = "",
    llm_backend: str = "",
) -> None:
    findings_path = out_dir / "findings.md"
    if not findings_path.exists():
        state = "running" if running else "not running"
        _write_text_atomic(
            findings_path,
            "# Repository Guardian Report\n\n"
            f"Guardian analysis is {state} for commit `{commit[:12]}`.\n"
            "No completed findings are available yet.\n",
        )
    _write_json_atomic(
        out_dir / "status.json",
        {
            "commit": commit,
            "generated_at": "",
            "findings": 0,
            "llm_model": llm_model,
            "llm_backend": llm_backend,
            "llm_transport_history": [llm_backend] if llm_backend else [],
            "llm_tokens": 0,
            "running": running,
            "error": error,
        },
    )


def run_bridge(
    config: GuardianConfig,
    *,
    out_dir: str,
    poll_interval: int,
    once: bool = False,
) -> None:
    """Run Guardian on initial HEAD and on every subsequent commit."""
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    last_commit: Optional[str] = None

    while True:
        commit = _head(config.repo_path)
        if commit and commit != last_commit:
            _write_status(
                output,
                commit=commit,
                running=True,
                llm_model=config.llm_model,
                llm_backend="pending",
            )
            print(
                f"guardian-codex-bridge: analyzing {commit[:8]}",
                file=sys.stderr,
                flush=True,
            )
            try:
                report = run_cycle(config)
                _write_report(output, report)
                last_commit = commit
                print(
                    "guardian-codex-bridge: wrote "
                    f"{len(report.findings)} finding(s) for {commit[:8]}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                _write_status(
                    output,
                    commit=commit,
                    running=False,
                    error=str(exc),
                    llm_model=config.llm_model,
                    llm_backend="error",
                )
                print(
                    f"guardian-codex-bridge: cycle failed for {commit[:8]}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

        if once:
            return
        time.sleep(max(1, poll_interval))


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Write live LLM-backed Guardian findings for Codex to read."
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out-dir", default="~/.guardian")
    parser.add_argument("--arm", choices=["memory", "memoryless"], default="memory")
    parser.add_argument("--memory-dir", default="~/.guardian/memory")
    parser.add_argument("--model", default="codex:gpt-5.6-luna")
    parser.add_argument("--index-types", nargs="+", default=["bm25"])
    parser.add_argument("--since", default="90 days ago")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--budget-tokens", type=int, default=50_000)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    out_dir = os.path.expanduser(args.out_dir)
    memory_dir = os.path.expanduser(args.memory_dir)

    if args.model.startswith(("vertex_ai/", "gemini/")):
        _apply_vertex_token_patch()

    cfg = GuardianConfig(
        repo_path=args.repo,
        index_types=tuple(args.index_types),
        use_llm=True,
        llm_model=args.model,
        memory_dir=memory_dir,
        arm=args.arm,
        since=args.since,
        top_n=args.top_n,
        budget_tokens=args.budget_tokens,
        episode_dir="/logs/agent/guardian_episode",
    )
    run_bridge(cfg, out_dir=out_dir, poll_interval=args.poll_interval, once=args.once)


if __name__ == "__main__":
    main()
