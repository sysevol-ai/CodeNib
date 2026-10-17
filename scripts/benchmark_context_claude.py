#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Run the ContextBench source agent through an existing Claude subscription.

Source-checkout experiment; use the official Agent SDK and logged-in CLI, with
CodeNib source tools and optional graph navigation. No API-key fallback.
SDK cost estimates are retained separately from actual billing, which is unknown.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.metadata
import json
import math
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
from scripts.contextbench_graph_navigation import graph_options, load_navigation

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


def normalize_event(kind, event, tool_map=None):
    """Convert actual SDK tool-result text, never requested ranges, into views."""
    messages = []
    tool_map = TOOLS if tool_map is None else tool_map
    if kind == "AssistantMessage":
        text, calls = [], []
        for block in event["content"]:
            if "name" in block and "input" in block:
                # An attempted unknown tool is not an advertised capability.
                # The isolated SDK/MCP inventory rejects it. Keep consuming the
                # stream so its error reply and terminal usage are not lost.
                function = {
                    "name": tool_map.get(block["name"], block["name"]),
                    "arguments": json.dumps(block["input"]),
                }
                if block["name"] not in tool_map:
                    function["unavailable"] = True
                calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": function,
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
                    **({"is_error": True} if block.get("is_error") else {}),
                }
            )
    return messages


