# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Agent-owned tool-use runtime for one Guardian L2 cycle."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

from ...log_utils import get_logger
from ..llm import AgentLoopSession, ContextManager, LoopOutcome
from .context import (
    compact_messages,
    initial_messages,
    summarization_messages,
)
from .exceptions import (
    BudgetExceeded,
    CycleInterrupt,
    Degraded,
    NoProgress,
    ReportSubmitted,
    WallClockExceeded,
)
from .state import (
    GRADE_RULES,
    CycleState,
    Hypothesis,
    check_invariants,
    save_checkpoint,
    validate_hypothesis,
)
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
    max_context_tokens: int = 200_000
    context_reserve_tokens: int = 20_000
    # Compatibility for older callers and intentionally tiny unit-test limits.
    max_context_chars: Optional[int] = None
    max_turns: int = 40
    wall_clock_seconds: int = 1_800
    investigator_deadline_seconds: int = 300
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
            for grade in (
                "conjecture",
                "supported",
                "finding",
                "refuted",
                "deferred",
                "resolved",
            )
        },
    }


def _find_hypothesis(state: CycleState, hypothesis_id: str) -> Hypothesis:
    for hypothesis in state.hypotheses:
        if hypothesis.id == hypothesis_id:
            return hypothesis
    raise ValueError(f"unknown hypothesis id: {hypothesis_id}")


def _bounded_observation(value: Any, max_chars: int = 16_000) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, sort_keys=True, default=str)
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "\n[truncated]"
    return rendered


def _investigation_excerpts(hypothesis: Hypothesis, repo_path: str) -> list:
    """Materialize bounded source already named by the hypothesis."""

    from ..investigator import SourceExcerpt

    excerpts = []
    seen = set()
    for locus in hypothesis.locus[:6]:
        text = str(locus)
        match = re.match(r"^(.*?):(\d+)(?:-(\d+))?$", text)
        if match:
            path = match.group(1)
            locus_start = int(match.group(2))
            locus_end = int(match.group(3) or locus_start)
            start = max(1, locus_start - 40)
            end = locus_end + 40
        else:
            path = text.split(":", 1)[0]
            start, end = 1, 120
        key = (path, start, end)
        if key in seen:
            continue
        seen.add(key)
        try:
            content = read_code(
                repo_path, path, start_line=start, end_line=end, max_chars=8_000
            )
        except (OSError, ValueError):
            continue
        excerpts.append(
            SourceExcerpt(
                path=path,
                start_line=start,
                end_line=start + max(0, len(content.splitlines()) - 1),
                content=content,
            )
        )
    return excerpts


