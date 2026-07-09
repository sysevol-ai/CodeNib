# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Controlled agent A/B for static-index and live JSON-RPC LSP providers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from codeminer.agent.harness import AgentHarnessSpec, run_agent_in_directory
from codeminer.agent.lsp_provider import (
    CAPABILITY_DEFINITION,
    CAPABILITY_REFERENCES,
    StaticLSPProvider,
    fingerprint_lsp_result,
    lsp_result_metadata,
)
from codeminer.agent.skills.loader import SkillLoader
from codeminer.agent.skills.registry import SkillRegistry
from codeminer.graph.code_graph import CodeGraph
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.ops.expand import ExpandContext

from .live_lsp_provider import LiveLSPReferenceProvider
from .lsp_provider_cli import load_lsp_provider_requests
from .lsp_provider_validation import LSPProviderRequest
from .lsp_readiness import wait_for_lsp_provider_readiness

_NATIVE_CAPABILITIES = frozenset({CAPABILITY_DEFINITION, CAPABILITY_REFERENCES})
_SYSTEM_PROMPT = (
    "Follow the user's LSP request exactly. Call the one available LSP tool "
    "once with the supplied arguments, then answer only with the returned "
    "repo-relative file:line locations, one per line."
)


def run_lsp_agent_arm(
    request: LSPProviderRequest,
    *,
    arm: str,
    provider: Any,
    graph: Any,
    llm: Any,
    repo_path: str | Path,
    rep: int = 1,
    order: int = 1,
    max_turns: int = 2,
) -> dict[str, Any]:
    """Run one agent cell while varying only the injected LSP provider."""

    capability = request.normalized_capability
    if capability not in _NATIVE_CAPABILITIES:
        raise ValueError(f"unsupported native LSP capability: {capability!r}")
    skill_id = f"lsp_{capability}"
    prompt = lsp_agent_prompt(request)
    result = None
    SkillRegistry.reset()
    try:
        registry = SkillRegistry()
        skill_dir = Path(__file__).resolve().parents[2] / "agent" / "skills" / skill_id
        metadata = SkillLoader().load_skill(
            str(skill_dir),
            {"expand": ExpandContext(code_graph=graph, lsp_provider=provider)},
        )
        if metadata is None:
            raise RuntimeError(f"could not load agent skill: {skill_id}")
        registry.register(metadata)
        runner = AgentHarnessSpec(
            max_turns=max_turns,
            allow_skills={skill_id},
            include_default_tools=False,
            system_prompt=_SYSTEM_PROMPT,
            first_turn_tool_choice=skill_id,
            force_first_turn_only=True,
            force_localization_contract=False,
        ).create_runner(llm=llm, registry=registry)
        result = run_agent_in_directory(runner, prompt, repo_path)
    finally:
        SkillRegistry.reset()

    tool_calls = [
        call
        for call in result.tool_calls
        if getattr(call, "skill_id", None) == skill_id
    ]
    first_call = tool_calls[0] if tool_calls else None
    tool_result = getattr(first_call, "result", None) if first_call else None
    provider_metadata = lsp_result_metadata(tool_result) or {}
    result_fingerprint = (
        fingerprint_lsp_result(tool_result)
        if isinstance(tool_result, (list, tuple))
        else None
    )
    expected_locations = [
        str(getattr(node, "node_name", ""))
        for node in (tool_result or [])
        if getattr(node, "node_name", None)
    ]
    resolved_arguments = (
        dict(getattr(first_call, "resolved_arguments", None) or {})
        if first_call
        else {}
    )
    usage = getattr(result, "usage", None)
    usage_payload = usage.to_dict() if hasattr(usage, "to_dict") else {}
    answer = str(getattr(result, "answer", None) or "")
    protocol_ok = bool(
        len(tool_calls) == 1
        and first_call is not None
        and not getattr(first_call, "error", None)
        and _arguments_match(request.arguments, resolved_arguments)
    )
    return {
        "request_id": request.request_id,
        "capability": capability,
        "arm": arm,
        "rep": int(rep),
        "order": int(order),
        "prompt_sha256": _sha256_text(prompt),
        "provider": provider_metadata.get("provider"),
        "provider_metadata": provider_metadata,
        "protocol_ok": protocol_ok,
        "tool_call_count": len(tool_calls),
        "tool_arguments": (
            dict(getattr(first_call, "arguments", None) or {}) if first_call else {}
        ),
        "resolved_arguments": resolved_arguments,
        "arguments_match": _arguments_match(request.arguments, resolved_arguments),
        "tool_error": (
            getattr(first_call, "error", None) if first_call else "not_called"
        ),
        "tool_duration_ms": (
            float(getattr(first_call, "duration_ms", 0.0)) if first_call else None
        ),
        "tool_result_count": (
            len(tool_result) if isinstance(tool_result, (list, tuple)) else None
        ),
        "tool_result_fingerprint": result_fingerprint,
        "tool_result_sha256": _result_sha256(tool_result),
        "expected_locations": expected_locations,
        "answer": answer,
        "answer_contains_all_locations": all(
            location in answer for location in expected_locations
        ),
        "total_turns": int(getattr(result, "total_turns", 0) or 0),
        "total_duration_ms": float(getattr(result, "total_duration_ms", 0.0) or 0.0),
        "usage": usage_payload,
    }


