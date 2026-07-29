# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

from codenib.wiki.evidence import (
    EvidenceItem,
    RelationItem,
    candidate_key,
    diversify_by_file,
    evidence_matches_claim,
    grounding_report,
    is_interaction_claim,
    parse_fact_plan,
    reciprocal_rank_fuse,
    relation_endpoints_named,
    relation_matches_claim,
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


def test_interaction_claim_recognizes_an_explicit_utilizes_handoff():
    assert is_interaction_claim(
        "`build_hierarchy` utilizes `CodeGraph.get_graph` to read graph state"
    )
    assert is_interaction_claim(
        "`WikiPage` receives indexed records from `IndexBuilderRegistry`"
    )


def test_relation_claim_must_name_the_cited_source_and_target():
    relation = RelationItem(
        id="R1",
        source="codenib/cli.py:index_repository()",
        target=("codenib/compiler/index_compiler.py:" "IndexCompiler.compile_repo()"),
    )

    assert relation_matches_claim(
        "`index_repository` calls `IndexCompiler.compile_repo` to build views",
        relation,
    )
    reversed_claim = (
        "`IndexCompiler.compile_repo` calls `index_repository` to build views"
    )
    assert relation_endpoints_named(reversed_claim, relation)
    assert not relation_matches_claim(reversed_claim, relation)
    assert not relation_matches_claim(
        "`wiki_page_graph` calls `compile_repo` to render a page",
        relation,
    )


def test_source_body_can_support_a_flow_missing_from_static_relations():
    evidence = EvidenceItem(
        id="E1",
        file="src/wiki.py",
        start_line=1,
        end_line=3,
        symbol="wiki_page_graph",
        kind="function",
        content="def wiki_page_graph():\n    return _bundle().code_graph()",
    )

    assert evidence_matches_claim(
        "`wiki_page_graph` calls `_bundle` to access the code graph",
        evidence,
    )
    assert not evidence_matches_claim(
        "`wiki_page_graph` calls `compile_repo` to access the code graph",
        evidence,
    )


def test_source_path_prefix_does_not_prove_a_flow_endpoint():
    evidence = EvidenceItem(
        id="E1",
        file="codenib/compiler/index_builders.py",
        start_line=1,
        end_line=3,
        symbol="register_default_builders",
        kind="function",
        content=(
            "def register_default_builders(registry):\n"
            "    registry.register(GraphBuilder())"
        ),
    )

    assert not evidence_matches_claim(
        "`codenib` invokes `register_default_builders`",
        evidence,
    )


def test_source_body_matches_a_class_qualified_self_call():
    evidence = EvidenceItem(
        id="E1",
        file="src/store.py",
        start_line=1,
        end_line=3,
        symbol="CodeStore.rebuild",
        kind="method",
        content="def rebuild(self):\n    self.clear()",
    )

    assert evidence_matches_claim(
        "`CodeStore.rebuild` calls `CodeStore.clear` before rebuilding",
        evidence,
    )


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
        {"thesis":{"statement":"Requests pass through Router","evidence":["E1"]},
         "sections":[{"title":"Flow","claims":[
          {"role":"flow","statement":"`Router` calls `validate`",
           "evidence":["E1","E99"]}
        ]}]}
        """,
        {"E1"},
    )

    assert plan["thesis"] == {
        "statement": "Requests pass through Router",
        "evidence": ["E1"],
    }
    assert plan["sections"][0]["claims"][0]["role"] == "flow"
    assert plan["sections"][0]["claims"][0]["evidence"] == ["E1"]
    assert errors == ["claim references unknown evidence: E99"]


def test_fact_plan_normalizes_legacy_claim_roles_conservatively():
    plan, errors = parse_fact_plan(
        """
        {"thesis":"legacy thesis","sections":[{"title":"Runtime","claims":[
          {"statement":"Users send a request","evidence":["E1"]},
          {"statement":"`Router` calls `Handler`","evidence":["E1","R1"]},
          {"role":"invented","statement":"The cache stores results",
           "evidence":["E2"]}
        ]}]}
        """,
        {"E1", "E2", "R1"},
    )

    assert errors == []
    assert plan["thesis"] == {"statement": "legacy thesis", "evidence": []}
    assert [claim["role"] for claim in plan["sections"][0]["claims"]] == [
        "entry",
        "flow",
        "responsibility",
    ]


def test_fact_plan_reclassifies_interactions_declared_as_components():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"Requests pass through a router","evidence":["E1"]},
         "sections":[{"title":"Flow","claims":[
          {"role":"component","statement":"`Router` calls `Handler`",
           "evidence":["E1"]}
         ]}]}
        """,
        {"E1"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] == "flow"


def test_fact_plan_normalizes_a_single_endpoint_flow_label():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"The frontend routes pages","evidence":["E1"]},
         "sections":[{"title":"Routing","claims":[
          {"role":"flow",
           "statement":"The frontend uses `currentLocation` to select a page",
           "evidence":["E1"]}
        ]}]}
        """,
        {"E1"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] != "flow"


def test_fact_plan_keeps_single_component_retrieval_as_a_responsibility():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"The Wiki serves pages","evidence":["E1"]},
         "sections":[{"title":"Serving","claims":[
          {"role":"responsibility",
           "statement":"`AgentWiki.page` retrieves pages from the local Wiki",
           "evidence":["E1"]}
        ]}]}
        """,
        {"E1"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] == "responsibility"