async def retrieve(
    *,
    repo,
    query_text,
    row,
    checkpoint,
    cli,
    model,
    max_turns,
    source_policy="native",
    completion_tokens=2048,
    completion_source_policy="definitions",
    case_meter_usd=3.0,
    graph_tool=None,
):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query, tool

    sdk_tools = []
    tool_map = dict(TOOLS)
    specs = [
        spec for spec in get_default_tool_specs() if spec.tool_id in TOOLS.values()
    ]
    if graph_tool is not None:
        specs.append(graph_tool)
        tool_map[f"mcp__source__{graph_tool.tool_id}"] = graph_tool.tool_id
    for spec in specs:
        execute = spec.executor_fn
        if spec.tool_id in TOOLS.values():
            execute = native.bind_context_source_tool(
                execute,
                repo=repo.resolve(),
                tool_id=spec.tool_id,
                source_policy=source_policy,
                completion_tokens=completion_tokens,
                completion_source_policy=completion_source_policy,
                observed_source=lambda: native.observed_nodes(
                    tuple(row["messages"]), repo
                ),
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
        max_budget_usd=case_meter_usd,
        system_prompt=native.retrieval_system_prompt(
            max_turns, graph_enabled=graph_tool is not None
        ),
        tools=[],
        allowed_tools=list(tool_map),
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
            if advertised != set(tool_map):
                raise ValueError(f"SDK tool isolation differs: {sorted(advertised)}")
        normalized = normalize_event(kind, data, tool_map)
        row["messages"].extend(normalized)
        for entry in normalized:
            for call in entry.get("tool_calls", []):
                if call["function"].get("unavailable"):
                    row.setdefault("unavailable_tool_attempts", []).append(
                        {"id": call["id"], "name": call["function"]["name"]}
                    )
        if kind == "ResultMessage":
            row["sdk_result"] = data
        checkpoint()
    if "sdk_result" not in row:
        raise RuntimeError("SDK stream ended without a terminal result")
    result = row["sdk_result"]
    row.update(
        status=(
            "budget_exhausted"
            if result["subtype"] in {"error_max_turns", "error_max_budget_usd"}
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
    source_policy = getattr(args, "source_policy", "native")
    completion_tokens = getattr(args, "completion_tokens", 2048)
    completion_source_policy = getattr(args, "completion_source_policy", "definitions")
    max_meter_usd = getattr(args, "max_meter_usd", 100.0)
    case_meter_usd = getattr(args, "case_meter_usd", 3.0)
    case_timeout = getattr(args, "case_timeout", 900.0)
    if (
        source_policy not in {"native", "functions"}
        or not 1 <= completion_tokens <= 8192
        or completion_source_policy not in {"definitions", "unseen"}
    ):
        raise ValueError("Invalid source policy or completion token budget")
    if not all(
        math.isfinite(v) and v > 0
        for v in (max_meter_usd, case_meter_usd, case_timeout)
    ):
        raise ValueError("SDK meter and timeout limits must be finite and positive")
    auth = subscription_auth(cli)
    manifest, cases = native.load_inputs(args.prepared)
    graph_records, graph_config = graph_options(args, cases)
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
            "source_policy": source_policy,
            "completion_tokens": completion_tokens,
            "completion_source_policy": completion_source_policy,
            "graph": graph_config,
            "max_meter_usd": max_meter_usd,
            "case_meter_usd": case_meter_usd,
            "case_timeout": case_timeout,
            "tools": list(TOOLS)
            + (["mcp__source__graph_neighbors"] if graph_config["enabled"] else []),
            "scope": (
                "Subscription-backed SDK harness using only CodeNib source tools. "
                "Runtime differs from native AgentRunner; report actual returned model "
                "and SDK metering separately from unknown actual billing. No API-key or"
                " OpenRouter fallback, no solver."
            ),
        },
    )
    encoder = tiktoken.get_encoding("cl100k_base")
    rows, stopped, metered = [], False, 0.0
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
                    "content": native.retrieval_system_prompt(
                        args.max_turns, graph_enabled=graph_config["enabled"]
                    ),
                },
                {"role": "user", "content": case.problem_statement},
            ],
        }
        started = time.perf_counter()
        repo = None
        source = json.loads(Path(entry["source"]).read_text())
        if source.get("status") == "error":
            # This known preparation failure made no model request. Retain its
            # zero result without cancelling unrelated selected tasks.
            row.update(
                status="preparation_failed",
                error=source.get("error", "Source preparation failed"),
                sdk_reported_cost_usd=0.0,
            )
        elif not stopped and metered < max_meter_usd:
            row["status"] = "running"
            try:
                repo = Path(source["repo"]).resolve()
                row["snapshot_before"] = native.audit_contextbench_snapshot(
                    repo, case.base_commit
                )
                graph_tool = None
                if graph_config["enabled"]:
                    graph_record = graph_records[case.instance_id]
                    graph_tool = load_navigation(
                        graph_record,
                        args.graphs,
                        repo,
                        token_budget=graph_config["tokens"],
                        source_policy=graph_config["source_policy"],
                        observed_source=lambda row=row, repo=repo: native.observed_nodes(
                            tuple(row["messages"]), repo
                        ),
                    ).tool_spec()
                    row["graph_available"] = graph_record["available"]
                asyncio.run(
                    asyncio.wait_for(
                        retrieve(
                            repo=repo,
                            query_text=case.problem_statement,
                            row=row,
                            checkpoint=lambda path=path, row=row: write_json(path, row),
                            cli=cli,
                            model=args.model,
                            max_turns=args.max_turns,
                            source_policy=source_policy,
                            completion_tokens=completion_tokens,
                            completion_source_policy=completion_source_policy,
                            case_meter_usd=min(case_meter_usd, max_meter_usd - metered),
                            graph_tool=graph_tool,
                        ),
                        timeout=case_timeout,
                    )
                )
            except Exception as exc:
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
            if row["status"] not in {"ok", "budget_exhausted"}:
                stopped = True
            meter = row.get("sdk_reported_cost_usd")
            if meter is not None:
                if (
                    not isinstance(meter, (int, float))
                    or isinstance(meter, bool)
                    or not math.isfinite(meter)
                    or meter < 0
                ):
                    row.update(status="error", error="Invalid SDK metering")
                    row.pop("sdk_reported_cost_usd", None)
                    stopped = True
                else:
                    metered += meter
            elif row["sdk_events"]:
                stopped = True
        if repo is not None and "snapshot_before" in row:
            try:
                native.audit_contextbench_snapshot(repo, case.base_commit)
                row["trajectory"] = native.contextbench_prediction(
                    instance_id=case.instance_id,
                    messages=row["messages"],
                    repo_path=repo,
                )
                native.add_delivery(row, repo, encoder)
                row["snapshot_after"] = native.audit_contextbench_snapshot(
                    repo, case.base_commit
                )
            except Exception as exc:
                row.update(status="error", error=f"{type(exc).__name__}: {exc}")
                for key in ("trajectory", "served_16384", "served_32768"):
                    row.pop(key, None)
                stopped = True
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
    parser.add_argument(
        "--max-meter-usd",
        type=float,
        default=100.0,
        help="SDK metering guard, not an actual billing measurement",
    )
    parser.add_argument("--case-meter-usd", type=float, default=3.0)
    parser.add_argument("--case-timeout", type=float, default=900.0)
    native.source_policy_arguments(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
