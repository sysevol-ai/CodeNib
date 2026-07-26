# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Frame construction and coherent context compaction for the L2 loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

from ...agent.history import count_message_tokens
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
and "deferred" when more work is not worth this cycle's budget.
L3 may return probe-valid evidence from execution or source-valid evidence from
a closed-form argument over exact source spans. Judge either on its merits.

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
        if item.grade in {"conjecture", "supported"}
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

    return count_message_tokens(messages, model=model)


def needs_compaction(
    messages: List[dict],
    *,
    max_tokens: int,
    reserve_tokens: int,
    model: Optional[str] = None,
) -> bool:
    """Return whether history has reached the model-output reserve boundary."""

    usable = max(1, max_tokens - max(0, reserve_tokens))
    return context_tokens(messages, model=model) > usable


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
    return [
        *messages,
        {
            "role": "user",
            "content": (
                f"{SUMMARY_PROMPT}\nCanonical state at compaction:\n"
                f"{json.dumps(snapshot, sort_keys=True, default=str)}"
            ),
        },
    ]


def _recent_complete_turns(messages: List[dict], keep_turns: int) -> List[dict]:
    if keep_turns <= 0:
        return []
    assistant_positions = [
        index
        for index, message in enumerate(messages[2:], start=2)
        if message.get("role") == "assistant"
    ]
    if not assistant_positions:
        return []
    start = assistant_positions[-keep_turns]
    return list(messages[start:])


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

    if len(messages) <= 2:
        return messages, []
    output_dir.mkdir(parents=True, exist_ok=True)
    archives = sorted(output_dir.glob("transcript_before_compaction_*.json"))
    target = output_dir / f"transcript_before_compaction_{len(archives) + 1:04d}.json"
    target.write_text(
        json.dumps(messages, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    recent = _recent_complete_turns(messages, keep_recent_turns)
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
    compacted = [
        messages[0],
        messages[1],
        {
            "role": "user",
            "content": (
                "=== COMPACTED WORKING MEMORY ===\n"
                f"{summary.strip()}\n\n"
                "Canonical Guardian state:\n"
                f"{json.dumps(snapshot, sort_keys=True, default=str)}\n"
                "=== END COMPACTED WORKING MEMORY ==="
            ),
        },
        *recent,
    ]
    event = {
        "event": "compaction",
        "strategy": "structured_summary",
        "archive": target.name,
        "messages_before": len(messages),
        "messages_after": len(compacted),
        "recent_turns_retained": keep_recent_turns,
        "summary_chars": len(summary),
    }
    return compacted, [event]
