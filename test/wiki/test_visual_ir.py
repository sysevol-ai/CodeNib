# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.wiki.visual_ir import (
    architecture_contract_from_mermaid,
    architecture_contract_from_plan,
    architecture_contract_from_relations,
    bar_chart_contract,
    flow_contract_from_plan,
    normalize_render_contract,
    page_visual_contract_report,
    storyboard_contract_from_story,
)


def _architecture_plan() -> dict:
    return {
        "components": [
            {
                "id": "entry",
                "label": "Entry surfaces",
                "responsibility": "Accept repository questions from the CLI and API.",
                "layer": "interface",
                "kind": "frontend",
                "evidence": ["E1"],
            },
            {
                "id": "planner",
                "label": "Planning core",
                "responsibility": "Turns a question into grounded retrieval work.",
                "layer": "coordination",
                "kind": "backend",
                "evidence": ["E2"],
            },
            {
                "id": "workers",
                "label": "Analysis workers",
                "responsibility": "Run retrieval and source analysis.",
                "layer": "execution",
                "kind": "backend",
                "evidence": ["E3"],
            },
            {
                "id": "artifacts",
                "label": "Evidence artifacts",
                "responsibility": "Keep the source-linked result returned to readers.",
                "layer": "data",
                "kind": "database",
                "evidence": ["E4"],
            },
            {
                "id": "config",
                "label": "Runtime policy",
                "responsibility": "Selects the execution policy used by the planner.",
                "layer": "coordination",
                "kind": "security",
                "evidence": ["E5"],
            },
        ],
        "connections": [
            {
                "source": "entry",
                "target": "planner",
                "label": "normalizes the question",
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
                "target": "artifacts",
                "label": "returns cited findings",
                "evidence": ["R3"],
            },
            {
                "source": "config",
                "target": "planner",
                "label": "constrains execution",
                "evidence": ["E5"],
            },
        ],
        "primary_path": ["entry", "planner", "workers", "artifacts"],
        "boundaries": [
            {
                "id": "execution_boundary",
                "label": "Execution boundary",
                "detail": "Policy separates planning from source execution.",
                "members": ["config", "workers"],
                "evidence": ["E3", "E5"],
            }
        ],
    }


def test_architecture_plan_models_roles_layers_and_a_primary_path():
    contract = architecture_contract_from_plan(_architecture_plan())

    assert contract is not None
    assert contract["provenance"] == "architecture-plan"
    assert contract["data"]["primary_path"] == [
        "entry",
        "planner",
        "workers",
        "artifacts",
    ]
    assert contract["data"]["nodes"][1] == {
        "id": "planner",
        "label": "Planning core",
        "detail": "Turns a question into grounded retrieval work.",
        "evidence": ["E2"],
        "kind": "backend",
        "layer": "coordination",
    }
    assert contract["data"]["boundaries"][0]["members"] == [
        "config",
        "workers",
    ]


def test_architecture_plan_rejects_call_graphs_disguised_as_architecture():
    callable_plan = _architecture_plan()
    callable_plan["components"][0]["label"] = "main()"
    assert architecture_contract_from_plan(callable_plan) is None

    call_edge_plan = _architecture_plan()
    call_edge_plan["connections"][0]["label"] = "calls"
    assert architecture_contract_from_plan(call_edge_plan) is None


def test_architecture_plan_rejects_a_copied_prompt_template():
    copied = _architecture_plan()
    for component, label in zip(
        copied["components"],
        (
            "Entry surfaces",
            "Coordination core",
            "Execution engine",
            "Result store",
            "Runtime policy",
        ),
        strict=True,
    ):
        component["label"] = label

    assert architecture_contract_from_plan(copied) is None


