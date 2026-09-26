# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from codenib.wiki.multimodal import plan_media_slots


def _architecture_plan() -> dict:
    return {
        "title": "How a question becomes grounded evidence",
        "components": [
            {
                "id": "reader",
                "label": "Reader surfaces",
                "responsibility": "Accept repository questions from the CLI and API.",
                "layer": "interface",
                "kind": "frontend",
                "evidence": ["E1"],
            },
            {
                "id": "planner",
                "label": "Analysis coordinator",
                "responsibility": "Turns a question into grounded retrieval work.",
                "layer": "coordination",
                "kind": "backend",
                "evidence": ["E2"],
            },
            {
                "id": "workers",
                "label": "Source analysis",
                "responsibility": "Retrieves and inspects repository evidence.",
                "layer": "execution",
                "kind": "backend",
                "evidence": ["E3"],
            },
            {
                "id": "answer",
                "label": "Grounded answer",
                "responsibility": "Returns findings with source locations.",
                "layer": "data",
                "kind": "database",
                "evidence": ["E4"],
            },
            {
                "id": "policy",
                "label": "Runtime policy",
                "responsibility": "Constrains how analysis work is executed.",
                "layer": "coordination",
                "kind": "security",
                "evidence": ["E5"],
            },
        ],
        "connections": [
            {
                "source": "reader",
                "target": "planner",
                "label": "frames the question",
                "evidence": ["R1"],
            },
            {
                "source": "planner",
                "target": "workers",
                "label": "dispatches grounded work",
                "evidence": ["R2"],
            },
            {
                "source": "workers",
                "target": "answer",
                "label": "returns cited findings",
                "evidence": ["R3"],
            },
            {
                "source": "policy",
                "target": "planner",
                "label": "sets execution constraints",
                "evidence": ["E5"],
            },
        ],
        "primary_path": ["reader", "planner", "workers", "answer"],
        "boundaries": [
            {
                "id": "execution",
                "label": "Execution boundary",
                "detail": "Coordination remains separate from source execution.",
                "members": ["planner", "workers", "policy"],
                "evidence": ["E2", "E3", "E5"],
            }
        ],
    }


def test_overview_plans_one_semantic_architecture_visual():
    slots = plan_media_slots(
        page_id="overview",
        title="Overview",
        citations=[
            {"file": "src/api.py"},
            {"file": "src/runtime.py"},
            {"file": "src/api.py"},
        ],
        architecture=_architecture_plan(),
    )

    assert len(slots) == 1
    assert slots[0]["id"] == "overview-system-architecture"
    assert slots[0]["kind"] == "diagram"
    assert slots[0]["placement"] == "aside"
    assert slots[0]["title"] == "How a question becomes grounded evidence"
    assert slots[0]["source_citations"] == ["src/api.py", "src/runtime.py"]
    assert slots[0]["human_prior"] == {"editable": True, "notes": []}
    contract = slots[0]["render_contract"]
    assert contract["provenance"] == "architecture-plan"
    assert contract["data"]["primary_path"] == [
        "reader",
        "planner",
        "workers",
        "answer",
    ]
    assert contract["data"]["boundaries"][0]["label"] == "Execution boundary"


def test_call_graph_inputs_never_become_the_architecture_card():
    # A mermaid block or story beats never become media at all; raw relations
    # only ever become the labelled relation-map fallback, never the
    # system-architecture card.
    slots = plan_media_slots(
        page_id="overview",
        title="Overview",
        diagram="graph TD\n  A --> B",
        story={
            "beats": [
                {"section": "Enter", "role": "entry", "evidence": ["E1"]},
                {"section": "Return", "role": "outcome", "evidence": ["E2"]},
            ]
        },
    )
    assert slots == []

    slots = plan_media_slots(
        page_id="overview",
        title="Overview",
        relations=[
            {
                "id": "R1",
                "source": "src/api.py:dispatch()",
                "target": "src/worker.py:run()",
            }
        ],
    )
    assert [slot["id"] for slot in slots] == ["overview-relation-map"]


def test_recorded_entry_path_is_the_fallback_when_no_architecture_is_admitted():
    flow = {
        "title": "From `dispatch()` to `save()`",
        "steps": [
            {"from": "`dispatch()`", "to": "`run()`", "label": "", "evidence": ["R1"]},
            {"from": "`run()`", "to": "`save()`", "label": "", "evidence": ["R2"]},
        ],
    }
    slots = plan_media_slots(page_id="overview", title="Overview", flow=flow)
    assert [slot["id"] for slot in slots] == ["overview-entry-path"]
    assert slots[0]["render_contract"]["adapter"] == "flow"
    assert slots[0]["title"] == "From `dispatch()` to `save()`"
    assert [node["label"] for node in slots[0]["render_contract"]["data"]["nodes"]] == [
        "dispatch()",
        "run()",
        "save()",
    ]

    # With an admitted architecture the recorded path never becomes a second
    # picture.
    slots = plan_media_slots(
        page_id="overview",
        title="Overview",
        architecture=_architecture_plan(),
        flow=flow,
    )
    assert [slot["id"] for slot in slots] == ["overview-system-architecture"]


def test_child_pages_never_receive_an_automatic_architecture_visual():
    slots = plan_media_slots(
        page_id="runtime",
        title="Runtime",
        architecture=_architecture_plan(),
    )

    assert slots == []


def test_generic_architecture_title_is_replaced_by_its_primary_transformation():
    architecture = _architecture_plan()
    architecture["title"] = "How the system is organized"

    slots = plan_media_slots(
        page_id="overview",
        title="Overview",
        architecture=architecture,
    )

    assert slots[0]["title"] == "Reader surfaces → Grounded answer"


def test_invalid_architecture_plan_is_omitted_instead_of_falling_back_to_calls():
    architecture = _architecture_plan()
    architecture["components"][0]["label"] = "dispatch()"

    slots = plan_media_slots(
        page_id="overview",
        title="Overview",
        architecture=architecture,
        relations=[{"id": "R1", "source": "dispatch()", "target": "run()"}],
    )

    # The invalid plan is dropped, never repaired from calls; the page falls
    # back to the labelled relation map instead of an architecture card.
    assert [slot["id"] for slot in slots] == ["overview-relation-map"]
    assert all(slot["id"] != "overview-system-architecture" for slot in slots)


def test_recorded_relations_are_the_last_fallback_visual():
    relations = [
        {
            "id": "R1",
            "source": "src/api.py:dispatch()",
            "target": "src/worker.py:run()",
        },
        {"id": "R2", "source": "src/worker.py:run()", "target": "src/store.py:save()"},
    ]
    slots = plan_media_slots(page_id="overview", title="Overview", relations=relations)
    assert [slot["id"] for slot in slots] == ["overview-relation-map"]
    assert slots[0]["render_contract"]["adapter"] == "architecture"
    assert slots[0]["render_contract"]["evidence"] == ["R1", "R2"]
    # The recorded entry path outranks the relation map.
    flow = {
        "steps": [
            {"from": "`dispatch()`", "to": "`run()`", "label": "", "evidence": ["R1"]},
            {"from": "`run()`", "to": "`save()`", "label": "", "evidence": ["R2"]},
        ]
    }
    slots = plan_media_slots(
        page_id="overview", title="Overview", relations=relations, flow=flow
    )
    assert [slot["id"] for slot in slots] == ["overview-entry-path"]
