# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

from codenib.wiki.evidence import EvidenceItem, RelationItem
from codenib.wiki.quality import (
    audit_cache,
    audit_page,
    audit_wiki,
    duplicate_prose_blocks,
    narrative_density_report,
    page_quality_report,
    plan_narrative_report,
    prose_integrity_report,
    section_evidence_report,
    section_narrative_report,
    section_sentence_redundancy_report,
    section_synthesis_report,
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


def test_duplicate_blocks_preserve_parallel_named_scripts():
    markdown = (
        "## SGL\n\n"
        "The `bash/run_sgl_fe_bc_eval.sh` script generates task IDs using "
        "`generate_task_strings.py` and executes the SGL test using "
        "`sgl_test_bigcodebench.py`.\n\n"
        "## LangChain\n\n"
        "The `bash/run_lc_bc_eval.sh` script generates task IDs using "
        "`generate_task_strings.py` and executes the LangChain test using "
        "`lc_test_bigcodebench.py`."
    )
    repeated = markdown + (
        "\n\n## SGL again\n\n"
        "The `bash/run_sgl_fe_bc_eval.sh` script generates task IDs with "
        "`generate_task_strings.py` and executes the SGL test with "
        "`sgl_test_bigcodebench.py`."
    )

    assert duplicate_prose_blocks(markdown) == []
    assert duplicate_prose_blocks(repeated) == [[1, 3]]


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


def test_framing_sections_do_not_consume_detail_evidence_novelty():
    markdown = (
        "# Overview\n\n"
        "The repository accepts source checkouts. [E1]\n\n"
        "## Purpose and scope\n\n"
        "The compiler creates the repository index. [E2]\n\n"
        "## At a glance\n\n"
        "The runtime loads the index for queries. [E3]\n\n"
        "## Compilation details\n\n"
        "Compilation writes the index consumed by the runtime. [E2] [E3]"
    )

    report = section_evidence_report(markdown)

    assert report["evidence_by_section"]["Purpose and scope"] == ["E2"]
    assert report["evidence_by_section"]["At a glance"] == ["E3"]
    assert report["new_evidence_by_section"]["Compilation details"] == ["E2", "E3"]
    assert report["sections_without_new_evidence"] == []


def test_internal_related_pages_do_not_count_as_repeated_prose():
    markdown = (
        "# Request Preparation\n\n"
        "The request pipeline prepares URLs, bodies, headers, and connection "
        "pool attributes. [E1]\n\n"
        "## Boundaries with Other Preparation Stages\n\n"
        "URL preparation, body encoding, header preparation, and connection "
        "pool selection are separate lifecycle stages. [E1]\n\n"
        "## Related pages\n\n"
        "- [URL and Parameter Encoding](?p=url-and-parameter-encoding)\n"
        "- [Body and File Encoding](?p=body-and-file-encoding)\n"
        "- [Connection Pool Management](?p=connection-pool-management)"
    )

    narrative = section_narrative_report(markdown)

    assert duplicate_prose_blocks(markdown) == []
    assert narrative["redundant_sections"] == []
    assert "Related pages" not in narrative["section_similarity"]


def test_internal_related_pages_do_not_dilute_section_synthesis_gate():
    technical = (
        "## Parse catalog\n\n"
        "`Alpha.parse()` returns parsed values. "
        "`Alpha.load()` loads input values.\n\n"
        "## Render catalog\n\n"
        "`Beta.render()` generates rendered output. "
        "`Beta.save()` saves rendered output.\n\n"
        "## Lifecycle\n\n"
        "The runtime coordinates parsing before delivery. "
        "It records state for later consumers."
    )
    with_navigation = (
        technical + "\n\n## Related pages\n\n"
        "- [Parser internals](?p=parser-internals)\n"
        "- [Rendering internals](?p=rendering-internals)"
    )

    assert section_synthesis_report(technical)["section_synthesis_valid"] is False
    assert section_synthesis_report(with_navigation)["section_synthesis_valid"] is False


def test_navigation_before_real_prose_preserves_the_section_heading():
    markdown = (
        "# Overview\n\n"
        "The repository prepares source context for requests. [E1]\n\n"
        "## Navigation and behavior\n\n"
        "- [Request pipeline](?p=request-pipeline)\n\n"
        "The runtime then dispatches the prepared request to an adapter. [E2]"
    )

    report = section_narrative_report(markdown)

    assert "Navigation and behavior" in report["section_similarity"]


def test_page_audit_adds_narrative_validity_to_legacy_quality():
    old_gate = audit_page(_page(repeated_prose=True))
    improved = audit_page(_page(repeated_prose=False))

    assert old_gate["grounding_valid"] is True
    assert old_gate["structural_valid"] is True
    assert old_gate["narrative_valid"] is False
    assert old_gate["publishable"] is False
    assert improved["publishable"] is True


def test_page_audit_never_publishes_flagged_promotional_prose():
    page = _page(repeated_prose=False)
    page["markdown"] += (
        "\n\n## Benefit\n\n"
        "This efficient runtime improves repository navigation. [E3]"
    )

    report = audit_page(page)

    assert report["grounding_valid"] is True
    assert report["style_valid"] is False
    assert report["promotional_phrases"] == ["efficient", "improves"]
    assert report["publishable"] is False


def test_page_audit_respects_an_explicit_failed_quality_gate():
    page = _page(repeated_prose=False)
    page["quality"]["valid"] = False

    report = audit_page(page)

    assert report["structural_valid"] is False
    assert report["publishable"] is False


def test_narrative_density_rejects_a_source_grounded_symbol_catalog():
    markdown = (
        "# Graph Exploration\n\n"
        "This page lists source-grounded graph operations from the repository. [E1]\n\n"
        "## Operations\n\n"
        "`visualize_graph` visualizes graph dependencies. "
        "`query_range` queries a selected source range. "
        "`build_hierarchical_code_graph` builds hierarchy levels. "
        "`load_graph` loads a persisted graph artifact. "
        "`save_graph` saves the graph artifact. [E1] [E2] [E3]"
    )

    report = narrative_density_report(markdown)

    assert report["catalog_sentence_count"] == 5
    assert report["catalog_sentence_ratio"] > 0.65
    assert report["narrative_density_valid"] is False


def test_narrative_density_does_not_count_a_handoff_as_an_isolated_api_fact():
    report = narrative_density_report(
        "`AgentRunner.run` executes requests and interacts with "
        "`AgentRunTrace.add` to record each event."
    )

    assert report["interaction_sentence_count"] == 1
    assert report["catalog_sentence_count"] == 0


def test_section_synthesis_rejects_a_page_dominated_by_operation_inventories():
    markdown = (
        "# Graph Navigation\n\n"
        "The graph view exposes persisted dependency state. [E1]\n\n"
        "## Visualization\n\n"
        "`visualize_graph` generates a graph image. "
        "`visualize_graph` requires an output path. [E1]\n\n"
        "## Querying\n\n"
        "`query_range` retrieves nodes in a source range. "
        "`query_range` requires depth one. [E2]\n\n"
        "## Persistence\n\n"
        "`load_graph` loads a graph artifact. "
        "`save_graph` serializes a graph artifact. [E3]"
    )

    report = section_synthesis_report(markdown)

    assert report["isolated_catalog_sections"] == [
        "Visualization",
        "Querying",
        "Persistence",
    ]
    assert report["section_synthesis_valid"] is False


def test_section_synthesis_admits_a_state_lifecycle():
    markdown = (
        "# Graph Navigation\n\n"
        "The graph view exposes persisted dependency state. [E1]\n\n"
        "## Load and Query\n\n"
        "`load_graph` writes restored state to `CodeGraph.graph`. "
        "`query_range` reads candidate nodes from `CodeGraph.graph`. [E1] [R1]\n\n"
        "## Derive and Persist\n\n"
        "`build_hierarchy` reads the base state from `CodeGraph.graph`. "
        "`save_graph` writes `CodeGraph.graph` to an artifact. [E2] [R2]\n\n"
        "## Boundary\n\n"
        "`load_graph` rejects an incompatible schema. "
        "Callers receive a reconstructed `CodeGraph`. [E3]"
    )

    report = section_synthesis_report(markdown)

    assert report["isolated_catalog_sections"] == []
    assert report["section_synthesis_valid"] is True


def test_sentence_redundancy_rejects_formulaic_restatement_in_a_section():
    report = section_sentence_redundancy_report(
        "# Hierarchy\n\n"
        "## Derivation\n\n"
        "The function organizes nodes and dependencies into a hierarchical graph. "
        "The hierarchical graph organizes the same nodes and dependencies for "
        "visualization."
    )

    assert len(report["redundant_sentence_pairs"]) == 1
    assert report["sentence_redundancy_valid"] is False


def test_sentence_redundancy_rejects_near_duplicate_registration_claims():
    report = section_sentence_redundancy_report(
        "# Indexing\n\n"
        "## Registration\n\n"
        "`register_default_builders()` registers index builders with "
        "`IndexBuilderRegistry.register()` to create a reusable index. "
        "This function also calls `IndexBuilderRegistry.register()` to "
        "register the index builders."
    )

    assert len(report["redundant_sentence_pairs"]) == 1
    assert report["sentence_redundancy_valid"] is False


def test_sentence_redundancy_allows_one_source_to_call_distinct_targets():
    report = section_sentence_redundancy_report(
        "# Indexing\n\n"
        "## Registration\n\n"
        "`register_default_builders()` calls `IndexBuilderRegistry.register()`. "
        "`register_default_builders()` calls `default_exclude_patterns()`."
    )

    assert report["redundant_sentence_pairs"] == []
    assert report["sentence_redundancy_valid"] is True


def test_sentence_redundancy_allows_handoff_and_local_detail_for_same_subject():
    report = section_sentence_redundancy_report(
        "# Controller\n\n"
        "## File Printing\n\n"
        "`Controller.print_file()` calls `Controller.print_file_ranges()` with "
        "the resolved line ranges. "
        "In diff context mode, `Controller.print_file()` builds line ranges "
        "from the Git diff keys plus and minus the context size."
    )

    assert report["redundant_sentence_pairs"] == []
    assert report["sentence_redundancy_valid"] is True


def test_sentence_redundancy_allows_symmetric_named_operations():
    report = section_sentence_redundancy_report(
        "# Logging\n\n"
        "## Output\n\n"
        "`LoggingManager.switch_to_file()` changes logging output to a file and "
        "updates handlers. "
        "`LoggingManager.switch_to_stdout()` changes logging output to stdout and "
        "updates handlers."
    )

    assert report["redundant_sentence_pairs"] == []
    assert report["sentence_redundancy_valid"] is True


def test_sentence_redundancy_allows_parallel_documented_entrypoints():
    report = section_sentence_redundancy_report(
        "# Evaluation\n\n"
        "## Entry points\n\n"
        "The `run_sgl_eval.sh` script generates task IDs and runs SGLang "
        "benchmarks. "
        "The `run_lc_eval.sh` script generates task IDs and runs LangChain "
        "benchmarks."
    )

    assert report["redundant_sentence_pairs"] == []
    assert report["sentence_redundancy_valid"] is True


def test_sentence_redundancy_allows_parallel_named_config_variants():
    report = section_sentence_redundancy_report(
        "# Templates\n\n"
        "## Config variants\n\n"
        "The JavaScript config in `examples/classic/docusaurus.config.js` is "
        "identical in header structure to the JavaScript template. "
        "The TypeScript config in "
        "`examples/classic-typescript/docusaurus.config.ts` is identical in "
        "header structure to the TypeScript template."
    )

    assert report["redundant_sentence_pairs"] == []
    assert report["sentence_redundancy_valid"] is True


def test_prose_integrity_rejects_internal_ids_and_private_user_entries():
    report = prose_integrity_report(
        "# Runtime\n\n"
        "Users run `_prepare()` before serving a request according to R2. [E1]"
    )

    assert report["narrated_evidence_ids"] == ["R2"]
    assert report["private_entry_sentences"]
    assert report["prose_integrity_valid"] is False


def test_prose_integrity_rejects_citation_only_paragraphs():
    report = prose_integrity_report(
        "# Runtime\n\n"
        "## Query\n\n"
        "`query()` calls `run()`. [E1] [R1]\n\n"
        "[E2](#evidence-E2)"
    )

    assert report["citation_only_blocks"] == ["[E2](#evidence-E2)"]
    assert report["prose_integrity_valid"] is False


def test_prose_integrity_rejects_narrated_relation_anchors():
    report = prose_integrity_report(
        "# Runtime\n\n"
        "`main` calls `compile_repo`, as indicated by the reference at "
        "`src/cli.py:42`. [R1]"
    )

    assert report["reference_narration_sentences"]
    assert report["prose_integrity_valid"] is False


def test_prose_integrity_does_not_treat_a_url_port_as_a_source_anchor():
    report = prose_integrity_report(
        "# Runtime\n\n" "The local server listens at `http://localhost:3000`. [E1]"
    )

    assert report["reference_narration_sentences"] == []
    assert report["prose_integrity_valid"] is True


def test_prose_integrity_allows_a_public_entry_to_delegate_to_a_helper():
    report = prose_integrity_report(
        "# Runtime\n\n"
        "## Public Workflow\n\n"
        "`codenib wiki` calls `_prepare()` before serving indexed pages. [E1]"
    )

    assert report["private_entry_sentences"] == []
    assert report["prose_integrity_valid"] is True


def test_prose_integrity_allows_public_entry_with_named_private_helpers():
    report = prose_integrity_report(
        "# Solvers\n\n"
        "The package uses `solve` as the public entry point, while specialized "
        "helpers such as `_tsolve` own per-domain work. [E1]"
    )

    assert report["private_entry_sentences"] == []
    assert report["prose_integrity_valid"] is True


def test_page_audit_requires_a_flow_when_static_relations_are_available():
    page = {
        "id": "agent-runtime",
        "title": "Agent Runtime",
        "markdown": (
            "# Agent Runtime\n\n"
            "The runtime executes repository requests from indexed source. "
            "[E1](#evidence-E1)\n\n"
            "## Methods\n\n"
            "The method `AgentRunner.run` executes the agent loop. "
            "[E1](#evidence-E1) The function `_role` determines a graph role. "
            "[E2](#evidence-E2)"
        ),
        "evidence": {
            "items": [{"id": "E1"}, {"id": "E2"}],
            "relations": [{"id": "R1"}],
        },
        "generation": {"mode": "generated"},
        "grounding": {"valid": True, "citation_coverage": 1.0},
        "quality": {"valid": True, "claim_coverage": 1.0},
    }

    report = audit_page(page)

    assert report["require_interaction"] is True
    assert report["interaction_valid"] is False
    assert report["publishable"] is False


def test_page_audit_respects_an_explicit_non_architectural_page_contract():
    page = {
        "id": "formats",
        "title": "Formats",
        "markdown": (
            "# Formats\n\n"
            "The package supports source-linked text and binary formats. [E1]\n\n"
            "## Text\n\n"
            "`read_text()` parses delimited records from a stream. [E1]\n\n"
            "## Binary\n\n"
            "`read_binary()` decodes typed records from a file. [E2]"
        ),
        "evidence": {
            "items": [{"id": "E1"}, {"id": "E2"}],
            "relations": [{"id": "R1"}],
        },
        "generation": {"mode": "generated"},
        "grounding": {"valid": True, "citation_coverage": 1.0},
        "quality": {
            "valid": True,
            "claim_coverage": 1.0,
            "require_interaction": False,
        },
    }

    report = audit_page(page)

    assert report["require_interaction"] is False
    assert report["interaction_valid"] is True


def test_architecture_narrative_admits_a_grounded_component_flow():
    plan = {
        "thesis": {
            "statement": "Wiki requests are resolved against compiled views",
            "evidence": ["E1", "E2"],
        },
        "sections": [
            {
                "title": "Request path",
                "claims": [
                    {
                        "role": "flow",
                        "statement": (
                            "`wiki_page` passes the requested page identifier "
                            "to `AgentWiki.page`"
                        ),
                        "evidence": ["E1", "E2", "R1"],
                    },
                    {
                        "role": "flow",
                        "statement": (
                            "`AgentWiki.page` returns source-linked Markdown "
                            "to `wiki_page`"
                        ),
                        "evidence": ["E1", "E2", "R2"],
                    },
                ],
            },
            {
                "title": "Ownership",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "`AgentWiki` owns page planning and evidence selection"
                        ),
                        "evidence": ["E2"],
                    },
                    {
                        "role": "responsibility",
                        "statement": (
                            "`WikiBundle` owns the loaded repository index handles"
                        ),
                        "evidence": ["E3"],
                    },
                ],
            },
        ],
    }
    markdown = (
        "Wiki requests enter through `wiki_page` and are resolved against "
        "compiled repository views. [E1] [E2]\n\n"
        "## Request path\n\n"
        "`wiki_page` passes the requested page identifier to `AgentWiki.page`. "
        "`AgentWiki.page` returns source-linked Markdown to `wiki_page`. "
        "[E1] [E2] [R1] [R2]\n\n"
        "## Ownership\n\n"
        "`AgentWiki` owns page planning and evidence selection. "
        "`WikiBundle` owns the loaded repository index handles. [E2] [E3]"
    )

    plan_report = plan_narrative_report(plan)
    quality = page_quality_report(
        markdown,
        plan,
        require_cited_intro=True,
        require_narrative_density=True,
        require_interaction=True,
    )

    assert plan_report["thesis_grounded"] is True
    assert plan_report["supported_interaction_claims"] == 2
    assert quality["interaction_sentence_count"] == 2
    assert quality["valid"] is True


