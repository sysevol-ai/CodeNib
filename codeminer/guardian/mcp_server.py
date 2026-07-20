# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Guardian MCP server — stdio transport.

Implements the Model Context Protocol (MCP) over stdin/stdout so that coding
agents (claude-code, codex, etc.) can call ``query_guardian`` as a tool while
Guardian monitors the repo autonomously in the background.

Two concurrently-running pieces:

- **Guardian watcher (background thread)**: polls ``git HEAD`` every
  ``POLL_INTERVAL`` seconds; on each new commit runs a full Guardian cycle and
  caches the findings.  Starts immediately on process launch.
- **MCP stdio loop (main thread)**: handles JSON-RPC 2.0 requests from the
  coding agent.  ``query_guardian`` is a pure read from the cache — it never
  triggers a cycle.

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

POLL_INTERVAL = 10          # seconds between HEAD polls
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "guardian"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Shared cache (watcher writes, MCP handler reads)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "findings": [],     # List[Dict] — last cycle's findings
    "commit": "",       # SHA of the commit that produced them
    "cycle_no": 0,      # how many cycles have completed
    "running": False,   # True while a cycle is in progress
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _head(repo_path: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _finding_to_dict(f: Finding) -> Dict[str, Any]:
    d = asdict(f)
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

def _watcher(config: GuardianConfig) -> None:
    """Poll for new commits and run a Guardian cycle on each one.

    Runs an initial cycle immediately so findings are available as soon as the
    coding agent starts working, then polls every POLL_INTERVAL seconds.
    """
    last_commit = ""

    while True:
        commit = _head(config.repo_path)

        if commit and commit != last_commit:
            with _lock:
                _cache["running"] = True

            _stderr(f"guardian: new commit {commit[:8]} — starting cycle {_cache['cycle_no'] + 1}")
            try:
                report = run_cycle(config)
                findings = [_finding_to_dict(f) for f in report.findings]
                with _lock:
                    _cache["findings"] = findings
                    _cache["commit"] = commit
                    _cache["cycle_no"] += 1
                    _cache["running"] = False
                last_commit = commit
                _stderr(
                    f"guardian: cycle {_cache['cycle_no']} done — "
                    f"{len(findings)} finding(s) cached"
                )
            except Exception as exc:  # noqa: BLE001
                with _lock:
                    _cache["running"] = False
                _stderr(f"guardian: cycle failed for {commit[:8]}: {exc}")

        time.sleep(POLL_INTERVAL)


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
        text = " ".join([
            f.get("title", ""),
            f.get("detail", ""),
            f.get("hypothesis", ""),
            f.get("narrative", ""),
            " ".join(e.get("file", "") for e in f.get("evidence", [])),
        ]).lower()
        if region_paths and any(p in text for p in region_paths):
            return True
        if hyp_tokens and any(t in text for t in hyp_tokens):
            return True
        return False

    filtered = [f for f in findings if _matches(f)]
    return filtered if filtered else findings


def _handle_query_guardian(arguments: Dict[str, Any]) -> str:
    hypothesis: str = arguments.get("hypothesis", "")
    region: Optional[List[str]] = arguments.get("region")

    with _lock:
        findings = list(_cache["findings"])
        commit = _cache["commit"]
        running = _cache["running"]
        cycle_no = _cache["cycle_no"]

    relevant = _filter_findings(findings, hypothesis, region)

    return json.dumps({
        "commit": commit,
        "cycle_no": cycle_no,
        "cycle_running": running,
        "total_findings": len(findings),
        "returned_findings": len(relevant),
        "findings": relevant,
    }, indent=2)


# ---------------------------------------------------------------------------
# MCP JSON-RPC 2.0 protocol (stdio)
# ---------------------------------------------------------------------------

_stdout_lock = threading.Lock()

_TOOL_SCHEMA = {
    "name": "query_guardian",
    "description": (
        "Ask the Repository Guardian for its latest findings about the repo. "
        "Guardian runs continuously in the background, firing a new analysis "
        "cycle on each commit. Call this when you are about to make a risky "
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
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": message}})


def _handle_request(msg: Dict[str, Any]) -> None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        # Notification — no response required
        return

    if method == "initialize":
        _respond(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

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
            _respond(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except Exception as exc:  # noqa: BLE001
            _respond(req_id, {
                "content": [{"type": "text", "text": f"guardian error: {exc}"}],
                "isError": True,
            })

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
            _stderr(f"guardian: error handling '{msg.get('method')}': {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Repository Guardian MCP server (stdio transport)"
    )
    parser.add_argument("--repo", required=True,
                        help="Path to the repository to monitor")
    parser.add_argument("--arm", choices=["memory", "memoryless"], default="memory",
                        help="'memory' (default) or 'memoryless' ablation arm")
    parser.add_argument("--memory-dir", default=None,
                        help="Cross-cycle memory store path (enables memory arm)")
    parser.add_argument("--model", default="vertex_ai/gemini-2.5-flash",
                        help="LiteLLM model string for Guardian's LLM calls")
    parser.add_argument("--index-types", nargs="+", default=["bm25"],
                        help="Index types: bm25, vector, ...")
    parser.add_argument("--since", default="90 days ago",
                        help="Churn window for git log")
    parser.add_argument("--top-n", type=int, default=5,
                        help="Max findings per cycle")
    parser.add_argument("--budget-tokens", type=int, default=50_000,
                        help="Token budget per Guardian cycle")
    parser.add_argument("--no-llm", action="store_true",
                        help="Heuristic signals only — no LLM calls (faster)")
    parser.add_argument("--poll-interval", type=int, default=POLL_INTERVAL,
                        help=f"Seconds between HEAD polls (default: {POLL_INTERVAL})")
    args = parser.parse_args(argv)

    global POLL_INTERVAL
    POLL_INTERVAL = args.poll_interval

    config = GuardianConfig(
        repo_path=args.repo,
        index_types=tuple(args.index_types),
        use_llm=not args.no_llm,
        llm_model=args.model,
        memory_dir=args.memory_dir,
        arm=args.arm,
        since=args.since,
        top_n=args.top_n,
        budget_tokens=args.budget_tokens,
    )

    watcher = threading.Thread(target=_watcher, args=(config,), daemon=True)
    watcher.start()

    _stderr(
        f"guardian: MCP server ready "
        f"(repo={args.repo}, arm={args.arm}, poll={POLL_INTERVAL}s)"
    )

    _stdio_loop()


if __name__ == "__main__":
    main()
