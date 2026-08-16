# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codenib.wiki.evidence import RelationItem, evidence_metadata
from codenib.wiki.multimodal import plan_media_slots


def test_media_slots_plan_diagram_and_image_from_page_evidence():
    slots = plan_media_slots(
        page_id="overview",
        title="Overview",
        citations=[
            {"file": "codenib/wiki/builder.py"},
            {"file": "codenib/web/app.py"},
            {"file": "codenib/wiki/builder.py"},
        ],
        diagram="graph TD\n  A --> B",
    )

    assert [slot["kind"] for slot in slots] == ["diagram", "image"]
    assert slots[0]["id"] == "overview-structure-diagram"
    assert slots[0]["placement"] == "lead"
    assert slots[0]["source_citations"] == [
        "codenib/wiki/builder.py",
        "codenib/web/app.py",
    ]
    assert slots[0]["human_prior"] == {"editable": True, "notes": []}


def test_media_slots_plan_storyboard_when_relations_are_available():
    slots = plan_media_slots(
        page_id="runtime",
        title="Runtime",
        citations=[{"file": "src/runtime.py"}],
        relations=[{"source": "A", "target": "B"}],
    )

    assert [slot["kind"] for slot in slots] == ["image", "storyboard"]
    assert slots[1]["id"] == "runtime-flow-storyboard"
    assert "components hand work" in slots[1]["purpose"]


def test_media_planning_serializes_slotted_relation_evidence():
    relation = RelationItem(
        id="R1",
        source="src/runtime.py:dispatch()",
        target="src/worker.py:run()",
        anchors=("src/runtime.py:12",),
    )
    metadata = evidence_metadata([], [relation])

    slots = plan_media_slots(
        page_id="runtime",
        title="Runtime",
        relations=metadata["relations"],
    )

    assert metadata["relations"] == [
        {
            "id": "R1",
            "source": "src/runtime.py:dispatch()",
            "target": "src/worker.py:run()",
            "anchors": ("src/runtime.py:12",),
        }
    ]
    assert [slot["kind"] for slot in slots] == ["storyboard"]


def test_media_slot_planning_only_probes_one_relation_and_skips_bad_citations():
    def relations():
        yield {"source": "A", "target": "B"}
        raise AssertionError("relation planning consumed more than one item")

    slots = plan_media_slots(
        page_id="runtime",
        title="Runtime",
        citations=[None, {"file": "x" * 5000}, {"file": "src/runtime.py"}],
        relations=relations(),
    )

    assert [slot["kind"] for slot in slots] == ["image", "storyboard"]
    assert slots[0]["source_citations"] == ["src/runtime.py"]