def _commit_diff(repo_path: str) -> str:
    """Return the bounded diff introduced by HEAD."""

    try:
        result = subprocess.run(
            ["git", "show", "--format=", "--no-ext-diff", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout[:30_000] if result.returncode == 0 else ""


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

        if call_name == "read_commit_diff":
            return _commit_diff(ctx.repo_path) or "(empty commit diff)"

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
                    new_grade = str(args["grade"])
                    if new_grade == "resolved":
                        reason = str(args.get("reason", "")).strip()
                        if hypothesis.first_seen_cycle >= state.cycle_no:
                            raise ValueError(
                                "grade='resolved' is only valid for a hypothesis "
                                "carried from an earlier cycle"
                            )
                        if not reason:
                            raise ValueError(
                                "grade='resolved' requires a non-empty reason"
                            )
                        hypothesis.evidence = list(
                            dict.fromkeys(
                                [
                                    *hypothesis.evidence,
                                    f"resolved:{state.commit}:{reason}",
                                ]
                            )
                        )
                    hypothesis.grade = new_grade
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
            if grant < 8_000:
                raise ValueError("budget_tokens must be at least 8000")
            remaining = (
                None
                if state.budget_total is None
                else max(0, state.budget_total - state.budget_spent)
            )
            if remaining is not None and grant > remaining:
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
                    budget_tokens=grant,
                    max_rounds=ctx.max_investigator_rounds,
                    usage_acc=ctx.inner_usage,
                    prior_sandbox=ctx.prior_sandbox,
                    memory_store=ctx.memory_store,
                    obligation=str(args.get("obligation", "")).strip(),
                    excerpts=_investigation_excerpts(hypothesis, ctx.repo_path),
                    commit_diff=_commit_diff(ctx.repo_path),
                    prior_attempts=[
                        {
                            "attempt": index + 1,
                            "evidence_ref": evidence,
                        }
                        for index, evidence in enumerate(hypothesis.evidence)
                        if evidence.startswith(
                            (
                                "probe-valid:",
                                "source-valid:",
                                "probe-invalid:",
                                "env:",
                            )
                        )
                    ],
                    deadline_s=ctx.investigator_deadline_seconds,
                    checkpoint_path=(
                        ctx.checkpoint_path.with_name(
                            f"investigation_{hypothesis.id}.json"
                        )
                        if ctx.checkpoint_path is not None
                        else None
                    ),
                    repo_identity=ctx.repo_path,
                    commit=state.commit,
                )
            finally:
                state.current = None
            after = int(getattr(ctx.inner_usage, "total_tokens", before) or before)
            spent = max(
                int(getattr(getattr(result, "budget", None), "actual", 0) or 0),
                after - before,
            )
            state.budget_spent += spent
            hypothesis.spent_tokens += spent
            hypothesis.attempts += 1
            hypothesis.last_touched_cycle = state.cycle_no
            prefix = {
                "valid": "probe-valid",
                "source": "source-valid",
                "invalid": "probe-invalid",
                "environment": "env",
            }.get(getattr(result, "evidence_status", "invalid"), "probe-invalid")
            if (
                bool(getattr(result, "degraded", False))
                or getattr(result, "evidence_status", "") == "environment"
                or getattr(result, "exit_status", "") == "environment_unavailable"
            ):
                state.degraded = True
            probe_ref = f"{prefix}:{state.cycle_no}:{hypothesis.attempts}"
            hypothesis.evidence.append(probe_ref)
            admissible_grades = (
                ["conjecture", "deferred"]
                if prefix in {"probe-invalid", "env"}
                else [
                    "conjecture",
                    "supported",
                    "finding",
                    "refuted",
                    "deferred",
                ]
            )
            if hypothesis.first_seen_cycle < state.cycle_no:
                admissible_grades.append("resolved")
            return _bounded_observation(
                {
                    **result.to_dict(),
                    "probe_ref": probe_ref,
                    "admissible_grades": admissible_grades,
                    "instruction": (
                        "Judge the evidence and call update_hypothesis using one "
                        "of admissible_grades; the runtime does not derive a grade."
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
    session_manager = AgentLoopSession(ctx.llm)
    loop_llm = session_manager.__enter__()
    try:
        while True:
            check_invariants(state)
            if (
                state.budget_total is not None
                and state.budget_spent >= state.budget_total
            ):
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

            max_context_tokens = (
                max(1, ctx.max_context_chars // 4)
                if ctx.max_context_chars is not None
                else ctx.max_context_tokens
            )
            reserve_tokens = min(
                max(0, ctx.context_reserve_tokens),
                max(0, max_context_tokens // 5),
            )
            model = getattr(ctx.llm, "model", None)
            context = ContextManager(
                transcript,
                max_tokens=max_context_tokens,
                reserve_tokens=reserve_tokens,
                model=model,
            )
            if context.needs_compaction():
                try:
                    summary_response = loop_llm._call_raw(
                        summarization_messages(transcript, state),
                        tools=TOOLS,
                        tool_choice="auto",
                        _guardian_text_response=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    state.degraded = True
                    raise Degraded(
                        f"context summarization failed: {exc}",
                        decision_log_tail=state.decision_log[-5:],
                    ) from exc
                summary_usage = getattr(summary_response, "usage", None)
                state.budget_spent += int(
                    getattr(summary_usage, "total_tokens", 0) or 0
                )
                if ctx.outer_usage is not None:
                    ctx.outer_usage.add(summary_response)
                if (
                    state.budget_total is not None
                    and state.budget_spent >= state.budget_total
                ):
                    raise BudgetExceeded(
                        "cycle token budget exhausted during context summarization "
                        f"({state.budget_spent}/{state.budget_total})",
                        decision_log_tail=state.decision_log[-5:],
                    )
                summary = (
                    getattr(summary_response.choices[0].message, "content", "") or ""
                ).strip()
                if not summary:
                    raise Degraded(
                        "context summarization returned an empty summary",
                        decision_log_tail=state.decision_log[-5:],
                    )
                transcript, events = compact_messages(
                    transcript,
                    summary=summary,
                    state=state,
                    output_dir=ctx.observation_dir or Path(".guardian_observations"),
                )
                state.compaction_events += len(events)
                state.decision_log.extend(events)
                session_manager.reset()

            context.messages = transcript
            transcript_tokens = context.token_count()
            if transcript_tokens > context.usable_tokens:
                raise BudgetExceeded(
                    "immutable frame plus compacted working set exceeds the "
                    f"context budget "
                    f"({transcript_tokens}/{context.usable_tokens} tokens)",
                    decision_log_tail=state.decision_log[-5:],
                )

            try:
                response = loop_llm._call_raw(
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
        outcome = LoopOutcome(type(exc).__name__, str(exc))
        state.exit_reason = outcome.status
        state.decision_log.append(exc.as_record())
        return state
    finally:
        if ctx.checkpoint_path is not None:
            save_checkpoint(ctx.checkpoint_path, state, transcript)
        session_manager.__exit__(None, None, None)
