#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Run the ContextBench source agent through an explicitly selected Claude route.

Source-checkout experiment; use the official Agent SDK and logged-in CLI, with
CodeNib source tools and optional graph navigation. Subscription is the default;
direct Anthropic API use requires an explicit route. There is no fallback.
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
from scripts.contextbench_file_map import DESCRIPTION as FILE_MAP_DESCRIPTION
from scripts.contextbench_file_map import mapped_read
from scripts.contextbench_file_source import DESCRIPTION as FILE_SOURCE_DESCRIPTION
from scripts.contextbench_file_source import FileSourceView
from scripts.contextbench_graph_navigation import (
    GRAPH_QUERY_POLICIES,
    graph_options,
    load_navigation,
)
from scripts.contextbench_grep_rerank import GREP_POLICIES, GrepSourceView

TOOLS = {f"mcp__source__{name}": name for name in ("read", "grep", "glob")}


def included_max_rate(data):
    """Require an allowed subscription event with paid overage explicitly off.

    This checks an observed SDK event, not an account spending limit. The first
    request may already have been sent when the event arrives. Never enable
    credits or infer that an absent flag means they are disabled.
    """
    info = data.get("rate_limit_info") or {}
    raw = info.get("raw") or {}
    if (
        info.get("status") not in {"allowed", "allowed_warning"}
        or raw.get("overageEnabled") is not False
        or raw.get("isUsingOverage") is not False
    ):
        raise RuntimeError("Max quota event does not confirm included usage only")
    return info


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


