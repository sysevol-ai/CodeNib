# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent-owned tool-use runtime for one Guardian L2 cycle."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from ...log_utils import get_logger
from .context import compact_messages, initial_messages
from .exceptions import (BudgetExceeded, CycleInterrupt, Degraded, NoProgress,
                         ReportSubmitted, WallClockExceeded)
from .state import (GRADE_RULES, CycleState, Hypothesis, check_invariants,
                    save_checkpoint, validate_hypothesis)
from .tools import TOOL_NAMES, TOOLS
from .tools_code import read_code, search_code

logger = get_logger(__name__)


@dataclass
class LoopContext:
    """Runtime dependencies that are deliberately not persisted in CycleState."""

    repo_path: str
    arm: str
    llm: object
    retriever: object
    memory_store: object = None
    sandbox: object = None
    prior_sandbox: object = None
    checkpoint_path: Optional[Path] = None
    observation_dir: Optional[Path] = None
    max_context_chars: int = 120_000
    max_turns: int = 40
    wall_clock_seconds: int = 1_800
    max_investigator_rounds: int = 8
    inner_usage: object = None
    outer_usage: object = None


def _state_summary(state: CycleState) -> dict:
    return {
        "budget_spent": state.budget_spent,
        "budget_total": state.budget_total,
        "current": state.current,
        "hypothesis_count": len(state.hypotheses),
        "grade_counts": {
            grade: sum(item.grade == grade for item in state.hypotheses)
            for grade in ("conjecture", "supported", "finding", "refuted", "deferred")
        },
    }


def _find_hypothesis(state: CycleState, hypothesis_id: str) -> Hypothesis:
    for hypothesis in state.hypotheses:
        if hypothesis.id == hypothesis_id:
            return hypothesis
    raise ValueError(f"unknown hypothesis id: {hypothesis_id}")


def _bounded_observation(value: Any, max_chars: int = 30_000) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, sort_keys=True, default=str)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "\n[truncated]"
    return rendered


