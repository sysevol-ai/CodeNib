#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Run the ContextBench source agent through an existing Claude subscription.

Source-checkout experiment; use the official Agent SDK and logged-in CLI, with
only the same three CodeNib source tools. No OpenRouter or API-key fallback.
SDK cost estimates are retained separately from actual billing, which is unknown.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tiktoken

from codenib.agent.tool_schema import tool_to_schema
from codenib.agent.tools.defaults import get_default_tool_specs
from scripts import benchmark_context_agent as native
from scripts.benchmark_jev_base import digest, write_json

TOOLS = {f"mcp__source__{name}": name for name in ("read", "grep", "glob")}


def subscription_auth(cli):
    """Inspect authentication through the supported CLI, never read OAuth secrets."""
    if any(
        os.environ.get(name)
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        )
    ):
        raise ValueError("Subscription experiment requires the existing CLI login only")
    result = subprocess.run(
        [cli, "auth", "status", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    status = json.loads(result.stdout)
    if not status.get("loggedIn") or status.get("authMethod") != "claude.ai":
        raise ValueError("Claude Code is not logged into a Claude subscription")
    return {
        key: status.get(key)
        for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
    }


def normalize_event(kind, event):
    """Convert actual SDK tool-result text, never requested ranges, into views."""
    messages = []
    if kind == "AssistantMessage":
        text, calls = [], []
        for block in event["content"]:
            if "name" in block and "input" in block:
                if block["name"] not in TOOLS:
                    raise ValueError(f"Unexpected SDK tool: {block['name']}")
                calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": TOOLS[block["name"]],
                            "arguments": json.dumps(block["input"]),
                        },
                    }
                )
            elif "text" in block:
                text.append(block["text"])
        messages.append({"role": "assistant", "content": "\n".join(text)})
        if calls:
            messages[-1]["tool_calls"] = calls
    elif kind == "UserMessage" and isinstance(event["content"], list):
        for block in event["content"]:
            if "tool_use_id" not in block:
                continue
            content = block.get("content") or ""
            if isinstance(content, list):
                content = "\n".join(
                    part["text"]
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "content": content,
                }
            )
    return messages


async def retrieve(*, repo, query_text, row, checkpoint, cli, model, max_turns):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query, tool

    sdk_tools = []
    for spec in get_default_tool_specs():
        if spec.tool_id not in TOOLS.values():
            continue
        argument = "file_path" if spec.tool_id == "read" else "path"
        execute = native._bind_source_tool(
            spec.executor_fn, repo=repo.resolve(), path_argument=argument
        )

        def bind(function):
            async def handler(arguments):
                try:
                    result = await asyncio.to_thread(function, **arguments)
                except Exception as exc:
                    result = f"Error: {type(exc).__name__}: {exc}"
                return {
                    "content": [{"type": "text", "text": result}],
                    "isError": result.startswith("Error"),
                }

            return handler

        schema = tool_to_schema(spec)["function"]
        sdk_tools.append(
            tool(spec.tool_id, schema["description"], schema["parameters"])(
                bind(execute)
            )
        )
    options = ClaudeAgentOptions(
        cli_path=cli,
        cwd=repo,
        model=model,
        effort="medium",
        max_turns=max_turns,
        system_prompt=native.SYSTEM.replace(
            "20 model turns", f"{max_turns} model turns"
        ),
        tools=[],
        allowed_tools=list(TOOLS),
        permission_mode="default",
        setting_sources=[],
        skills=[],
        plugins=[],
        agents={},
        mcp_servers={"source": create_sdk_mcp_server("source", tools=sdk_tools)},
        strict_mcp_config=True,
        extra_args={"no-session-persistence": None},
    )
    async for message in query(prompt=query_text, options=options):
        kind = type(message).__name__
        data = dataclasses.asdict(message)
        row["sdk_events"].append({"kind": kind, "data": data})
        if kind == "SystemMessage" and data.get("subtype") == "init":
            advertised = set(data["data"].get("tools", []))
            if advertised != set(TOOLS):
                raise ValueError(f"SDK tool isolation differs: {sorted(advertised)}")
        row["messages"].extend(normalize_event(kind, data))
        if kind == "ResultMessage":
            row["sdk_result"] = data
        checkpoint()
    if "sdk_result" not in row:
        raise RuntimeError("SDK stream ended without a terminal result")
    result = row["sdk_result"]
    row.update(
        status=(
            "budget_exhausted"
            if result["subtype"] == "error_max_turns"
            else "error" if result["is_error"] else "ok"
        ),
        answer=result.get("result", ""),
        total_turns=result["num_turns"],
        sdk_reported_cost_usd=result.get("total_cost_usd"),
    )


