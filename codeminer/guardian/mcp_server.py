# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Guardian MCP server — stdio transport.

Implements the Model Context Protocol (MCP) over stdin/stdout so that coding
agents (claude-code, codex, etc.) can start and query Guardian with one
``query_guardian`` action.

Two concurrently-running pieces:

- **Guardian watcher (background thread)**: starts on the first
  ``query_guardian`` action, treats the setup-time commit as cycle 0, and runs
  full cycles only for later commits.
- **MCP stdio loop (main thread)**: handles JSON-RPC 2.0 requests from the
  coding agent.

Usage::

    python -m codeminer.guardian.mcp_server \\
        --repo /app \\
        --arm memory \\
        --memory-dir /app/.guardian/memory \\
        --model vertex_ai/gemini-2.5-flash

Claude Code MCP config (injected by the custom Pier agent)::

    {
      "mcpServers": {
        "guardian": {
          "command": "python",
          "args": ["-m", "codeminer.guardian.mcp_server",
                   "--repo", "/app",
                   "--arm", "memory",
                   "--memory-dir", "/app/.guardian/memory"]
        }
      }
    }
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .cycle import GuardianConfig, run_cycle
from .report import Finding

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL = 10  # seconds between HEAD polls
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "guardian"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Shared cache (watcher writes, MCP handler reads)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "findings": [],  # List[Dict] — last cycle's findings
    "backlog": [],
    "degraded": False,
    "analysis_status": "pending",
    "commit": "",  # SHA of the commit that produced them
    "cycle_no": 0,  # how many cycles have completed
    "running": False,  # True while a cycle is in progress
    "started": False,
    "baseline_commit": "",
    "observed_head": "",
}
_start_lock = threading.Lock()
_watcher_config: Optional[GuardianConfig] = None
_watcher_baseline = ""
_watcher_thread: Optional[threading.Thread] = None

# Trace log — set by main() from --trace-log arg; None disables tracing.
_trace_log_path: Optional[str] = None
_trace_lock = threading.Lock()
_call_counter = 0