def _dispatch(call_name: str, args: dict, state: CycleState, ctx: LoopContext) -> str:
    """Dispatch one call. Tool failures become observations; only submit exits."""
    try:
        if call_name == "list_signals":
            kind = args.get("kind")
            offset = max(0, int(args.get("since", 0) or 0))
            selected = [
                item for item in state.signals if kind is None or item.kind == kind
            ][offset:]
            return _bounded_observation([item.__dict__ for item in selected])

        if call_name == "recall":
            if ctx.memory_store is None:
                return "[]"
            recall = getattr(ctx.memory_store, "recall", None)
            if recall is None:
                return _bounded_observation(
                    ctx.memory_store.recent_findings(k=int(args.get("k", 10)))
                )
            return _bounded_observation(
                recall(
                    query=str(args.get("query", "")),
                    kind=args.get("kind"),
                    locus=args.get("locus"),
                    k=int(args.get("k", 10)),
                )
            )

        if call_name == "search_code":
            return search_code(
                ctx.repo_path,
                str(args.get("query", "")),
                retriever=ctx.retriever,
                top_k=int(args.get("top_k", 8)),
            )

        if call_name == "read_code":
            return read_code(
                ctx.repo_path,
                str(args.get("path", "")),
                start_line=int(args.get("start_line", 1)),
                end_line=args.get("end_line"),
            )

        if call_name == "read_observation":
            if ctx.observation_dir is None:
                raise ValueError("observation storage is disabled")
            ref = Path(str(args.get("ref", ""))).name
            target = ctx.observation_dir / ref
            if target.parent != ctx.observation_dir or not target.is_file():
                raise ValueError(f"unknown observation ref: {ref}")
            return target.read_text(encoding="utf-8", errors="replace")

        if call_name == "write_hypothesis":
            hypothesis = Hypothesis.create(
                claim=str(args.get("claim", "")),
                consequence=str(args.get("consequence", "")),
                remedy=str(args.get("remedy", "")),
                origin=str(args.get("origin", "")),
                locus=args.get("locus") or [],
                evidence=args.get("evidence") or [],
                confidence=float(args.get("confidence", 0.5)),
                cycle_no=state.cycle_no,
                supersedes=args.get("supersedes") or [],
            )
            existing = next(
                (item for item in state.hypotheses if item.id == hypothesis.id), None
            )
            if existing is not None:
                return (
                    f"(rejected: duplicate hypothesis id {hypothesis.id}; "
                    "use update_hypothesis and supersedes)"
                )
            state.hypotheses.append(hypothesis)
            return json.dumps({"id": hypothesis.id, "grade": hypothesis.grade})

        if call_name == "update_hypothesis":
            hypothesis = _find_hypothesis(state, str(args.get("id", "")))
            old = hypothesis.__dict__.copy()
            try:
                if "evidence" in args:
                    hypothesis.evidence = list(
                        dict.fromkeys(hypothesis.evidence + list(args["evidence"]))
                    )
                if "confidence" in args:
                    hypothesis.confidence = float(args["confidence"])
                if "remedy" in args:
                    hypothesis.remedy = str(args["remedy"]).strip()
                if "supersedes" in args:
                    hypothesis.supersedes = list(
                        dict.fromkeys(hypothesis.supersedes + list(args["supersedes"]))
                    )
                if "grade" in args:
                    hypothesis.grade = str(args["grade"])
                hypothesis.last_touched_cycle = state.cycle_no
                validate_hypothesis(hypothesis)
            except Exception:
                hypothesis.__dict__.update(old)
                raise
            return json.dumps(
                {
                    "id": hypothesis.id,
                    "grade": hypothesis.grade,
                    "admissible": GRADE_RULES[hypothesis.grade](hypothesis),
                }
            )

        if call_name == "investigate":
            hypothesis = _find_hypothesis(state, str(args.get("hypothesis_id", "")))
            grant = int(args.get("budget_tokens", 0))
            remaining = max(0, state.budget_total - state.budget_spent)
            if grant <= 0:
                raise ValueError("budget_tokens must be positive")
            if grant > remaining:
                raise ValueError(
                    f"requested {grant} tokens but only {remaining} remain"
                )
            if ctx.sandbox is None:
                raise ValueError("investigator sandbox is unavailable")
            from ..investigator import run_investigator

            state.current = hypothesis.id
            before = int(getattr(ctx.inner_usage, "total_tokens", 0) or 0)
            try:
                result = run_investigator(
                    hypothesis,
                    ctx.llm,
                    ctx.retriever,
                    ctx.sandbox,
                    budget_tokens=before + grant,
                    max_rounds=ctx.max_investigator_rounds,
                    usage_acc=ctx.inner_usage,
                    prior_sandbox=ctx.prior_sandbox,
                )
            finally:
                state.current = None
            after = int(getattr(ctx.inner_usage, "total_tokens", before) or before)
            spent = max(int(getattr(result, "tokens_used", 0) or 0), after - before)
            state.budget_spent += spent
            hypothesis.spent_tokens += spent
            hypothesis.attempts += 1
            hypothesis.last_touched_cycle = state.cycle_no
            probe_ref = f"probe:{state.cycle_no}:{hypothesis.attempts}"
            hypothesis.evidence.append(probe_ref)
            return _bounded_observation(
                {
                    **result.to_dict(),
                    "probe_ref": probe_ref,
                    "instruction": (
                        "Judge the evidence and call update_hypothesis; "
                        "the runtime does not derive a grade from this verdict."
                    ),
                }
            )

        if call_name == "submit_report":
            summary = str(args.get("summary", "")).strip()
            state.report_summary = summary
            raise ReportSubmitted(summary, decision_log_tail=state.decision_log[-5:])

        return f"(unknown tool: {call_name!r})"
    except ReportSubmitted:
        raise
    except (
        Exception
    ) as exc:  # noqa: BLE001 - tool failures are observations by contract
        return f"(tool_error {type(exc).__name__}: {exc})"


