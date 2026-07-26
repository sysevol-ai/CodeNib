# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Carried state and checkpoint contract for Guardian's L2 cycle loop."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..signals.types import Signal
from .exceptions import StateInconsistent

VALID_GRADES = frozenset({"conjecture", "supported", "finding", "refuted", "deferred"})
VALID_ORIGINS = frozenset({"signal", "memory", "exploration", "human"})


@dataclass
class Hypothesis:
    """A falsifiable claim about a solvable engineering problem."""

    id: str
    claim: str
    consequence: str
    remedy: str
    grade: str
    origin: str
    locus: List[str]
    evidence: List[str]
    confidence: float
    attempts: int
    spent_tokens: int
    first_seen_cycle: int
    last_touched_cycle: int
    supersedes: List[str]

    @classmethod
    def create(
        cls,
        *,
        claim: str,
        consequence: str,
        remedy: str,
        origin: str,
        locus: Iterable[str],
        evidence: Optional[Iterable[str]] = None,
        confidence: float = 0.5,
        cycle_no: int = 0,
        grade: str = "conjecture",
        supersedes: Optional[Iterable[str]] = None,
    ) -> "Hypothesis":
        normalized_locus = sorted({str(item) for item in locus if str(item)})
        digest_input = json.dumps(
            [claim.strip(), normalized_locus],
            sort_keys=True,
            separators=(",", ":"),
        )
        hypothesis_id = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:20]
        hypothesis = cls(
            id=hypothesis_id,
            claim=claim.strip(),
            consequence=consequence.strip(),
            remedy=remedy.strip(),
            grade=grade,
            origin=origin,
            locus=normalized_locus,
            evidence=list(evidence or []),
            confidence=float(confidence),
            attempts=0,
            spent_tokens=0,
            first_seen_cycle=cycle_no,
            last_touched_cycle=cycle_no,
            supersedes=list(supersedes or []),
        )
        validate_hypothesis(hypothesis)
        return hypothesis

    @property
    def target(self) -> str:
        """Compatibility view used by the unchanged L3 investigator."""
        return self.locus[0] if self.locus else ""

    @property
    def statement(self) -> str:
        """Compatibility view used by the unchanged L3 investigator."""
        return self.claim

    @property
    def rank(self) -> int:
        """L3 prompt compatibility; L2 no longer stores a rank."""
        return 0

    @property
    def kind(self) -> str:
        """L3 prompt compatibility without reintroducing signal taxonomy."""
        return self.origin


def _has_valid_evidence(hypothesis: Hypothesis) -> bool:
    return any(
        item.startswith(("probe-valid:", "source-valid:"))
        for item in hypothesis.evidence
    )


GRADE_RULES: Dict[str, Callable[[Hypothesis], bool]] = {
    "finding": lambda h: bool(h.remedy) and _has_valid_evidence(h),
    "supported": _has_valid_evidence,
    "refuted": _has_valid_evidence,
    "conjecture": lambda h: bool(h.claim and h.consequence and h.remedy),
    "deferred": lambda h: True,
}


def validate_hypothesis(hypothesis: Hypothesis) -> None:
    """Reject structurally invalid hypotheses and inadmissible grades."""
    if not hypothesis.claim or not hypothesis.consequence or not hypothesis.remedy:
        raise ValueError("claim, consequence, and remedy must all be non-empty")
    if hypothesis.origin not in VALID_ORIGINS:
        raise ValueError(
            f"origin must be one of {sorted(VALID_ORIGINS)}, got {hypothesis.origin!r}"
        )
    if hypothesis.grade not in VALID_GRADES:
        raise ValueError(
            f"grade must be one of {sorted(VALID_GRADES)}, got {hypothesis.grade!r}"
        )
    if not 0.0 <= hypothesis.confidence <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    if not GRADE_RULES[hypothesis.grade](hypothesis):
        if hypothesis.grade in {"finding", "supported", "refuted"}:
            raise ValueError(
                f"grade={hypothesis.grade!r} requires a probe-valid: or "
                "source-valid: evidence reference"
                + (" and a remedy" if hypothesis.grade == "finding" else "")
            )
        raise ValueError(f"hypothesis is not admissible at grade={hypothesis.grade!r}")


@dataclass
class CycleState:
    """The complete state carried between L2 model turns and cycles."""

    cycle_no: int
    commit: str
    hypotheses: List[Hypothesis]
    signals: List[Signal]
    current: Optional[str]
    budget_total: Optional[int]
    budget_spent: int
    decision_log: List[dict]
    exit_reason: Optional[str]
    carried_from: Optional[int]
    report_summary: str = ""
    degraded: bool = False
    compaction_events: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CycleState":
        raw_budget_total = raw.get("budget_total", 0)
        return cls(
            cycle_no=int(raw["cycle_no"]),
            commit=str(raw.get("commit", "")),
            hypotheses=[Hypothesis(**item) for item in raw.get("hypotheses", [])],
            signals=[Signal(**item) for item in raw.get("signals", [])],
            current=raw.get("current"),
            budget_total=(None if raw_budget_total is None else int(raw_budget_total)),
            budget_spent=int(raw.get("budget_spent", 0)),
            decision_log=list(raw.get("decision_log", [])),
            exit_reason=raw.get("exit_reason"),
            carried_from=raw.get("carried_from"),
            report_summary=str(raw.get("report_summary", "")),
            degraded=bool(raw.get("degraded", False)),
            compaction_events=int(raw.get("compaction_events", 0)),
        )


def check_invariants(state: CycleState) -> None:
    """Validate the iteration-boundary invariants or raise a typed exit."""
    problems: List[str] = []
    if state.budget_spent < 0 or (
        state.budget_total is not None and state.budget_total < 0
    ):
        problems.append("budget values must be non-negative")
    ids = [hypothesis.id for hypothesis in state.hypotheses]
    if len(ids) != len(set(ids)):
        problems.append("hypothesis ids must be unique")
    if state.current is not None and state.current not in set(ids):
        problems.append("current must name an existing hypothesis")

    for hypothesis in state.hypotheses:
        try:
            validate_hypothesis(hypothesis)
        except ValueError as exc:
            problems.append(f"{hypothesis.id}: {exc}")

    if problems:
        raise StateInconsistent(
            "; ".join(problems), decision_log_tail=state.decision_log[-5:]
        )


def save_checkpoint(
    path: os.PathLike[str] | str,
    state: CycleState,
    messages: Iterable[dict],
) -> None:
    """Atomically persist state plus the reconstructible model transcript."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"state": state.to_dict(), "messages": list(messages)}
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_checkpoint(
    path: os.PathLike[str] | str,
) -> tuple[CycleState, List[dict]]:
    """Load a checkpoint written by :func:`save_checkpoint`."""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    state = CycleState.from_dict(payload["state"])
    messages = list(payload.get("messages", []))
    check_invariants(state)
    return state, messages
