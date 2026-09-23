# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from codenib.wiki.story import (
    derive_story_from_markdown,
    finalize_story_ir,
    story_quality_report,
)


def test_story_ir_aligns_beats_and_derives_evidence_budget():
    plan = finalize_story_ir(
        {
            "sections": [
                {
                    "title": "Accept input",
                    "claims": [
                        {
                            "role": "entry",
                            "statement": "The entry accepts a payload",
                            "evidence": ["E1"],
                        }
                    ],
                },
                {
                    "title": "Persist result",
                    "claims": [
                        {
                            "role": "contract",
                            "statement": "The store writes the result",
                            "evidence": ["E2"],
                        }
                    ],
                },
            ],
            "story": {
                "origin": "planned",
                "reader_question": {
                    "statement": "How does the payload reach storage?",
                    "evidence": ["E1", "E2"],
                },
                "beats": [
                    {"section": "Accept input", "role": "entry"},
                    {
                        "section": "Persist result",
                        "role": "outcome",
                        "transition": {
                            "statement": "The normalized payload is then persisted",
                            "evidence": ["E1", "E2"],
                        },
                    },
                ],
            },
        },
        available_ids=["E1", "E2", "E3"],
    )

    story = plan["story"]
    assert [beat["section"] for beat in story["beats"]] == [
        "Accept input",
        "Persist result",
    ]
    assert story["beats"][1]["evidence"] == ["E2", "E1"]
    assert story["evidence_budget"] == {
        "available_source_evidence": 3,
        "allocated_source_evidence": 2,
        "allocated_relations": 0,
        "beat_coverage": 1.0,
        "max_beat_reuse": 1.0,
        "unallocated_source_evidence": ["E3"],
        "sections": [
            {"section": "Accept input", "evidence": ["E1"]},
            {"section": "Persist result", "evidence": ["E2", "E1"]},
        ],
    }
    assert story_quality_report(plan)["story_valid"] is True


def test_story_ir_marks_partially_planned_pages_as_mixed():
    plan = finalize_story_ir(
        {
            "sections": [
                {"title": "Entry", "claims": [{"evidence": ["E1"]}]},
                {"title": "Runtime", "claims": [{"evidence": ["E2"]}]},
            ],
            "story": {
                "origin": "planned",
                "beats": [{"section": "Entry", "role": "entry"}],
            },
        },
        available_ids=["E1", "E2"],
    )

    assert plan["story"]["origin"] == "mixed"
    assert [beat["role"] for beat in plan["story"]["beats"]] == [
        "entry",
        "outcome",
    ]
    report = story_quality_report(plan)
    # Missing bridges are advice for the rubric review, not a blocking defect.
    assert report["story_valid"] is True
    assert report["story_transition_coverage"] == 0.0
    assert (
        "story lacks enough grounded transitions between its beats"
        in report["story_advisories"]
    )


def test_three_beat_story_needs_one_explicit_grounded_bridge():
    plan = finalize_story_ir(
        {
            "sections": [
                {"title": "Input", "claims": [{"evidence": ["E1"]}]},
                {"title": "Mechanism", "claims": [{"evidence": ["E2"]}]},
                {"title": "Output", "claims": [{"evidence": ["E3"]}]},
            ],
            "story": {
                "origin": "planned",
                "reader_question": {
                    "statement": "How does input become output?",
                    "evidence": ["E1", "E3"],
                },
                "beats": [
                    {"section": "Input", "role": "entry"},
                    {"section": "Mechanism", "role": "mechanism"},
                    {
                        "section": "Output",
                        "role": "outcome",
                        "transition": {
                            "statement": "The mechanism hands its result to output",
                            "evidence": ["E2", "E3"],
                        },
                    },
                ],
            },
        },
        available_ids=["E1", "E2", "E3"],
    )

    report = story_quality_report(plan)

    assert report["story_valid"] is True
    assert report["story_transition_coverage"] == 0.5
    assert report["story_minimum_transitions"] == 1


def test_markdown_story_derivation_keeps_schema_without_inventing_prose():
    story = derive_story_from_markdown(
        "# Overview\n\nIntro.\n\n"
        "## Request flow\n\nDetails.\n\n"
        "## Runtime boundary\n\nDetails.\n\n"
        "## Related pages\n\n- [More](?p=more)"
    )

    assert story["origin"] == "derived"
    assert [beat["section"] for beat in story["beats"]] == [
        "Request flow",
        "Runtime boundary",
    ]
    assert "reader_question" not in story
    assert all("transition" not in beat for beat in story["beats"])
