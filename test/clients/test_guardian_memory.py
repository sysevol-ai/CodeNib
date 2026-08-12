# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for structured specification-memory persistence."""

from __future__ import annotations

from pathlib import Path

from codenib.clients.guardian import (
    Evidence,
    EvidenceAuthority,
    EvidenceSourceType,
    GuardianMemoryStore,
    GuardianResult,
    ReviewStatus,
    RoundRecord,
    SpecificationMemory,
    SpecificationProvenance,
    SpecificationRecord,
    SpecificationStatus,
)


def test_persisted_memory_load_revalidates_repository_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "repository"
    workspace.mkdir()
    source = workspace / "contract.py"
    source.write_text("mode = 'safe'\n")
    evidence = Evidence(
        path="contract.py",
        line_start=1,
        line_end=1,
        description="the configured mode",
        source_type=EvidenceSourceType.INTERFACE,
        authority=EvidenceAuthority.CONVENTIONAL,
        evidence_id="EV-1",
        supports=("LS-1",),
        snapshot="b" * 40,
        blob_sha256=__import__("hashlib").sha256(source.read_bytes()).hexdigest(),
    )
    specification = SpecificationRecord(
        specification_id="LS-1",
        statement="Copies preserve mode.",
        condition="when copied",
        scope=("copy",),
        supporting_evidence=("EV-1",),
        counterevidence=(),
        provenance=(SpecificationProvenance("explorer", 1, "C-1"),),
        status=SpecificationStatus.PROPOSED,
        status_reason="one conventional source",
    )
    memory = SpecificationMemory(
        session_id="GS-test",
        specifications=(specification,),
        evidence=(evidence,),
        rounds=(
            RoundRecord(
                round=1,
                briefs=(),
                explorer_outputs=(),
                aggregation_summary="one proposed specification",
                frontier=("LS-1",),
                new_information=True,
            ),
        ),
        observed_snapshots=("b" * 40,),
    )
    result = GuardianResult(
        base_commit="a" * 40,
        candidate_commit="b" * 40,
        status=ReviewStatus.COMPLETE,
        memory=memory,
    )
    store = GuardianMemoryStore(tmp_path / "memory")
    store.update(result, workspace)

    assert store.load(workspace).evidence[0].fresh is True
    source.write_text("mode = 'unsafe'\n")
    loaded = store.load(workspace)
    assert loaded.session_id == "GS-test"
    assert loaded.evidence[0].fresh is False
    assert loaded.specifications[0].status is SpecificationStatus.PROPOSED
    assert loaded.rounds[0].frontier == ("LS-1",)
