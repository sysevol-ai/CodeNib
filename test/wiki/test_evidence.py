# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

from codenib.wiki.evidence import (
    EvidenceItem,
    candidate_key,
    diversify_by_file,
    grounding_report,
    parse_fact_plan,
    reciprocal_rank_fuse,
    remove_promotional_sentences,
)


def _get(node, key, default=None):
    return node.get(key, default)


def test_reciprocal_rank_fusion_tracks_routes_and_deduplicates():
    shared = {"file": "src/core.py", "start_line": 1, "node_name": "run"}
    dense_only = {"file": "src/model.py", "start_line": 2, "node_name": "Model"}
    lexical_only = {"file": "src/cli.py", "start_line": 3, "node_name": "main"}

    fused = reciprocal_rank_fuse(
        [
            ("dense", [shared, dense_only]),
            ("bm25", [shared, lexical_only]),
        ],
        key=lambda node: candidate_key(node, _get),
    )

    assert fused[0][0] is shared
    assert fused[0][1] == ("dense", "bm25")
    assert {item[0]["node_name"] for item in fused} == {"run", "Model", "main"}


def test_diversification_bounds_candidates_from_one_file():
    ranked = [
        (SimpleNamespace(file="a.py", name=f"a{i}"), ("dense",), 1.0 / (i + 1))
        for i in range(4)
    ]
    ranked.append((SimpleNamespace(file="b.py", name="b"), ("bm25",), 0.1))

    selected = diversify_by_file(
        ranked,
        file_of=lambda node: node.file,
        limit=3,
        max_per_file=2,
    )

    assert [item[0].file for item in selected] == ["a.py", "a.py", "b.py"]


def test_fact_plan_drops_unknown_evidence_ids():
    plan, errors = parse_fact_plan(
        """
        {"thesis":"routing","sections":[{"title":"Flow","claims":[
          {"statement":"Router calls validate","evidence":["E1","E99"]}
        ]}]}
        """,
        {"E1"},
    )

    assert plan["sections"][0]["claims"][0]["evidence"] == ["E1"]
    assert errors == ["claim references unknown evidence: E99"]


def test_grounding_report_accepts_cited_source_backed_markdown():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/core.py",
            start_line=1,
            end_line=8,
            symbol="Router",
            kind="class",
            content="class Router:\n    def dispatch(self):\n        return validate()",
        )
    ]
    markdown = (
        "The `Router` owns request dispatch and invokes validation before "
        "returning the result. [E1]\n\n"
        "## Source location\n\n"
        "Its implementation is defined in `src/core.py` and provides the "
        "entry point represented by this page. [E1]"
    )

    report = grounding_report(markdown, evidence, [])

    assert report["valid"] is True
    assert report["citation_coverage"] == 1.0
    assert report["unsupported_identifiers"] == []


def test_grounding_report_requires_citation_at_paragraph_boundary():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/core.py",
            start_line=1,
            end_line=8,
            symbol="Router",
            kind="class",
            content="class Router: pass",
        )
    ]

    report = grounding_report(
        "The `Router` is defined in `src/core.py`. [E1] "
        "It also controls an unrelated deployment workflow.",
        evidence,
        [],
    )

    assert report["valid"] is False
    assert report["citation_coverage"] == 0.0


def test_grounding_report_rejects_unknown_names_and_citations():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/core.py",
            start_line=1,
            end_line=8,
            symbol="Router",
            kind="class",
            content="class Router: pass",
        )
    ]

    report = grounding_report(
        "The invented `MagicRouter` is implemented in `src/magic.py` and "
        "controls every request in the repository. [E9]",
        evidence,
        [],
    )

    assert report["valid"] is False
    assert report["unknown_citations"] == ["E9"]
    assert "MagicRouter" in report["unsupported_identifiers"]
    assert report["unknown_files"] == ["src/magic.py"]


def test_grounding_report_accepts_path_like_terms_present_in_evidence():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=8,
            symbol="README.md",
            kind="file",
            content="The frontend requires Node.js when developing from source.",
        )
    ]

    report = grounding_report(
        "Source development uses Node.js for the frontend toolchain. [E1]",
        evidence,
        [],
    )

    assert report["valid"] is True
    assert report["unknown_files"] == []


def test_grounding_report_accepts_call_without_source_type_annotations():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/server.py",
            start_line=1,
            end_line=8,
            symbol="src/server.py:wiki_page()",
            kind="function",
            content=(
                "async def wiki_page(repo_id: str, page_id: str) -> dict:\n"
                "    return await load_page(repo_id, page_id)"
            ),
        )
    ]

    report = grounding_report(
        "The server dispatches `wiki_page(repo_id, page_id)` for a page request. [E1]",
        evidence,
        [],
    )

    assert report["valid"] is True
    assert report["unsupported_identifiers"] == []


def test_grounding_report_reports_promotional_prose_without_conflating_sources():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/core.py",
            start_line=1,
            end_line=8,
            symbol="Router",
            kind="class",
            content="class Router: pass",
        )
    ]

    report = grounding_report(
        "The powerful `Router` significantly enhances productivity for "
        "developers while optimizing resource use, allowing for rapid "
        "dispatch. [E1]",
        evidence,
        [],
    )

    assert report["valid"] is True
    assert report["promotional_phrases"] == [
        "allowing for",
        "enhances productivity",
        "optimizing",
        "powerful",
        "significantly",
    ]


def test_grounding_report_finds_generic_benefit_synonyms():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/core.py",
            start_line=1,
            end_line=8,
            symbol="Router",
            kind="class",
            content="class Router: pass",
        )
    ]

    report = grounding_report(
        "The responsive and adaptable `Router` provides easy access while "
        "ensuring that all relevant state is retained. [E1]",
        evidence,
        [],
    )

    assert report["valid"] is True
    assert report["promotional_phrases"] == [
        "adaptable",
        "ensuring that all relevant",
        "provides easy",
        "responsive",
    ]


def test_promotional_sentence_removal_preserves_support_marker():
    markdown = (
        "The `Router` dispatches requests from `src/core.py`. "
        "This powerful feature allows developers to work quickly. [E1]"
    )

    cleaned = remove_promotional_sentences(markdown)

    assert cleaned == "The `Router` dispatches requests from `src/core.py`. [E1]"