def _trace(record: Dict[str, Any]) -> None:
    if not _trace_log_path:
        return
    line = json.dumps(record, default=str) + "\n"
    with _trace_lock:
        try:
            with open(_trace_log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _finding_to_dict(f: Finding) -> Dict[str, Any]:
    d = asdict(f)
    d.update(
        {
            "title": f.claim,
            "detail": f.consequence,
            "hypothesis": f.claim,
            "verdict": "confirmed",
            "kind": f.origin,
        }
    )
    d["evidence"] = [
        {
            "file": e["file"],
            "node": e["node_name"],
            "lines": f"{e['start_line']}-{e['end_line']}",
            "score": e["score"],
        }
        for e in d.get("evidence", [])
    ]
    return d


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Guardian watcher (background thread)
# ---------------------------------------------------------------------------


def _poll_once(config: GuardianConfig, last_commit: str) -> str:
    """Observe HEAD once, analyzing it only when it follows ``last_commit``."""

    commit = _head(config.repo_path)
    with _lock:
        _cache["observed_head"] = commit

    if not commit or commit == last_commit:
        return last_commit

    with _lock:
        _cache["running"] = True
        next_cycle = _cache["cycle_no"] + 1

    _stderr(f"guardian: new commit {commit[:8]} — starting cycle {next_cycle}")
    try:
        report = run_cycle(config)
        findings = [_finding_to_dict(f) for f in report.findings]
        backlog = [asdict(item) for item in getattr(report, "backlog", [])]
        with _lock:
            _cache["findings"] = findings
            _cache["backlog"] = backlog
            _cache["degraded"] = bool(getattr(report, "degraded", False))
            _cache["analysis_status"] = str(
                getattr(report, "analysis_status", "complete")
            )
            _cache["commit"] = commit
            _cache["cycle_no"] += 1
            _cache["running"] = False
            cycle_no = _cache["cycle_no"]
        _stderr(
            f"guardian: cycle {cycle_no} done — " f"{len(findings)} finding(s) cached"
        )
        return commit
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _cache["running"] = False
            _cache["degraded"] = True
            _cache["analysis_status"] = "failed"
        _stderr(f"guardian: cycle failed for {commit[:8]}: {exc}")
        return last_commit


def _watcher(config: GuardianConfig, baseline_commit: str) -> None:
    """Poll for new commits; the baseline is observed but never analyzed."""

    last_commit = baseline_commit
    while True:
        last_commit = _poll_once(config, last_commit)

        time.sleep(POLL_INTERVAL)


def _ensure_watcher_started() -> bool:
    """Start monitoring once, in response to the coding agent's action."""

    global _watcher_thread
    with _start_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return False
        if _watcher_config is None:
            raise RuntimeError("Guardian watcher is not configured")
        watcher = threading.Thread(
            target=_watcher,
            args=(_watcher_config, _watcher_baseline),
            daemon=True,
            name="guardian-commit-watcher",
        )
        _watcher_thread = watcher
        with _lock:
            _cache["started"] = True
            _cache["baseline_commit"] = _watcher_baseline
            _cache["commit"] = _watcher_baseline
            _cache["observed_head"] = _head(_watcher_config.repo_path)
        watcher.start()
        _stderr(
            "guardian: monitoring started by query_guardian "
            f"(baseline={_watcher_baseline[:8]})"
        )
        return True


# ---------------------------------------------------------------------------
# query_guardian tool
# ---------------------------------------------------------------------------


def _filter_findings(
    findings: List[Dict[str, Any]],
    hypothesis: str,
    region: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """Return findings relevant to hypothesis / region; falls back to all."""
    if not hypothesis and not region:
        return findings

    region_paths = {r.split(":")[0].lower() for r in region} if region else set()
    hyp_tokens = {t.lower() for t in hypothesis.split() if len(t) > 3}

    def _matches(f: Dict[str, Any]) -> bool:
        text = " ".join(
            [
                f.get("title", ""),
                f.get("detail", ""),
                f.get("hypothesis", ""),
                f.get("narrative", ""),
                " ".join(f.get("locus", [])),
                " ".join(e.get("file", "") for e in f.get("evidence", [])),
            ]
        ).lower()
        if region_paths and any(p in text for p in region_paths):
            return True
        if hyp_tokens and any(t in text for t in hyp_tokens):
            return True
        return False

    filtered = [f for f in findings if _matches(f)]
    return filtered if filtered else findings


def _handle_query_guardian(arguments: Dict[str, Any]) -> str:
    global _call_counter
    started_now = _ensure_watcher_started()
    hypothesis: str = arguments.get("hypothesis", "")
    region: Optional[List[str]] = arguments.get("region")

    with _lock:
        findings = list(_cache["findings"])
        backlog = list(_cache["backlog"])
        degraded = bool(_cache["degraded"])
        analysis_status = str(_cache["analysis_status"])
        commit = _cache["commit"]
        running = _cache["running"]
        cycle_no = _cache["cycle_no"]
        started = _cache["started"]
        baseline_commit = _cache["baseline_commit"]
        observed_head = _cache["observed_head"]

    relevant = _filter_findings(findings, hypothesis, region)

    with _trace_lock:
        _call_counter += 1
        call_no = _call_counter

    result = {
        "commit": commit,
        "cycle_no": cycle_no,
        "cycle_running": running,
        "guardian_started": started,
        "started_by_this_action": started_now,
        "baseline_commit": baseline_commit,
        "observed_head": observed_head,
        "status": (
            "running"
            if running
            else (
                "baseline_unchanged"
                if cycle_no == 0 and observed_head == baseline_commit
                else ("pending" if cycle_no == 0 else "ready")
            )
        ),
        "total_findings": len(findings),
        "returned_findings": len(relevant),
        "findings": relevant,
        "backlog": backlog,
        "high_confidence_backlog": sum(
            float(item.get("confidence", 0.0) or 0.0) >= 0.8 for item in backlog
        ),
        "degraded": degraded,
        "analysis_status": analysis_status,
    }

    _trace(
        {
            "call_no": call_no,
            "ts": datetime.datetime.utcnow().isoformat() + "Z",
            "hypothesis": hypothesis,
            "region": region,
            "commit": commit,
            "cycle_no": cycle_no,
            "guardian_started": started,
            "started_by_this_action": started_now,
            "total_findings": len(findings),
            "returned_findings": len(relevant),
            "finding_titles": [f.get("title", "") for f in relevant],
        }
    )

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# MCP JSON-RPC 2.0 protocol (stdio)
# ---------------------------------------------------------------------------

_stdout_lock = threading.Lock()

_TOOL_SCHEMA = {
    "name": "query_guardian",
    "description": (
        "Ask the Repository Guardian for its latest findings about the repo. "
        "This action starts Guardian if needed. The setup-time HEAD is retained "
        "as cycle 0 without an LLM analysis; later coding-agent commits trigger "
        "analysis cycles. Call this when you are about to make a risky "
        "change, when tests start failing unexpectedly, or when you want a "
        "second opinion on whether a refactor might break existing contracts."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "hypothesis": {
                "type": "string",
                "description": (
                    "What you suspect might be a problem. "
                    "E.g. 'I refactored parse_config — did I break any callers?' "
                    "Used to filter findings to the most relevant ones."
                ),
            },
            "region": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of file paths or file:function pairs to focus on. "
                    "E.g. ['igel/feature_schema.py', 'igel/igel.py:fit']. "
                    "If omitted, all findings are returned."
                ),
            },
        },
        "required": ["hypothesis"],
    },
}


