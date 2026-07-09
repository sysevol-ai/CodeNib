# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Hypothesize step for Repository Guardian.

Takes observe signals (churn hotspots + graph-diff drift) plus paged cross-cycle
memory and produces a ranked list of :class:`Hypothesis` objects — one per
investigation candidate.  Two entry points:

* :func:`hypothesize` — calls the LLM once to rank signals using memory context.
  Used by the memory arm when a model is configured.
* :func:`heuristic_hypotheses` — pure deterministic fallback: rank by commit
  count (churn) with drift signals appended at a fixed score.  Used when no LLM
  is available or ``arm="memoryless"``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from ..log_utils import get_logger

if TYPE_CHECKING:
    from .llm_investigator import LLMUsage
    from .signals import DriftSignal, Hotspot

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Hypothesis:
    """One ranked investigation candidate produced by the Hypothesize step."""

    rank: int
    target: str
    kind: str           # "churn" | "drift" | "test_failure"
    statement: str      # one-sentence hypothesis passed to the investigate step
    confidence: float   # 0.0–1.0 (model estimate or heuristic proxy)
    memory_cited: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are a repository risk analyst for the Repository Guardian system.

You receive two inputs:
1. SIGNALS — deterministic signals detected this cycle (churn hotspots and
   structural drift from graph-diff).
2. MEMORY — findings produced by prior cycles on this repository (most recent
   first, paged to a maximum of 5 entries).

Your task: return a JSON array of investigation hypotheses, ranked by estimated
risk (rank 1 = highest priority). Each element must be an object with exactly
these keys:

  {
    "rank":          <integer, 1 = highest>,
    "target":        "<file path or symbol name from the signals>",
    "kind":          "<one of: churn | drift | test_failure>",
    "statement":     "<one sentence: what risk you hypothesize and why>",
    "confidence":    <float 0.0–1.0>,
    "memory_cited":  ["<prior finding title or snippet that informed this>"]
  }

Rules:
- Only reference targets that appear in SIGNALS; do not invent new paths.
- If MEMORY is empty, base hypotheses only on SIGNALS.
- memory_cited may be an empty list if memory is not relevant.
- Return ONLY the JSON array. No prose wrapper, no markdown fences.
"""


def _build_hypothesize_prompt(
    hotspots: "List[Hotspot]",
    drift_signals: "List[DriftSignal]",
    prior_findings: List[dict],
    *,
    max_prior: int = 5,
) -> str:
    parts: List[str] = []

    parts.append("=== SIGNALS ===")
    if hotspots:
        parts.append("Churn hotspots (files changed most in this window):")
        for h in hotspots:
            parts.append(f"  - {h.path}  ({h.commit_count} commits)")
    else:
        parts.append("Churn hotspots: (none)")

    if drift_signals:
        parts.append("Graph-diff drift signals:")
        for s in drift_signals:
            parts.append(f"  - [{s.kind}] {s.symbol} ({s.file}): {s.detail}")
    else:
        parts.append("Graph-diff drift signals: (none)")

    parts.append("")
    parts.append("=== MEMORY (prior findings, most recent first) ===")
    if prior_findings:
        from .memory import format_findings_for_prompt

        parts.append(format_findings_for_prompt(prior_findings, max_findings=max_prior))
    else:
        parts.append("(no prior findings in memory)")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM-backed hypothesize
# ---------------------------------------------------------------------------


def hypothesize(
    hotspots: "List[Hotspot]",
    drift_signals: "List[DriftSignal]",
    prior_findings: List[dict],
    llm: object,
    *,
    usage_acc: "Optional[LLMUsage]" = None,
    top_n: int = 5,
) -> List[Hypothesis]:
    """Call the LLM once to rank signals into hypotheses.

    Falls back to :func:`heuristic_hypotheses` if the LLM call fails or
    returns malformed JSON.  Always returns a list; never raises.
    """
    if not hotspots and not drift_signals:
        return []

    user_msg = _build_hypothesize_prompt(hotspots, drift_signals, prior_findings)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    try:
        response = llm._call_raw(messages)  # type: ignore[union-attr]
        if usage_acc is not None:
            usage_acc.add(response)
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hypothesize: LLM call failed (%s); using heuristic fallback", exc)
        return heuristic_hypotheses(hotspots, drift_signals, top_n=top_n)

    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("expected a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "hypothesize: model returned malformed JSON (%s); using heuristic fallback.\n"
            "Raw response (first 300 chars): %s",
            exc,
            raw[:300],
        )
        return heuristic_hypotheses(hotspots, drift_signals, top_n=top_n)

    results: List[Hypothesis] = []
    for item in items[:top_n]:
        if not isinstance(item, dict):
            continue
        try:
            results.append(
                Hypothesis(
                    rank=int(item.get("rank", len(results) + 1)),
                    target=str(item.get("target", "")),
                    kind=str(item.get("kind", "churn")),
                    statement=str(item.get("statement", "")),
                    confidence=float(item.get("confidence", 0.5)),
                    memory_cited=list(item.get("memory_cited") or []),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.debug("hypothesize: skipping malformed item %r: %s", item, exc)

    if not results:
        logger.warning("hypothesize: model returned no valid hypotheses; using heuristic fallback")
        return heuristic_hypotheses(hotspots, drift_signals, top_n=top_n)

    results.sort(key=lambda h: h.rank)
    logger.info(
        "hypothesize: LLM produced %d hypothesis(es) (top: %s, conf=%.2f)",
        len(results), results[0].target if results else "—", results[0].confidence if results else 0,
    )
    return results


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------

_DRIFT_CONFIDENCE = 0.60  # fixed confidence score for graph-diff drift signals
_CHURN_CONFIDENCE_MAX = 0.80
_CHURN_CONFIDENCE_MIN = 0.30


def heuristic_hypotheses(
    hotspots: "List[Hotspot]",
    drift_signals: "Optional[List[DriftSignal]]" = None,
    *,
    top_n: int = 5,
) -> List[Hypothesis]:
    """Rank signals without a model call.

    Churn hotspots are ranked by descending commit count; confidence is linearly
    scaled between ``_CHURN_CONFIDENCE_MIN`` and ``_CHURN_CONFIDENCE_MAX`` relative
    to the highest-churn file.  Drift signals are appended after churn with a
    fixed confidence.
    """
    if drift_signals is None:
        drift_signals = []

    results: List[Hypothesis] = []
    rank = 1

    sorted_hotspots = sorted(hotspots, key=lambda h: (-h.commit_count, h.path))
    max_count = sorted_hotspots[0].commit_count if sorted_hotspots else 1
    for h in sorted_hotspots:
        if rank > top_n:
            break
        ratio = h.commit_count / max_count if max_count else 0.0
        conf = _CHURN_CONFIDENCE_MIN + ratio * (_CHURN_CONFIDENCE_MAX - _CHURN_CONFIDENCE_MIN)
        results.append(
            Hypothesis(
                rank=rank,
                target=h.path,
                kind="churn",
                statement=(
                    f"{h.path} changed in {h.commit_count} commits and may accumulate "
                    "technical debt or introduce regressions."
                ),
                confidence=round(conf, 2),
                memory_cited=[],
            )
        )
        rank += 1

    for s in drift_signals:
        if rank > top_n:
            break
        results.append(
            Hypothesis(
                rank=rank,
                target=s.symbol,
                kind="drift",
                statement=f"[{s.kind}] {s.symbol}: {s.detail}",
                confidence=_DRIFT_CONFIDENCE,
                memory_cited=[],
            )
        )
        rank += 1

    return results
