# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codenib.wiki.media_evidence import build_media_evidence_pack


def test_build_media_evidence_pack_filters_to_slot_citations():
    slot = {
        "id": "overview-flow-storyboard",
        "kind": "storyboard",
        "title": "Overview flow",
        "purpose": "Explain how wiki media is generated.",
        "source_citations": ["codenib/wiki/media_generation.py"],
        "human_prior": {"style": "teaching-blog", "avoid": ["decorative images"]},
    }
    pack = build_media_evidence_pack(
        slot,
        page_id="overview",
        page_title="Overview",
        page_markdown="# Overview\n\nMedia slots become assets.",
        citations=[
            {
                "file": "codenib/wiki/media_generation.py",
                "symbol": "materialize_media_slots",
                "start_line": 281,
                "end_line": 312,
                "snippet": "def materialize_media_slots(...):",
            },
            {
                "file": "unrelated.py",
                "symbol": "ignored",
                "snippet": "not relevant",
            },
        ],
        relations=[
            {
                "source": "plan_media_slots",
                "target": "materialize_media_slots",
                "relation": "feeds",
            }
        ],
    )

    assert pack["slot_id"] == "overview-flow-storyboard"
    assert pack["page"] == {
        "id": "overview",
        "title": "Overview",
        "summary": "Media slots become assets.",
    }
    assert pack["sources"] == [
        {
            "file": "codenib/wiki/media_generation.py",
            "symbol": "materialize_media_slots",
            "start_line": 281,
            "end_line": 312,
            "snippet": "def materialize_media_slots(...):",
        }
    ]
    assert pack["relations"] == [
        {
            "source": "plan_media_slots",
            "target": "materialize_media_slots",
            "relation": "feeds",
        }
    ]
    assert pack["human_prior"]["style"] == "teaching-blog"
    assert "decorative" in pack["output_requirements"][1]


def test_build_media_evidence_pack_reads_bounded_snippets():
    long_source = "x" * 200

    def source_reader(_citation):
        return long_source

    pack = build_media_evidence_pack(
        {"id": "slot", "kind": "image", "source_citations": ["src/app.py"]},
        citations=[{"file": "src/app.py", "line": 7}],
        source_reader=source_reader,
        max_snippet_bytes=12,
    )

    assert pack["sources"][0]["snippet"] == "xxxxxxxxxxx…"
    assert pack["sources"][0]["start_line"] == 7


def test_build_media_evidence_pack_deduplicates_relations_and_sources():
    slot = {"id": "slot", "kind": "diagram", "source_citations": ["src/app.py"]}
    pack = build_media_evidence_pack(
        slot,
        citations=[
            {"file": "src/app.py", "symbol": "app", "start_line": 1},
            {"file": "src/app.py", "symbol": "app", "start_line": 1},
        ],
        relations=[
            {"from": "app", "to": "view", "kind": "calls"},
            {"from": "app", "to": "view", "kind": "calls"},
        ],
    )

    assert len(pack["sources"]) == 1
    assert pack["relations"] == [
        {"source": "app", "target": "view", "relation": "calls"}
    ]