def test_fact_plan_keeps_public_invocation_as_an_entry():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"The runtime answers queries","evidence":["E1"]},
         "sections":[{"title":"Entry","claims":[
          {"role":"entry",
           "statement":"Users initiate the runtime by calling `AgentRunner.run`",
           "evidence":["E1"]}
        ]}]}
        """,
        {"E1"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] == "entry"


def test_fact_plan_reclassifies_unquoted_cross_component_handoff_as_flow():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"The Wiki serves indexed source","evidence":["E1"]},
         "sections":[{"title":"Runtime","claims":[
          {"role":"component",
           "statement":"The Wiki subsystem receives indexed data from the compiler",
           "evidence":["E1","E2"]}
        ]}]}
        """,
        {"E1", "E2"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] == "flow"


def test_fact_plan_reclassifies_runtime_interaction_with_wiki_as_flow():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"The runtime answers queries","evidence":["E1"]},
         "sections":[{"title":"Runtime","claims":[
          {"role":"responsibility",
           "statement":"The agent runtime interacts with the Wiki",
           "evidence":["E1","E2"]}
        ]}]}
        """,
        {"E1", "E2"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] == "flow"


def test_fact_plan_reclassifies_unquoted_cross_component_data_use_as_flow():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"The runtime answers queries","evidence":["E1"]},
         "sections":[{"title":"Runtime","claims":[
          {"role":"entry",
           "statement":"`AgentRunner.run` utilizes data from the Wiki subsystem",
           "evidence":["E1","E2"]}
        ]}]}
        """,
        {"E1", "E2"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] == "flow"


