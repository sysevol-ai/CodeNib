# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Compatibility helpers around the stateful outer-loop hypothesis type.

The production cycle no longer performs a one-shot ``hypothesize`` stage.
These helpers remain for callers evaluating candidate formation in isolation;
the real cycle forms hypotheses through ``write_hypothesis`` tool calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, List, Optional

from ...log_utils import get_logger
from ..loop.state import Hypothesis

if TYPE_CHECKING:
    from ..investigator import LLMUsage
    from ..signals import Signal

logger = get_logger(__name__)

_SYSTEM = """\
You are Repository Guardian. Return a JSON array of complete hypotheses.
A signal is only a measurement; "A.txt changed three times" is not a hypothesis.
Every item must contain claim, consequence, remedy, origin, locus, evidence,
and confidence. Claims may originate from signal, memory, exploration, or human
context and are not restricted to paths named by signals. Return only JSON.
"""


def _signal_context(
    hotspots: "List[Signal]",
    drift_signals: "List[Signal]",
    prior_findings: List[dict],
) -> str:
    return json.dumps(
        {
            "signals": [
                {
                    "kind": "churn",
                    "locus": [item.path],
                    "detail": f"{item.commit_count} commits in the configured window",
                }
                for item in hotspots
            ]
            + [
                {
                    "kind": item.kind,
                    "locus": [value for value in [item.file, item.symbol] if value],
                    "detail": item.detail,
                }
                for item in drift_signals
            ],
            "memory": prior_findings,
        },
        sort_keys=True,
    )


def hypothesize(
    hotspots: "List[Signal]",
    drift_signals: "List[Signal]",
    prior_findings: List[dict],
    llm: object,
    *,
    usage_acc: "Optional[LLMUsage]" = None,
    top_n: int = 5,
    repo_path: str = "",
) -> List[Hypothesis]:
    """One-shot compatibility adapter; production uses the L2 tool loop."""
    del repo_path
    if not hotspots and not drift_signals and not prior_findings:
        return []
    try:
        response = llm._call_raw(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": _signal_context(hotspots, drift_signals, prior_findings),
                },
            ]
        )
        if usage_acc is not None:
            usage_acc.add(response)
        items = json.loads(response.choices[0].message.content or "[]")
        if not isinstance(items, list):
            raise ValueError("expected a JSON array")
        results = []
        for item in items[:top_n]:
            results.append(
                Hypothesis.create(
                    claim=str(item.get("claim", "")),
                    consequence=str(item.get("consequence", "")),
                    remedy=str(item.get("remedy", "")),
                    origin=str(item.get("origin", "exploration")),
                    locus=item.get("locus") or [],
                    evidence=item.get("evidence") or [],
                    confidence=float(item.get("confidence", 0.5)),
                )
            )
        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("one-shot hypothesize degraded: %s", exc)
        return heuristic_hypotheses(hotspots, drift_signals, top_n=top_n)


def heuristic_hypotheses(
    hotspots: "List[Signal]",
    drift_signals: "Optional[List[Signal]]" = None,
    *,
    top_n: int = 5,
) -> List[Hypothesis]:
    """Degraded, memory-blind fallback; its cycles must be excluded from C−B."""
    candidates: List[Hypothesis] = []
    ranked_hotspots = sorted(hotspots, key=lambda item: (-item.commit_count, item.path))
    for hotspot in ranked_hotspots:
        candidates.append(
            Hypothesis.create(
                claim=(
                    f"Recent edits to {hotspot.path} may have introduced a "
                    "behavioral regression not covered by its current tests"
                ),
                consequence=(
                    "Callers may observe behavior inconsistent with the "
                    "pre-change contract"
                ),
                remedy=(
                    f"Review the changed behavior in {hotspot.path} and add a "
                    "targeted regression test before correcting any mismatch"
                ),
                origin="signal",
                locus=[hotspot.path],
                evidence=[],
                confidence=0.3,
            )
        )
    for signal in drift_signals or []:
        locus = [item for item in [signal.file, signal.symbol] if item]
        candidates.append(
            Hypothesis.create(
                claim=(
                    f"The {signal.kind} structural change at "
                    f"{signal.symbol or signal.file} may violate a dependency contract"
                ),
                consequence="Dependent symbols may fail or silently change behavior",
                remedy="Inspect affected dependents and add a contract regression probe",
                origin="signal",
                locus=locus,
                evidence=[],
                confidence=0.3,
            )
        )
    return candidates[:top_n]


__all__ = ["Hypothesis", "heuristic_hypotheses", "hypothesize"]