def test_relation_contract_keeps_real_endpoints_and_edge_evidence():
    contract = architecture_contract_from_relations(
        [
            {
                "id": "R1",
                "source": "src/api.py:dispatch()",
                "target": "src/worker.py:run()",
            },
            {
                "id": "R2",
                "source": "src/worker.py:run()",
                "target": "src/store.py:save()",
            },
        ]
    )

    assert contract is not None
    assert contract["adapter"] == "architecture"
    assert contract["provenance"] == "relations"
    assert contract["evidence"] == ["R1", "R2"]
    assert [node["label"] for node in contract["data"]["nodes"]] == [
        "dispatch()",
        "run()",
        "save()",
    ]
    assert [edge["evidence"] for edge in contract["data"]["edges"]] == [
        ["R1"],
        ["R2"],
    ]
    assert [edge["label"] for edge in contract["data"]["edges"]] == [
        "calls",
        "calls",
    ]


def test_flow_contract_accepts_one_admitted_path_and_rejects_disconnected_steps():
    contract = flow_contract_from_plan(
        {
            "steps": [
                {"from": "parse()", "to": "plan()", "evidence": ["R1"]},
                {"from": "plan()", "to": "render()", "evidence": ["R2"]},
            ]
        }
    )

    assert contract is not None
    assert contract["adapter"] == "flow"
    assert [node["label"] for node in contract["data"]["nodes"]] == [
        "parse()",
        "plan()",
        "render()",
    ]
    assert (
        flow_contract_from_plan(
            {
                "steps": [
                    {"from": "a", "to": "b", "evidence": ["R1"]},
                    {"from": "c", "to": "d", "evidence": ["R2"]},
                ]
            }
        )
        is None
    )


def test_storyboard_contract_uses_final_story_beat_order_and_wording():
    contract = storyboard_contract_from_story(
        {
            "beats": [
                {
                    "section": "Start at the API",
                    "role": "entry",
                    "evidence": ["E1"],
                },
                {
                    "section": "Follow the dispatch",
                    "role": "handoff",
                    "evidence": ["E2", "R1"],
                    "transition": {
                        "statement": "The validated request becomes queued work.",
                        "evidence": ["E2", "R1"],
                    },
                },
            ]
        }
    )

    assert contract is not None
    assert contract["adapter"] == "storyboard"
    panels = contract["data"]["panels"]
    assert [panel["title"] for panel in panels] == [
        "Start at the API",
        "Follow the dispatch",
    ]
    assert panels[1]["detail"] == "The validated request becomes queued work."
    assert panels[1]["evidence"] == ["E2", "R1"]


def test_storyboard_adapter_removes_redundant_roles_and_source_paths():
    contract = storyboard_contract_from_story(
        {
            "beats": [
                {
                    "section": "Entry: Benchmark Setup",
                    "role": "entry",
                    "evidence": ["E1"],
                },
                {
                    "section": "Outcome: Parsed Results",
                    "role": "outcome",
                    "evidence": ["E2"],
                    "transition": {
                        "statement": "`evaluation/run.py:main()` returns parsed results.",
                        "evidence": ["E2"],
                    },
                },
            ]
        }
    )

    assert contract is not None
    panels = contract["data"]["panels"]
    assert [panel["title"] for panel in panels] == [
        "Benchmark Setup",
        "Parsed Results",
    ]
    assert panels[1]["detail"] == "`main()` returns parsed results."


def test_mermaid_adapter_parses_only_the_deterministic_builder_subset():
    contract = architecture_contract_from_mermaid(
        'graph TD\n  ROOT["CodeNib"]\n  ROOT --> WIKI["wiki<br/>42 symbols"]',
        evidence=["codenib/wiki/builder.py"],
    )

    assert contract is not None
    assert contract["provenance"] == "deterministic-index"
    assert [node["label"] for node in contract["data"]["nodes"]] == [
        "CodeNib",
        "wiki · 42 symbols",
    ]
    assert contract["data"]["edges"] == [
        {"source": "ROOT", "target": "WIKI", "label": "", "evidence": []}
    ]
    assert (
        architecture_contract_from_mermaid(
            "sequenceDiagram\n  API->>DB: query",
            evidence=["src/api.py"],
        )
        is None
    )