def api_auth(cli):
    """Verify the requested first-party API route without saving identity."""
    if not os.environ.get("ANTHROPIC_API_KEY") or any(
        os.environ.get(name)
        for name in (
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        )
    ):
        raise ValueError("Direct API experiments require only ANTHROPIC_API_KEY")
    result = subprocess.run(
        [cli, "auth", "status", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    status = json.loads(result.stdout)
    expected = {
        "loggedIn": True,
        "authMethod": "api_key",
        "apiProvider": "firstParty",
        "apiKeySource": "ANTHROPIC_API_KEY",
    }
    if any(status.get(key) != value for key, value in expected.items()):
        raise ValueError("CLI did not confirm the requested Anthropic API route")
    return expected


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


def matching_budget_terminal(result, data):
    """Recognize only a metered budget result repeated by the SDK exception."""
    if not isinstance(result, dict) or not isinstance(data, dict):
        return False
    if (
        result.get("subtype") not in {"error_max_turns", "error_max_budget_usd"}
        or data.get("subtype") != result["subtype"]
        or data.get("type") != "result"
        or result.get("is_error") is not True
        or data.get("is_error") is not True
        or result.get("api_error_status") is not None
        or data.get("api_error_status") is not None
        or result.get("terminal_reason") == "api_error"
        or data.get("terminal_reason") != result.get("terminal_reason")
    ):
        return False
    for key in ("uuid", "session_id"):
        if not isinstance(result.get(key), str) or not result[key]:
            return False
        if data.get(key) != result[key]:
            return False
    fee = result.get("total_cost_usd")
    return (
        type(fee) in (int, float)
        and math.isfinite(fee)
        and fee >= 0
        and type(data.get("total_cost_usd")) in (int, float)
        and data["total_cost_usd"] == fee
    )


async def sdk_messages(stream, *, row, checkpoint, result_error_type):
    """Keep a typed, matched post-result budget exit for final receipt checks.

    SDK0.2.165 yields the CLI error result, then raises ResultError on its
    intentional nonzero exit. A matched budget result still has to pass every
    existing quota/auth/source/meter check in retrieve; this adds no retry.
    """
    try:
        async for message in stream:
            yield message
    except Exception as exc:
        if not isinstance(exc, result_error_type):
            raise
        data = getattr(exc, "data", None)
        verified = matching_budget_terminal(row.get("sdk_result"), data)
        row["sdk_post_result_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "data": data,
            "budget_terminal_verified": verified,
        }
        checkpoint()
        if not verified:
            raise


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
    graph_query_policy=GRAPH_QUERY_POLICIES[0],
    billing_route="subscription",
    grep_view=None,
    file_map_tokens=0,
    file_source_view=None,
    max_included_only=False,
    context_tool=None,
    read_hints=None,
):
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query, tool

    if billing_route not in {"subscription", "anthropic-api"}:
        raise ValueError("Unknown billing route")
    if grep_view is not None and source_policy != "native":
        raise ValueError("Matched grep views require native read/source policy")
    if not 0 <= file_map_tokens <= 4096:
        raise ValueError("File-map token budget must be in [0, 4096]")
    if file_map_tokens and (
        source_policy != "native"
        or grep_view is not None
        or graph_tool is not None
        or file_source_view is not None
    ):
        raise ValueError("File maps require unmodified native source tools")
    if file_source_view is not None and (
        source_policy != "native" or grep_view is not None or graph_tool is not None
    ):
        raise ValueError("File source requires unmodified native source tools")
    if max_included_only and billing_route != "subscription":
        raise ValueError("Included Max usage requires the subscription route")
    if context_tool is not None and (
        source_policy != "native"
        or graph_tool is not None
        or grep_view is not None
        or file_map_tokens
        or file_source_view is not None
    ):
        raise ValueError("CodeNib context requires an otherwise native source arm")
    if read_hints is not None and context_tool is None:
        raise ValueError("Reference hints require the explicit CodeNib context tool")
    sdk_tools = []
    tool_map = dict(TOOLS)
    specs = [
        spec for spec in get_default_tool_specs() if spec.tool_id in TOOLS.values()
    ]
    if graph_tool is not None:
        specs.append(graph_tool)
        tool_map[f"mcp__source__{graph_tool.tool_id}"] = graph_tool.tool_id
    if context_tool is not None:
        if context_tool.tool_id != "explore_context":
            raise ValueError("Only the explicit CodeNib context tool is supported")
        specs.append(context_tool)
        tool_map["mcp__source__explore_context"] = "explore_context"
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

        if spec.tool_id == "grep" and grep_view is not None:
            execute = grep_view.wrap(
                execute,
                observed_source=lambda: native.observed_nodes(
                    tuple(row["messages"]), repo
                ),
            )
        if spec.tool_id == "read" and file_map_tokens:
            execute = mapped_read(
                execute,
                repo=repo,
                token_budget=file_map_tokens,
                observed_messages=lambda: tuple(row["messages"]),
            )
        if spec.tool_id == "read" and file_source_view is not None:
            execute = file_source_view.wrap(
                execute, observed_messages=lambda: tuple(row["messages"])
            )
        if spec.tool_id == "read" and read_hints is not None:
            execute = read_hints.wrap(
                execute, observed_messages=lambda: tuple(row["messages"])
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
        if spec.tool_id == "grep" and grep_view is not None:
            schema["description"] = grep_view.description + "\n" + schema["description"]
        if spec.tool_id == "read" and file_map_tokens:
            schema["description"] = FILE_MAP_DESCRIPTION + "\n" + schema["description"]
        if spec.tool_id == "read" and file_source_view is not None:
            schema["description"] = (
                FILE_SOURCE_DESCRIPTION + "\n" + schema["description"]
            )
        if spec.tool_id == "read" and read_hints is not None:
            from scripts.contextbench_context_hints import DESCRIPTION

            schema["description"] = DESCRIPTION + "\n" + schema["description"]
        sdk_tools.append(
            tool(spec.tool_id, schema["description"], schema["parameters"])(
                bind(execute)
            )
        )
    system_prompt = native.retrieval_system_prompt(
        max_turns,
        graph_enabled=graph_tool is not None,
        graph_query_policy=graph_query_policy,
    )
    if context_tool is not None:
        from scripts.contextbench_codenib_context import CONTEXT_GUIDANCE

        system_prompt = system_prompt.replace(
            "Use grep and glob to locate implementations, then read their actual source.",
            CONTEXT_GUIDANCE,
        )
        row["context_system_prompt"] = system_prompt
    if read_hints is not None:
        from scripts.contextbench_context_hints import DESCRIPTION

        system_prompt += "\n" + DESCRIPTION
        row["context_system_prompt"] = system_prompt
        row["read_context_hints"] = True
    options = ClaudeAgentOptions(
        cli_path=cli,
        cwd=repo,
        model=model,
        effort="medium",
        max_turns=max_turns,
        max_budget_usd=case_meter_usd,
        system_prompt=system_prompt,
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
    max_rate_seen = False
    if max_included_only:
        row["max_included_rate_confirmed"] = False
    stream = sdk_messages(
        query(prompt=query_text, options=options),
        row=row,
        checkpoint=checkpoint,
        result_error_type=getattr(sys.modules["claude_agent_sdk"], "ResultError", ()),
    )
    async for message in stream:
        kind = type(message).__name__
        data = dataclasses.asdict(message)
        row["sdk_events"].append({"kind": kind, "data": data})
        if kind == "ResultMessage":
            row["sdk_result"] = data
            # Preserve terminal usage even if a subsequent receipt check fails.
            row["sdk_reported_cost_usd"] = data.get("total_cost_usd")
        if max_included_only:
            if kind == "RateLimitEvent":
                row["max_rate_event"] = data
                checkpoint()
                row["max_included_rate_info"] = included_max_rate(data)
                max_rate_seen = True
        if kind == "SystemMessage" and data.get("subtype") == "init":
            if max_included_only and data["data"].get("apiKeySource") != "none":
                checkpoint()
                raise ValueError("Included Max run did not confirm subscription auth")
            if (
                billing_route == "anthropic-api"
                and data["data"].get("apiKeySource") != "ANTHROPIC_API_KEY"
            ):
                raise ValueError("SDK did not confirm the requested API-key route")
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
        checkpoint()
        if grep_view is not None and grep_view.ledger["failed"]:
            raise RuntimeError("Grep selection failed; stop this SDK stream")
        if file_source_view is not None and file_source_view.ledger["failed"]:
            raise RuntimeError("File-source selection failed; stop this SDK stream")
    if "sdk_result" not in row:
        raise RuntimeError("SDK stream ended without a terminal result")
    # Quota events can follow assistant output. Retain those raw observations
    # pending validation and drain to terminal usage rather than interrupting a
    # valid event ordering. No case is accepted without an included-only event;
    # an explicitly unsafe quota event still stops the stream immediately above.
    if max_included_only:
        if not max_rate_seen:
            checkpoint()
            raise RuntimeError("No included Max quota event in terminal SDK stream")
        row["max_included_rate_confirmed"] = True
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
        raise ValueError("Claude inference requires --live")
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
    billing_route = getattr(args, "billing_route", "subscription")
    max_included_only = getattr(args, "max_included_only", False)
    grep_policy = getattr(args, "grep_view", "native")
    file_map_tokens = getattr(args, "file_map_tokens", 0)
    file_source_tokens = getattr(args, "file_source_tokens", 0)
    grep_tokens = getattr(args, "grep_tokens", 4096)
    jev_cost_usd = getattr(args, "jev_cost_usd", 0.05)
    if billing_route not in {"subscription", "anthropic-api"}:
        raise ValueError("Unknown billing route")
    if max_included_only and billing_route != "subscription":
        raise ValueError("Included Max usage requires the subscription route")
    if grep_policy not in GREP_POLICIES:
        raise ValueError("Unknown grep view")
    if not 0 <= file_map_tokens <= 4096:
        raise ValueError("File-map token budget must be in [0, 4096]")
    if file_map_tokens and (source_policy != "native" or grep_policy != "native"):
        raise ValueError("File maps require unmodified native source tools")
    if not 0 <= file_source_tokens <= 4096:
        raise ValueError("File-source token budget must be in [0, 4096]")
    if file_source_tokens and (
        source_policy != "native" or grep_policy != "native" or file_map_tokens
    ):
        raise ValueError("File source requires unmodified native source tools")
    if grep_policy != "native" and source_policy != "native":
        raise ValueError("Matched grep views require native read/source policy")
    if (
        not 256 <= grep_tokens <= 8192
        or not math.isfinite(jev_cost_usd)
        or jev_cost_usd <= 0
    ):
        raise ValueError("Invalid grep budget")
    if grep_policy in {"jev", "jev-hints"} and not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("Jev experiments require OPENROUTER_API_KEY")
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
    auth = subscription_auth(cli) if billing_route == "subscription" else api_auth(cli)
    if max_included_only and (
        auth.get("subscriptionType") != "max" or auth.get("apiProvider") != "firstParty"
    ):
        raise ValueError("Included Max run requires a first-party Max login")
    manifest, cases = native.load_inputs(args.prepared)
    graph_records, graph_config = graph_options(args, cases)
    if file_map_tokens and graph_config["enabled"]:
        raise ValueError("File maps require unmodified native source tools")
    if file_source_tokens and graph_config["enabled"]:
        raise ValueError("File source requires unmodified native source tools")
    args.output.mkdir(parents=True, exist_ok=False)
    if grep_policy != "native":
        (args.output / "grep-receipts").mkdir()
    if file_source_tokens:
        (args.output / "read-receipts").mkdir()
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
            "billing_route": billing_route,
            "max_included_only": max_included_only,
            "grep_view": {
                "policy": grep_policy,
                "tokens": grep_tokens,
                "jev_cost_usd": jev_cost_usd,
            },
            "model_requested": args.model,
            "file_map_tokens": file_map_tokens,
            "file_source_tokens": file_source_tokens,
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
                "Explicitly routed SDK harness using only CodeNib source tools. "
                "Runtime differs from native AgentRunner; report actual returned model "
                "and SDK metering separately from account billing/credit. No route "
                "fallback, no solver. Optional Jev API usage is separately recorded."
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
                        args.max_turns,
                        graph_enabled=graph_config["enabled"],
                        graph_query_policy=graph_config["policy"],
                    ),
                },
                {"role": "user", "content": case.problem_statement},
            ],
        }
        started = time.perf_counter()
        repo = None
        grep_view = None
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
                file_source_view = None
                if file_source_tokens:
                    receipt_path = (
                        args.output / "read-receipts" / f"{case.instance_id}.json"
                    )
                    file_source_view = FileSourceView(
                        repo=repo,
                        token_budget=file_source_tokens,
                        checkpoint=lambda data, path=receipt_path: write_json(
                            path, data
                        ),
                    )
                if grep_policy != "native":
                    ledger_path = (
                        args.output / "grep-receipts" / f"{case.instance_id}.json"
                    )
                    grep_view = GrepSourceView(
                        repo=repo,
                        query=case.problem_statement,
                        policy=grep_policy,
                        token_budget=grep_tokens,
                        max_cost_usd=jev_cost_usd,
                        checkpoint=lambda data, path=ledger_path: write_json(
                            path, data
                        ),
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
                        empty_fallback=graph_config.get("empty_fallback", False),
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
                            billing_route=billing_route,
                            grep_view=grep_view,
                            file_map_tokens=file_map_tokens,
                            file_source_view=file_source_view,
                            max_included_only=max_included_only,
                            graph_tool=graph_tool,
                            graph_query_policy=graph_config["policy"],
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
        if grep_view is not None:
            row["grep_view"] = {
                key: grep_view.ledger[key]
                for key in ("policy", "cost_usd", "failed", "unknown_usage_calls")
            }
            row["grep_view"].update(
                views=len(grep_view.ledger["views"]),
                api_calls=len(grep_view.ledger["calls"]),
            )
            if grep_view.ledger["failed"]:
                row.update(status="error", error="Grep source selection failed")
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
            "billing_route": (
                "existing_claude_subscription"
                if billing_route == "subscription"
                else "anthropic_api_key"
            ),
            "actual_bill_usd": None,
            "sdk_reported_cost_usd": sum(
                r.get("sdk_reported_cost_usd") or 0 for r in rows
            ),
            "missing_metering_cases": sum(
                bool(r["sdk_events"]) and r.get("sdk_reported_cost_usd") is None
                for r in rows
            ),
            "cost_scope": (
                "SDK metering is separate from account billing and promotional "
                "credit balance. There is no automatic billing-route fallback."
            ),
            "jev_api_cost_usd": sum(
                r.get("grep_view", {}).get("cost_usd", 0) for r in rows
            ),
            "jev_api_calls": sum(
                r.get("grep_view", {}).get("api_calls", 0) for r in rows
            ),
            "jev_unknown_usage_calls": sum(
                r.get("grep_view", {}).get("unknown_usage_calls", 0) for r in rows
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
    parser.add_argument(
        "--billing-route",
        choices=("subscription", "anthropic-api"),
        default="subscription",
    )
    parser.add_argument("--grep-view", choices=GREP_POLICIES, default="native")
    parser.add_argument("--file-map-tokens", type=int, default=0)
    parser.add_argument("--file-source-tokens", type=int, default=0)
    parser.add_argument("--grep-tokens", type=int, default=4096)
    parser.add_argument("--jev-cost-usd", type=float, default=0.05)
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument(
        "--max-included-only",
        action="store_true",
        help="Require SDK quota events with usage credits explicitly disabled",
    )
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