def _send(obj: Dict[str, Any]) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _respond(req_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _respond_error(req_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle_request(msg: Dict[str, Any]) -> None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        # Notification — no response required
        return

    if method == "initialize":
        _respond(
            req_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    elif method == "ping":
        _respond(req_id, {})

    elif method == "tools/list":
        _respond(req_id, {"tools": [_TOOL_SCHEMA]})

    elif method == "tools/call":
        name = params.get("name")
        if name != "query_guardian":
            _respond_error(req_id, -32601, f"Unknown tool: {name}")
            return
        try:
            text = _handle_query_guardian(params.get("arguments") or {})
            _respond(
                req_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            )
        except Exception as exc:  # noqa: BLE001
            _respond(
                req_id,
                {
                    "content": [{"type": "text", "text": f"guardian error: {exc}"}],
                    "isError": True,
                },
            )

    else:
        _respond_error(req_id, -32601, f"Method not found: {method}")


def _stdio_loop() -> None:
    """Read newline-delimited JSON-RPC from stdin until EOF."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _stderr(f"guardian: bad JSON on stdin: {exc}")
            continue
        try:
            _handle_request(msg)
        except Exception as exc:  # noqa: BLE001
            _stderr(f"guardian: error handling {msg.get('method')!r}: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> None:
    global POLL_INTERVAL, _trace_log_path, _watcher_config, _watcher_baseline
    parser = argparse.ArgumentParser(
        description="Repository Guardian MCP server (stdio transport)"
    )
    parser.add_argument(
        "--repo", required=True, help="Path to the repository to monitor"
    )
    parser.add_argument(
        "--arm",
        choices=["memory", "memoryless"],
        default="memory",
        help="'memory' (default) or 'memoryless' ablation arm",
    )
    parser.add_argument(
        "--memory-dir",
        default=None,
        help="Cross-cycle memory store path (enables memory arm)",
    )
    parser.add_argument(
        "--model",
        default="vertex_ai/gemini-2.5-flash",
        help="LiteLLM model string for Guardian's LLM calls",
    )
    parser.add_argument(
        "--index-types",
        nargs="+",
        default=["bm25"],
        help="Index types: bm25, vector, ...",
    )
    parser.add_argument(
        "--since", default="90 days ago", help="Churn window for git log"
    )
    parser.add_argument("--top-n", type=int, default=5, help="Max findings per cycle")
    budget_group = parser.add_mutually_exclusive_group()
    budget_group.add_argument(
        "--budget-tokens",
        type=int,
        default=50_000,
        help="Token budget per Guardian cycle",
    )
    budget_group.add_argument(
        "--no-budget-limit",
        action="store_true",
        help=(
            "Disable the cycle token limit while retaining turn and wall-clock limits"
        ),
    )
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=200_000,
        help="Model context window allocated to the L2 agent loop",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Heuristic signals only — no LLM calls (faster)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=POLL_INTERVAL,
        help=f"Seconds between HEAD polls (default: {POLL_INTERVAL})",
    )
    parser.add_argument(
        "--trace-log",
        default=None,
        help="Path to append one JSON line per query_guardian call",
    )
    parser.add_argument(
        "--baseline-commit",
        default=None,
        help="Cycle-0 commit to observe without analyzing.",
    )
    parser.add_argument(
        "--baseline-file",
        default=None,
        help="File containing the setup-time cycle-0 commit.",
    )
    args = parser.parse_args(argv)

    POLL_INTERVAL = args.poll_interval
    _trace_log_path = args.trace_log

    config = GuardianConfig(
        repo_path=args.repo,
        index_types=tuple(args.index_types),
        use_llm=not args.no_llm,
        llm_model=args.model,
        memory_dir=args.memory_dir,
        arm=args.arm,
        since=args.since,
        top_n=args.top_n,
        budget_tokens=None if args.no_budget_limit else args.budget_tokens,
        max_context_tokens=args.max_context_tokens,
    )
    baseline_commit = args.baseline_commit
    if args.baseline_file:
        try:
            with open(args.baseline_file, encoding="utf-8") as handle:
                baseline_commit = handle.read().strip()
        except OSError as exc:
            parser.error(f"cannot read --baseline-file: {exc}")
    if not baseline_commit:
        baseline_commit = _head(args.repo)
    _watcher_config = config
    _watcher_baseline = baseline_commit
    with _lock:
        _cache.update(
            {
                "findings": [],
                "commit": baseline_commit,
                "cycle_no": 0,
                "running": False,
                "started": False,
                "baseline_commit": baseline_commit,
                "observed_head": _head(args.repo),
            }
        )

    _stderr(
        f"guardian: MCP server ready; waiting for query_guardian "
        f"(repo={args.repo}, baseline={baseline_commit[:8]}, arm={args.arm})"
    )

    _stdio_loop()


if __name__ == "__main__":
    main()
