# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Prompts, frames, and compaction snapshots for the cycle agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

from ..llm import ContextManager
from .state import CycleState

SYSTEM_PROMPT = """\
You are Repository Guardian's cycle agent. You proactively inspect a repository
for verified, actionable engineering problems.

A signal is only a deterministic measurement. It is not a hypothesis or a
finding. "A.txt changed three times" is a signal and must never be reported as a
finding.

A hypothesis requires all of:
- claim: a falsifiable statement about behavior
- consequence: what breaks, degrades, or is lost if the claim is true
- remedy: a concrete engineering change that resolves it

Use tools in whatever order the evidence warrants. You may inspect signals,
recall prior trajectories, explore code, write hypotheses, investigate them,
and update their grades. New evidence may lead to new hypotheses. Only grade
"finding" means verified and actionable. Use "supported" when the claim holds
but the remedy is not yet actionable, "refuted" when a probe contradicts it,
and "deferred" when more work is not worth this cycle's budget. At the start of
a new commit, reconcile every carried hypothesis whose locus or contract may
have changed. Mark it "resolved" when the new commit addressed the previously
real problem; cite the fixing diff, source span, or test in the reason. Do not
use "refuted" for a defect that was real in an earlier commit and is now fixed.

When several independent source reads are known up front, request them together
as one batch. The runtime executes explicitly parallel-safe reads concurrently
and returns their observations in request order. Keep dependent reads and all
state-changing calls ordered. A large result may be represented by an
observation ref and preview; call read_observation with an offset and limit only
when the omitted portion is material.

You own the decision whether a hypothesis needs L3 investigation. Do not invoke
L3 by default. If a simple claim follows conclusively from exact source spans,
call update_hypothesis with structured source_evidence and grade it directly;
the runtime validates those spans and records source-valid evidence. Invoke L3
when the claim depends on execution, integration behavior, environment state,
historical comparison, or evidence not closed over the inspected source. L3 may
return probe-valid evidence from execution or source-valid evidence from its own
closed-form argument. Judge either on its merits.

This is a commit review. Inspect read_commit_diff before broad exploration and
prioritize contracts changed by this commit. Distinguish defects introduced or
exposed by HEAD from pre-existing repository problems; do not spend the cycle
on an unrelated pre-existing issue while changed-surface hypotheses remain.

Resolve repeated claims through update_hypothesis and supersedes instead of
creating duplicate findings. When further work is not worth its cost, call
submit_report. Do not merely answer in prose: normal completion is a
submit_report tool call.
"""


def opening_context(state: CycleState, *, repo_path: str, arm: str) -> str:
    """Build the immutable cycle frame shown at the start of every run."""
    open_hypotheses = [
        {
            "id": item.id,
            "claim": item.claim,
            "grade": item.grade,
            "locus": item.locus,
            "confidence": item.confidence,
            "last_touched_cycle": item.last_touched_cycle,
        }
        for item in state.hypotheses
        if item.grade in {"conjecture", "supported", "finding", "deferred"}
    ]
    return "\n".join(
        [
            "=== IMMUTABLE CYCLE FRAME ===",
            f"Repository: {repo_path}",
            f"Commit: {state.commit}",
            f"Cycle: {state.cycle_no}",
            f"Memory arm: {arm} (the recall tool is present in every arm)",
            (
                "Token budget: unlimited"
                if state.budget_total is None
                else f"Token budget: {state.budget_total}"
            ),
            "Open carried hypotheses:",
            json.dumps(open_hypotheses, sort_keys=True),
            "The full signal set is available through list_signals.",
            "=== END FRAME ===",
        ]
    )


def initial_messages(state: CycleState, *, repo_path: str, arm: str) -> List[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": opening_context(state, repo_path=repo_path, arm=arm),
        },
    ]


SUMMARY_PROMPT = """\
Summarize the Guardian work so this same agent can continue after its old
conversation is replaced. Preserve concrete facts, not general advice:
- the reviewed commit and changed contracts;
- source spans and tool results that materially constrain the review;
- every hypothesis considered and its current evidentiary status;
- approaches ruled out and why;
- unresolved questions and the next intended checks.
Do not invent evidence. Return only the working-memory summary.
"""


def context_tokens(messages: List[dict], *, model: Optional[str] = None) -> int:
    """Estimate transcript tokens using the model tokenizer when available."""

    return ContextManager(messages, 1, 0, model=model).token_count()


def needs_compaction(
    messages: List[dict],
    *,
    max_tokens: int,
    reserve_tokens: int,
    model: Optional[str] = None,
) -> bool:
    """Return whether history has reached the model-output reserve boundary."""

    return ContextManager(
        messages,
        max_tokens=max_tokens,
        reserve_tokens=reserve_tokens,
        model=model,
    ).needs_compaction()


def summarization_messages(messages: List[dict], state: CycleState) -> List[dict]:
    """Append a cache-friendly summarization request to the current history."""

    snapshot = {
        "cycle": state.cycle_no,
        "commit": state.commit,
        "current_hypothesis": state.current,
        "hypotheses": [
            {
                "id": item.id,
                "claim": item.claim,
                "grade": item.grade,
                "locus": item.locus,
                "evidence": item.evidence,
                "confidence": item.confidence,
            }
            for item in state.hypotheses
        ],
    }
    return ContextManager(messages, 1, 0).summarization_messages(
        SUMMARY_PROMPT, snapshot
    )


def compact_messages(
    messages: List[dict],
    *,
    summary: str,
    state: CycleState,
    output_dir: Path,
    keep_recent_turns: int = 0,
) -> Tuple[List[dict], List[dict]]:
    """Replace old history with one summary plus a small recent working set.

    The complete pre-compaction transcript is archived for auditability, but
    raw tool observations are not individually evicted and rehydrated.
    """

    snapshot = {
        "cycle": state.cycle_no,
        "commit": state.commit,
        "current_hypothesis": state.current,
        "hypotheses": [
            {
                "id": item.id,
                "claim": item.claim,
                "grade": item.grade,
                "locus": item.locus,
                "evidence": item.evidence,
                "confidence": item.confidence,
            }
            for item in state.hypotheses
        ],
    }
    manager = ContextManager(messages, 1, 0)
    events = manager.compact(
        summary=summary,
        canonical_snapshot=snapshot,
        output_dir=output_dir,
        memory_heading="COMPACTED WORKING MEMORY",
        keep_recent_turns=keep_recent_turns,
    )
    if manager.messages and len(manager.messages) > 2:
        manager.messages[2]["content"] = manager.messages[2]["content"].replace(
            "Canonical state:", "Canonical Guardian state:"
        )
    return manager.messages, events
