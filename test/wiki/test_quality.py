# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from codenib.wiki.quality import (
    audit_cache,
    audit_page,
    section_evidence_report,
    section_narrative_report,
    summarize_page_audits,
)


def _page(*, repeated_prose: bool) -> dict:
    workflow = (
        "The repository compiles source into reusable indexed context, then "
        "serves that context through its local Wiki. [E1]"
        if repeated_prose
        else "The command accepts a checkout and prepares a local Wiki. [E1]"
    )
    return {
        "id": "overview",
        "title": "Overview",
        "markdown": (
            "# Overview\n\n"
            "The repository compiles source into reusable indexed context and "
            "serves that context through a local Wiki. [E1]\n\n"
            "## Workflow\n\n"
            f"{workflow}\n\n"
            "## Runtime\n\n"
            "The runtime serves source-linked requests. [E3]"
        ),
        "generation": {
            "mode": "generated",
            "repaired": False,
            "fallback": None,
        },
        "grounding": {"valid": True, "citation_coverage": 1.0},
        "quality": {"valid": True, "claim_coverage": 1.0},
    }


def test_section_evidence_report_detects_semantic_intro_reuse():
    report = section_evidence_report(_page(repeated_prose=True)["markdown"])

    assert report["intro_evidence"] == ["E1"]
    assert report["intro_only_sections"] == ["Workflow"]
    assert report["new_evidence_by_section"]["Workflow"] == []


def test_section_narrative_report_detects_repeated_prose():
    repeated = section_narrative_report(_page(repeated_prose=True)["markdown"])
    distinct = section_narrative_report(_page(repeated_prose=False)["markdown"])

    assert repeated["redundant_sections"] == ["Workflow"]
    assert distinct["redundant_sections"] == []


def test_section_evidence_report_detects_section_source_reuse():
    markdown = (
        "# Overview\n\n"
        "The repository serves indexed source context. [E1]\n\n"
        "## Build\n\n"
        "The compiler writes a repository index. [E2]\n\n"
        "## Load\n\n"
        "The runtime loads the repository index. [E3]\n\n"
        "## Serve\n\n"
        "The server exposes the loaded repository index. [E2] [E3]"
    )

    report = section_evidence_report(markdown)

    assert report["intro_only_sections"] == []
    assert report["sections_without_new_evidence"] == ["Serve"]


def test_page_audit_adds_narrative_validity_to_legacy_quality():
    old_gate = audit_page(_page(repeated_prose=True))
    improved = audit_page(_page(repeated_prose=False))

    assert old_gate["grounding_valid"] is True
    assert old_gate["structural_valid"] is True
    assert old_gate["narrative_valid"] is False
    assert old_gate["publishable"] is False
    assert improved["publishable"] is True


def test_cache_audit_ignores_outline_records_and_summarizes_pages(tmp_path):
    pages = [_page(repeated_prose=True), _page(repeated_prose=False)]
    for index, page in enumerate(pages):
        (tmp_path / f"agentwiki_page_{index}.json").write_text(
            json.dumps({"model": "test", "data": page})
        )
    (tmp_path / "agentwiki_outline.json").write_text(
        json.dumps({"model": "test", "data": {"pages": []}})
    )
    (tmp_path / "agentwiki_broken.json").write_text("{")

    report = audit_cache(tmp_path)

    assert report == summarize_page_audits(pages)
    assert report["pages"] == 2
    assert report["publishable"] == 1
    assert report["narrative_valid"] == 1