def _assistant_message(message: object) -> dict:
    tool_calls = getattr(message, "tool_calls", None) or []
    return {
        "role": "assistant",
        "content": getattr(message, "content", "") or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in tool_calls
        ],
    }


def run_cycle_loop(
    state: CycleState,
    ctx: LoopContext,
    *,
    messages: Optional[List[dict]] = None,
) -> CycleState:
    """Run model turns until a typed exit, checkpointing every boundary."""
    transcript = list(
        messages
        if messages is not None
        else initial_messages(state, repo_path=ctx.repo_path, arm=ctx.arm)
    )
    started = time.monotonic()
    turn = 0
    try:
        while True:
            check_invariants(state)
            if state.budget_spent >= state.budget_total:
                raise BudgetExceeded(
                    f"cycle token budget exhausted "
                    f"({state.budget_spent}/{state.budget_total})",
                    decision_log_tail=state.decision_log[-5:],
                )
            if turn >= ctx.max_turns:
                raise BudgetExceeded(
                    f"cycle turn limit reached ({turn}/{ctx.max_turns})",
                    decision_log_tail=state.decision_log[-5:],
                )
            elapsed = time.monotonic() - started
            if elapsed >= ctx.wall_clock_seconds:
                raise WallClockExceeded(
                    f"cycle wall-clock limit reached ({elapsed:.1f}s)",
                    decision_log_tail=state.decision_log[-5:],
                )

            compacted, events = compact_messages(
                transcript,
                max_chars=ctx.max_context_chars,
                output_dir=ctx.observation_dir or Path(".guardian_observations"),
            )
            transcript = compacted
            if events:
                state.compaction_events += len(events)
                state.decision_log.extend(events)
            context_chars = sum(
                len(json.dumps(message, sort_keys=True, default=str))
                for message in transcript
            )
            if context_chars > ctx.max_context_chars:
                raise BudgetExceeded(
                    "immutable frame plus active working set exceeds the "
                    f"context budget ({context_chars}/{ctx.max_context_chars} chars)",
                    decision_log_tail=state.decision_log[-5:],
                )

            try:
                response = ctx.llm._call_raw(
                    transcript, tools=TOOLS, tool_choice="auto"
                )
            except Exception as exc:  # noqa: BLE001
                state.degraded = True
                raise Degraded(
                    f"model unavailable: {exc}",
                    decision_log_tail=state.decision_log[-5:],
                ) from exc

            usage = getattr(response, "usage", None)
            state.budget_spent += int(getattr(usage, "total_tokens", 0) or 0)
            if ctx.outer_usage is not None:
                ctx.outer_usage.add(response)
            message = response.choices[0].message
            assistant = _assistant_message(message)
            transcript.append(assistant)
            calls = getattr(message, "tool_calls", None) or []
            if not calls:
                raise NoProgress(
                    (
                        getattr(message, "content", "") or "model made no tool call"
                    ).strip(),
                    decision_log_tail=state.decision_log[-5:],
                )

            for call in calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                before = _state_summary(state)
                record = {
                    "event": "tool_call",
                    "turn": turn,
                    "tool_call_id": call.id,
                    "tool": name,
                    "arguments": args,
                    "state_before": before,
                }
                state.decision_log.append(record)
                if name not in TOOL_NAMES:
                    observation = f"(unknown tool: {name!r})"
                else:
                    observation = _dispatch(name, args, state, ctx)
                record["observation"] = observation[:2_000]
                record["state_after"] = _state_summary(state)
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": observation,
                    }
                )
            turn += 1
            if ctx.checkpoint_path is not None:
                save_checkpoint(ctx.checkpoint_path, state, transcript)
    except CycleInterrupt as exc:
        state.exit_reason = type(exc).__name__
        state.decision_log.append(exc.as_record())
        return state
    finally:
        if ctx.checkpoint_path is not None:
            save_checkpoint(ctx.checkpoint_path, state, transcript)