def test_topic_narrative_allows_direct_source_backing_for_a_flow():
    statement = (
        "`Command.envs()` forwards the provided variables to " "`self.std.envs(vars)`."
    )
    plan = {
        "thesis": {
            "statement": "`Command` configures spawned process environments",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Environment",
                "claims": [
                    {
                        "role": "flow",
                        "statement": statement,
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/process/command.rs",
            start_line=1,
            end_line=4,
            symbol="Command.envs",
            kind="method",
            content="fn envs(vars) { self.std.envs(vars); }",
        )
    ]
    relations = [
        RelationItem(
            id="R1",
            source="Command.envs()",
            target="self.std.envs(vars)",
        )
    ]

    topic = plan_narrative_report(
        plan,
        require_relation_backing=False,
        relations=relations,
        evidence_items=evidence,
    )
    overview = plan_narrative_report(
        plan,
        require_relation_backing=True,
        relations=relations,
        evidence_items=evidence,
    )

    assert topic["supported_interaction_claims"] == 1
    assert topic["invalid_flow_claims"] == []
    assert overview["invalid_flow_claims"] == [statement]


def test_component_heavy_plan_is_valid_when_it_contains_a_supported_flow():
    component_claims = [
        {
            "role": "component",
            "statement": f"Component {index} records compatibility metadata",
            "evidence": ["E1"],
        }
        for index in range(4)
    ]
    plan = {
        "thesis": {
            "statement": "Compatibility data drives transform selection",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Selection",
                "claims": [
                    *component_claims,
                    {
                        "role": "flow",
                        "statement": "`filterAvailable()` passes plugins to `presetEnv()`",
                        "evidence": ["R1"],
                    },
                ],
            }
        ],
    }
    relation = RelationItem(
        id="R1",
        source="filterAvailable()",
        target="presetEnv()",
    )

    report = plan_narrative_report(plan, relations=[relation])
    isolated = plan_narrative_report(
        {
            **plan,
            "sections": [
                {
                    "title": "Selection",
                    "claims": [
                        *component_claims,
                        {
                            "role": "component",
                            "statement": "Component 5 records target metadata",
                            "evidence": ["E1"],
                        },
                    ],
                }
            ],
        },
        relations=[relation],
    )

    assert report["supported_interaction_claims"] == 1
    assert report["component_claim_ratio"] == 0.8
    assert report["component_dominated"] is False
    assert isolated["component_dominated"] is True
    assert isolated["plan_role_integrity_valid"] is True


def test_plan_narrative_rejects_flow_labels_without_a_relation_handoff():
    report = plan_narrative_report(
        {
            "thesis": {"statement": "A graph is served", "evidence": ["E1"]},
            "sections": [
                {
                    "title": "Query",
                    "claims": [
                        {
                            "role": "flow",
                            "statement": "`query_range` retrieves graph nodes",
                            "evidence": ["E1"],
                        }
                    ],
                }
            ],
        },
        require_relation_backing=True,
    )

    assert report["supported_interaction_claims"] == 0
    assert report["invalid_flow_claims"] == ["`query_range` retrieves graph nodes"]
    assert report["plan_role_integrity_valid"] is False


def test_plan_narrative_rejects_a_citation_with_different_relation_endpoints():
    report = plan_narrative_report(
        {
            "thesis": {"statement": "A wiki serves graph data", "evidence": ["E1"]},
            "sections": [
                {
                    "title": "Flow",
                    "claims": [
                        {
                            "role": "flow",
                            "statement": (
                                "`wiki_page_graph` calls `compile_repo` to render "
                                "a page"
                            ),
                            "evidence": ["E1", "R1"],
                        }
                    ],
                }
            ],
        },
        require_relation_backing=True,
        relations=[
            RelationItem(
                id="R1",
                source="codenib/cli.py:index_repository()",
                target=(
                    "codenib/compiler/index_compiler.py:" "IndexCompiler.compile_repo()"
                ),
            )
        ],
    )

    assert report["supported_interaction_claims"] == 0
    assert report["invalid_flow_claims"]
    assert report["plan_role_integrity_valid"] is False


def test_plan_narrative_rejects_a_reversed_static_relation():
    statement = "`AgentRunner.run` invokes `query` to process a request"
    report = plan_narrative_report(
        {
            "thesis": {"statement": "The runtime serves requests", "evidence": ["E1"]},
            "sections": [
                {
                    "title": "Flow",
                    "claims": [
                        {
                            "role": "flow",
                            "statement": statement,
                            "evidence": ["E1"],
                        }
                    ],
                }
            ],
        },
        require_relation_backing=True,
        relations=[
            RelationItem(
                id="R1",
                source="codenib/agent/runner.py:query()",
                target="codenib/agent/runner.py:AgentRunner.run()",
            )
        ],
        evidence_items=[
            EvidenceItem(
                id="E1",
                file="codenib/agent/runner.py",
                start_line=1,
                end_line=4,
                symbol="query",
                kind="function",
                content="def query():\n    return AgentRunner().run()",
            )
        ],
    )

    assert report["supported_interaction_claims"] == 0
    assert report["invalid_flow_claims"] == [statement]
    assert report["plan_role_integrity_valid"] is False


def test_plan_narrative_accepts_a_flow_supported_by_one_source_body():
    statement = "`wiki_page_graph` calls `_bundle` to access the code graph"
    report = plan_narrative_report(
        {
            "thesis": {"statement": "A wiki serves graph data", "evidence": ["E1"]},
            "sections": [
                {
                    "title": "Flow",
                    "claims": [
                        {
                            "role": "flow",
                            "statement": statement,
                            "evidence": ["E1"],
                        }
                    ],
                }
            ],
        },
        require_relation_backing=True,
        relations=[
            RelationItem(
                id="R1",
                source="unrelated",
                target="component",
            )
        ],
        evidence_items=[
            EvidenceItem(
                id="E1",
                file="src/wiki.py",
                start_line=1,
                end_line=3,
                symbol="wiki_page_graph",
                kind="function",
                content="def wiki_page_graph():\n    return _bundle().code_graph()",
            )
        ],
    )

    assert report["supported_interaction_claims"] == 1
    assert report["invalid_flow_claims"] == []
    assert report["plan_role_integrity_valid"] is True


def test_plan_narrative_rejects_a_flow_spliced_across_unrelated_sources():
    statement = "`WikiPage` receives indexed records from `IndexBuilderRegistry`"
    report = plan_narrative_report(
        {
            "thesis": {"statement": "The Wiki serves pages", "evidence": ["E1"]},
            "sections": [
                {
                    "title": "Flow",
                    "claims": [
                        {
                            "role": "flow",
                            "statement": statement,
                            "evidence": ["E1", "E2"],
                        }
                    ],
                }
            ],
        },
        evidence_items=[
            EvidenceItem(
                id="E1",
                file="src/wiki.py",
                start_line=1,
                end_line=2,
                symbol="WikiPage",
                kind="class",
                content="class WikiPage: pass",
            ),
            EvidenceItem(
                id="E2",
                file="src/index.py",
                start_line=1,
                end_line=2,
                symbol="IndexBuilderRegistry",
                kind="class",
                content="class IndexBuilderRegistry: pass",
            ),
        ],
    )

    assert report["supported_interaction_claims"] == 0
    assert report["invalid_flow_claims"] == [statement]
    assert report["plan_role_integrity_valid"] is False


def test_plan_narrative_rejects_multi_sentence_claims():
    report = plan_narrative_report(
        {
            "thesis": {"statement": "The runtime serves requests", "evidence": ["E1"]},
            "sections": [
                {
                    "title": "Runtime",
                    "claims": [
                        {
                            "role": "responsibility",
                            "statement": (
                                "The runtime loads an index. It then serves queries."
                            ),
                            "evidence": ["E1"],
                        }
                    ],
                }
            ],
        }
    )

    assert report["multi_sentence_claims"]
    assert report["plan_role_integrity_valid"] is False


def test_plan_narrative_ignores_sentence_punctuation_inside_literals():
    statement = (
        "`generate_config_file()` prints the path and asks "
        "'Overwrite? (y/N):' before replacing it."
    )
    report = plan_narrative_report(
        {
            "thesis": {
                "statement": "`generate_config_file()` owns config generation",
                "evidence": ["E1"],
            },
            "sections": [
                {
                    "title": "Overwrite protection",
                    "claims": [
                        {
                            "role": "contract",
                            "statement": statement,
                            "evidence": ["E1"],
                        }
                    ],
                }
            ],
        }
    )

    assert report["multi_sentence_claims"] == []
    assert report["plan_role_integrity_valid"] is True


def test_plan_narrative_rejects_relation_only_non_flow_claims():
    report = plan_narrative_report(
        {
            "thesis": {
                "statement": "The runtime serves repository context",
                "evidence": ["R1"],
            },
            "sections": [
                {
                    "title": "Runtime",
                    "claims": [
                        {
                            "role": "responsibility",
                            "statement": "The runtime owns repository context",
                            "evidence": ["R1"],
                        }
                    ],
                }
            ],
        }
    )

    assert report["thesis_grounded"] is False
    assert report["invalid_source_claims"] == ["The runtime owns repository context"]
    assert report["plan_role_integrity_valid"] is False


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


def test_wiki_audit_generates_nested_pages_and_requires_generated_mode():
    ready = _page(repeated_prose=False)
    ready["id"] = "overview"
    degraded = _page(repeated_prose=False)
    degraded["id"] = "runtime"
    degraded["title"] = "Runtime"
    degraded["generation"]["mode"] = "degraded"

    class Builder:
        def page_tree(self):
            return [
                {
                    "id": "overview",
                    "children": [{"id": "runtime", "children": []}],
                }
            ]

        def page(self, page_id):
            return {"overview": ready, "runtime": degraded}[page_id]

    report = audit_wiki(Builder())

    assert report["expected_pages"] == report["generated_pages"] == 2
    assert report["ready_pages"] == 1
    assert report["passed"] is False
    assert report["details"][1]["failures"] == ["generation:degraded"]


def test_wiki_audit_reports_missing_and_failed_pages_without_stopping():
    class Builder:
        def page_tree(self):
            return [
                {"id": "missing", "children": []},
                {"id": "failed", "children": []},
            ]

        def page(self, page_id):
            if page_id == "failed":
                raise RuntimeError("backend unavailable")
            return None

    report = audit_wiki(Builder())

    assert report["generated_pages"] == report["ready_pages"] == 0
    assert report["passed"] is False
    assert report["details"] == [
        {
            "id": "missing",
            "title": "missing",
            "ready": False,
            "failures": ["generation:missing"],
            "error": "page builder returned no page",
        },
        {
            "id": "failed",
            "title": "failed",
            "ready": False,
            "failures": ["generation:error"],
            "error": "RuntimeError: backend unavailable",
        },
    ]