def test_fact_plan_reclassifies_query_through_component_as_flow():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"The runtime answers queries","evidence":["E1"]},
         "sections":[{"title":"Runtime","claims":[
          {"role":"component",
           "statement":"`AgentRunner.run` queries graph data through `CodeGraph.query_range`",
           "evidence":["E1","E2"]}
        ]}]}
        """,
        {"E1", "E2"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] == "flow"


def test_fact_plan_normalizes_mislabeled_component_responsibility():
    plan, errors = parse_fact_plan(
        """
        {"thesis":{"statement":"The graph serves navigation","evidence":["E1"]},
         "sections":[{"title":"Navigation","claims":[
          {"role":"entry",
           "statement":"`CodeGraph.query_range` queries source ranges",
           "evidence":["E1"]}
        ]}]}
        """,
        {"E1"},
    )

    assert errors == []
    assert plan["sections"][0]["claims"][0]["role"] == "responsibility"


def test_interaction_claim_requires_named_endpoints_and_a_handoff():
    assert is_interaction_claim("`Router` dispatches requests to `Handler`") is True
    assert (
        is_interaction_claim(
            "`AgentWiki.source` retrieves content by calling `WikiBuilder.source`"
        )
        is True
    )
    assert (
        is_interaction_claim(
            "`AgentRunner.run` executes requests and interacts with `Trace.add`"
        )
        is True
    )
    assert is_interaction_claim("`Router` dispatches requests") is False
    assert is_interaction_claim("`Router` and `Handler` process requests") is False


def test_return_contract_is_not_confused_with_a_handoff_recipient():
    assert not is_interaction_claim(
        "`query_range` returns a `RangeQueryResult` relevant to the source range"
    )
    assert is_interaction_claim(
        "`compile_repo` returns a `RepoManifest` to `index_repository`"
    )


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


def test_grounding_report_flags_unmeasured_clarity_and_speed_claims():
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
        "The `Router` ensures accurate and fast dispatch, improving clarity and "
        "helping developers identify issues in this vital path. [E1]",
        evidence,
        [],
    )

    assert set(report["promotional_phrases"]) >= {
        "accurate",
        "ensures",
        "fast",
        "helping developers",
        "improving",
        "vital",
    }


def test_promotional_sentence_removal_preserves_support_marker():
    markdown = (
        "The `Router` dispatches requests from `src/core.py`. "
        "This powerful feature allows developers to work quickly. [E1]"
    )

    cleaned = remove_promotional_sentences(markdown)

    assert cleaned == "The `Router` dispatches requests from `src/core.py`. [E1]"


def test_fully_attributed_table_counts_as_cited():
    """A table cites per row; its last cell closes with a pipe, not a citation."""

    from codenib.wiki.evidence import _block_is_cited

    table = (
        "| Capability | Implemented by | Source |\n"
        "|---|---|---|\n"
        "| Send a request | `Session.send()` | [E1] |\n"
        "| Open a connection | `HTTPAdapter.get_connection()` | [E2] |"
    )
    assert _block_is_cited(table)


def test_table_with_an_unattributed_row_is_not_cited():
    from codenib.wiki.evidence import _block_is_cited

    table = (
        "| Capability | Implemented by | Source |\n"
        "|---|---|---|\n"
        "| Send a request | `Session.send()` | [E1] |\n"
        "| Open a connection | `HTTPAdapter.get_connection()` |  |"
    )
    assert not _block_is_cited(table)


def test_prose_still_needs_a_trailing_citation_run():
    from codenib.wiki.evidence import _block_is_cited

    assert _block_is_cited("`Session.send()` dispatches the request. [E1]")
    assert not _block_is_cited("`Session.send()` dispatches the request.")


def test_promotional_words_quoted_from_source_are_not_the_page_s_own():
    """A project's own tagline is reported with a citation, not asserted."""

    from codenib.wiki.evidence import EvidenceItem, grounding_report

    readme = EvidenceItem(
        id="E1",
        file="README.md",
        start_line=1,
        end_line=3,
        symbol="README.md",
        kind="doc",
        content="A fast and flexible static site generator written in Go.",
    )
    quoted = "A fast and flexible static site generator written in Go. [E1]"
    assert grounding_report(quoted, [readme], [])["promotional_phrases"] == []


def test_promotional_words_the_page_wrote_itself_still_flag():
    from codenib.wiki.evidence import EvidenceItem, grounding_report

    src = EvidenceItem(
        id="E1",
        file="site.go",
        start_line=1,
        end_line=3,
        symbol="Build",
        kind="function",
        content="func Build() error { return nil }",
    )
    authored = "`Build()` is a fast and flexible entry point. [E1]"
    assert grounding_report(authored, [src], [])["promotional_phrases"]


def _doc(content, file="src/models.py", symbol="PreparedRequest.prepare_url"):
    from codenib.wiki.evidence import EvidenceItem

    return EvidenceItem(
        id="E1",
        file=file,
        start_line=1,
        end_line=9,
        symbol=symbol,
        kind="method",
        content=content,
    )


def test_qualified_display_name_resolves_from_both_halves():
    from codenib.wiki.evidence import grounding_report

    item = _doc(
        "struct AnyNodeRef;", file="crates/ast/src/generated.rs", symbol="AnyNodeRef"
    )
    md = "The `crates/ast/src/generated.rs:AnyNodeRef` enum wraps every node. [E1]"
    assert grounding_report(md, [item], [])["unsupported_identifiers"] == []


def test_attribute_access_resolves_from_owner_and_leaf():
    from codenib.wiki.evidence import grounding_report

    item = _doc(
        "class PreparedRequest:\n    def prepare_url(self):\n        self.url = None"
    )
    md = "`PreparedRequest.url` holds the encoded target after preparation. [E1]"
    assert grounding_report(md, [item], [])["unsupported_identifiers"] == []


def test_uri_scheme_is_not_an_identifier():
    from codenib.wiki.evidence import grounding_report

    item = _doc("def prepare_url(self):\n    return None")
    md = "`prepare_url` rejects a `mailto:` target before encoding the host. [E1]"
    assert grounding_report(md, [item], [])["unsupported_identifiers"] == []


def test_an_invented_symbol_still_flags():
    from codenib.wiki.evidence import grounding_report

    item = _doc("def prepare_url(self):\n    return None")
    md = "`PreparedRequest.teleport_payload()` moves the body offsite. [E1]"
    assert grounding_report(md, [item], [])["unsupported_identifiers"]