def run(args):
    if not args.live:
        raise ValueError("Claude subscription inference requires --live")
    cli = shutil.which("claude")
    if not cli:
        raise ValueError("Claude Code CLI is not installed")
    if not 1 <= args.max_turns <= 40:
        raise ValueError("max_turns must be in [1, 40]")
    auth = subscription_auth(cli)
    manifest, cases = native.load_inputs(args.prepared)
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(
        args.output / "config.json",
        {
            "prepared": str(args.prepared.resolve()),
            "manifest_sha256": digest(args.prepared / "manifest.json"),
            "implementation": native.freeze(args.output),
            "backend": "claude-agent-sdk",
            "sdk_version": importlib.metadata.version("claude-agent-sdk"),
            "cli_version": subprocess.check_output(
                [cli, "--version"], text=True
            ).strip(),
            "auth": auth,
            "model_requested": args.model,
            "max_turns": args.max_turns,
            "tools": list(TOOLS),
            "scope": (
                "Subscription-backed SDK harness using only CodeNib source tools. "
                "Runtime differs from native AgentRunner; report actual returned model "
                "and SDK metering separately from unknown actual billing. No API-key or"
                " OpenRouter fallback, no solver."
            ),
        },
    )
    encoder = tiktoken.get_encoding("cl100k_base")
    rows, stopped = [], False
    for entry in manifest["cases"]:
        case = cases[entry["instance_id"]]
        path = args.output / f"{case.instance_id}.json"
        row = {
            "instance_id": case.instance_id,
            "status": "not_run",
            "calls": [],
            "sdk_events": [],
            "messages": [
                {
                    "role": "system",
                    "content": native.SYSTEM.replace(
                        "20 model turns", f"{args.max_turns} model turns"
                    ),
                },
                {"role": "user", "content": case.problem_statement},
            ],
        }
        started = time.perf_counter()
        repo = None
        if not stopped:
            row["status"] = "running"
            try:
                source = json.loads(Path(entry["source"]).read_text())
                repo = Path(source["repo"]).resolve()
                row["snapshot_before"] = native.audit_contextbench_snapshot(
                    repo, case.base_commit
                )
                asyncio.run(
                    retrieve(
                        repo=repo,
                        query_text=case.problem_statement,
                        row=row,
                        checkpoint=lambda path=path, row=row: write_json(path, row),
                        cli=cli,
                        model=args.model,
                        max_turns=args.max_turns,
                    )
                )
            except Exception as exc:
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
            if row["status"] not in {"ok", "budget_exhausted"}:
                stopped = True
        if repo is not None:
            row["trajectory"] = native.contextbench_prediction(
                instance_id=case.instance_id, messages=row["messages"], repo_path=repo
            )
            native.add_delivery(row, repo, encoder)
            row["snapshot_after"] = native.audit_contextbench_snapshot(
                repo, case.base_commit
            )
        row.update(wall_s=time.perf_counter() - started, actual_bill_usd=None)
        write_json(path, row)
        rows.append(row)
        print(
            json.dumps({"case": case.instance_id, "status": row["status"]}), flush=True
        )
    write_json(
        args.output / "usage.json",
        {
            "billing_route": "existing_claude_subscription",
            "actual_bill_usd": None,
            "sdk_reported_cost_usd": sum(
                r.get("sdk_reported_cost_usd") or 0 for r in rows
            ),
            "missing_metering_cases": sum(
                bool(r["sdk_events"]) and r.get("sdk_reported_cost_usd") is None
                for r in rows
            ),
            "cost_scope": (
                "SDK metering estimate is not a paid API receipt. Subscription limits "
                "and any account-level extra usage remain applicable."
            ),
            "statuses": dict(Counter(r["status"] for r in rows)),
            "cases": len(rows),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="opus")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--live", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