def lsp_agent_prompt(request: LSPProviderRequest) -> str:
    """Render one provider-independent prompt using agent-facing line numbers."""

    arguments = dict(request.arguments)
    if "line" in arguments:
        arguments["line"] = int(arguments["line"]) + 1
    skill_id = f"lsp_{request.normalized_capability}"
    return (
        f"Call {skill_id} exactly once with this JSON object:\n"
        f"{json.dumps(arguments, sort_keys=True)}\n"
        "Then return only the locations from the tool result."
    )


def exact_lsp_result_guard(
    request: LSPProviderRequest,
    *,
    static_provider: Any,
    live_provider: Any,
) -> dict[str, Any]:
    """Require identical model-visible DTOs before admitting an A/B case."""

    static_result = _dispatch(static_provider, request)
    live_result = _dispatch(live_provider, request)
    static_payload = _result_payload(static_result)
    live_payload = _result_payload(live_result)
    return {
        "request": request.to_dict(),
        "exact_match": static_payload == live_payload,
        "static_result_count": len(static_payload),
        "live_result_count": len(live_payload),
        "static_result_sha256": _sha256_json(static_payload),
        "live_result_sha256": _sha256_json(live_payload),
    }


def summarize_lsp_agent_ab(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate paired agent cells without treating LLM wall time as LSP latency."""

    rows = [dict(row) for row in payload.get("cells") or []]
    by_arm: dict[str, dict[str, Any]] = {}
    for arm in sorted({str(row.get("arm")) for row in rows}):
        arm_rows = [row for row in rows if str(row.get("arm")) == arm]
        by_arm[arm] = {
            "cell_count": len(arm_rows),
            "protocol_ok_count": sum(bool(row.get("protocol_ok")) for row in arm_rows),
            "answer_location_pass_count": sum(
                bool(row.get("answer_contains_all_locations")) for row in arm_rows
            ),
            "tool_duration_ms_p50": _median(
                row.get("tool_duration_ms") for row in arm_rows
            ),
            "agent_duration_ms_p50": _median(
                row.get("total_duration_ms") for row in arm_rows
            ),
            "turns_p50": _median(row.get("total_turns") for row in arm_rows),
            "tokens_p50": _median(
                (row.get("usage") or {}).get("total_tokens") for row in arm_rows
            ),
        }

    pairs: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for row in rows:
        pairs.setdefault(
            (str(row.get("request_id")), int(row.get("rep") or 0)), []
        ).append(row)
    complete_pairs = [pair for pair in pairs.values() if len(pair) == 2]
    return {
        "cell_count": len(rows),
        "pair_count": len(complete_pairs),
        "protocol_ok_count": sum(bool(row.get("protocol_ok")) for row in rows),
        "tool_result_equal_pair_count": sum(
            len({row.get("tool_result_sha256") for row in pair}) == 1
            for pair in complete_pairs
        ),
        "answer_equal_pair_count": sum(
            len({str(row.get("answer") or "").strip() for row in pair}) == 1
            for pair in complete_pairs
        ),
        "by_arm": by_arm,
    }


def run_lsp_agent_ab(
    requests: Sequence[LSPProviderRequest],
    *,
    graph: Any,
    project_root: str | Path,
    language: str,
    llm: Any,
    command: Optional[Sequence[str]] = None,
    max_cases: int = 3,
    reps: int = 1,
    minimum_warmup_reps: int = 2,
    max_warmup_reps: int = 10,
    warmup_poll_seconds: float = 1.0,
) -> dict[str, Any]:
    """Run a guarded crossover A/B while keeping one warm live LSP session."""

    candidates = [
        request
        for request in requests
        if request.normalized_capability in _NATIVE_CAPABILITIES
    ]
    if not candidates:
        raise ValueError("no native LSP requests available")
    if max_cases < 1 or reps < 1:
        raise ValueError("max_cases and reps must be positive")

    static_provider = StaticLSPProvider(graph)
    live_provider = LiveLSPReferenceProvider(
        project_root=project_root,
        language=language,
        command=command,
        skip_probe=True,
    )
    live_start = time.monotonic()
    live_provider.start()
    live_start_ms = (time.monotonic() - live_start) * 1000
    try:
        readiness = wait_for_lsp_provider_readiness(
            candidates,
            graph=graph,
            static_provider=static_provider,
            live_provider=live_provider,
            minimum_reps=minimum_warmup_reps,
            until_stable=True,
            max_reps=max_warmup_reps,
            poll_seconds=warmup_poll_seconds,
            minimum_equivalent_count=max_cases,
        )
        guards = [
            exact_lsp_result_guard(
                request,
                static_provider=static_provider,
                live_provider=live_provider,
            )
            for request in candidates
        ]
        eligible = [
            request
            for request, guard in zip(candidates, guards, strict=True)
            if guard["exact_match"]
        ][:max_cases]
        if len(eligible) < max_cases:
            raise RuntimeError(
                f"only {len(eligible)} exact-equivalent requests available; "
                f"need {max_cases}"
            )

        cells = []
        for case_index, request in enumerate(eligible):
            for rep in range(1, reps + 1):
                arms = (
                    [("static", static_provider), ("live", live_provider)]
                    if (case_index + rep) % 2
                    else [("live", live_provider), ("static", static_provider)]
                )
                for order, (arm, provider) in enumerate(arms, 1):
                    cells.append(
                        run_lsp_agent_arm(
                            request,
                            arm=arm,
                            provider=provider,
                            graph=graph,
                            llm=llm,
                            repo_path=project_root,
                            rep=rep,
                            order=order,
                        )
                    )
    finally:
        live_provider.close()

    payload = {
        "schema_version": 1,
        "benchmark": "lsp_agent_backend_ab",
        "subject": {
            "project_root": str(Path(project_root).resolve()),
            "language": language,
            "git_commit": _git_commit(Path(project_root)),
            "model": getattr(llm, "model", None),
            "live_command": list(live_provider.effective_command or command or []),
        },
        "setup": {
            "live_start_ms": live_start_ms,
            "warmup_wall_ms": readiness.wall_ms,
            "warmup_reps": readiness.completed_reps,
            "warmup_equivalent_count": readiness.equivalent_count,
        },
        "candidate_count": len(candidates),
        "guardrails": guards,
        "selected_requests": [request.to_dict() for request in eligible],
        "cells": cells,
    }
    payload["summary"] = summarize_lsp_agent_ab(payload)
    return payload


def render_lsp_agent_ab_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload.get("summary") or summarize_lsp_agent_ab(payload)
    setup = payload.get("setup") or {}
    lines = ["# LSP agent backend A/B", ""]
    lines.append(
        "The agent prompt, model, tool schema, and model-visible tool result are "
        "held constant; only the injected semantic provider changes."
    )
    lines.append("")
    lines.append(
        f"Pairs: {summary.get('pair_count', 0)}; exact tool-result pairs: "
        f"{summary.get('tool_result_equal_pair_count', 0)}; protocol-ok cells: "
        f"{summary.get('protocol_ok_count', 0)}/{summary.get('cell_count', 0)}"
    )
    lines.append(
        f"Live setup: {float(setup.get('live_start_ms') or 0):.2f} ms start + "
        f"{float(setup.get('warmup_wall_ms') or 0):.2f} ms behavioral warmup."
    )
    lines.extend(
        [
            "",
            "| arm | cells | protocol | tool p50 ms | agent p50 ms | turns p50 | tokens p50 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, row in (summary.get("by_arm") or {}).items():
        lines.append(
            f"| {arm} | {row.get('cell_count', 0)} | "
            f"{row.get('protocol_ok_count', 0)} | "
            f"{_fmt(row.get('tool_duration_ms_p50'))} | "
            f"{_fmt(row.get('agent_duration_ms_p50'))} | "
            f"{_fmt(row.get('turns_p50'))} | {_fmt(row.get('tokens_p50'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", required=True, help="Path to a CodeGraph graph.pkl")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--requests", required=True)
    parser.add_argument("--command", help="Live LSP command string")
    parser.add_argument("--capability", choices=["definition", "references"])
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--minimum-warmup-reps", type=int, default=2)
    parser.add_argument("--max-warmup-reps", type=int, default=10)
    parser.add_argument("--warmup-poll-seconds", type=float, default=1.0)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--vertex-project")
    parser.add_argument("--vertex-location")
    parser.add_argument("--num-retries", type=int, default=3)
    parser.add_argument("--enable-prompt-cache", action="store_true")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-markdown")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        graph = CodeGraph.load_graph(args.graph)
        requests = load_lsp_provider_requests(args.requests)
        if args.capability:
            requests = [
                request
                for request in requests
                if request.normalized_capability == args.capability
            ]
        extra_kwargs = {"num_retries": args.num_retries}
        if args.vertex_project:
            extra_kwargs["vertex_project"] = args.vertex_project
        if args.vertex_location:
            extra_kwargs["vertex_location"] = args.vertex_location
        llm = LiteLLMChat(
            model=args.model,
            temperature=0,
            max_tokens=args.max_tokens,
            extra_kwargs=extra_kwargs,
        )
        with _prompt_cache_policy(enabled=args.enable_prompt_cache):
            payload = run_lsp_agent_ab(
                requests,
                graph=graph,
                project_root=args.project_root,
                language=args.language,
                llm=llm,
                command=shlex.split(args.command) if args.command else None,
                max_cases=args.max_cases,
                reps=args.reps,
                minimum_warmup_reps=args.minimum_warmup_reps,
                max_warmup_reps=args.max_warmup_reps,
                warmup_poll_seconds=args.warmup_poll_seconds,
            )
        payload["prompt_cache_enabled"] = bool(args.enable_prompt_cache)
        _write_text(
            args.output_json, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
        markdown = render_lsp_agent_ab_markdown(payload)
        if args.output_markdown:
            _write_text(args.output_markdown, markdown)
        else:
            sys.stdout.write(markdown)
    except Exception as exc:  # noqa: BLE001 - CLI should fail cleanly.
        sys.stderr.write(f"codeminer-lsp-agent-ab: {exc}\n")
        return 1

    summary = payload["summary"]
    all_protocol_ok = summary["protocol_ok_count"] == summary["cell_count"]
    all_tool_results_equal = (
        summary["tool_result_equal_pair_count"] == summary["pair_count"]
    )
    return 0 if all_protocol_ok and all_tool_results_equal else 2


def _dispatch(provider: Any, request: LSPProviderRequest) -> Any:
    method = getattr(provider, request.normalized_capability)
    return method(**dict(request.arguments))


def _result_payload(result: Any) -> list[Any]:
    payload = []
    for node in result or []:
        if hasattr(node, "model_dump"):
            payload.append(node.model_dump(exclude_none=True))
        elif isinstance(node, Mapping):
            payload.append(dict(node))
        else:
            payload.append(vars(node))
    return payload


def _result_sha256(result: Any) -> Optional[str]:
    if not isinstance(result, (list, tuple)):
        return None
    return _sha256_json(_result_payload(result))


def _arguments_match(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def _median(values: Iterable[Any]) -> Optional[float]:
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return statistics.median(numeric) if numeric else None


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _git_commit(root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


@contextmanager
def _prompt_cache_policy(*, enabled: bool):
    key = "CODEMINER_DISABLE_PROMPT_CACHE"
    previous = os.environ.get(key)
    if not enabled:
        os.environ[key] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _write_text(path: str | Path, text: str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "exact_lsp_result_guard",
    "lsp_agent_prompt",
    "main",
    "render_lsp_agent_ab_markdown",
    "run_lsp_agent_ab",
    "run_lsp_agent_arm",
    "summarize_lsp_agent_ab",
]
