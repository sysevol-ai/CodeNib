# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Frame construction and arm-blind context compaction for the L2 loop."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Tuple

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


def _message_size(message: dict) -> int:
    return len(json.dumps(message, sort_keys=True, default=str))


def compact_messages(
    messages: List[dict],
    *,
    max_chars: int,
    output_dir: Path,
) -> Tuple[List[dict], List[dict]]:
    """Externalize old tool observations while preserving the immutable frame.

    The first two messages are never compacted. The policy depends only on
    serialized message size, so it is identical in memory and memoryless arms.
    """
    if max_chars <= 0 or sum(_message_size(item) for item in messages) <= max_chars:
        return messages, []

    output_dir.mkdir(parents=True, exist_ok=True)
    compacted = list(messages)
    events: List[dict] = []
    for index in range(2, len(compacted)):
        if sum(_message_size(item) for item in compacted) <= max_chars:
            break
        message = compacted[index]
        if message.get("role") != "tool":
            continue
        content = str(message.get("content", ""))
        if len(content) < 256:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]
        target = output_dir / f"{digest}.txt"
        if not target.exists():
            target.write_text(content, encoding="utf-8")
        compacted[index] = {
            **message,
            "content": (
                f"[observation externalized: {target.name}; "
                "use read_observation to recover it]"
            ),
        }
        events.append(
            {
                "event": "compaction",
                "message_index": index,
                "observation_ref": target.name,
                "original_chars": len(content),
            }
        )
    return compacted, events
