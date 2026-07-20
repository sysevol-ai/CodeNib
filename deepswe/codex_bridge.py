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
from pathlib import Path
from typing import Optional

from codeminer.guardian.cycle import GuardianConfig, run_cycle
from codeminer.guardian.report import GuardianReport, render_markdown


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
    _write_json_atomic(
        out_dir / "status.json",
        {
            "commit": report.commit,
            "generated_at": report.generated_at,
            "findings": len(report.findings),
            "llm_tokens": (
                getattr(report.llm_usage, "total_tokens", 0)
                if report.llm_usage is not None
                else 0
            ),
            "running": False,
            "error": "",
        },
    )


def _write_status(out_dir: Path, *, commit: str, running: bool, error: str = "") -> None:
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
            _write_status(output, commit=commit, running=True)
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
                _write_status(output, commit=commit, running=False, error=str(exc))
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
    parser.add_argument("--out-dir", default="/app/.guardian")
    parser.add_argument("--arm", choices=["memory", "memoryless"], default="memory")
    parser.add_argument("--memory-dir", default="/app/.guardian/memory")
    parser.add_argument("--model", default="vertex_ai/gemini-2.5-flash")
    parser.add_argument("--index-types", nargs="+", default=["bm25"])
    parser.add_argument("--since", default="90 days ago")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--budget-tokens", type=int, default=50_000)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    out_dir = os.path.expanduser(args.out_dir)
    memory_dir = os.path.expanduser(args.memory_dir)

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
