# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for controller-owned Guardian memory persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codenib.clients.guardian import (
    Evidence,
    FindingStatus,
    GuardianFinding,
    GuardianMemoryStore,
    GuardianResult,
    ReviewStatus,
)


def _result(snapshot: str, finding: GuardianFinding) -> GuardianResult:
    return GuardianResult(
        base_commit="a" * 40,
        candidate_commit=snapshot,
        status=ReviewStatus.COMPLETE,
        findings=(finding,) if finding.status is FindingStatus.VIOLATED else (),
        assessments=(finding,),
    )


def _finding(status: FindingStatus, *, memory_id: str = "") -> GuardianFinding:
    return GuardianFinding(
        statement="Prediction preserves the fitted feature layout.",
        condition="when preprocessing expands raw input columns",
        status=status,
        evidence=(
            Evidence(
                path="feature.py",
                line_start=1,
                line_end=1,
                description="the prediction path applies the recorded layout",
            ),
        ),
        patch_assessment="the current snapshot was assessed",
        recommendation="apply the recorded layout",
        confidence=0.9,
        memory_id=memory_id,
    )


def test_memory_persists_assessments_and_revalidates_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "repository"
    workspace.mkdir()
    source = workspace / "feature.py"
    source.write_text("enabled = True\n", encoding="utf-8")
    store = GuardianMemoryStore(tmp_path / "state")

    first = store.update(_result("b" * 40, _finding(FindingStatus.VIOLATED)), workspace)

    assert first.observed_snapshots == ("b" * 40,)
    assert len(first.specifications) == 1
    remembered = store.load(workspace).specifications[0]
    assert remembered.evidence[0].fresh is True
    assert remembered.assessments[0].status is FindingStatus.VIOLATED

    source.write_text("enabled = False\n", encoding="utf-8")
    assert store.load(workspace).specifications[0].evidence[0].fresh is False

    second_finding = _finding(
        FindingStatus.SATISFIED,
        memory_id=remembered.memory_id,
    )
    second = store.update(_result("c" * 40, second_finding), workspace)

    assert len(second.specifications) == 1
    assert [
        assessment.status for assessment in second.specifications[0].assessments
    ] == [
        FindingStatus.VIOLATED,
        FindingStatus.SATISFIED,
    ]
    assert second.observed_snapshots == ("b" * 40, "c" * 40)
    events = [
        json.loads(line)
        for line in store.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["sequence"] for event in events] == [1, 2]
    assert events[0]["updates"][0]["memory_id"] == remembered.memory_id

    retracted = _finding(
        FindingStatus.RETRACTED,
        memory_id=remembered.memory_id,
    )
    third = store.update(_result("d" * 40, retracted), workspace)
    assert third.specifications[0].assessments[-1].status is FindingStatus.RETRACTED


def test_memory_fails_closed_when_materialized_state_is_invalid(
    tmp_path: Path,
) -> None:
    store = GuardianMemoryStore(tmp_path / "state")
    store.root.mkdir()
    store.state_path.write_text('{"schema_version": 999}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="cannot be loaded"):
        store.load(tmp_path)