def test_chart_contract_rejects_unproven_or_negative_metrics():
    contract = bar_chart_contract(
        [
            {"label": "Python", "value": 12, "evidence": ["E1"]},
            {"label": "Rust", "value": 4, "evidence": ["E2"]},
        ],
        evidence=["E1", "E2"],
        unit="files",
    )

    assert contract["data"]["bars"][0]["value"] == 12.0
    with pytest.raises(ValueError, match="non-negative"):
        bar_chart_contract(
            [
                {"label": "before", "value": -1, "evidence": ["E1"]},
                {"label": "after", "value": 2, "evidence": ["E2"]},
            ],
            evidence=["E1", "E2"],
        )
    with pytest.raises(ValueError, match="requires evidence"):
        bar_chart_contract(
            [
                {"label": "before", "value": 1, "evidence": []},
                {"label": "after", "value": 2, "evidence": ["E2"]},
            ],
            evidence=["E2"],
        )


def test_page_visual_audit_rejects_contract_evidence_not_exposed_by_page():
    contract = normalize_render_contract(
        {
            "schema_version": 1,
            "adapter": "flow",
            "provenance": "relations",
            "evidence": ["R9"],
            "data": {
                "nodes": [
                    {"id": "a", "label": "API", "evidence": ["R9"]},
                    {"id": "b", "label": "Worker", "evidence": ["R9"]},
                ],
                "edges": [
                    {
                        "source": "a",
                        "target": "b",
                        "evidence": ["R9"],
                    }
                ],
            },
        }
    )
    page = {
        "evidence": {"items": [], "relations": [{"id": "R1"}]},
        "media_slots": [
            {"id": "flow", "render_contract": contract, "asset": {"uri": "x.svg"}}
        ],
    }

    report = page_visual_contract_report(page)

    assert report["valid"] is False
    assert report["typed_slots"] == 1
    assert report["materialized_typed_slots"] == 0
    assert "not on the page: R9" in report["contracts"][0]["errors"][0]


def test_architecture_plan_keeps_role_names_that_merely_contain_a_slash():
    plan = _architecture_plan()
    plan["components"][0]["label"] = "I/O driver"
    plan["components"][1]["label"] = "NumPy/SciPy backend"
    assert architecture_contract_from_plan(plan) is not None

    path_plan = _architecture_plan()
    path_plan["components"][0]["label"] = "src/core/router"
    assert architecture_contract_from_plan(path_plan) is None


def test_architecture_plan_rejects_call_graph_phrasing_not_the_word_calls():
    noun_plan = _architecture_plan()
    noun_plan["connections"][0]["label"] = "file operations and system calls"
    assert architecture_contract_from_plan(noun_plan) is not None

    verb_plan = _architecture_plan()
    verb_plan["connections"][0]["label"] = "calls the planner"
    assert architecture_contract_from_plan(verb_plan) is None


def test_architecture_plan_maps_a_kind_written_as_a_layer():
    plan = _architecture_plan()
    plan["components"][0]["layer"] = "frontend"
    contract = architecture_contract_from_plan(plan)
    assert contract is not None
    entry = next(n for n in contract["data"]["nodes"] if n["id"] == "entry")
    assert entry["layer"] == "interface"


def test_architecture_plan_trims_a_primary_path_step_without_a_connection():
    plan = _architecture_plan()
    ids = [c["id"] for c in plan["components"]]
    edges = {(e["source"], e["target"]) for e in plan["connections"]}
    # Append a component the path names but no connection reaches.
    plan["components"].append(
        {
            "id": "orphan",
            "label": "Detached role",
            "responsibility": "Named on the path without an authored edge.",
            "layer": "execution",
            "kind": "backend",
            "evidence": ["E3"],
        }
    )
    original = list(plan["primary_path"])
    plan["primary_path"] = [*original[:-1], "orphan", original[-1]]
    contract = architecture_contract_from_plan(plan)
    assert contract is not None, (ids, edges, plan["primary_path"])
    assert contract["data"]["primary_path"] == original
