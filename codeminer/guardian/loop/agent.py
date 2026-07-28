# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Domain policy and tools for Repository Guardian's cycle agent."""

from __future__ import annotations

import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, List, Optional

from ...log_utils import get_logger
from ..agent import AgentExit, ToolAgentRuntime, execution_batches
from ..llm import ContextManager
from .prompts import (
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
from .tools import PARALLEL_SAFE_TOOL_NAMES, TOOL_NAMES, TOOLS
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
    max_inline_observation_chars: int = 4_000
    max_observation_read_chars: int = 16_000


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


def _validated_l2_source_evidence(
    raw_evidence: object,
    *,
    repo_path: str,
    commit: str,
) -> List[str]:
    """Validate exact L2 source citations and mint admissible evidence refs."""

    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ValueError("source_evidence must be a non-empty list")
    validated: List[str] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            raise ValueError("each source_evidence item must be an object")
        path = str(item.get("path", "")).strip()
        description = str(item.get("description", "")).strip()
        start_line = int(item.get("start_line", 0))
        end_line = int(item.get("end_line", 0))
        if not path or not description:
            raise ValueError("source_evidence path and description must be non-empty")
        if Path(path).is_absolute():
            raise ValueError("source_evidence path must be repository-relative")
        if start_line < 1 or end_line < start_line:
            raise ValueError("source_evidence must name a valid positive line range")
        if end_line - start_line > 200:
            raise ValueError("source_evidence spans may not exceed 201 lines")
        excerpt = read_code(
            repo_path,
            path,
            start_line=start_line,
            end_line=end_line,
            max_chars=1_000_000,
        )
        last_line = re.match(r"^\s*(\d+)\s", excerpt.splitlines()[-1])
        if (
            excerpt == "(empty range)"
            or not last_line
            or int(last_line.group(1)) < end_line
        ):
            raise ValueError(
                f"source_evidence range does not exist: "
                f"{path}:{start_line}-{end_line}"
            )
        locus = f"{path}:{start_line}-{end_line}"
        validated.extend(
            [
                f"source-valid:l2:{commit}:{locus}",
                f"source-note:{locus}: {description}",
            ]
        )
    return validated


def _model_evidence(raw_evidence: object) -> List[str]:
    """Accept model-authored notes but reserve runtime evidence namespaces."""

    if raw_evidence is None:
        return []
    if not isinstance(raw_evidence, list):
        raise ValueError("evidence must be a list")
    evidence = [str(item).strip() for item in raw_evidence]
    reserved = ("probe-valid:", "source-valid:", "source-note:", "resolved:")
    forged = [item for item in evidence if item.startswith(reserved)]
    if forged:
        raise ValueError(
            "evidence references beginning with probe-valid:, source-valid:, "
            "source-note:, or resolved: are runtime-generated; use "
            "source_evidence for direct L2 source citations"
        )
    return evidence


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
            offset = max(0, int(args.get("offset", 0) or 0))
            limit = min(
                max(1, int(args.get("limit", 4_000) or 4_000)),
                max(1, ctx.max_observation_read_chars),
            )
            content = target.read_text(encoding="utf-8", errors="replace")
            selected = content[offset : offset + limit]
            suffix = (
                f"\n[observation {ref}: chars {offset}-{offset + len(selected)} "
                f"of {len(content)}]"
            )
            return (selected or "(empty observation range)") + suffix

        if call_name == "write_hypothesis":
            hypothesis = Hypothesis.create(
                claim=str(args.get("claim", "")),
                consequence=str(args.get("consequence", "")),
                remedy=str(args.get("remedy", "")),
                origin=str(args.get("origin", "")),
                locus=args.get("locus") or [],
                evidence=_model_evidence(args.get("evidence")),
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
                        dict.fromkeys(
                            hypothesis.evidence + _model_evidence(args["evidence"])
                        )
                    )
                if "source_evidence" in args:
                    hypothesis.evidence = list(
                        dict.fromkeys(
                            [
                                *hypothesis.evidence,
                                *_validated_l2_source_evidence(
                                    args["source_evidence"],
                                    repo_path=ctx.repo_path,
                                    commit=state.commit,
                                ),
                            ]
                        )
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
            # Checkpoint/replay fixtures from the pre-understanding protocol may
            # contain summary-only submissions. Preserve replay compatibility;
            # current model calls are constrained by the stricter tool schema.
            understanding = str(args.get("understanding", "")).strip() or summary
            open_questions = [
                str(item).strip()
                for item in args.get("open_questions", [])
                if str(item).strip()
            ]
            if not summary:
                raise ValueError("summary must be non-empty")
            state.report_summary = summary
            state.understanding = understanding
            state.open_questions = open_questions
            raise ReportSubmitted(summary, decision_log_tail=state.decision_log[-5:])

        return f"(unknown tool: {call_name!r})"
    except ReportSubmitted:
        raise
    except (
        Exception
    ) as exc:  # noqa: BLE001 - tool failures are observations by contract
        return f"(tool_error {type(exc).__name__}: {exc})"


def _compact_observation(
    observation: str,
    *,
    ctx: LoopContext,
    state: CycleState,
    turn: int,
    index: int,
    call_id: str,
    call_name: str,
) -> str:
    """Persist a large result and return a bounded preview with a stable ref."""

    limit = max(1, ctx.max_inline_observation_chars)
    if (
        call_name == "read_observation"
        or len(observation) <= limit
        or ctx.observation_dir is None
    ):
        return observation
    ctx.observation_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256(
        f"{state.cycle_no}\0{turn}\0{index}\0{call_id}\0{observation}".encode(
            "utf-8", errors="replace"
        )
    ).hexdigest()[:12]
    ref = f"obs_{state.cycle_no:04d}_{turn:03d}_{index:02d}_{digest}.txt"
    target = ctx.observation_dir / ref
    target.write_text(observation, encoding="utf-8")
    return (
        f"[observation ref={ref} chars={len(observation)} "
        f"preview_chars={limit}]\n"
        f"{observation[:limit]}\n"
        f"[preview ended; use read_observation(ref={json.dumps(ref)}, "
        f"offset={limit}, limit=4000) if more is material]"
    )


def run_cycle_agent(
    state: CycleState,
    ctx: LoopContext,
    *,
    messages: Optional[List[dict]] = None,
) -> CycleState:
    """Run the cycle agent until a typed exit, checkpointing every boundary."""
    transcript = list(
        messages
        if messages is not None
        else initial_messages(state, repo_path=ctx.repo_path, arm=ctx.arm)
    )
    started = time.monotonic()
    turn = 0
    agent = ToolAgentRuntime(ctx.llm, transcript)
    agent.__enter__()
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
                    summary_response = agent.call_raw(
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
                agent.replace_messages(transcript)
                transcript = agent.messages

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
                agent_turn = agent.request(tools=TOOLS)
            except Exception as exc:  # noqa: BLE001
                state.degraded = True
                raise Degraded(
                    f"model unavailable: {exc}",
                    decision_log_tail=state.decision_log[-5:],
                ) from exc

            response = agent_turn.response
            usage = getattr(response, "usage", None)
            state.budget_spent += int(getattr(usage, "total_tokens", 0) or 0)
            if ctx.outer_usage is not None:
                ctx.outer_usage.add(response)
            transcript = agent.messages
            calls = agent_turn.tool_calls
            if not calls:
                raise NoProgress(
                    (
                        getattr(agent_turn.message, "content", "")
                        or "model made no tool call"
                    ).strip(),
                    decision_log_tail=state.decision_log[-5:],
                )

            cursor = 0
            for batch in execution_batches(calls, PARALLEL_SAFE_TOOL_NAMES):
                records = []
                for call in batch:
                    args = dict(call.arguments)
                    record = {
                        "event": "tool_call",
                        "turn": turn,
                        "tool_call_id": call.id,
                        "tool": call.name,
                        "arguments": args,
                        "state_before": _state_summary(state),
                    }
                    state.decision_log.append(record)
                    records.append(record)

                if len(batch) > 1:
                    with ThreadPoolExecutor(
                        max_workers=min(len(batch), 8),
                        thread_name_prefix="guardian-read",
                    ) as executor:
                        futures = [
                            executor.submit(
                                _dispatch,
                                call.name,
                                dict(call.arguments),
                                state,
                                ctx,
                            )
                            for call in batch
                        ]
                        observations = [future.result() for future in futures]
                else:
                    call = batch[0]
                    observations = [
                        (
                            f"(unknown tool: {call.name!r})"
                            if call.name not in TOOL_NAMES
                            else _dispatch(call.name, dict(call.arguments), state, ctx)
                        )
                    ]

                for batch_index, (
                    call,
                    record,
                    observation,
                ) in enumerate(
                    zip(batch, records, observations, strict=True),
                    start=cursor,
                ):
                    compact_observation = _compact_observation(
                        observation,
                        ctx=ctx,
                        state=state,
                        turn=turn,
                        index=batch_index,
                        call_id=call.id,
                        call_name=call.name,
                    )
                    record["observation"] = compact_observation[:2_000]
                    record["state_after"] = _state_summary(state)
                    agent.observe(call, compact_observation)
                cursor += len(batch)
                transcript = agent.messages
            turn += 1
            if ctx.checkpoint_path is not None:
                save_checkpoint(ctx.checkpoint_path, state, transcript)
    except CycleInterrupt as exc:
        outcome = AgentExit(type(exc).__name__, str(exc))
        state.exit_reason = outcome.status
        state.decision_log.append(exc.as_record())
        return state
    finally:
        if ctx.checkpoint_path is not None:
            save_checkpoint(ctx.checkpoint_path, state, transcript)
        agent.__exit__(None, None, None)
