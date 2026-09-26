# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast unit tests for the agent wiki retrieval guardrails."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import codenib.wiki.agent_wiki as agent_wiki_module
from codenib.graph.code_graph import CodeGraph
from codenib.wiki.agent_wiki import (
    AgentWiki,
    _admissible_excerpt_caption,
    _apply_overview_editorial_budget,
    _candidate_score,
    _clean_markdown,
    _compact_dense_plan,
    _compact_overview_identifiers,
    _condense_relation_free_overview,
    _drop_duplicate_framing,
    _ensure_cited_intro,
    _excerpt_focus_terms,
    _excerpt_window,
    _fact_plan_markdown,
    _format_supported_literals,
    _framing_repeats_claim,
    _handoff_label,
    _interaction_row,
    _journey_flow,
    _journey_from_path,
    _normalize_plan_support,
    _overview_symbol_role_score,
    _owning_topic,
    _page_planning_guidance,
    _page_quality_report,
    _plan_evidence_constraints,
    _plan_quality_warnings,
    _plan_repair_limit,
    _plan_repair_score,
    _prepare_evidence_content,
    _prune_uncited_blocks,
    _readme_intro,
    _relation_backed_recovery_plan,
    _remove_orphan_headings,
    _renderable_plan,
    _subsystem_table,
    _supplement_topic_relation_flows,
)
from codenib.wiki.builder import Symbol
from codenib.wiki.evidence import (
    EvidenceItem,
    RelationItem,
    candidate_key,
    grounding_report,
)
from codenib.wiki.fences import strip_code_fences
from codenib.wiki.multimodal import MEDIA_PLAN_VERSION
from codenib.wiki.quality import (
    duplicate_prose_blocks,
    prose_integrity_report,
    section_sentence_redundancy_report,
)
from codenib.wiki.sqlite_store import SQLiteWikiStore
from codenib.wiki.store import WikiStoreError
from codenib.wiki.story import finalize_story_ir, story_quality_report


class _FakeVectorStore:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def search_with_content(self, query, top_k):
        self.calls.append((query, top_k))
        return self.nodes[:top_k]


def test_rel_uses_bound_source_inventory_without_live_exists(monkeypatch):
    class Reader:
        def captured_relative_path(self, path):
            return "src/app.py" if path.endswith("src/app.py") else None

    def fail_exists(_path):
        raise AssertionError("bound Wiki must not inspect the live checkout")

    monkeypatch.setattr(os.path, "exists", fail_exists)
    wiki = AgentWiki(
        SimpleNamespace(
            entry=SimpleNamespace(
                repo="owner/repo",
                repo_dir="/repo",
                language="python",
            ),
            source_reader=Reader(),
        ),
        model="fake-model",
    )

    assert wiki._rel("/builder/root/src/app.py") == "src/app.py"
    assert wiki._rel("/builder/root/private/secret.py") is None


def _add_overview_architecture(
    plan: dict,
    evidence_ids: tuple[str, ...] = ("E1", "E2", "E3", "E4"),
) -> dict:
    refs = evidence_ids or ("E1",)
    plan["architecture"] = {
        "title": "How repository input becomes a source-linked result",
        "components": [
            {
                "id": "reader",
                "label": "Reader input",
                "responsibility": "Supplies the repository request.",
                "layer": "external",
                "kind": "external",
                "evidence": [refs[0]],
            },
            {
                "id": "surface",
                "label": "Entry surfaces",
                "responsibility": "Accept repository input.",
                "layer": "interface",
                "kind": "frontend",
                "evidence": [refs[min(1, len(refs) - 1)]],
            },
            {
                "id": "coordination",
                "label": "Workflow coordination",
                "responsibility": "Plans the repository work.",
                "layer": "coordination",
                "kind": "backend",
                "evidence": [refs[min(2, len(refs) - 1)]],
            },
            {
                "id": "execution",
                "label": "Repository execution",
                "responsibility": "Builds the source-backed result.",
                "layer": "execution",
                "kind": "backend",
                "evidence": [refs[min(2, len(refs) - 1)]],
            },
            {
                "id": "result",
                "label": "Source-linked result",
                "responsibility": "Returns the result to the reader.",
                "layer": "data",
                "kind": "database",
                "evidence": [refs[-1]],
            },
        ],
        "connections": [
            {
                "source": "reader",
                "target": "surface",
                "label": "submits repository input",
                "evidence": [refs[0]],
            },
            {
                "source": "surface",
                "target": "coordination",
                "label": "frames repository work",
                "evidence": [refs[min(1, len(refs) - 1)]],
            },
            {
                "source": "coordination",
                "target": "execution",
                "label": "dispatches planned work",
                "evidence": [refs[min(2, len(refs) - 1)]],
            },
            {
                "source": "execution",
                "target": "result",
                "label": "returns source-linked output",
                "evidence": [refs[-1]],
            },
        ],
        "primary_path": [
            "reader",
            "surface",
            "coordination",
            "execution",
            "result",
        ],
        "boundaries": [],
    }
    return plan


def test_markdown_cleanup_removes_outer_fence_and_uncited_prose():
    fenced = (
        "```markdown\n"
        "Unsupported introductory prose without an evidence marker anywhere.\n\n"
        "## Flow\n\n"
        "The source-backed routing implementation is shown here. [E1]\n"
        "```"
    )

    cleaned = _clean_markdown(fenced)
    pruned = _prune_uncited_blocks(cleaned)

    assert not cleaned.startswith("```")
    assert "Unsupported introductory" not in pruned
    assert "## Flow" in pruned
    assert "[E1]" in pruned


def test_style_repair_is_a_bounded_zero_temperature_edit():
    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return "The runtime loads the generated page. [E1]"

    llm = LLM()
    wiki = AgentWiki(
        SimpleNamespace(
            entry=SimpleNamespace(
                repo="owner/repo",
                language="python",
                repo_dir="",
            )
        ),
        model="fake-model",
        llm=llm,
    )

    rendered = wiki._repair_style(
        "The runtime efficiently loads the generated page. [E1]",
        ["efficiently"],
    )

    assert rendered == "The runtime loads the generated page. [E1]"
    assert llm.calls[0][1]["temperature"] == 0.0
    prompt = " ".join(llm.calls[0][0][0]["content"].split())
    assert "do not replace it with a synonym" in prompt


def test_enabled_story_review_uses_its_reader_rubric_without_shadowing_method():
    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            return json.dumps(
                {
                    key: {"score": 1, "quote": "The page states the case."}
                    for key in ("problem", "path", "decision", "failure")
                }
                | {"jargon": False, "repetition": False, "notes": ""}
            )

    llm = LLM()
    wiki = AgentWiki(
        SimpleNamespace(entry=SimpleNamespace(repo="owner/repo", language="python")),
        model="fake-model",
        llm=llm,
        story_review=True,
    )

    review = wiki._review_story("The page states the case. [E1]")

    assert llm.calls == 1
    assert review is not None
    assert review["passed"] is True


def test_candidate_score_prefers_fewer_style_warnings():
    quality = {
        "valid": True,
        "rendered_sections": 3,
        "substantive_blocks": 4,
        "claim_coverage": 1.0,
    }
    noisy = {"valid": True, "promotional_phrases": ["dynamic", "efficient"]}
    cleaner = {"valid": True, "promotional_phrases": ["dynamic"]}

    assert _candidate_score(cleaner, quality) > _candidate_score(noisy, quality)


def test_plan_repair_score_keeps_richer_progress_under_the_same_warning():
    warning = ["Overview needs at least 8 supported claims"]
    sparse = {
        "sections": [
            {
                "title": "Runtime",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The runtime handles requests",
                        "evidence": ["E1"],
                    }
                ],
            }
        ]
    }
    richer = {
        "sections": [
            *sparse["sections"],
            {
                "title": "Serving",
                "claims": [
                    {
                        "role": "flow",
                        "statement": "The server calls the runtime",
                        "evidence": ["E2", "R1"],
                    }
                ],
            },
        ]
    }

    assert _plan_repair_score(richer, warning) < _plan_repair_score(sparse, warning)


def test_plan_repair_score_prefers_editorial_budget_over_more_detail():
    concise = {
        "sections": [
            {
                "title": "Core Formatting",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "`format_value()` applies rounding rules",
                        "evidence": ["E1"],
                    }
                ],
            }
        ]
    }
    overloaded = {
        "sections": [
            *concise["sections"],
            *[
                {
                    "title": f"Detail {index}",
                    "claims": [
                        {
                            "role": "component",
                            "statement": f"Detail {index} records one symbol",
                            "evidence": [f"E{index}"],
                        },
                        {
                            "role": "component",
                            "statement": f"Detail {index} records another symbol",
                            "evidence": [f"E{index}"],
                        },
                    ],
                }
                for index in range(2, 7)
            ],
        ]
    }

    assert _plan_repair_score(concise, []) < _plan_repair_score(
        overloaded,
        ["Overview exceeds the editorial section budget of four"],
    )


def test_plan_repair_limit_distinguishes_truth_from_composition_feedback():
    assert _plan_repair_limit(["claim cites evidence that does not exist"]) == 3
    assert _plan_repair_limit(["section 'Flow' reads as a callable catalog"]) == 1
    assert _plan_repair_limit(["section 'A' substantially repeats section 'B'"]) == 1
    assert _plan_repair_limit([]) == 0


def test_relation_free_overview_keeps_one_explanatory_fact_per_topic():
    plan = {
        "sections": [
            {
                "title": "Formatting",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "`format_value()` formats a value",
                        "evidence": ["E1"],
                    },
                    {
                        "role": "contract",
                        "statement": (
                            "`format_value()` rejects precision for integral "
                            "presentation types"
                        ),
                        "evidence": ["E2"],
                    },
                ],
            },
            {
                "title": "Output",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "`write()` writes formatted output",
                        "evidence": ["E3"],
                    },
                    {
                        "role": "flow",
                        "statement": (
                            "`write_file()` calls `write()` before flushing "
                            "the output buffer"
                        ),
                        "evidence": ["E4"],
                    },
                ],
            },
        ]
    }

    condensed = _condense_relation_free_overview(plan)

    assert plan["sections"][0]["claims"][0]["statement"].startswith(
        "`format_value()` formats"
    )
    assert [section["claims"][0]["role"] for section in condensed["sections"]] == [
        "contract",
        "flow",
    ]
    assert [len(section["claims"]) for section in condensed["sections"]] == [1, 1]


def test_overview_editorial_budget_removes_inventory_chrome_and_caps_detail():
    plan = {
        "purpose": {"statements": ["Purpose"], "evidence": ["E1"]},
        "map": [{"concern": "Concern", "entity": "Entity", "evidence": ["E1"]}],
        "see_also": [{"page": str(index)} for index in range(4)],
        "story": {
            "beats": [
                {"section": f"Stage {index}", "role": "mechanism"} for index in range(6)
            ]
        },
        "sections": [
            {
                "title": f"Stage {index}",
                "lead": {
                    "statements": ["First sentence", "Second sentence"],
                    "evidence": [f"E{index + 1}"],
                },
                "excerpt": {"evidence": f"E{index + 1}"},
                "claims": [
                    {
                        "role": role,
                        "statement": f"Stage {index} {role} claim",
                        "evidence": [f"E{index + 1}"],
                    }
                    for role in ("component", "responsibility", "flow")
                ],
            }
            for index in range(6)
        ],
    }

    concise = _apply_overview_editorial_budget(plan)

    assert "purpose" not in concise
    assert "map" not in concise
    assert len(concise["see_also"]) == 2
    assert len(concise["sections"]) == 4
    assert concise["sections"][0]["title"] == "Stage 0"
    assert concise["sections"][-1]["title"] == "Stage 5"
    assert sum(len(section["claims"]) for section in concise["sections"]) == 4
    assert all(len(section["claims"]) <= 2 for section in concise["sections"])
    assert all("excerpt" not in section for section in concise["sections"])
    assert all("lead" not in section for section in concise["sections"])


def test_transition_preview_deduplicates_the_same_callable_claim():
    assert _framing_repeats_claim(
        "The agent's search actions are parsed by `SearchOutputParser.parse()`, "
        "which routes to `parse_explore()` for exploration results.",
        "`SearchOutputParser.parse()` routes to "
        "`SearchOutputParser.parse_explore()` when the method is `explore`.",
    )


def test_markdown_cleanup_removes_empty_sections_but_keeps_parent_sections():
    markdown = (
        "## Runtime\n\n"
        "### Dispatch\n\n"
        "Requests enter through the dispatcher. [E1]\n\n"
        "## Empty section\n\n"
        "## Storage\n\n"
        "The repository stores an index manifest. [E2]"
    )

    cleaned = _remove_orphan_headings(markdown)

    assert "## Runtime" in cleaned
    assert "### Dispatch" in cleaned
    assert "## Empty section" not in cleaned
    assert "## Storage" in cleaned


def test_supported_literals_render_as_inline_code():
    text = (
        "The 'run' function executes the command "
        "'codenib wiki /path/to/repository' from 'codenib/cli.py', while the "
        "'stable behavior' description remains prose."
    )

    rendered = _format_supported_literals(text)

    assert "`run` function" in rendered
    assert "`codenib wiki /path/to/repository`" in rendered
    assert "`codenib/cli.py`" in rendered
    assert "'stable behavior'" in rendered


def test_overview_display_hides_path_qualification_owned_by_citations():
    rendered = _compact_overview_identifiers(
        "`src/runtime.py:Runtime.run()` calls `src/store.py:Store.save()`"
    )

    assert rendered == "`Runtime.run()` calls `Store.save()`"


def test_parent_page_guidance_reserves_child_implementation_details():
    guidance = _page_planning_guidance(
        {
            "id": "indexing",
            "children": [
                {"title": "BM25 Indexing"},
                {"title": "Vector Indexing"},
            ],
        }
    )

    assert "This is a parent page" in guidance
    assert "BM25 Indexing, Vector Indexing" in guidance


def test_overview_guidance_treats_outline_as_candidate_evidence():
    guidance = _page_planning_guidance(
        {
            "id": "overview",
            "major_topics": [
                {"title": "Indexing"},
                {"title": "Wiki Serving"},
                {"title": "Agent Runtime"},
            ],
        }
    )

    assert "Indexing, Wiki Serving, Agent Runtime" in guidance
    assert "candidates, not a coverage checklist" in guidance
    assert "two to four sections" in guidance
    assert "semantic roles, layers, primary runtime path" in guidance
    assert "not a static call graph" in guidance
    assert "Unused evidence is evidence of editorial restraint" in guidance


def test_overview_meta_uses_parent_level_files_from_each_major_topic():
    meta = {
        "id": "overview",
        "title": "Overview",
        "files": [
            "README.md",
            "src/cli.py",
            "src/mcp/__main__.py",
            "src/web/app.py",
            "web/src/main.tsx",
            "web/lib/router.tsx",
        ],
    }
    topics = [
        {
            "id": "indexing",
            "title": "Indexing",
            "files": ["src/index/bm25.py", "src/compiler/index_builders.py"],
            "children": [
                {
                    "title": "BM25",
                    "files": ["src/index/bm25.py"],
                }
            ],
        },
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "files": ["src/agent/runner.py", "src/agent/rerank.py"],
            "children": [
                {
                    "title": "Reranking",
                    "files": ["src/agent/rerank.py"],
                }
            ],
        },
        {
            "id": "graph-navigation",
            "title": "Graph Navigation",
            "files": ["src/graph/code_graph.py"],
            "children": [],
        },
    ]

    result = AgentWiki._overview_page_meta(meta, topics)

    assert result["major_topics"][0]["files"] == ["src/compiler/index_builders.py"]
    assert result["major_topics"][1]["files"] == ["src/agent/runner.py"]
    assert result["major_topics"][0]["keywords"] == []
    assert "src/graph/code_graph.py" in result["files"]
    assert "src/agent/rerank.py" not in result["files"]
    assert result["files"][0] == "README.md"


def test_overview_meta_prefers_public_child_file_over_internal_parent_file():
    result = AgentWiki._overview_page_meta(
        {
            "id": "overview",
            "title": "Overview",
            "files": ["README.md"],
        },
        [
            {
                "id": "formatting",
                "title": "Formatting",
                "files": ["include/fmt/format.h", "include/fmt/format-inl.h"],
                "children": [
                    {
                        "title": "Floating Point Formatting",
                        "files": ["include/fmt/format.h"],
                    }
                ],
            }
        ],
    )

    assert result["major_topics"][0]["files"] == ["include/fmt/format.h"]


def test_overview_meta_keeps_readme_documented_workflow_entry():
    result = AgentWiki._overview_page_meta(
        {
            "id": "overview",
            "title": "Overview",
            "files": ["README.md", "tests/test_edit_iterate.py"],
        },
        [
            {
                "id": "evaluation-workflows",
                "title": "Evaluation Workflows",
                "files": ["bash/run_eval.sh"],
                "children": [],
            }
        ],
    )

    assert result["files"][:3] == [
        "README.md",
        "tests/test_edit_iterate.py",
        "bash/run_eval.sh",
    ]


def test_overview_symbol_roles_prefer_runtime_entrypoints_over_helpers():
    assert _overview_symbol_role_score("AgentRunner.run") > (
        _overview_symbol_role_score("compile_repo")
    )
    assert _overview_symbol_role_score("Server.dispatch_request") > (
        _overview_symbol_role_score("format_metadata")
    )
    assert _overview_symbol_role_score("AgentWiki.page") > (
        _overview_symbol_role_score("AgentWiki.outline")
    )


def test_overview_constraints_bind_topics_to_retrieved_evidence():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=5,
            symbol="README.md",
            kind="file",
            content="The repository builds indexes and serves a local Wiki.",
        ),
        EvidenceItem(
            id="E2",
            file="src/compiler/index_builders.py",
            start_line=1,
            end_line=10,
            symbol="IndexBuilderRegistry",
            kind="class",
            content="class IndexBuilderRegistry: pass",
        ),
    ]

    constraints = _plan_evidence_constraints(
        {
            "id": "overview",
            "major_topics": [
                {
                    "title": "Indexing",
                    "files": ["src/compiler/index_builders.py"],
                }
            ],
        },
        evidence,
        [
            RelationItem(
                id="R1",
                source="src/compiler/index_builders.py:IndexBuilderRegistry.build()",
                target="src/compiler/manifest.py:RepoManifest.save()",
                anchors=("src/compiler/index_builders.py:8",),
            )
        ],
    )

    assert "Indexing=E2" in constraints
    assert "E2 (`IndexBuilderRegistry`)" in constraints
    assert "R1 (`IndexBuilderRegistry.build()` -> `RepoManifest.save()`)" in constraints
    assert "R# relations may ground semantic architecture connections" in constraints
    assert "do not require prose call claims" in constraints
    assert "do not shorten a method to its owner class" in constraints
    assert "unselected topics must not receive placeholder sections" in constraints


def test_overview_constraints_guide_relation_free_topics_away_from_catalogs():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=5,
            symbol="README.md",
            kind="file",
            content="The repository formats values.",
        ),
        EvidenceItem(
            id="E2",
            file="include/format.h",
            start_line=1,
            end_line=20,
            symbol="format_value",
            kind="function",
            content="format_value applies precision and rounding rules",
        ),
    ]

    constraints = _plan_evidence_constraints(
        {
            "id": "overview",
            "major_topics": [{"title": "Formatting", "files": ["include/format.h"]}],
        },
        evidence,
        [],
    )

    assert "No static relation facts are available" in constraints
    assert "mechanism, contract, or responsibility" in constraints
    assert "leave callable inventories to child pages" in constraints


def test_parent_constraints_allocate_source_evidence_to_each_child():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/trace.py",
            start_line=1,
            end_line=10,
            symbol="WorkflowTracer.start_turn",
            kind="method",
            content="start_turn records a workflow turn",
        ),
        EvidenceItem(
            id="E2",
            file="src/logging.py",
            start_line=1,
            end_line=10,
            symbol="LoggingManager.get_logger",
            kind="method",
            content="get_logger returns a configured logger",
        ),
    ]

    constraints = _plan_evidence_constraints(
        {
            "id": "logging-and-tracing",
            "children": [
                {"title": "Workflow Tracing", "files": ["src/trace.py"]},
                {"title": "Logging Management", "files": ["src/logging.py"]},
            ],
        },
        evidence,
    )

    assert "Workflow Tracing=E1 (`WorkflowTracer.start_turn`)" in constraints
    assert "Logging Management=E2 (`LoggingManager.get_logger`)" in constraints
    assert "intro evidence alone does not count" in constraints


def test_page_quality_requires_supported_plan_coverage():
    plan = {
        "sections": [
            {"title": "Routing", "claims": [{"evidence": ["E1"]}]},
            {"title": "Storage", "claims": [{"evidence": ["E2"]}]},
            {"title": "Runtime", "claims": [{"evidence": ["E3"]}]},
        ]
    }
    sparse = (
        "The repository exposes a request path. [E1]\n\n"
        "## Routing\n\n"
        "The router dispatches source requests to the runtime. [E1]"
    )
    complete = (
        "The repository exposes indexed context to callers. [E1]\n\n"
        "## Routing\n\n"
        "The router dispatches source requests to the runtime. [E1]\n\n"
        "## Storage\n\n"
        "The index persists repository source records. [E2]\n\n"
        "## Runtime\n\n"
        "The runtime serves those records to clients. [E3]"
    )

    assert _page_quality_report(sparse, plan)["valid"] is False
    report = _page_quality_report(complete, plan)
    assert report["valid"] is True
    assert report["claim_coverage"] == 1.0


def test_page_quality_rejects_repeated_prose_blocks():
    plan = {
        "sections": [
            {"title": "Entry points", "claims": [{"evidence": ["E1"]}]},
            {"title": "Runtime", "claims": [{"evidence": ["E2"]}]},
            {"title": "Storage", "claims": [{"evidence": ["E3"]}]},
        ]
    }
    markdown = (
        "The repository compiles source into reusable indexes and serves them "
        "through a local developer Wiki. [E1]\n\n"
        "## Entry points\n\n"
        "The repository compiles source into reusable indexes, then serves "
        "those indexes through its local developer Wiki. [E1]\n\n"
        "## Runtime\n\n"
        "The runtime answers source-linked repository queries. [E2]\n\n"
        "## Storage\n\n"
        "The manifest records the available repository views. [E3]"
    )

    report = _page_quality_report(markdown, plan)

    assert report["valid"] is False
    assert report["duplicate_blocks"] == [[1, 2]]


def test_overview_quality_rejects_thin_sections():
    plan = {
        "sections": [
            {
                "title": title,
                "claims": [{"evidence": [first]}, {"evidence": [second]}],
            }
            for title, first, second in [
                ("Workflow", "E1", "E2"),
                ("Execution", "E3", "E4"),
                ("Subsystems", "E5", "E6"),
            ]
        ]
    }
    markdown = (
        "The repository serves indexed source to developers. [E1]\n\n"
        "## Workflow\n\n"
        "The CLI accepts a repository path. It starts the Wiki server after "
        "indexing the checkout. [E1] [E2]\n\n"
        "## Execution\n\n"
        "The compiler builds repository views. The runtime loads those views "
        "for incoming requests. [E3] [E4]\n\n"
        "## Subsystems\n\n"
        "The repository has subsystems. [E5] [E6]"
    )

    report = _page_quality_report(markdown, plan, require_dense_sections=True)

    assert report["valid"] is False
    assert report["thin_sections"] == ["Subsystems"]


def test_overview_quality_allows_distinct_fact_from_intro_evidence():
    plan = {
        "sections": [
            {"title": "Workflow", "claims": [{"evidence": ["E1"]}]},
            {"title": "Execution", "claims": [{"evidence": ["E2"]}]},
            {"title": "Subsystems", "claims": [{"evidence": ["E3"]}]},
        ]
    }
    markdown = (
        "The repository assembles source-backed context for coding agents and "
        "serves it through local interfaces. [E1]\n\n"
        "## Workflow\n\n"
        "A user supplies a checkout to the command-line entry point. The command "
        "then opens a local repository workspace for inspection. [E1]\n\n"
        "## Execution\n\n"
        "The compiler materializes searchable repository records. The runtime "
        "loads those records when a request arrives. [E2]\n\n"
        "## Subsystems\n\n"
        "The manifest records view provenance and status. The server uses that "
        "state to expose available repository capabilities. [E3]"
    )

    report = _page_quality_report(
        markdown,
        plan,
        require_dense_sections=True,
        require_narrative_novelty=True,
    )

    assert report["valid"] is True
    assert report["intro_only_sections"] == ["Workflow"]
    assert report["new_evidence_by_section"]["Workflow"] == []
    assert report["redundant_sections"] == []


def test_overview_quality_rejects_semantic_intro_repetition():
    plan = {
        "sections": [
            {"title": "Purpose Again", "claims": [{"evidence": ["E1"]}]},
            {"title": "Execution", "claims": [{"evidence": ["E2"]}]},
            {"title": "Runtime", "claims": [{"evidence": ["E3"]}]},
        ]
    }
    markdown = (
        "The repository compiles source into reusable indexed context and serves "
        "that context through a local Wiki. [E1]\n\n"
        "## Purpose Again\n\n"
        "The repository compiles source into reusable indexed context, then "
        "serves that context through its local Wiki. [E1]\n\n"
        "## Execution\n\n"
        "The compiler writes searchable repository views. It also records their "
        "state in a manifest. [E2]\n\n"
        "## Runtime\n\n"
        "The server loads repository views. It returns source-linked pages for "
        "incoming requests. [E3]"
    )

    report = _page_quality_report(
        markdown,
        plan,
        require_dense_sections=True,
        require_narrative_novelty=True,
    )

    assert report["valid"] is False
    assert report["redundant_sections"] == ["Purpose Again"]


def test_overview_quality_requires_cited_intro():
    plan = {
        "sections": [
            {"title": "Workflow", "claims": [{"evidence": ["E1"]}]},
            {"title": "Execution", "claims": [{"evidence": ["E2"]}]},
            {"title": "Runtime", "claims": [{"evidence": ["E3"]}]},
        ]
    }
    markdown = (
        "## Workflow\n\n"
        "The command accepts a repository path and prepares a local Wiki. [E1]\n\n"
        "## Execution\n\n"
        "The compiler writes searchable repository views and a manifest. [E2]\n\n"
        "## Runtime\n\n"
        "The server loads the views and returns source-linked pages. [E3]"
    )

    report = _page_quality_report(
        markdown,
        plan,
        require_dense_sections=True,
        require_cited_intro=True,
    )

    assert report["valid"] is False
    assert report["cited_intro"] is False


def test_fact_plan_renderer_drops_unsupported_claims():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=10,
            symbol="README.md",
            kind="file",
            content=(
                "CodeNib builds a source-linked Wiki from a local repository "
                "and exposes indexed source context."
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/index.py",
            start_line=1,
            end_line=8,
            symbol="Indexer",
            kind="class",
            content=(
                "class Indexer:\n"
                "    def build(self, repository):\n"
                "        return repository.records()"
            ),
        ),
    ]
    markdown = _fact_plan_markdown(
        {
            "sections": [
                {
                    "title": "Indexing",
                    "claims": [
                        {
                            "statement": "The `Indexer` builds repository records",
                            "evidence": ["E2"],
                        }
                    ],
                },
                {
                    "title": "Invented",
                    "claims": [
                        {
                            "statement": "The powerful `MagicIndex` is universal",
                            "evidence": ["E1"],
                        }
                    ],
                },
            ]
        },
        evidence,
        [],
    )

    assert "## Indexing" in markdown
    assert "## Invented" not in markdown
    assert "`MagicIndex`" not in markdown


def test_fact_plan_renderer_admits_only_source_backed_framing():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/models.py",
            start_line=1,
            end_line=8,
            symbol="PreparedRequest.prepare_url",
            kind="method",
            content=(
                "class PreparedRequest:\n"
                "    def prepare_url(self, url):\n"
                "        # Normalize HTTP request URLs before parsing.\n"
                "        self.url = url\n"
                "        return self.url"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/sessions.py",
            start_line=1,
            end_line=6,
            symbol="Session.merge_environment_settings",
            kind="method",
            content=(
                "def merge_environment_settings(self, proxies):\n"
                "    # Merge environment settings with proxy defaults.\n"
                "    return merge_setting(proxies, self.proxies)"
            ),
        ),
        EvidenceItem(
            id="E3",
            file="src/adapters.py",
            start_line=1,
            end_line=6,
            symbol="HTTPAdapter.send",
            kind="method",
            content=(
                "class HTTPAdapter:\n"
                "    def send(self, prepared_request):\n"
                "        # Send the prepared request through the adapter.\n"
                "        return prepared_request"
            ),
        ),
    ]
    plan = {
        "purpose": {
            "statements": [
                "`PreparedRequest.prepare_url()` normalizes HTTP request URLs",
                "The user-friendly `RequestEncodingMixin` handles every body",
            ],
            "evidence": ["E1"],
        },
        "map": [
            {
                "concern": "HTTP request URL normalization",
                "entity": "`PreparedRequest.prepare_url()`",
                "evidence": ["E1"],
            },
            {
                "concern": "Environment settings and proxy defaults",
                "entity": "`Session.merge_environment_settings()`",
                "evidence": ["E2"],
            },
            {
                "concern": "Prepared request adapter sending",
                "entity": "`HTTPAdapter.send()`",
                "evidence": ["E3"],
            },
            {
                "concern": "Universal request routing",
                "entity": "`MagicGateway.route()`",
                "evidence": ["E1"],
            },
        ],
        "sections": [
            {
                "title": "URL preparation",
                "lead": {
                    "statements": [
                        "The prepared request stores its normalized URL",
                        "The `Authorization` helper retries every request",
                    ],
                    "evidence": ["E1"],
                },
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "`PreparedRequest.prepare_url()` normalizes HTTP "
                            "request URLs before parsing"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    rendered = _renderable_plan(plan, evidence, [])
    markdown = _fact_plan_markdown(rendered, evidence, [])

    assert rendered["purpose"]["statements"] == [
        "`PreparedRequest.prepare_url()` normalizes HTTP request URLs"
    ]
    assert len(rendered["map"]) == 3
    assert rendered["sections"][0]["lead"]["statements"] == [
        "The prepared request stores its normalized URL"
    ]
    assert "RequestEncodingMixin" not in markdown
    assert "MagicGateway" not in markdown
    assert "Authorization" not in markdown
    assert "user-friendly" not in markdown


def test_fact_plan_story_reorders_sections_and_renders_grounded_transitions():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/pipeline.py",
            start_line=1,
            end_line=8,
            symbol="Pipeline.accept",
            kind="method",
            content=(
                "def accept(self, request_payload):\n"
                "    # The normalization stage produces a normalized request "
                "payload.\n"
                "    return normalize(request_payload)"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/store.py",
            start_line=1,
            end_line=8,
            symbol="Store.write",
            kind="method",
            content=(
                "def write(self, normalized_payload):\n"
                "    # The persistence stage stores the normalized request "
                "payload.\n"
                "    self.storage.append(normalized_payload)"
            ),
        ),
    ]
    plan = {
        "thesis": {
            "statement": "The pipeline joins normalization and persistence stages",
            "evidence": ["E1", "E2"],
        },
        "story": {
            "origin": "planned",
            "reader_question": {
                "statement": (
                    "How does `Pipeline.accept()` move a request payload into "
                    "storage?"
                ),
                "evidence": ["E1", "E2"],
            },
            "beats": [
                {"section": "Accept input", "role": "entry"},
                {
                    "section": "Persist result",
                    "role": "outcome",
                    "transition": {
                        "statement": (
                            "The normalization stage hands its result to the "
                            "persistence stage"
                        ),
                        "evidence": ["E1", "E2"],
                    },
                },
            ],
        },
        # Deliberately reverse the facts: the story, not retrieval order,
        # controls the reader's path.
        "sections": [
            {
                "title": "Persist result",
                "claims": [
                    {
                        "role": "contract",
                        "statement": (
                            "`Store.write()` stores the normalized request payload"
                        ),
                        "evidence": ["E2"],
                    }
                ],
            },
            {
                "title": "Accept input",
                "claims": [
                    {
                        "role": "entry",
                        "statement": (
                            "`Pipeline.accept()` produces a normalized request "
                            "payload"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            },
        ],
    }

    rendered = _renderable_plan(plan, evidence, [])
    rendered = finalize_story_ir(rendered, available_ids=["E1", "E2"])
    markdown = _fact_plan_markdown(rendered, evidence, [])

    assert [section["title"] for section in rendered["sections"]] == [
        "Accept input",
        "Persist result",
    ]
    assert "**Reader question:**" in markdown
    assert markdown.index("## Accept input") < markdown.index("## Persist result")
    assert "*The normalization stage hands its result" in markdown
    assert rendered["story"]["evidence_budget"]["allocated_source_evidence"] == 2
    assert story_quality_report(rendered)["story_valid"] is True


def test_fact_plan_story_drops_a_transition_spliced_across_unrelated_sources():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/alpha.py",
            start_line=1,
            end_line=3,
            symbol="alpha",
            kind="function",
            content="def alpha():\n    return prepared_value",
        ),
        EvidenceItem(
            id="E2",
            file="src/beta.py",
            start_line=1,
            end_line=3,
            symbol="beta",
            kind="function",
            content=(
                "def beta(prepared_value):\n"
                "    # Store a prepared value.\n"
                "    storage.append(prepared_value)"
            ),
        ),
    ]
    plan = {
        "story": {
            "origin": "planned",
            "beats": [
                {"section": "Prepare", "role": "entry"},
                {
                    "section": "Store",
                    "role": "handoff",
                    "transition": {
                        "statement": "`alpha()` calls `beta()` with the prepared value",
                        "evidence": ["E1", "E2"],
                    },
                },
            ],
        },
        "sections": [
            {
                "title": "Prepare",
                "claims": [
                    {
                        "statement": "`alpha()` returns a prepared value",
                        "evidence": ["E1"],
                    }
                ],
            },
            {
                "title": "Store",
                "claims": [
                    {
                        "statement": "`beta()` stores a prepared value",
                        "evidence": ["E2"],
                    }
                ],
            },
        ],
    }

    rendered = _renderable_plan(plan, evidence, [])

    assert "transition" not in rendered["story"]["beats"][1]
    assert "calls `beta()`" not in _fact_plan_markdown(rendered, evidence, [])


def test_fact_plan_excerpt_caption_is_neutral_source_metadata():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/codec.py",
            start_line=1,
            end_line=8,
            symbol="encode_payload",
            kind="function",
            content=(
                "def encode_payload(value):\n"
                "    payload = value.encode('utf-8')\n"
                "    return payload"
            ),
        )
    ]
    markdown = _fact_plan_markdown(
        {
            "sections": [
                {
                    "title": "Payload encoding",
                    "excerpt": {
                        "evidence": "E1",
                        "why": "The function shows how values become bytes",
                    },
                    "claims": [
                        {
                            "role": "contract",
                            "statement": (
                                "`encode_payload()` encodes a value as UTF-8 bytes"
                            ),
                            "evidence": ["E1"],
                        }
                    ],
                }
            ]
        },
        evidence,
        [],
    )

    integrity = prose_integrity_report(markdown)

    assert "Source excerpt from `encode_payload`. [E1]" in markdown
    assert "`src/codec.py:1-8`" not in markdown
    assert "shows how values become bytes" not in markdown
    assert integrity["reference_narration_sentences"] == []
    assert integrity["prose_integrity_valid"] is True


def test_concise_overview_renderer_keeps_the_argument_not_the_scaffolding():
    evidence = [
        EvidenceItem(
            id="E0",
            file="README.md",
            start_line=1,
            end_line=2,
            symbol="README.md",
            kind="file",
            content="The project turns repository requests into source-linked results.",
        ),
        *[
            EvidenceItem(
                id=f"E{index}",
                file=f"src/stage_{index}.py",
                start_line=1,
                end_line=5,
                symbol=f"stage_{index}",
                kind="function",
                content=statement,
            )
            for index, statement in [
                (1, "stage_1 accepts the repository request and prepares it"),
                (2, "stage_2 transforms the prepared repository request"),
                (3, "stage_3 returns the source-linked result to the caller"),
            ]
        ],
    ]
    plan = {
        "thesis": {
            "statement": "The project turns repository requests into source-linked results",
            "evidence": ["E0"],
        },
        "purpose": {
            "statements": ["`stage_1` prepares repository requests"],
            "evidence": ["E1"],
        },
        "map": [
            {
                "concern": f"Stage {index}",
                "entity": f"`stage_{index}`",
                "evidence": [f"E{index}"],
            }
            for index in range(1, 4)
        ],
        "sections": [
            {
                "title": title,
                "excerpt": {"evidence": f"E{index}"},
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": statement,
                        "evidence": [f"E{index}"],
                    }
                ],
            }
            for index, title, statement in [
                (1, "The entry", "`stage_1` prepares the repository request"),
                (2, "The mechanism", "`stage_2` transforms the prepared request"),
                (3, "The result", "`stage_3` returns the source-linked result"),
            ]
        ],
    }

    markdown = _fact_plan_markdown(
        plan,
        evidence,
        [],
        concise_overview=True,
    )

    assert "## Purpose and scope" not in markdown
    assert "## At a glance" not in markdown
    assert "Source excerpt" not in markdown
    assert "```" not in markdown
    assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
        "## The entry",
        "## The mechanism",
        "## The result",
    ]


def test_fact_plan_renderer_drops_a_lead_that_repeats_its_claim():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/sessions.py",
            start_line=1,
            end_line=8,
            symbol="Session.merge_environment_settings",
            kind="method",
            content=(
                "def merge_environment_settings(self, proxies, verify):\n"
                "    # Collect environment settings and combine session defaults.\n"
                "    proxies = merge_setting(proxies, self.proxies)\n"
                "    verify = merge_setting(verify, self.verify)\n"
                "    return proxies, verify"
            ),
        )
    ]
    markdown = _fact_plan_markdown(
        {
            "sections": [
                {
                    "title": "Environment settings",
                    "lead": {
                        "statements": [
                            "`Session.merge_environment_settings()` collects "
                            "environment settings and combines session defaults"
                        ],
                        "evidence": ["E1"],
                    },
                    "claims": [
                        {
                            "role": "responsibility",
                            "statement": (
                                "`Session.merge_environment_settings()` calls "
                                "`merge_setting()` to combine environment "
                                "settings with session defaults"
                            ),
                            "evidence": ["E1"],
                        }
                    ],
                }
            ]
        },
        evidence,
        [],
    )

    assert markdown.count("combines session defaults") == 0
    assert markdown.count("combine environment settings with session defaults") == 1


def test_fact_plan_rejects_behavior_not_supported_by_the_cited_body():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/wiki.py",
            start_line=10,
            end_line=18,
            symbol="AgentWiki._find",
            kind="method",
            content=(
                "def _find(self, page_identifier):\n"
                "    return next(page for page in self.pages "
                "if page.id == page_identifier)"
            ),
        )
    ]
    false_statement = "`AgentWiki._find()` identifies relevant repository evidence"
    true_statement = "`AgentWiki._find()` returns a page by its page identifier"
    plan = {
        "thesis": {
            "statement": "Wiki page lookup uses page identifiers",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Lookup",
                "claims": [
                    {"statement": false_statement, "evidence": ["E1"]},
                    {"statement": true_statement, "evidence": ["E1"]},
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "wiki-agent", "title": "Wiki Agent"},
        plan,
        evidence,
    )
    markdown = _fact_plan_markdown(plan, evidence, [])

    assert any(
        false_statement in warning and "not supported by concrete terms" in warning
        for warning in warnings
    )
    assert false_statement not in markdown
    assert true_statement in markdown


def test_plan_support_promotes_an_admitted_claim_when_thesis_is_unsupported():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/wiki.py",
            start_line=10,
            end_line=18,
            symbol="AgentWiki.page",
            kind="method",
            content=(
                "def page(self, page_identifier):\n"
                "    return next(page for page in self.pages "
                "if page.id == page_identifier)"
            ),
        )
    ]
    supported = "`AgentWiki.page()` returns a page by its page identifier"
    plan = {
        "thesis": {
            "statement": "This page documents intelligent repository search",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Lookup",
                "claims": [
                    {
                        "role": "contract",
                        "statement": supported,
                        "evidence": ["E1"],
                    },
                    {
                        "role": "component",
                        "statement": "`AgentWiki.page()` returns the matching page",
                        "evidence": ["E1"],
                    },
                ],
            }
        ],
    }

    normalized = _normalize_plan_support(plan, evidence, [])
    markdown = _fact_plan_markdown(normalized, evidence, [])
    warnings = _plan_quality_warnings(
        {"id": "wiki-agent", "title": "Wiki Agent"},
        normalized,
        evidence,
    )

    assert normalized["thesis"] == {
        "statement": supported,
        "evidence": ["E1"],
    }
    assert markdown.startswith(f"{supported}. [E1]")
    assert markdown.count(supported) == 1
    assert not any("thesis" in warning for warning in warnings)


def test_fact_plan_renderer_drops_claims_repeated_by_readme_intro():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=10,
            symbol="README.md",
            kind="file",
            content=(
                "The repository compiles source into reusable indexes and serves "
                "them through a local developer Wiki."
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/cli.py",
            start_line=1,
            end_line=8,
            symbol="main",
            kind="function",
            content="def main(): return launch_cli()",
        ),
    ]
    markdown = _fact_plan_markdown(
        {
            "sections": [
                {
                    "title": "Purpose",
                    "claims": [
                        {
                            "statement": (
                                "The repository compiles source into reusable "
                                "indexes and serves them through a developer Wiki"
                            ),
                            "evidence": ["E1"],
                        }
                    ],
                },
                {
                    "title": "Entry point",
                    "claims": [
                        {
                            "statement": "The `main` function launches the CLI",
                            "evidence": ["E2"],
                        }
                    ],
                },
            ]
        },
        evidence,
        [],
    )

    assert "## Purpose" not in markdown
    assert "## Entry point" in markdown
    assert markdown.count("compiles source into reusable indexes") == 1


def test_fact_plan_renderer_deduplicates_fallback_intro():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/runtime.py",
            start_line=1,
            end_line=8,
            symbol="run",
            kind="function",
            content="def run(): return Runtime.start()",
        ),
        EvidenceItem(
            id="E2",
            file="src/runtime.py",
            start_line=10,
            end_line=18,
            symbol="load_config",
            kind="function",
            content="def load_config(): return read_runtime_configuration()",
        ),
    ]
    first = "The `run` function starts the runtime."
    markdown = _fact_plan_markdown(
        {
            "sections": [
                {
                    "title": "Runtime",
                    "claims": [
                        {"statement": first, "evidence": ["E1"]},
                        {
                            "statement": (
                                "The `load_config` function reads the runtime "
                                "configuration before requests are served"
                            ),
                            "evidence": ["E2"],
                        },
                    ],
                }
            ]
        },
        evidence,
        [],
    )

    assert markdown.count(first) == 1
    assert "## Runtime" in markdown
    assert "`load_config`" in markdown


def test_renderable_plan_aligns_intro_deduplication_with_quality_gate():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/runtime.py",
            start_line=1,
            end_line=8,
            symbol="run",
            kind="function",
            content="def run(): return Runtime.start()",
        ),
        EvidenceItem(
            id="E2",
            file="src/runtime.py",
            start_line=10,
            end_line=18,
            symbol="load_config",
            kind="function",
            content="def load_config(): return read_runtime_configuration()",
        ),
    ]
    thesis = "The `run` function returns `Runtime.start()` to start the runtime"
    plan = {
        "thesis": {"statement": thesis, "evidence": ["E1"]},
        "sections": [
            {
                "title": "Startup",
                "claims": [
                    {
                        "role": "entry",
                        "statement": thesis,
                        "evidence": ["E1"],
                    }
                ],
            },
            {
                "title": "Configuration",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "The `load_config` function reads the runtime "
                            "configuration"
                        ),
                        "evidence": ["E2"],
                    }
                ],
            },
        ],
    }

    render_plan = _renderable_plan(plan, evidence, [])
    markdown = _fact_plan_markdown(render_plan, evidence, [])
    quality = _page_quality_report(
        markdown,
        render_plan,
        require_cited_intro=True,
        require_grounded_thesis=True,
        evidence_items=evidence,
    )

    assert [section["title"] for section in render_plan["sections"]] == [
        "Configuration"
    ]
    assert markdown.count(thesis) == 1
    assert quality["planned_sections"] == quality["rendered_sections"] == 1
    assert quality["planned_claims"] == quality["covered_claims"] == 1
    assert quality["valid"] is True


def test_renderable_plan_deduplicates_claims_across_overview_sections():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/client.py",
            start_line=1,
            end_line=12,
            symbol="Client.send",
            kind="method",
            content=(
                "def send(self, request):\n"
                "    prepared = self.prepare(request)\n"
                "    return self.adapter.dispatch(prepared)"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/client.py",
            start_line=1,
            end_line=12,
            symbol="Client.send",
            kind="method",
            content=(
                "def send(self, request):\n"
                "    prepared = self.prepare(request)\n"
                "    return self.adapter.dispatch(prepared)"
            ),
        ),
    ]
    repeated = (
        "`Client.send()` prepares an incoming request and dispatches the "
        "prepared request through its configured adapter"
    )
    plan = {
        "thesis": {
            "statement": "The client coordinates HTTP request execution",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Client requests",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": repeated,
                        "evidence": ["E1"],
                    }
                ],
            },
            {
                "title": "Adapter dispatch",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": repeated,
                        "evidence": ["E2"],
                    }
                ],
            },
        ],
    }

    rendered = _renderable_plan(plan, evidence, [])

    assert [section["title"] for section in rendered["sections"]] == ["Client requests"]


def test_dense_plan_compaction_drops_thin_sections_but_keeps_three_areas():
    plan = {
        "sections": [
            {"title": "Core", "claims": [{"evidence": ["E1"]}]},
            {"title": "Compute", "claims": [{"evidence": ["E2"]}]},
            {"title": "Grouping", "claims": [{"evidence": ["E3"]}]},
            {"title": "Parallel", "claims": [{"evidence": ["E4"]}]},
        ]
    }

    compacted = _compact_dense_plan(
        plan,
        {"thin_sections": ["Parallel"], "redundant_sections": []},
    )

    assert [section["title"] for section in compacted["sections"]] == [
        "Core",
        "Compute",
        "Grouping",
    ]
    assert len(plan["sections"]) == 4


def test_renderable_plan_keeps_specific_claims_for_distinct_named_scripts():
    evidence = [
        EvidenceItem(
            id="E1",
            file="bash/run_sgl_fe_bc_eval.sh",
            start_line=1,
            end_line=20,
            symbol="bash/run_sgl_fe_bc_eval.sh",
            kind="file",
            content=(
                "python generate_task_strings.py\n" "python sgl_test_bigcodebench.py"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="bash/run_lc_bc_eval.sh",
            start_line=1,
            end_line=20,
            symbol="bash/run_lc_bc_eval.sh",
            kind="file",
            content=(
                "python generate_task_strings.py\n" "python lc_test_bigcodebench.py"
            ),
        ),
    ]
    plan = {
        "thesis": {
            "statement": "The evaluation scripts generate and execute tasks",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "SGL Evaluation",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "The `bash/run_sgl_fe_bc_eval.sh` script generates "
                            "task IDs and runs the SGL BigCodeBench experiment"
                        ),
                        "evidence": ["E1"],
                    },
                    {
                        "role": "flow",
                        "statement": (
                            "The `bash/run_sgl_fe_bc_eval.sh` script generates "
                            "task IDs using `generate_task_strings.py` and "
                            "executes them with `sgl_test_bigcodebench.py`"
                        ),
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "LangChain Evaluation",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "The `bash/run_lc_bc_eval.sh` script generates task "
                            "IDs and runs the LangChain BigCodeBench experiment"
                        ),
                        "evidence": ["E2"],
                    },
                    {
                        "role": "flow",
                        "statement": (
                            "The `bash/run_lc_bc_eval.sh` script generates task "
                            "IDs using `generate_task_strings.py` and executes "
                            "them with `lc_test_bigcodebench.py`"
                        ),
                        "evidence": ["E2"],
                    },
                ],
            },
        ],
    }

    rendered = _renderable_plan(plan, evidence, [])

    assert [
        [claim["statement"] for claim in section["claims"]]
        for section in rendered["sections"]
    ] == [
        [
            "The `bash/run_sgl_fe_bc_eval.sh` script generates task IDs using "
            "`generate_task_strings.py` and executes them with "
            "`sgl_test_bigcodebench.py`"
        ],
        [
            "The `bash/run_lc_bc_eval.sh` script generates task IDs using "
            "`generate_task_strings.py` and executes them with "
            "`lc_test_bigcodebench.py`"
        ],
    ]


def test_overview_plan_requires_dense_page_wide_evidence():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=None,
            end_line=None,
            symbol="README.md",
            kind="file",
            content="The repository serves indexed source through a local Wiki.",
        ),
        EvidenceItem(
            id="E2",
            file="src/cli.py",
            start_line=None,
            end_line=None,
            symbol="main",
            kind="function",
            content=(
                "def main(repository_path):\n"
                "    request = RepositoryRequest(repository_path)\n"
                "    return CLI(request)"
            ),
        ),
        EvidenceItem(
            id="E3",
            file="src/compiler.py",
            start_line=None,
            end_line=None,
            symbol="Compiler",
            kind="class",
            content=(
                "class Compiler:\n"
                "    def record(self, repository_indexes):\n"
                "        return repository_indexes"
            ),
        ),
        EvidenceItem(
            id="E4",
            file="src/server.py",
            start_line=None,
            end_line=None,
            symbol="Server",
            kind="class",
            content=(
                "class Server:\n"
                "    def page(self, wiki_request):\n"
                "        return SourceLinkedWikiPage(wiki_request)"
            ),
        ),
    ]
    sparse = {
        "sections": [
            {
                "title": title,
                "claims": [{"statement": statement, "evidence": [evidence_id]}],
            }
            for title, statement, evidence_id in [
                ("Workflow", "The CLI accepts a repository path", "E2"),
                ("Flow", "The compiler creates repository indexes", "E3"),
                ("Subsystems", "The server returns Wiki pages", "E4"),
            ]
        ]
    }
    dense = {
        "thesis": {
            "statement": "The repository serves indexed source through a local Wiki",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The CLI accepts a repository path",
                        "evidence": ["E2"],
                    },
                ],
            },
            {
                "title": "Flow",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The compiler records repository indexes",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Subsystems",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The server returns Wiki pages",
                        "evidence": ["E4"],
                    },
                    {
                        "role": "contract",
                        "statement": "Wiki requests return source-linked pages",
                        "evidence": ["E4"],
                    },
                ],
            },
        ],
    }
    _add_overview_architecture(dense)
    meta = {"id": "overview"}

    assert _plan_quality_warnings(meta, sparse, evidence)
    assert _plan_quality_warnings(meta, dense, evidence) == []


def test_overview_density_counts_the_rendered_thesis():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (
                1,
                "README.md",
                "The command builds a source-linked Wiki for a repository.",
            ),
            (
                2,
                "src/index.py",
                "The indexer records searchable source units and returns them.",
            ),
            (
                3,
                "src/wiki.py",
                "The Wiki serves generated pages and preserves source links.",
            ),
            (
                4,
                "src/runtime.py",
                "The runtime executes requests and returns bounded context.",
            ),
            (
                5,
                "src/graph.py",
                "The graph resolves symbols and returns source locations.",
            ),
        ]
    ]
    plan = {
        "thesis": {
            "statement": "The command builds a source-linked Wiki for a repository",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Indexing",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The indexer records searchable source units",
                        "evidence": ["E2"],
                    }
                ],
            },
            {
                "title": "Wiki Serving",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The Wiki serves generated pages",
                        "evidence": ["E3"],
                    },
                    {
                        "role": "contract",
                        "statement": "The Wiki preserves source links",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Agent Runtime",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The runtime executes requests",
                        "evidence": ["E4"],
                    },
                    {
                        "role": "contract",
                        "statement": "The runtime returns bounded context",
                        "evidence": ["E4"],
                    },
                ],
            },
            {
                "title": "Graph Navigation",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The graph resolves symbols",
                        "evidence": ["E5"],
                    },
                    {
                        "role": "contract",
                        "statement": "The graph returns source locations",
                        "evidence": ["E5"],
                    },
                ],
            },
        ],
    }

    warnings = _plan_quality_warnings({"id": "overview"}, plan, evidence)

    assert not any(
        warning.startswith("Overview needs at least 8 supported narrative facts")
        for warning in warnings
    )


def test_overview_fact_minimum_tracks_editorial_floor_not_topic_count():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (1, "README.md", "The project coordinates four repository workflows."),
            (2, "src/env.py", "The environment runner executes benchmark commands."),
            (3, "src/editor.py", "The editor creates repository patches."),
            (4, "tests/evaluate.py", "The evaluation script runs benchmark cases."),
            (5, "src/trace.py", "The tracer records each workflow turn."),
        ]
    ]
    topics = [
        ("Environment", "src/env.py", "The environment runner executes commands", "E2"),
        ("Editing", "src/editor.py", "The editor creates patches", "E3"),
        ("Evaluation", "tests/evaluate.py", "The evaluation script runs cases", "E4"),
        ("Tracing", "src/trace.py", "The tracer records workflow turns", "E5"),
    ]
    plan = {
        "thesis": {
            "statement": "The project coordinates four repository workflows",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": title,
                "claims": [
                    {
                        "role": "entry",
                        "statement": statement,
                        "evidence": [evidence_id],
                    }
                ],
            }
            for title, _file, statement, evidence_id in topics
        ],
    }
    meta = {
        "id": "overview",
        "major_topics": [
            {"title": title, "files": [file]} for title, file, _statement, _id in topics
        ],
    }

    warnings = _plan_quality_warnings(meta, plan, evidence)
    no_thesis = {**plan, "thesis": {"statement": "", "evidence": []}}
    sparse_warnings = _plan_quality_warnings(meta, no_thesis, evidence)

    assert not any(
        warning.startswith("Overview needs at least") for warning in warnings
    )
    assert "page thesis must be a source-grounded claim" in sparse_warnings
    assert not any(
        "every planned or allocated topic" in warning for warning in sparse_warnings
    )


def test_overview_does_not_expand_a_topic_to_consume_multiple_sources():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=4,
            symbol="README.md",
            kind="file",
            content="The project formats text.",
        ),
        EvidenceItem(
            id="E2",
            file="include/fmt/chrono.h",
            start_line=10,
            end_line=15,
            symbol="run",
            kind="function",
            content="run calls handle",
        ),
        EvidenceItem(
            id="E3",
            file="include/fmt/chrono.h",
            start_line=20,
            end_line=25,
            symbol="fallback",
            kind="function",
            content="fallback calls gmtime_s",
        ),
    ]
    plan = {
        "thesis": {
            "statement": "The project formats text",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Chrono Support",
                "claims": [
                    {
                        "role": "flow",
                        "statement": "`run` calls `handle`",
                        "evidence": ["E2"],
                    }
                ],
            }
        ],
    }
    meta = {
        "id": "overview",
        "major_topics": [
            {"title": "Chrono Support", "files": ["include/fmt/chrono.h"]}
        ],
    }

    warnings = _plan_quality_warnings(meta, plan, evidence)

    assert not any("two complementary supported facts" in item for item in warnings)


def test_overview_accepts_one_substantive_fact_for_an_allocated_topic():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=4,
            symbol="README.md",
            kind="file",
            content="The project records workflow traces.",
        ),
        EvidenceItem(
            id="E2",
            file="src/trace.py",
            start_line=10,
            end_line=20,
            symbol="WorkflowTracer.start_turn",
            kind="method",
            content="start_turn records a prompt for the new tracing turn",
        ),
        EvidenceItem(
            id="E3",
            file="src/trace.py",
            start_line=30,
            end_line=35,
            symbol="create_tracer",
            kind="function",
            content="create_tracer returns WorkflowTracer",
        ),
    ]
    plan = {
        "thesis": {
            "statement": "The project records workflow traces",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Logging and Tracing",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "`WorkflowTracer.start_turn` begins a new tracing "
                            "turn and records the prompt used"
                        ),
                        "evidence": ["E2"],
                    }
                ],
            }
        ],
    }
    meta = {
        "id": "overview",
        "major_topics": [{"title": "Logging and Tracing", "files": ["src/trace.py"]}],
    }

    warnings = _plan_quality_warnings(meta, plan, evidence)

    assert not any(
        "needs two complementary supported facts" in item for item in warnings
    )


def test_overview_plan_allows_one_source_for_a_cohesive_section():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (
                1,
                "docs/workflow.md",
                "The Wiki accepts a repository path and builds indexes.",
            ),
            (2, "src/cli.py", "The CLI starts command execution through the compiler."),
            (3, "src/compiler.py", "The compiler writes a manifest."),
            (4, "src/server.py", "The server returns Wiki pages."),
        ]
    ]
    plan = {
        "thesis": {
            "statement": "The Wiki builds indexes for repository requests",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The Wiki accepts a repository path",
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Execution",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The compiler writes a manifest",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Runtime",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The server returns Wiki pages",
                        "evidence": ["E4"],
                    },
                    {
                        "role": "contract",
                        "statement": "Wiki requests return Markdown pages",
                        "evidence": ["E4"],
                    },
                ],
            },
        ],
    }
    _add_overview_architecture(plan)

    assert _plan_quality_warnings({"id": "overview"}, plan, evidence) == []


def test_overview_plan_allows_distinct_readme_facts():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (
                1,
                "README.md",
                "The project builds a source-linked repository Wiki from local "
                "source code for coding agents.",
            ),
            (2, "src/cli.py", "The CLI accepts a repository path."),
            (3, "src/compiler.py", "The compiler writes an index manifest."),
            (4, "src/server.py", "The server returns Wiki pages."),
        ]
    ]
    plan = {
        "thesis": {
            "statement": "The project builds a source-linked repository Wiki",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The CLI accepts a path",
                        "evidence": ["E2"],
                    },
                ],
            },
            {
                "title": "Execution",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The compiler writes a manifest",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Runtime",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The server returns pages",
                        "evidence": ["E4"],
                    },
                    {
                        "role": "contract",
                        "statement": "Wiki requests receive server pages",
                        "evidence": ["E4"],
                    },
                ],
            },
        ],
    }
    _add_overview_architecture(plan)

    warnings = _plan_quality_warnings({"id": "overview"}, plan, evidence)

    assert warnings == []


def test_overview_plan_allows_core_source_reuse_for_distinct_claims():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (1, "docs/workflow.md", "The Wiki accepts and indexes a repository."),
            (2, "src/cli.py", "The CLI starts command execution through the compiler."),
            (3, "src/compiler.py", "The compiler writes a manifest."),
            (4, "src/server.py", "The server returns Wiki pages."),
        ]
    ]
    plan = {
        "thesis": {
            "statement": "The Wiki accepts and indexes a repository",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The Wiki accepts a repository",
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Execution",
                "claims": [
                    {
                        "role": "component",
                        "statement": "The CLI starts command execution",
                        "evidence": ["E2"],
                    },
                ],
            },
            {
                "title": "Subsystems",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The CLI starts the compiler",
                        "evidence": ["E2"],
                    },
                    {
                        "role": "contract",
                        "statement": "The compiler owns the manifest",
                        "evidence": ["E3"],
                    },
                ],
            },
        ],
    }
    _add_overview_architecture(plan)

    warnings = _plan_quality_warnings({"id": "overview"}, plan, evidence)

    assert warnings == []


def test_overview_plan_may_leave_a_major_topic_to_its_child_page():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (1, "README.md", "The project serves indexed source."),
            (2, "src/index.py", "The index builder stores source units."),
            (3, "src/wiki.py", "The Wiki returns source-linked pages."),
            (4, "src/agent.py", "The agent runtime processes queries."),
        ]
    ]
    plan = {
        "thesis": {
            "statement": "The project serves indexed source",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Indexing",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The command accepts a repository",
                        "evidence": ["E1"],
                    },
                    {
                        "role": "responsibility",
                        "statement": "The index builder stores source units",
                        "evidence": ["E2"],
                    },
                ],
            },
            {
                "title": "Wiki Serving",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The Wiki returns source-linked pages",
                        "evidence": ["E3"],
                    }
                ],
            },
            {
                "title": "Workflow Integration",
                "claims": [
                    {
                        "role": "component",
                        "statement": "The project includes an agent runtime",
                        "evidence": ["E4"],
                    }
                ],
            },
        ],
    }
    meta = {
        "id": "overview",
        "major_topics": [
            {"title": "Indexing", "files": ["src/index.py"]},
            {"title": "Wiki Serving", "files": ["src/wiki.py"]},
            {"title": "Agent Runtime", "files": ["src/agent.py"]},
        ],
    }

    warnings = _plan_quality_warnings(meta, plan, evidence)

    assert not any("Agent Runtime" in warning for warning in warnings)


def test_overview_plan_does_not_require_a_prose_relation():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=1,
            end_line=10,
            symbol=symbol,
            kind="function",
            content=content,
        )
        for index, file, symbol, content in [
            (1, "README.md", "README.md", "The project serves indexed source."),
            (
                2,
                "src/index.py",
                "search",
                "def search(query): return retrieve(query)",
            ),
            (
                3,
                "src/wiki.py",
                "page",
                "def page(page_id): return generate_page(page_id)",
            ),
            (
                4,
                "src/agent.py",
                "run",
                "def run(query): return compile_query(query)",
            ),
        ]
    ]
    plan = {
        "thesis": {
            "statement": "The project serves indexed source",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Indexing",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The search function accepts a query",
                        "evidence": ["E2"],
                    },
                    {
                        "role": "responsibility",
                        "statement": "The search function returns results",
                        "evidence": ["E2"],
                    },
                ],
            },
            {
                "title": "Wiki Serving",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The page function accepts a page ID",
                        "evidence": ["E3"],
                    },
                    {
                        "role": "responsibility",
                        "statement": "The page function returns content",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Agent Runtime",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The run function accepts a query",
                        "evidence": ["E4"],
                    },
                    {
                        "role": "responsibility",
                        "statement": "The run function compiles the query",
                        "evidence": ["E4"],
                    },
                ],
            },
        ],
    }
    relations = [
        RelationItem(
            id="R0",
            source="examples/agent.py:run_agent()",
            target="src/index.py:Registry.register()",
        ),
        RelationItem(
            id="R1",
            source="src/agent.py:run()",
            target="src/agent.py:compile_query()",
            anchors=("src/agent.py:2",),
        ),
    ]
    meta = {
        "id": "overview",
        "major_topics": [
            {"title": "Agent Runtime", "files": ["src/agent.py"]},
        ],
    }

    warnings = _plan_quality_warnings(meta, plan, evidence, relations)

    assert not any("allocated relation" in warning for warning in warnings)
    assert not any("supported component handoff" in warning for warning in warnings)
    assert any("semantic architecture plan" in warning for warning in warnings)


def test_overview_does_not_supplement_an_allocated_callable_relation():
    plan = {
        "sections": [
            {
                "title": "Agent Runtime",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "`run()` accepts a query",
                        "evidence": ["E1"],
                    },
                    {
                        "role": "responsibility",
                        "statement": "`run()` processes the query",
                        "evidence": ["E1"],
                    },
                ],
            }
        ]
    }
    relation = RelationItem(
        id="R1",
        source="src/agent.py:run()",
        target="src/agent.py:compile_query()",
        anchors=("src/agent.py:2",),
    )

    supplemented = _supplement_topic_relation_flows(
        {
            "id": "overview",
            "major_topics": [
                {
                    "title": "Agent Runtime",
                    "files": ["src/agent.py"],
                }
            ],
        },
        plan,
        [relation],
    )

    assert supplemented == plan


def test_overview_removes_an_existing_callable_handoff():
    plan = {
        "sections": [
            {
                "title": "Editor Tools",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "`Editor.create_patch()` creates a patch",
                        "evidence": ["E1"],
                    },
                    {
                        "role": "flow",
                        "statement": (
                            "`Editor.revise_bug()` uses `_edit_with_new_code()` "
                            "to update a snippet"
                        ),
                        "evidence": ["E2"],
                    },
                ],
            }
        ]
    }
    relation = RelationItem(
        id="R1",
        source="src/editor.py:Editor.revise_bug()",
        target="src/editor.py:Editor._edit_with_new_code()",
        anchors=("src/editor.py:12",),
    )

    supplemented = _supplement_topic_relation_flows(
        {
            "id": "overview",
            "major_topics": [{"title": "Editor Tools", "files": ["src/editor.py"]}],
        },
        plan,
        [relation],
    )

    assert supplemented["sections"][0]["claims"] == [
        {
            "role": "responsibility",
            "statement": "`Editor.create_patch()` creates a patch",
            "evidence": ["E1"],
        }
    ]


def test_parent_page_supplements_a_relation_in_its_source_section():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/tracing.py",
            start_line=10,
            end_line=20,
            symbol="Tracer.end_turn",
            kind="method",
            content="def end_turn(self): return self.turn.duration()",
        ),
        EvidenceItem(
            id="E2",
            file="src/logging.py",
            start_line=1,
            end_line=8,
            symbol="LoggingManager.get_logger",
            kind="method",
            content="def get_logger(self): pass",
        ),
    ]
    relation = RelationItem(
        id="R1",
        source="src/tracing.py:Tracer.end_turn()",
        target="src/tracing.py:Turn.duration()",
        anchors=("src/tracing.py:12",),
    )
    plan = {
        "sections": [
            {
                "title": "Tracing",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "`Tracer.end_turn()` records the turn",
                        "evidence": ["E1"],
                    }
                ],
            },
            {
                "title": "Logging",
                "claims": [
                    {
                        "role": "component",
                        "statement": "`LoggingManager.get_logger()` returns a logger",
                        "evidence": ["E2"],
                    }
                ],
            },
        ]
    }

    supplemented = _supplement_topic_relation_flows(
        {"id": "logging-and-tracing"},
        plan,
        [relation],
        evidence,
    )

    assert supplemented["sections"][0]["claims"][-1] == {
        "role": "flow",
        "statement": "`Tracer.end_turn()` calls `Turn.duration()`",
        "evidence": ["R1"],
    }
    assert len(supplemented["sections"][1]["claims"]) == 1


def test_parent_page_does_not_attach_an_unrelated_relation():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/runtime.py",
            start_line=1,
            end_line=4,
            symbol="Runtime.run",
            kind="method",
            content="def run(self): pass",
        )
    ]
    plan = {
        "sections": [
            {
                "title": "Runtime",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "`Runtime.run()` handles a request",
                        "evidence": ["E1"],
                    }
                ],
            }
        ]
    }
    relation = RelationItem(
        id="R1",
        source="src/storage.py:Store.load()",
        target="src/storage.py:Store.read()",
    )

    supplemented = _supplement_topic_relation_flows(
        {"id": "runtime"},
        plan,
        [relation],
        evidence,
    )

    assert supplemented == plan


def test_overview_does_not_create_a_section_only_to_cover_relations():
    relations = [
        RelationItem(
            id="R1",
            source="src/index.py:register_builders()",
            target="src/index.py:Registry.register()",
        ),
        RelationItem(
            id="R2",
            source="src/index.py:register_builders()",
            target="src/index.py:default_filters()",
        ),
    ]

    supplemented = _supplement_topic_relation_flows(
        {
            "id": "overview",
            "major_topics": [
                {
                    "title": "Indexing",
                    "files": ["src/index.py"],
                }
            ],
        },
        {"sections": []},
        relations,
    )

    assert supplemented["sections"] == []


def test_overview_plan_rejects_private_helper_as_user_entrypoint():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (1, "src/cli.py", "def _run_wiki(): pass"),
            (2, "src/compiler.py", "class Compiler: pass"),
            (3, "src/server.py", "class Server: pass"),
            (4, "src/runtime.py", "class Runtime: pass"),
        ]
    ]
    plan = {
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "statement": "Users execute `_run_wiki()` to build a Wiki",
                        "evidence": ["E1"],
                    },
                    {
                        "statement": "The CLI starts repository processing",
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Build",
                "claims": [
                    {"statement": "The compiler writes views", "evidence": ["E2"]},
                    {"statement": "The compiler writes state", "evidence": ["E2"]},
                ],
            },
            {
                "title": "Serve",
                "claims": [
                    {"statement": "The server returns pages", "evidence": ["E3"]},
                    {"statement": "The runtime loads views", "evidence": ["E4"]},
                ],
            },
        ]
    }

    warnings = _plan_quality_warnings({"id": "overview"}, plan, evidence)

    assert (
        "section 'Workflow' describes a private helper as a user entry point"
        in warnings
    )


def test_page_plan_rejects_private_helper_as_public_entrypoint():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/wiki.py",
            start_line=10,
            end_line=20,
            symbol="AgentWiki._retrieve",
            kind="method",
            content=(
                "def _retrieve(self):\n"
                '    """Obtain indexed source candidates for Wiki page planning."""\n'
                "    return self.candidates"
            ),
        )
    ]
    plan = {
        "thesis": {
            "statement": "Wiki pages are planned from indexed source evidence",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Public Entry Points",
                "claims": [
                    {
                        "statement": "`AgentWiki._retrieve()` obtains candidates",
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "wiki-serving"},
        plan,
        evidence,
    )

    assert warnings == [
        "section 'Public Entry Points' describes a private helper as a user "
        "entry point"
    ]


def test_page_plan_allows_private_work_in_a_mixed_entry_section():
    evidence = [
        EvidenceItem(
            id="E1",
            file="core/resample.py",
            start_line=10,
            end_line=20,
            symbol="DataWithCoords._resample",
            kind="method",
            content="def _resample(self): return Resampler(self)",
        )
    ]
    plan = {
        "sections": [
            {
                "title": "Entry Points and Shared Construction",
                "claims": [
                    {
                        "statement": (
                            "`DataWithCoords._resample()` constructs the "
                            "resample object"
                        ),
                        "role": "responsibility",
                        "evidence": ["E1"],
                    }
                ],
            }
        ]
    }

    warnings = _plan_quality_warnings(
        {"id": "resampling"},
        plan,
        evidence,
    )

    assert not any("private helper" in warning for warning in warnings)


def test_page_plan_allows_a_public_entry_to_delegate_to_a_private_helper():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/cli.py",
            start_line=10,
            end_line=20,
            symbol="main",
            kind="function",
            content=(
                "def main(repository_state):\n" "    return _prepare(repository_state)"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/cli.py",
            start_line=22,
            end_line=30,
            symbol="_prepare",
            kind="function",
            content="def _prepare(repository_state): return repository_state",
        ),
    ]
    relations = [
        RelationItem(
            id="R1", source="main", target="_prepare", anchors=("src/cli.py:11",)
        )
    ]
    plan = {
        "thesis": {
            "statement": "The command prepares repository state",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Public Workflow",
                "claims": [
                    {
                        "role": "flow",
                        "statement": "`main` calls `_prepare` before serving",
                        "evidence": ["E1", "E2", "R1"],
                    }
                ],
            },
            {
                "title": "Preparation",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "`_prepare(repository_state)` returns " "`repository_state`"
                        ),
                        "evidence": ["E2"],
                    }
                ],
            },
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "runtime", "title": "Runtime"},
        plan,
        evidence,
        relations,
    )

    assert warnings == []


def test_page_plan_requires_two_claims_when_multiple_sources_are_available():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/worker.py",
            start_line=1,
            end_line=5,
            symbol="Worker.run",
            kind="method",
            content=(
                "def run(self):\n"
                '    """Return the worker queue."""\n'
                "    return self.queue"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/queue.py",
            start_line=1,
            end_line=5,
            symbol="Queue",
            kind="class",
            content="class Queue: pass",
        ),
    ]
    plan = {
        "thesis": {
            "statement": "`Worker.run()` returns the worker queue",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Execution",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "`Worker.run()` returns the worker queue",
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "worker", "title": "Worker"},
        plan,
        evidence,
    )

    assert warnings == [
        "page needs at least two supported facts when multiple source "
        "evidence items are available"
    ]


def test_page_plan_counts_a_distinct_supported_thesis_as_a_fact():
    evidence = [
        EvidenceItem(
            id="E1",
            file="include/fmt/chrono.h",
            start_line=1,
            end_line=20,
            symbol="parse_chrono_format",
            kind="function",
            content="parse_chrono_format uses Handler to parse chrono fields",
        ),
        EvidenceItem(
            id="E2",
            file="include/fmt/chrono.h",
            start_line=21,
            end_line=45,
            symbol="format",
            kind="function",
            content="format converts sys_time and casts subseconds",
        ),
    ]
    plan = {
        "thesis": {
            "statement": (
                "`parse_chrono_format()` delegates chrono field parsing to " "`Handler`"
            ),
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Formatting",
                "claims": [
                    {
                        "role": "flow",
                        "statement": (
                            "`format()` converts `sys_time` and casts its "
                            "subsecond duration"
                        ),
                        "evidence": ["E2"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "chrono-support", "title": "Chrono Support"},
        plan,
        evidence,
    )

    assert not any("two supported facts" in warning for warning in warnings)


def test_page_plan_rejects_a_flow_label_without_a_component_handoff():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/graph.py",
            start_line=10,
            end_line=20,
            symbol="query_range",
            kind="function",
            content="def query_range(): return graph",
        ),
        EvidenceItem(
            id="E2",
            file="src/graph.py",
            start_line=1,
            end_line=2,
            symbol="graph",
            kind="field",
            content="graph = {}",
        ),
    ]
    relations = [RelationItem(id="R1", source="query_range", target="graph")]
    plan = {
        "thesis": {
            "statement": "The graph answers range queries",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Query",
                "claims": [
                    {
                        "role": "flow",
                        "statement": "`query_range` retrieves graph nodes",
                        "evidence": ["E1", "E2", "R1"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "graph", "title": "Graph"},
        plan,
        evidence,
        relations,
    )

    assert not any(
        warning.startswith(
            "page has static relations but no supported component handoff"
        )
        for warning in warnings
    )
    assert any(
        warning.startswith(
            "flow claim '`query_range` retrieves graph nodes' is not an explicit "
            "component handoff"
        )
        and "R1: `query_range` -> `graph`" in warning
        for warning in warnings
    )


def test_page_plan_rejects_describing_a_method_as_a_command():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/runner.py",
            start_line=10,
            end_line=20,
            symbol="AgentRunner.run",
            kind="method",
            content="def run(self, query): return query",
        )
    ]
    plan = {
        "thesis": {
            "statement": "The runtime processes user queries",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Runtime",
                "claims": [
                    {
                        "role": "entry",
                        "statement": (
                            "The command `AgentRunner.run` processes a query"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "runtime", "title": "Runtime"},
        plan,
        evidence,
    )

    assert (
        "claim 'The command `AgentRunner.run` processes a query' describes a "
        "code callable as a CLI command" in warnings
    )


def test_plan_admission_normalizes_a_function_mislabeled_as_a_command():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/api.py",
            start_line=10,
            end_line=20,
            symbol="query",
            kind="function",
            content=(
                "def query(repository):\n"
                '    """Run an agent turn over a repository and return the result."""\n'
                "    return run_agent(repository)"
            ),
        )
    ]
    plan = {
        "thesis": {
            "statement": "The command `query()` returns an agent result",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Request",
                "claims": [
                    {
                        "role": "entry",
                        "statement": (
                            "The command `query()` runs an agent turn over a "
                            "repository and returns the result"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    admitted = _normalize_plan_support(plan, evidence, [])
    rendered = _renderable_plan(admitted, evidence, [])

    assert rendered["thesis"]["statement"].startswith("The function `query()`")
    assert rendered["sections"][0]["claims"][0]["statement"].startswith(
        "The function `query()`"
    )
    assert (
        _plan_quality_warnings(
            {"id": "request", "title": "Request"},
            rendered,
            evidence,
        )
        == []
    )


def test_page_plan_rejects_generalizing_method_evidence_to_its_class():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/wiki.py",
            start_line=10,
            end_line=20,
            symbol="AgentWiki.outline",
            kind="method",
            content="def outline(self): return self._indexed_source_outline",
        )
    ]
    plan = {
        "thesis": {
            "statement": "The Wiki organizes indexed source",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Wiki",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": ("`AgentWiki` manages repository presentation"),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "wiki", "title": "Wiki"},
        plan,
        evidence,
    )

    assert (
        "claim '`AgentWiki` manages repository presentation' generalizes method "
        "evidence `AgentWiki.outline` to its owner class" in warnings
    )


def test_page_plan_allows_method_behavior_from_method_evidence():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/wiki.py",
            start_line=10,
            end_line=20,
            symbol="AgentWiki.outline",
            kind="method",
            content="def outline(self): return self._indexed_source_outline",
        )
    ]
    plan = {
        "thesis": {
            "statement": "The Wiki organizes indexed source",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Wiki",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": ("`AgentWiki.outline` returns the Wiki outline"),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    assert (
        _plan_quality_warnings(
            {"id": "wiki", "title": "Wiki"},
            plan,
            evidence,
        )
        == []
    )


def test_page_plan_rejects_labeling_a_function_as_a_method():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/builders.py",
            start_line=10,
            end_line=20,
            symbol="register_default_builders",
            kind="function",
            content="def register_default_builders(registry): pass",
        )
    ]
    plan = {
        "thesis": {
            "statement": "The compiler registers index builders",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Indexing",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "The `register_default_builders` method registers "
                            "builders"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "indexing", "title": "Indexing"},
        plan,
        evidence,
    )

    assert (
        "claim 'The `register_default_builders` method registers builders' "
        "labels `register_default_builders` as method, but cited evidence "
        "records function" in warnings
    )

    normalized = _normalize_plan_support(plan, evidence, [])
    normalized_statement = normalized["sections"][0]["claims"][0]["statement"]
    assert normalized_statement == (
        "The `register_default_builders` function registers builders"
    )
    assert (
        _plan_quality_warnings(
            {"id": "indexing", "title": "Indexing"},
            normalized,
            evidence,
        )
        == []
    )


def test_plan_support_drops_an_unrelated_relation_when_source_proves_flow():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/store.py",
            start_line=1,
            end_line=3,
            symbol="CodeStore.rebuild",
            kind="method",
            content="def rebuild(self):\n    self.clear()",
        )
    ]
    relations = [RelationItem(id="R1", source="search", target="documents")]
    plan = {
        "thesis": {
            "statement": "The store rebuilds its index",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Rebuild",
                "claims": [
                    {
                        "role": "flow",
                        "statement": (
                            "`CodeStore.rebuild` calls `CodeStore.clear` before "
                            "rebuilding"
                        ),
                        "evidence": ["E1", "R1"],
                    }
                ],
            }
        ],
    }

    normalized = _normalize_plan_support(plan, evidence, relations)

    assert normalized["sections"][0]["claims"][0]["evidence"] == ["E1"]
    assert (
        _plan_quality_warnings(
            {"id": "store", "title": "Store"},
            normalized,
            evidence,
            relations,
        )
        == []
    )


def test_plan_support_drops_an_unproved_flow_but_keeps_local_facts():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=3,
            symbol="README.md",
            kind="file",
            content="Users run codenib wiki to index a repository.",
        ),
        EvidenceItem(
            id="E2",
            file="codenib/compiler/index_builders.py",
            start_line=1,
            end_line=3,
            symbol="register_default_builders",
            kind="function",
            content=(
                "def register_default_builders(registry):\n"
                "    registry.register(IndexBuilder)"
            ),
        ),
    ]
    plan = {
        "thesis": {
            "statement": "The command indexes repositories",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Indexing",
                "claims": [
                    {
                        "role": "flow",
                        "statement": ("`codenib` invokes `register_default_builders`"),
                        "evidence": ["E1", "E2"],
                    },
                    {
                        "role": "responsibility",
                        "statement": (
                            "`register_default_builders` registers index builders"
                        ),
                        "evidence": ["E2"],
                    },
                ],
            }
        ],
    }

    normalized = _normalize_plan_support(plan, evidence, [])

    assert normalized["sections"][0]["claims"] == [
        {
            "role": "responsibility",
            "statement": "`register_default_builders` registers index builders",
            "evidence": ["E2"],
        }
    ]


def test_plan_support_downgrades_a_mislabeled_local_state_claim():
    evidence = [
        EvidenceItem(
            id="E1",
            file="sklearn/feature_extraction/text.py",
            start_line=10,
            end_line=18,
            symbol="TfidfVectorizer.idf_",
            kind="property",
            content=(
                "@idf_.setter\n"
                "def idf_(self, value):\n"
                "    if not hasattr(self, '_tfidf'):\n"
                "        self._tfidf = TfidfTransformer()\n"
                "    self._tfidf.idf_ = value"
            ),
        )
    ]
    plan = {
        "sections": [
            {
                "title": "Configuration",
                "claims": [
                    {
                        "role": "flow",
                        "statement": (
                            "The `idf_` setter creates a `TfidfTransformer` if "
                            "one does not exist and assigns the provided IDF "
                            "values to it"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ]
    }

    normalized = _normalize_plan_support(plan, evidence, [])

    claim = normalized["sections"][0]["claims"][0]
    assert claim["role"] == "component"
    assert not any(
        warning.startswith("flow claim")
        for warning in _plan_quality_warnings(
            {"id": "text-vectorization"},
            normalized,
            evidence,
        )
    )


def test_plan_support_promotes_a_grounded_section_lead_to_thesis():
    evidence = [
        EvidenceItem(
            id="E1",
            file="tokio/src/sync/barrier.rs",
            start_line=139,
            end_line=198,
            symbol="Barrier.wait_internal",
            kind="method",
            content=(
                "The count and generation are stored behind a synchronous "
                "mutex. A watch channel carries the generation number to "
                "waiting tasks."
            ),
        )
    ]
    lead = (
        "The count and generation are stored behind a synchronous mutex, and "
        "a watch channel carries the generation number to waiting tasks"
    )
    plan = {
        "sections": [
            {
                "title": "Barrier generation counting and release",
                "lead": {"statements": [lead], "evidence": ["E1"]},
                "claims": [
                    {
                        "statement": (
                            "`Barrier.wait_internal()` increments the arrival "
                            "count. `Barrier.wait()` waits for its result"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ]
    }

    normalized = _normalize_plan_support(plan, evidence, [])

    assert normalized["thesis"] == {
        "statement": lead,
        "evidence": ["E1"],
    }


def test_relation_recovery_plan_uses_verified_call_endpoints():
    evidence = [
        EvidenceItem(
            id="E1",
            file="include/fmt/format.h",
            start_line=2590,
            end_line=2626,
            symbol="include/fmt/format.h:do_write_float()",
            kind="function",
            content="do_write_float(...) { return write_fixed(...); }",
        ),
        EvidenceItem(
            id="E2",
            file="include/fmt/format.h",
            start_line=2524,
            end_line=2586,
            symbol="write_fixed",
            kind="function",
            content="write_fixed(...) { /* write fixed-point digits */ }",
        ),
    ]
    relations = [
        RelationItem(
            id="R1",
            source="include/fmt/format.h:fmt.detail.do_write_float()",
            target="include/fmt/format.h:fmt.detail.write_fixed()",
        )
    ]

    plan = _relation_backed_recovery_plan(
        {
            "id": "type-dispatch-and-writing",
            "summary": "Routes formatted values to specialized write functions",
        },
        evidence,
        relations,
    )
    markdown = _fact_plan_markdown(plan, evidence, relations)

    assert (
        _plan_quality_warnings(
            {"id": "type-dispatch-and-writing"},
            plan,
            evidence,
            relations,
        )
        == []
    )
    assert "## Verified call path" in markdown
    # Relations render as rows with their call site, never as a sentence.
    assert (
        "**Interactions**\n- `fmt.detail.do_write_float()` → "
        "`fmt.detail.write_fixed()` [R1]" in markdown
    )
    assert "calls `fmt.detail.write_fixed()`" not in markdown
    assert "Source-backed components" not in markdown
    assert "is indexed from" not in markdown


def test_plan_support_drops_method_to_owner_generalization():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/wiki.py",
            start_line=10,
            end_line=20,
            symbol="AgentWiki._find",
            kind="method",
            content=(
                "def _find(self, page_identifier):\n"
                "    return PageMetadata(page_identifier)"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/wiki.py",
            start_line=22,
            end_line=30,
            symbol="AgentWiki.page",
            kind="method",
            content=(
                "def page(self, page_identifier):\n"
                "    return PageMetadata(page_identifier)"
            ),
        ),
    ]
    plan = {
        "thesis": {
            "statement": "Wiki pages expose page metadata",
            "evidence": ["E2"],
        },
        "sections": [
            {
                "title": "Lookup",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": ("The `AgentWiki` class searches page metadata"),
                        "evidence": ["E1"],
                    },
                    {
                        "role": "contract",
                        "statement": ("`AgentWiki.page()` returns page metadata"),
                        "evidence": ["E2"],
                    },
                ],
            }
        ],
    }

    normalized = _normalize_plan_support(plan, evidence, [])

    assert normalized["sections"][0]["claims"] == [
        {
            "role": "contract",
            "statement": "`AgentWiki.page()` returns page metadata",
            "evidence": ["E2"],
        }
    ]


def test_page_plan_rejects_sections_that_only_inventory_operations():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/graph.py",
            start_line=1,
            end_line=20,
            symbol="CodeGraph",
            kind="class",
            content="class CodeGraph: pass",
        )
    ]
    plan = {
        "thesis": {
            "statement": "The graph stores repository structure",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": title,
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": f"`{left}` retrieves graph state",
                        "evidence": ["E1"],
                    },
                    {
                        "role": "contract",
                        "statement": f"`{right}` requires a graph path",
                        "evidence": ["E1"],
                    },
                ],
            }
            for title, left, right in (
                ("Load", "load_graph", "load_graph"),
                ("Query", "query_graph", "query_graph"),
                ("Save", "save_graph", "save_graph"),
            )
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "graph", "title": "Graph"},
        plan,
        evidence,
    )

    assert any(
        warning.startswith("page plan is dominated by isolated operation sections:")
        for warning in warnings
    )


def test_page_plan_rejects_a_thesis_repeated_by_a_richer_section_fact():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/container.py",
            start_line=1,
            end_line=20,
            symbol="copy_file_to_container",
            kind="function",
            content=(
                "def copy_file_to_container(container, content, path):\n"
                "    archive = make_tar(content)\n"
                "    container.put_archive(path, archive)"
            ),
        )
    ]
    plan = {
        "thesis": {
            "statement": (
                "`copy_file_to_container` transfers a string into a Docker "
                "container at a specified path"
            ),
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "File Management",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "`copy_file_to_container` writes a string to a "
                            "temporary file, creates a TAR archive, and "
                            "transfers it to the specified path in the Docker "
                            "container using `container.put_archive`"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "containers", "title": "Containers"},
        plan,
        evidence,
    )

    assert any("thesis or sections substantially repeat" in item for item in warnings)


def test_page_plan_rejects_a_page_wide_callable_catalog():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/trace.py",
            start_line=1,
            end_line=40,
            symbol="WorkflowTracer",
            kind="class",
            content="class WorkflowTracer: pass",
        )
    ]
    plan = {
        "thesis": {
            "statement": "The page documents workflow tracing operations",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Trace",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": f"`{symbol}` {verb} workflow trace data",
                        "evidence": ["E1"],
                    }
                    for symbol, verb in (
                        ("start_turn", "creates"),
                        ("end_turn", "sets"),
                        ("get_trace", "retrieves"),
                        ("save_trace", "saves"),
                    )
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "tracing", "title": "Tracing"},
        plan,
        evidence,
    )

    assert any("page reads as a callable catalog" in item for item in warnings)


def test_page_plan_rejects_incidental_helper_section():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file="src/runtime.py",
            start_line=0,
            end_line=10,
            symbol=symbol,
            kind="method",
            content=content,
        )
        for index, symbol, content in [
            (
                1,
                "AgentRunner.run",
                "def run(self): return execute_configured_request_loop()",
            ),
            (
                2,
                "AgentRunner.configure",
                "def configure(self, request): self.request = request",
            ),
            (
                3,
                "AgentRunner._serialize",
                "def _serialize(self, state): return format_state(state)",
            ),
        ]
    ]
    plan = {
        "thesis": {
            "statement": "The agent runtime executes a configured request loop",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Agent Execution",
                "claims": [
                    {
                        "statement": "`AgentRunner.run()` executes the loop",
                        "evidence": ["E1"],
                    }
                ],
            },
            {
                "title": "Internal Helpers",
                "claims": [
                    {
                        "statement": "`AgentRunner._serialize()` formats state",
                        "evidence": ["E3"],
                    }
                ],
            },
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "agent-runtime", "title": "Agent Runtime"},
        plan,
        evidence,
    )

    assert warnings == [
        "section 'Internal Helpers' elevates incidental helpers over the page's "
        "core responsibility"
    ]


def test_page_plan_allows_a_declared_utility_child_section():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/editor.py",
            start_line=0,
            end_line=20,
            symbol="Editor.create_patch",
            kind="method",
            content="create_patch returns a git diff patch",
        ),
        EvidenceItem(
            id="E2",
            file="src/edit_utils.py",
            start_line=0,
            end_line=20,
            symbol="run_cmd",
            kind="function",
            content="run_cmd returns stdout and stderr",
        ),
    ]
    plan = {
        "thesis": {
            "statement": "`Editor.create_patch` returns a git diff patch",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Edit Utilities",
                "claims": [
                    {
                        "role": "contract",
                        "statement": "`run_cmd` returns stdout and stderr",
                        "evidence": ["E2"],
                    }
                ],
            }
        ],
    }
    meta = {
        "id": "editing-tools",
        "title": "Editing Tools",
        "children": [{"id": "edit-utilities", "title": "Edit Utilities"}],
    }

    warnings = _plan_quality_warnings(meta, plan, evidence)

    assert not any("elevates incidental helpers" in item for item in warnings)


def test_parent_page_requires_section_level_evidence_from_each_child():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/trace.py",
            start_line=1,
            end_line=10,
            symbol="WorkflowTracer.get_summary",
            kind="method",
            content="get_summary returns workflow trace totals",
        ),
        EvidenceItem(
            id="E2",
            file="src/logging.py",
            start_line=1,
            end_line=10,
            symbol="LoggingManager.get_logger",
            kind="method",
            content="get_logger returns a configured logger",
        ),
    ]
    plan = {
        "thesis": {
            "statement": ("`WorkflowTracer.get_summary` returns workflow trace totals"),
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Logging Management",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "`LoggingManager.get_logger` returns a configured " "logger"
                        ),
                        "evidence": ["E2"],
                    }
                ],
            }
        ],
    }
    meta = {
        "id": "logging-and-tracing",
        "title": "Logging and Tracing",
        "children": [
            {"title": "Workflow Tracing", "files": ["src/trace.py"]},
            {"title": "Logging Management", "files": ["src/logging.py"]},
        ],
    }

    warnings = _plan_quality_warnings(meta, plan, evidence)

    assert (
        "parent page needs a section-level 'Workflow Tracing' fact grounded in "
        "its allocated evidence" in warnings
    )
    assert not any("'Logging Management'" in item for item in warnings)


def test_page_plan_rejects_evidence_ids_narrated_as_prose():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/worker.py",
            start_line=0,
            end_line=5,
            symbol="Worker",
            kind="class",
            content="class Worker: pass",
        ),
        EvidenceItem(
            id="E2",
            file="src/store.py",
            start_line=0,
            end_line=5,
            symbol="Store",
            kind="class",
            content="class Store: pass",
        ),
    ]
    plan = {
        "thesis": {
            "statement": "The worker persists results through a store",
            "evidence": ["E1", "E2"],
        },
        "sections": [
            {
                "title": "Result flow",
                "claims": [
                    {
                        "role": "flow",
                        "statement": (
                            "`Worker` calls `Store`, as indicated by relation R1"
                        ),
                        "evidence": ["E1", "E2", "R1"],
                    }
                ],
            }
        ],
    }

    warnings = _plan_quality_warnings(
        {"id": "result-flow", "title": "Result Flow"},
        plan,
        evidence,
        [RelationItem(id="R1", source="Worker", target="Store")],
    )

    assert (
        "claim statements must not narrate evidence IDs; use only the "
        "evidence array" in warnings
    )


def test_overview_plan_rejects_filename_as_subsystem_name():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (1, "src/cli.py", "def main(): pass"),
            (2, "src/compiler.py", "class Compiler: pass"),
            (3, "src/server.py", "class Server: pass"),
            (4, "src/runtime.py", "class Runtime: pass"),
        ]
    ]
    plan = {
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {"statement": "The CLI accepts a path", "evidence": ["E1"]},
                    {"statement": "The CLI starts processing", "evidence": ["E1"]},
                ],
            },
            {
                "title": "Build",
                "claims": [
                    {"statement": "The compiler writes views", "evidence": ["E2"]},
                    {"statement": "The compiler writes state", "evidence": ["E2"]},
                ],
            },
            {
                "title": "Subsystems",
                "claims": [
                    {
                        "statement": "The `server.py` subsystem returns pages",
                        "evidence": ["E3"],
                    },
                    {"statement": "The runtime loads views", "evidence": ["E4"]},
                ],
            },
        ]
    }

    warnings = _plan_quality_warnings({"id": "overview"}, plan, evidence)

    assert "section 'Subsystems' names a source file as a subsystem" in warnings


def test_fact_plan_caps_total_model_calls_when_repairs_never_improve(monkeypatch):
    response = json.dumps(
        {
            "thesis": {
                "statement": "`Router.run()` returns `dispatch(request)`",
                "evidence": ["E1"],
            },
            "sections": [
                {
                    "title": "Routing",
                    "claims": [
                        {
                            "role": "contract",
                            "statement": "`Router.run()` returns `dispatch(request)`",
                            "evidence": ["E1"],
                        }
                    ],
                }
            ],
        }
    )

    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            return response

    llm = LLM()
    wiki = AgentWiki(
        SimpleNamespace(entry=SimpleNamespace(repo="owner/repo", language="python")),
        model="fake-model",
        llm=llm,
    )
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/router.py",
            start_line=1,
            end_line=2,
            symbol="Router.run",
            kind="method",
            content="def run(request):\n    return dispatch(request)",
        )
    ]
    monkeypatch.setattr(
        "codenib.wiki.agent_wiki._plan_quality_warnings",
        lambda *_args, **_kwargs: ["claim cites evidence that does not exist"],
    )
    metrics = {}

    wiki._fact_plan(
        {"id": "routing", "title": "Routing", "summary": "Request routing"},
        evidence,
        [],
        metrics=metrics,
    )

    assert llm.calls == 3
    assert metrics["model_calls"] == 3
    assert metrics["fresh_replans"] + metrics["repair_attempts"] == 2


def test_overview_fact_plan_accepts_a_concise_supported_plan():
    sparse = {
        "thesis": "indexed source",
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "statement": "The CLI accepts a path",
                        "evidence": ["E2"],
                    }
                ],
            },
            {
                "title": "Flow",
                "claims": [
                    {
                        "statement": "The compiler builds indexes",
                        "evidence": ["E3"],
                    }
                ],
            },
            {
                "title": "Subsystems",
                "claims": [
                    {
                        "statement": "The server returns pages",
                        "evidence": ["E4"],
                    }
                ],
            },
        ],
    }
    _add_overview_architecture(sparse)
    dense = {
        "thesis": {
            "statement": "The repository serves indexed source through a local Wiki",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "The CLI accepts a repository path",
                        "evidence": ["E2"],
                    },
                    {
                        "role": "purpose",
                        "statement": "The Wiki exposes indexed source",
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Flow",
                "claims": [
                    {
                        "role": "component",
                        "statement": "The CLI accepts repository requests",
                        "evidence": ["E2"],
                    },
                    {
                        "role": "responsibility",
                        "statement": "The compiler records repository indexes",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Subsystems",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "The server returns Wiki pages",
                        "evidence": ["E4"],
                    },
                    {
                        "role": "contract",
                        "statement": "Wiki requests return Markdown pages",
                        "evidence": ["E4"],
                    },
                ],
            },
        ],
    }

    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.responses = iter((json.dumps(sparse), json.dumps(dense)))
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            return next(self.responses)

    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file=file,
            start_line=None,
            end_line=None,
            symbol=file,
            kind="file",
            content=content,
        )
        for index, file, content in [
            (
                1,
                "README.md",
                "The repository serves indexed source through a local Wiki.",
            ),
            (
                2,
                "src/cli.py",
                "def main(repository_path):\n"
                "    request = RepositoryRequest(repository_path)\n"
                "    return CLI(request)",
            ),
            (
                3,
                "src/compiler.py",
                "class Compiler:\n"
                "    def record(self, repository_indexes):\n"
                "        return repository_indexes",
            ),
            (
                4,
                "src/server.py",
                "class Server:\n"
                "    def page(self, wiki_request):\n"
                "        return SourceLinkedWikiPage(wiki_request)",
            ),
        ]
    ]
    llm = LLM()
    wiki = AgentWiki(
        SimpleNamespace(
            entry=SimpleNamespace(repo="owner/repo", language="python"),
        ),
        model="fake-model",
        llm=llm,
    )

    plan, warnings = wiki._fact_plan(
        {"id": "overview", "title": "Overview", "summary": "Repository architecture"},
        evidence,
        [],
    )

    assert llm.calls == 1
    assert warnings == []
    assert [len(section["claims"]) for section in plan["sections"]] == [1, 1, 1]


def test_overview_fact_plan_merges_complementary_repairs():
    initial = {
        "thesis": {
            "statement": "The repository serves indexed source through a local Wiki",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "role": "entry",
                        "statement": "`main()` returns `CLI(request)`",
                        "evidence": ["E2"],
                    }
                ],
            },
        ],
    }
    _add_overview_architecture(initial)
    complementary = {
        "thesis": initial["thesis"],
        "sections": [
            {
                "title": "Runtime",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "`Server.page()` returns `PageResponse(page)`",
                        "evidence": ["E4"],
                    }
                ],
            },
        ],
    }

    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.responses = iter((json.dumps(initial), json.dumps(complementary)))
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            return next(self.responses)

    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=None,
            end_line=None,
            symbol="README.md",
            kind="file",
            content="The repository serves indexed source through a local Wiki.",
        ),
        EvidenceItem(
            id="E2",
            file="src/cli.py",
            start_line=None,
            end_line=None,
            symbol="main",
            kind="function",
            content=(
                "def main(repository_path):\n"
                "    request = RepositoryRequest(repository_path)\n"
                "    return CLI(request)"
            ),
        ),
        EvidenceItem(
            id="E3",
            file="src/compiler.py",
            start_line=None,
            end_line=None,
            symbol="Compiler.record",
            kind="method",
            content=(
                "class Compiler:\n"
                "    def record(self, repository_indexes):\n"
                "        self.manifest = Manifest(repository_indexes)\n"
                "        return self.manifest"
            ),
        ),
        EvidenceItem(
            id="E4",
            file="src/server.py",
            start_line=None,
            end_line=None,
            symbol="Server.page",
            kind="method",
            content=(
                "class Server:\n"
                "    def page(self, wiki_request):\n"
                "        page = SourceLinkedWikiPage(wiki_request)\n"
                "        return PageResponse(page)"
            ),
        ),
    ]
    llm = LLM()
    wiki = AgentWiki(
        SimpleNamespace(
            entry=SimpleNamespace(repo="owner/repo", language="python"),
        ),
        model="fake-model",
        llm=llm,
    )

    plan, warnings = wiki._fact_plan(
        {"id": "overview", "title": "Overview", "summary": "Repository architecture"},
        evidence,
        [],
    )

    assert llm.calls == 2, warnings
    assert [section["title"] for section in plan["sections"]] == [
        "Workflow",
        "Runtime",
    ]
    assert not any(warning.startswith("Overview needs") for warning in warnings)


def test_fact_plan_replans_when_no_claim_survives_source_admission():
    unsupported = {
        "thesis": {
            "statement": "`AgentRunner.run()` deploys a distributed scheduler",
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Execution",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "`AgentRunner.run()` deploys a distributed scheduler"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }
    supported = {
        "thesis": {
            "statement": ("`AgentRunner.run()` returns `self.execute(query)`"),
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Execution",
                "claims": [
                    {
                        "role": "contract",
                        "statement": ("`load_config()` returns `read_config()`"),
                        "evidence": ["E2"],
                    },
                    {
                        "role": "contract",
                        "statement": "`AgentRunner.run()` rejects an empty query",
                        "evidence": ["E1"],
                    },
                ],
            }
        ],
    }

    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.responses = iter((json.dumps(unsupported), json.dumps(supported)))
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            return next(self.responses)

    evidence = [
        EvidenceItem(
            id="E1",
            file="src/runner.py",
            start_line=10,
            end_line=14,
            symbol="AgentRunner.run",
            kind="method",
            content=(
                "def run(self, query):\n"
                '    """Reject an empty query before execution."""\n'
                "    if not query:\n"
                "        raise ValueError('empty query')\n"
                "    return self.execute(query)"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/config.py",
            start_line=1,
            end_line=3,
            symbol="load_config",
            kind="function",
            content="def load_config():\n    return read_config()",
        ),
    ]
    llm = LLM()
    wiki = AgentWiki(
        SimpleNamespace(
            entry=SimpleNamespace(repo="owner/repo", language="python"),
        ),
        model="fake-model",
        llm=llm,
    )

    plan, warnings = wiki._fact_plan(
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "summary": "Agent execution",
        },
        evidence,
        [],
    )

    assert llm.calls == 2
    assert plan["sections"][0]["claims"][0]["statement"] == (
        "`load_config()` returns `read_config()`"
    )
    assert not any("no claims" in warning for warning in warnings)


def test_readme_intro_skips_logo_markup():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=20,
            symbol="README.md",
            kind="file",
            content=(
                '<div align="center">\n<img src="logo.svg">\n</div>\n\n'
                "CodeNib compiles repository views and serves source-linked "
                "context through a local Wiki and tools.\n"
            ),
        )
    ]

    assert _readme_intro(evidence) == (
        "CodeNib compiles repository views and serves source-linked context "
        "through a local Wiki and tools.",
        "E1",
    )


def test_readme_intro_skips_commands_and_warning_chrome():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=20,
            symbol="README.md",
            kind="file",
            content=(
                "# Efficient ASE Framework based on SGLang\n\n"
                "```bash\n"
                "conda create -n flashcoder python=3.10 -y\n"
                "```\n\n"
                "Warning: Do not interrupt the cleanup process.\n"
            ),
        )
    ]

    assert _readme_intro(evidence) == (
        "Efficient ASE Framework based on SGLang",
        "E1",
    )


def test_overview_neutralizes_a_promotional_readme_heading():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=20,
            symbol="README.md",
            kind="file",
            content="# Efficient ASE Framework based on SGLang",
        )
    ]
    draft = (
        "## Evaluation\n\n"
        "The evaluation script executes benchmark cases from a repository. [E1]"
    )

    rendered = _ensure_cited_intro(
        draft,
        evidence,
        canonical_readme=True,
        repository_name="sysevol-ai/FlashCoder",
    )

    assert rendered.startswith("FlashCoder is an ASE framework based on SGLang. [E1]")
    assert "Efficient" not in rendered


def test_overview_uses_canonical_readme_intro():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=20,
            symbol="README.md",
            kind="file",
            content=(
                "The repository compiles source into reusable views and serves "
                "them through a local developer Wiki."
            ),
        )
    ]
    draft = (
        "This document provides a comprehensive overview of the system. [E1]\n\n"
        "> **Reader question:** How does source become a Wiki? [E1]\n\n"
        "## Workflow\n\n"
        "The command builds a repository Wiki. [E1]"
    )

    rendered = _ensure_cited_intro(draft, evidence, canonical_readme=True)

    assert rendered.startswith(
        "The repository compiles source into reusable views and serves them "
        "through a local developer Wiki. [E1]"
    )
    assert "This document provides" not in rendered
    assert "> **Reader question:** How does source become a Wiki? [E1]" in rendered
    assert "## Workflow" in rendered


def test_overview_keeps_supported_thesis_when_canonical_intro_is_a_fragment():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=20,
            symbol="README.md",
            kind="file",
            content=("jq is a lightweight and flexible command-line JSON processor."),
        )
    ]
    draft = (
        "`jq` parses JSON input, compiles a filter, and executes the compiled "
        "program against each value. [E1]\n\n"
        "## Execution\n\n"
        "The runtime evaluates compiled filters against JSON values. [E1]"
    )

    rendered = _ensure_cited_intro(
        draft,
        evidence,
        canonical_readme=True,
        repository_name="jq",
    )

    assert rendered == draft
    assert not rendered.startswith("jq is a lightweight.")


def test_overview_drops_purpose_that_repeats_canonical_intro():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=20,
            symbol="README.md",
            kind="file",
            content=(
                "Axios is a promise-based HTTP client that sends requests from "
                "browsers and Node.js applications."
            ),
        )
    ]
    draft = (
        "A supported planning thesis that will be replaced. [E1]\n\n"
        "## Purpose and scope\n\n"
        "Axios is a promise-based HTTP client for sending requests from browser "
        "and Node.js applications. [E1]\n\n"
        "## Request pipeline\n\n"
        "The request pipeline normalizes configuration before dispatch. [E1]"
    )

    rendered = _ensure_cited_intro(
        draft,
        evidence,
        canonical_readme=True,
        repository_name="Axios",
    )

    assert rendered.startswith("Axios is a promise-based HTTP client")
    assert "## Purpose and scope" not in rendered
    assert "## Request pipeline" in rendered


def test_overview_truncates_promotional_readme_tail_at_complete_clause():
    evidence = [
        EvidenceItem(
            id="E1",
            file="README.md",
            start_line=1,
            end_line=20,
            symbol="README.md",
            kind="file",
            content=(
                "# {fmt}\n\n"
                "**{fmt}** is an open-source formatting library providing a "
                "fast and safe\nalternative to C stdio and C++ iostreams."
            ),
        )
    ]
    draft = "## Workflow\n\nThe `format` function formats text. [E1]"

    rendered = _ensure_cited_intro(
        draft,
        evidence,
        canonical_readme=True,
        repository_name="fmt",
    )

    assert rendered.startswith("{fmt} is an open-source formatting library. [E1]")
    assert "providing a and" not in rendered


def test_readme_evidence_drops_chrome_and_keeps_complete_paragraphs():
    content = (
        "<!-- license -->\n"
        '<div align="center">\n'
        '<img src="logo.svg">\n'
        "<p>Documentation · GitHub · CI</p>\n"
        "</div>\n\n"
        "The repository compiles source into reusable views and serves them "
        "through a local developer Wiki. This sentence must remain complete.\n\n"
        + ("Implementation details follow this overview. " * 100)
    )

    prepared = _prepare_evidence_content("README.md", content, limit=220)

    assert "<div" not in prepared
    assert "logo.svg" not in prepared
    assert prepared.startswith("The repository compiles source")
    assert "This sentence must remain complete." in prepared
    assert prepared.endswith((".", "!", "?"))


@pytest.mark.parametrize("indexed_end_line", [2, 3])
def test_evidence_items_prefer_the_current_source_span(tmp_path, indexed_end_line):
    source = tmp_path / "src" / "core.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Router:\n"
        "    def dispatch(self, headers):\n"
        "        return headers['Authorization']\n",
        encoding="utf-8",
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        )
    )
    wiki = AgentWiki(bundle, model="fake-model")

    evidence = wiki._evidence_items(
        [
            {
                "file": "src/core.py",
                "node_name": "Router.dispatch",
                "type": "method",
                "start_line": 0,
                "end_line": indexed_end_line,
                "content": "class Router:\n    pass  # stale index excerpt",
            }
        ]
    )

    assert len(evidence) == 1
    assert "Authorization" in evidence[0].content
    assert "stale index excerpt" not in evidence[0].content
    assert (evidence[0].start_line, evidence[0].end_line) == (1, 3)
    citation = wiki._citation_payload(evidence)[0]
    assert citation["end_line"] == len(source.read_text().splitlines())


def test_evidence_items_keep_indexed_content_for_an_incomplete_span(tmp_path):
    source = tmp_path / "src" / "legacy.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def live_implementation():\n"
        "    return 'only the first source line would have been read'\n",
        encoding="utf-8",
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        )
    )
    wiki = AgentWiki(bundle, model="fake-model")

    evidence = wiki._evidence_items(
        [
            {
                "file": "src/legacy.py",
                "node_name": "legacy",
                "type": "function",
                "start_line": 0,
                "content": (
                    "def legacy():\n"
                    "    indexed_body_is_complete()\n"
                    "    return 'ready'"
                ),
            }
        ]
    )

    assert len(evidence) == 1
    assert evidence[0].start_line == 1
    assert evidence[0].end_line == 1
    assert "indexed_body_is_complete" in evidence[0].content
    assert "live_implementation" not in evidence[0].content


def test_wiki_context_exposes_page_identity_without_generated_summaries(tmp_path):
    wiki = AgentWiki(
        SimpleNamespace(
            entry=SimpleNamespace(repo_dir=str(tmp_path), language="python")
        ),
        model="fake-model",
    )
    wiki._outline = {
        "pages": [
            {
                "id": "routing",
                "title": "Request Routing",
                "summary": "The invented MagicGateway controls every request",
            }
        ]
    }

    context = wiki._wiki_context("overview")

    assert context == "- [routing] Request Routing"
    assert "MagicGateway" not in context


class _FakeBM25:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def search(self, query, top_k, **kwargs):
        self.calls.append((query, top_k, kwargs))
        return self.nodes[:top_k]


def test_agent_wiki_retrieval_reranks_by_page_files_and_keywords(tmp_path):
    nodes = [
        {
            "file": "src/unrelated.py",
            "node_name": "unrelated",
            "start_line": 0,
            "end_line": 5,
            "content": "generic helper",
        },
        {
            "file": "src/core/http.py",
            "node_name": "dispatch_request",
            "start_line": 10,
            "end_line": 30,
            "content": "request pipeline dispatch handling",
        },
        {
            "file": "src/core/http.py",
            "node_name": "dispatch_request",
            "start_line": 10,
            "end_line": 30,
            "content": "duplicate copy should collapse",
        },
        {
            "file": "src/core/interceptors.py",
            "node_name": "InterceptorChain",
            "start_line": 40,
            "end_line": 70,
            "content": "request response modification",
        },
    ]
    store = _FakeVectorStore(nodes)
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=store,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"]),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "request-pipeline",
            "title": "Request Pipeline",
            "summary": "How requests are dispatched",
            "files": ["src/core/http.py"],
            "keywords": ["request pipeline", "dispatch"],
        },
        top_k=2,
    )

    assert store.calls
    query, top_k = store.calls[0]
    assert "src/core/http.py" in query
    assert top_k == 8
    assert [node["node_name"] for node in result] == [
        "dispatch_request",
        "InterceptorChain",
    ]


def test_agent_wiki_fuses_dense_and_bm25_routes(tmp_path):
    shared = {
        "file": "src/core.py",
        "node_name": "dispatch",
        "start_line": 0,
        "end_line": 4,
        "content": "def dispatch(): pass",
    }
    dense = _FakeVectorStore(
        [
            shared,
            {
                "file": "src/model.py",
                "node_name": "Model",
                "start_line": 0,
                "end_line": 4,
                "content": "class Model: pass",
            },
        ]
    )
    bm25 = _FakeBM25(
        [
            shared,
            {
                "file": "src/cli.py",
                "node_name": "main",
                "start_line": 0,
                "end_line": 4,
                "content": "def main(): pass",
            },
        ]
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=dense,
        bm25=bm25,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {"title": "Dispatch", "summary": "request dispatch", "keywords": []},
        top_k=3,
    )

    assert [node["node_name"] for node in result] == ["dispatch", "Model", "main"]
    assert wiki._retrieval_routes[candidate_key(shared, wiki._node_attr)] == (
        "dense",
        "bm25",
    )
    assert bm25.calls[0][2]["return_code_content"] is True


def test_wiki_relations_exclude_fields_and_constants_from_narrative(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "codenib.web.codemap.build_page_subgraph",
        lambda *args, **kwargs: {
            "available": True,
            "nodes": [
                {
                    "id": "run",
                    "label": "src/runner.py:AgentRunner.run()",
                    "kind": "method",
                },
                {
                    "id": "trace",
                    "label": "src/trace.py:AgentRunTrace.add()",
                    "kind": "method",
                },
                {
                    "id": "logger",
                    "label": "src/runner.py:logger",
                    "kind": "function",
                },
                {
                    "id": "constant",
                    "label": "src/types.py:EDGE_TYPE_REFERENCE",
                    "kind": "class",
                },
                {
                    "id": "private",
                    "label": "src/runner.py:AgentRunner._prepare()",
                    "kind": "method",
                },
            ],
            "edges": [
                {
                    "source": "run",
                    "target": "trace",
                    "anchors": [{"file": "src/runner.py", "line": 40}],
                },
                {
                    "source": "run",
                    "target": "logger",
                    "anchors": [{"file": "src/runner.py", "line": 41}],
                },
                {
                    "source": "run",
                    "target": "constant",
                    "anchors": [{"file": "src/runner.py", "line": 42}],
                },
                {
                    "source": "run",
                    "target": "private",
                    "anchors": [{"file": "src/runner.py", "line": 43}],
                },
            ],
        },
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        code_graph=lambda: object(),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/runner.py",
            start_line=1,
            end_line=50,
            symbol="AgentRunner.run",
            kind="method",
            content="def run(): pass",
        )
    ]

    relations = wiki._relation_items(evidence)

    assert relations == [
        RelationItem(
            id="R1",
            source="src/runner.py:AgentRunner.run()",
            target="src/trace.py:AgentRunTrace.add()",
            anchors=("src/runner.py:40",),
        )
    ]


def test_parent_wiki_adds_one_anchored_callable_handoff_per_evidence(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "codenib.web.codemap.build_page_subgraph",
        lambda *args, **kwargs: {
            "available": True,
            "nodes": [],
            "edges": [],
        },
    )
    graph = CodeGraph()
    graph._add_vertex(
        "src/wiki.py:AgentWiki.page()",
        {
            "type": "method",
            "file": "src/wiki.py",
            "start_line": 0,
            "end_line": 9,
            "unified_name": "src/wiki.py:AgentWiki.page()",
        },
    )
    graph._add_vertex(
        "src/wiki.py:AgentWiki._generate_page()",
        {
            "type": "method",
            "file": "src/wiki.py",
            "start_line": 20,
            "end_line": 29,
            "unified_name": "src/wiki.py:AgentWiki._generate_page()",
        },
    )
    graph._add_vertex(
        "src/wiki.py:AgentWiki.outline()",
        {
            "type": "method",
            "file": "src/wiki.py",
            "start_line": 31,
            "end_line": 39,
            "unified_name": "src/wiki.py:AgentWiki.outline()",
        },
    )
    graph._add_edge(
        "src/wiki.py:AgentWiki.page()",
        "src/wiki.py:AgentWiki._generate_page()",
        "reference",
        anchor_file="src/wiki.py",
        anchor_line=3,
    )
    graph._add_edge(
        "src/wiki.py:AgentWiki.page()",
        "src/wiki.py:AgentWiki.outline()",
        "reference",
        anchor_file="src/wiki.py",
        anchor_line=4,
    )
    graph.build_range_indexes()
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        code_graph=lambda: graph,
    )
    wiki = AgentWiki(bundle, model="fake-model")
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/wiki.py",
            start_line=1,
            end_line=10,
            symbol="AgentWiki.page",
            kind="method",
            content=(
                "def page(self, page_id):\n" "    return self._generate_page(page_id)"
            ),
        )
    ]

    relations = wiki._relation_items(evidence, {"id": "overview"})

    assert relations == [
        RelationItem(
            id="R1",
            source="src/wiki.py:AgentWiki.page()",
            target="src/wiki.py:AgentWiki._generate_page()",
            anchors=("src/wiki.py:4",),
        )
    ]


def test_parent_page_retrieval_keeps_one_child_boundary_file():
    meta = {
        "id": "agent-runtime",
        "title": "Agent Runtime",
        "keywords": ["agent", "runtime"],
        "files": ["src/runner.py", "src/rerank_agent.py"],
        "children": [
            {
                "id": "rerank-agent",
                "title": "Rerank Agent",
                "keywords": ["rerank", "agent"],
                "files": ["src/rerank_agent.py"],
            }
        ],
    }

    assert AgentWiki._page_retrieval_files(meta) == [
        "src/runner.py",
        "src/rerank_agent.py",
    ]
    assert AgentWiki._child_specific_terms(meta) == {"rerank"}


def test_parent_page_retrieval_keeps_child_boundary_and_excludes_global(tmp_path):
    parent = {
        "file": "src/runner.py",
        "node_name": "AgentRunner",
        "start_line": 0,
        "end_line": 20,
        "content": "class AgentRunner: pass",
    }
    child = {
        "file": "src/rerank_agent.py",
        "node_name": "RerankAgent",
        "start_line": 0,
        "end_line": 20,
        "content": "class RerankAgent: pass",
    }
    global_match = {
        "file": "src/context.py",
        "node_name": "ContextLedger",
        "start_line": 0,
        "end_line": 20,
        "content": "class ContextLedger: pass",
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25([child, global_match, parent]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "summary": "Agent execution and context handling",
            "keywords": ["agent", "runtime"],
            "files": ["src/runner.py", "src/rerank_agent.py"],
            "children": [
                {
                    "id": "rerank-agent",
                    "title": "Rerank Agent",
                    "keywords": ["rerank", "agent"],
                    "files": ["src/rerank_agent.py"],
                }
            ],
        },
        top_k=4,
    )

    assert [node["file"] for node in result] == [
        "src/runner.py",
        "src/rerank_agent.py",
    ]


def test_parent_without_core_files_selects_two_anchors_per_child(tmp_path):
    nodes = [
        {
            "file": "src/runner.py",
            "node_name": "AgentRunner.run",
            "type": "method",
            "start_line": 20,
            "end_line": 40,
            "content": "def run(self): pass",
        },
        {
            "file": "src/rerank_agent.py",
            "node_name": "RerankAgent.__init__",
            "type": "method",
            "start_line": 10,
            "end_line": 18,
            "content": "def __init__(self): pass",
        },
        {
            "file": "src/runner.py",
            "node_name": "AgentRunner",
            "type": "class",
            "start_line": 0,
            "end_line": 80,
            "content": "class AgentRunner: pass",
        },
        {
            "file": "src/rerank_agent.py",
            "node_name": "RerankAgent",
            "type": "class",
            "start_line": 0,
            "end_line": 60,
            "content": "class RerankAgent: pass",
        },
    ]
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25(nodes),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "summary": "Agent execution",
            "keywords": ["agent", "runtime"],
            "files": ["src/runner.py", "src/rerank_agent.py"],
            "children": [
                {
                    "id": "agent-runner",
                    "title": "Agent Runner",
                    "files": ["src/runner.py"],
                },
                {
                    "id": "rerank-agent",
                    "title": "Rerank Agent",
                    "files": ["src/rerank_agent.py"],
                },
            ],
        },
        top_k=8,
    )

    assert [node["node_name"] for node in result] == [
        "AgentRunner",
        "AgentRunner.run",
        "RerankAgent",
        "RerankAgent.__init__",
    ]


def test_parent_anchor_quota_counts_unique_spans_across_routes(tmp_path):
    dense_nodes = [
        {
            "file": "src/models.py",
            "node_name": "PreparedRequest.prepare_url",
            "type": "method",
            "start_line": 10,
            "end_line": 30,
            "content": "def prepare_url(self): pass",
        },
        {
            "file": "src/models.py",
            "node_name": "PreparedRequest.prepare_body",
            "type": "method",
            "start_line": 32,
            "end_line": 52,
            "content": "def prepare_body(self): pass",
        },
        {
            "file": "src/sessions.py",
            "node_name": "Session.prepare_request",
            "type": "method",
            "start_line": 60,
            "end_line": 80,
            "content": "def prepare_request(self): pass",
        },
        {
            "file": "src/sessions.py",
            "node_name": "Session.merge_environment_settings",
            "type": "method",
            "start_line": 82,
            "end_line": 102,
            "content": "def merge_environment_settings(self): pass",
        },
    ]
    sparse_aliases = [
        {
            **dense_nodes[0],
            "node_name": "src/models.py:PreparedRequest.prepare_url()",
        },
        {
            **dense_nodes[2],
            "node_name": "src/sessions.py:Session.prepare_request()",
        },
    ]
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=_FakeVectorStore(dense_nodes),
        bm25=_FakeBM25(sparse_aliases),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "request-preparation",
            "title": "Request Preparation",
            "summary": "Preparing URL, body, and session settings",
            "keywords": ["prepare", "request", "settings"],
            "files": ["src/models.py", "src/sessions.py"],
            "children": [
                {
                    "id": "url-preparation",
                    "title": "URL Preparation",
                    "files": ["src/models.py"],
                },
                {
                    "id": "session-settings",
                    "title": "Session Settings",
                    "files": ["src/sessions.py"],
                },
            ],
        },
        top_k=8,
    )

    assert {
        (node["file"], node["start_line"], node["end_line"]) for node in result
    } == {
        ("src/models.py", 10, 30),
        ("src/models.py", 32, 52),
        ("src/sessions.py", 60, 80),
        ("src/sessions.py", 82, 102),
    }


def test_parent_anchor_quota_keeps_distinct_symbols_without_spans(tmp_path):
    dense_nodes = [
        {
            "file": "src/models.py",
            "node_name": symbol,
            "type": "method",
            "start_line": 0,
            "end_line": 0,
            "content": content,
        }
        for symbol, content in (
            ("PreparedRequest.prepare_url", "def prepare_url(self): pass"),
            ("PreparedRequest.prepare_body", "def prepare_body(self): pass"),
            ("PreparedRequest.prepare_headers", "def prepare_headers(self): pass"),
        )
    ]
    sparse_alias = {
        **dense_nodes[0],
        "node_name": "src/models.py:PreparedRequest.prepare_url()",
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=_FakeVectorStore(dense_nodes),
        bm25=_FakeBM25([sparse_alias]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "request-preparation",
            "title": "Request Preparation",
            "summary": "Preparing URL, body, and headers",
            "keywords": ["prepare", "request"],
            "files": ["src/models.py"],
            "children": [
                {
                    "id": "prepared-request-fields",
                    "title": "Prepared Request Fields",
                    "files": ["src/models.py"],
                }
            ],
        },
        top_k=8,
    )

    assert {node["node_name"] for node in result} == {
        "PreparedRequest.prepare_url",
        "PreparedRequest.prepare_body",
        "PreparedRequest.prepare_headers",
    }


def test_parent_with_one_shared_file_keeps_multiple_broad_symbols(tmp_path):
    nodes = [
        {
            "file": "include/fmt/chrono.h",
            "node_name": "duration_cast",
            "type": "function",
            "start_line": 10,
            "end_line": 20,
            "content": "duration duration duration",
        },
        {
            "file": "include/fmt/chrono.h",
            "node_name": "parse_chrono_format",
            "type": "function",
            "start_line": 30,
            "end_line": 50,
            "content": "parse chrono format",
        },
        {
            "file": "include/fmt/chrono.h",
            "node_name": "write_tm_str",
            "type": "function",
            "start_line": 60,
            "end_line": 70,
            "content": "write chrono time string",
        },
    ]
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="cpp"),
        vector_store=None,
        bm25=_FakeBM25(nodes),
        manifest=SimpleNamespace(languages=["cpp"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "chrono-support",
            "title": "Chrono Support",
            "summary": "Chrono parsing and formatting",
            "keywords": ["chrono", "format"],
            "files": ["include/fmt/chrono.h"],
            "children": [
                {
                    "id": "duration-casting",
                    "title": "Duration Casting",
                    "keywords": ["duration", "cast"],
                    "files": ["include/fmt/chrono.h"],
                }
            ],
        },
        top_k=8,
    )

    assert [node["node_name"] for node in result] == [
        "parse_chrono_format",
        "write_tm_str",
        "duration_cast",
    ]


def test_parent_boundary_prefers_request_actions_over_long_helpers(tmp_path):
    wiki = AgentWiki(
        SimpleNamespace(
            entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        ),
        model="fake-model",
    )
    meta = {
        "id": "graph-navigation",
        "title": "Graph Navigation",
        "keywords": ["graph", "navigation"],
        "children": [{"id": "graph", "title": "Graph"}],
        "_parent_boundary_fallback": True,
    }
    visualize = {
        "node_name": "CodeGraph.visualize_graph",
        "type": "method",
        "start_line": 1,
        "end_line": 150,
        "content": "def visualize_graph(self): pass",
    }
    query = {
        "node_name": "CodeGraph.query_range",
        "type": "method",
        "start_line": 1,
        "end_line": 40,
        "content": "def query_range(self, file, start, end): pass",
    }

    assert wiki._outline_anchor_rank(meta, query) > wiki._outline_anchor_rank(
        meta,
        visualize,
    )


def test_overview_topic_terms_outrank_generic_handler_names(tmp_path):
    wiki = AgentWiki(
        SimpleNamespace(
            entry=SimpleNamespace(repo_dir=str(tmp_path), language="cpp"),
        ),
        model="fake-model",
    )
    meta = {
        "id": "overview",
        "title": "Chrono Support",
        "keywords": ["chrono", "parse_chrono_format", "duration_cast"],
    }
    topic_symbol = {
        "node_name": "parse_chrono_format",
        "type": "function",
        "start_line": 10,
        "end_line": 80,
        "content": "parse chrono format",
    }
    generic_handler = {
        "node_name": "handle_nan_inf",
        "type": "function",
        "start_line": 90,
        "end_line": 110,
        "content": "handle nan and infinity values",
    }

    assert wiki._outline_anchor_rank(
        meta,
        topic_symbol,
    ) > wiki._outline_anchor_rank(meta, generic_handler)


def test_page_retrieval_promotes_two_symbols_per_outline_file(tmp_path):
    target_nodes = [
        {
            "file": "src/runtime.py",
            "node_name": "AgentRunner",
            "start_line": 0,
            "end_line": 20,
            "content": "class AgentRunner: pass",
        },
        {
            "file": "src/runtime.py",
            "node_name": "run",
            "start_line": 22,
            "end_line": 40,
            "content": "def run(): pass",
        },
    ]
    unrelated = [
        {
            "file": f"src/helper_{index}.py",
            "node_name": f"helper_{index}",
            "start_line": 0,
            "end_line": 4,
            "content": "agent runtime helper",
        }
        for index in range(4)
    ]
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25([target_nodes[0], *unrelated, target_nodes[1]]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "summary": "Agent execution and context handling",
            "keywords": ["AgentRunner", "run"],
            "files": ["src/runtime.py"],
        },
        top_k=4,
    )

    assert [node["node_name"] for node in result[:2]] == ["AgentRunner", "run"]
    assert all(
        wiki._retrieval_routes[candidate_key(node, wiki._node_attr)][0] == "outline"
        for node in target_nodes
    )


def test_page_retrieval_prefers_public_outline_symbols(tmp_path):
    private = {
        "file": "src/wiki.py",
        "node_name": "AgentWiki._retrieve",
        "start_line": 0,
        "end_line": 10,
        "type": "method",
        "content": "def _retrieve(): pass",
    }
    public = {
        "file": "src/wiki.py",
        "node_name": "AgentWiki.page",
        "start_line": 12,
        "end_line": 22,
        "type": "method",
        "content": "def page(): pass",
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25([private, public]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "wiki-serving",
            "title": "Wiki Serving",
            "summary": "Wiki page serving",
            "keywords": ["wiki", "page"],
            "files": ["src/wiki.py"],
        },
        top_k=4,
    )

    assert [node["node_name"] for node in result[:2]] == [
        "AgentWiki.page",
        "AgentWiki._retrieve",
    ]


def test_page_retrieval_excludes_eval_candidates_from_runtime_page(tmp_path):
    runtime = {
        "file": "src/agent/runner.py",
        "node_name": "AgentRunner.run",
        "start_line": 0,
        "end_line": 20,
        "type": "method",
        "content": "def run(): pass",
    }
    evaluation = {
        "file": "src/eval/agent_study.py",
        "node_name": "run_agent_study",
        "start_line": 0,
        "end_line": 20,
        "type": "function",
        "content": "def run_agent_study(): pass",
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25([evaluation, runtime]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "summary": "Agent execution",
            "keywords": ["agent", "run"],
            "files": ["src/agent/runner.py"],
        },
        top_k=4,
    )

    assert [node["node_name"] for node in result] == ["AgentRunner.run"]


def test_page_retrieval_adds_representative_symbols_from_outline_files(tmp_path):
    private = {
        "file": "src/agent/runner.py",
        "node_name": "AgentRunner._serialize",
        "start_line": 0,
        "end_line": 20,
        "type": "method",
        "content": "def _serialize(): pass",
    }
    public = Symbol(
        file="src/agent/runner.py",
        name="AgentRunner.run",
        type="method",
        start_line=30,
        end_line=120,
        content="def run(): execute_agent_loop()",
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25([private]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    wiki._wb._symbols = lambda: (public,)

    result = wiki._retrieve(
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "summary": "Agent execution",
            "keywords": ["agent", "run"],
            "files": ["src/agent/runner.py"],
        },
        top_k=4,
    )

    assert wiki._node_attr(result[0], "name") == "AgentRunner.run"
    assert wiki._retrieval_routes[candidate_key(public, wiki._node_attr)] == (
        "outline",
    )


def test_page_retrieval_prefers_public_remaining_candidates(tmp_path):
    anchor = {
        "file": "src/runtime.py",
        "node_name": "AgentRunner.run",
        "start_line": 0,
        "end_line": 20,
        "type": "method",
        "content": "def run(): pass",
    }
    private = {
        "file": "src/internal.py",
        "node_name": "_serialize",
        "start_line": 0,
        "end_line": 20,
        "type": "function",
        "content": "def _serialize(): return agent_context",
    }
    public = {
        "file": "src/context.py",
        "node_name": "ContextLedger",
        "start_line": 0,
        "end_line": 20,
        "type": "class",
        "content": "class ContextLedger: pass",
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25([anchor, private, public]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "summary": "Agent execution and context",
            "keywords": ["agent", "context"],
            "files": ["src/runtime.py"],
        },
        top_k=4,
    )

    assert [node["node_name"] for node in result[:3]] == [
        "AgentRunner.run",
        "ContextLedger",
        "_serialize",
    ]


def test_overview_retrieval_reserves_space_for_architecture_anchors(tmp_path):
    files = [
        "README.md",
        "src/cli.py",
        "src/compiler.py",
        "src/runtime.py",
        "src/server.py",
        "src/wiki.py",
    ]
    for file in files:
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {file}\n")

    unrelated = {
        "file": "src/helper.py",
        "node_name": "_coerce_value",
        "start_line": 0,
        "end_line": 2,
        "content": "def _coerce_value(value): return value",
    }
    cli_symbol = {
        "file": "src/cli.py",
        "node_name": "main",
        "start_line": 0,
        "end_line": 2,
        "content": "def main(): return compile_repository()",
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25([cli_symbol, unrelated]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "overview",
            "title": "Overview",
            "summary": "Repository purpose and architecture",
            "keywords": ["entry point", "runtime", "coerce"],
            "files": files,
        },
        top_k=8,
    )

    assert [node["file"] for node in result[:6]] == files
    assert result[1]["node_name"] == "main"
    assert result[6]["node_name"] == "_coerce_value"
    assert wiki._retrieval_routes[candidate_key(cli_symbol, wiki._node_attr)] == (
        "outline",
        "bm25",
    )


def test_overview_retrieval_keeps_two_anchors_per_major_topic(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("Repository overview.\n")
    runtime_path = tmp_path / "src/runtime.py"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text(
        "class AgentRunner:\n"
        "    def run(self):\n"
        "        return execute_agent_loop()\n"
    )
    runtime_class = {
        "file": "src/runtime.py",
        "node_name": "AgentRunner",
        "type": "class",
        "start_line": 0,
        "end_line": 2,
        "content": runtime_path.read_text(),
    }
    runtime_entry = {
        "file": "src/runtime.py",
        "node_name": "AgentRunner.run",
        "type": "method",
        "start_line": 1,
        "end_line": 2,
        "content": "def run(self):\n    return execute_agent_loop()",
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(repo_dir=str(tmp_path), language="python"),
        vector_store=None,
        bm25=_FakeBM25([runtime_entry, runtime_class]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")

    result = wiki._retrieve(
        {
            "id": "overview",
            "title": "Overview",
            "summary": "Repository architecture",
            "keywords": ["runtime"],
            "files": ["README.md", "src/runtime.py"],
            "major_topics": [
                {
                    "title": "Agent Runtime",
                    "files": ["src/runtime.py"],
                }
            ],
        },
        top_k=4,
    )

    names = [wiki._node_attr(node, "node_name") for node in result]
    assert "AgentRunner" in names
    assert "AgentRunner.run" in names


def test_overview_uses_validated_fact_plan_without_narration(tmp_path):
    source = {
        "README.md": (
            "The repository compiles local source into lexical, semantic, and "
            "structural views for a source-linked developer Wiki and repository "
            "tools. It stores reusable artifacts for later requests.\n\n"
            "Users run codenib wiki with a repository path. The command detects "
            "languages and starts the local Wiki."
        ),
        "src/cli.py": "def main():\n    return compile_repository()",
        "src/compiler.py": (
            "class Compiler:\n"
            "    def build(self, repository):\n"
            "        return repository.views()"
        ),
        "src/server.py": (
            "class Server:\n"
            "    def page(self, page_identifier: str) -> MarkdownContent:\n"
            "        return SourceLinkedWikiPage(page_identifier)"
        ),
    }
    for file, content in source.items():
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    plan_payload = {
        "thesis": {
            "statement": (
                "The repository compiles local source into lexical, semantic, "
                "and structural views"
            ),
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "role": "entry",
                        "statement": (
                            "Users run `codenib wiki` with a repository path "
                            "to detect languages and start the local Wiki"
                        ),
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Execution",
                "claims": [
                    {
                        "role": "flow",
                        "statement": (
                            "`main` invokes `compile_repository` to begin the "
                            "repository compilation workflow"
                        ),
                        "evidence": ["E2"],
                    },
                ],
            },
            {
                "title": "Subsystems",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "The `Server` returns source-linked Wiki pages "
                            "to callers"
                        ),
                        "evidence": ["E4"],
                    },
                    {
                        "role": "contract",
                        "statement": (
                            "The page method accepts a page identifier and "
                            "returns Markdown content"
                        ),
                        "evidence": ["E4"],
                    },
                ],
            },
        ],
    }
    _add_overview_architecture(plan_payload)
    plan = json.dumps(plan_payload)

    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            return plan

    llm = LLM()
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=_FakeBM25([]),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )
    wiki = AgentWiki(bundle, model="fake-model", llm=llm)
    wiki._outline = {"pages": []}
    page = wiki._generate_page(
        {
            "id": "overview",
            "title": "Overview",
            "summary": "Repository purpose and architecture",
            "keywords": ["workflow", "compiler", "server"],
            "files": list(source),
        }
    )

    assert llm.calls == 1
    assert page["generation"]["renderer"] == "fact_plan"
    assert page["generation"]["fallback"] is None
    assert page["generation"]["mode"] == "generated"
    assert page["quality"]["valid"] is True
    assert page["architecture"]["primary_path"] == [
        "reader",
        "surface",
        "coordination",
        "execution",
        "result",
    ]
    assert len(page["media_slots"]) == 1
    assert page["media_slots"][0]["render_contract"]["provenance"] == (
        "architecture-plan"
    )
    assert "## Workflow" in page["markdown"]


def test_generated_page_uses_fact_plan_and_reports_grounding(tmp_path):
    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return json.dumps(
                {
                    "thesis": {
                        "statement": "The `Router` owns repository request dispatch",
                        "evidence": ["E1"],
                    },
                    "sections": [
                        {
                            "title": "Flow",
                            "claims": [
                                {
                                    "role": "responsibility",
                                    "statement": (
                                        "`dispatch` invokes `handle` for "
                                        "source-backed repository requests"
                                    ),
                                    "evidence": ["E1"],
                                }
                            ],
                        }
                    ],
                }
            )

    node = {
        "file": "src/core.py",
        "node_name": "Router",
        "type": "class",
        "start_line": 0,
        "end_line": 6,
        "content": (
            "class Router:\n"
            "    def dispatch(self, repository_request):\n"
            "        return handle(repository_request)"
        ),
    }
    llm = LLM()
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=_FakeVectorStore([node]),
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )
    database = tmp_path / "wiki-cache" / "wiki.sqlite3"
    wiki = AgentWiki(
        bundle, model="fake-model", llm=llm, store=SQLiteWikiStore(database)
    )
    meta = {
        "id": "routing",
        "title": "Request Routing",
        "summary": "How requests move through the repository",
        "keywords": ["dispatch"],
        "files": ["src/core.py"],
    }
    wiki._outline = {"pages": [meta]}
    wiki._write_cache("outline", wiki._outline)
    page = wiki.page("routing")

    assert len(llm.calls) == 1
    assert page["grounding"]["valid"] is True
    assert page["quality"]["valid"] is True
    assert page["generation"]["mode"] == "generated"
    assert page["generation"]["renderer"] == "fact_plan"
    assert page["generation"]["metrics"]["model_calls"] == 1
    assert page["generation"]["metrics"]["repair_attempts"] == 0
    assert page["generation"]["metrics"]["total_ms"] >= 0
    assert page["citations"][0]["start_line"] == 1
    assert page["evidence"]["items"][0]["routes"] == ["outline", "dense"]

    reloaded = AgentWiki(
        bundle, model="another-model", llm=llm, store=SQLiteWikiStore(database)
    )
    assert reloaded.page("routing")["evidence"] == page["evidence"]
    assert len(llm.calls) == 1


def test_empty_fact_plan_replans_from_original_evidence(tmp_path):
    valid_plan = json.dumps(
        {
            "thesis": {
                "statement": "The `Router` owns repository request dispatch",
                "evidence": ["E1"],
            },
            "sections": [
                {
                    "title": "Flow",
                    "claims": [
                        {
                            "role": "responsibility",
                            "statement": (
                                "`dispatch` invokes `handle` for source-backed "
                                "repository requests"
                            ),
                            "evidence": ["E1"],
                        }
                    ],
                }
            ],
        }
    )

    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.prompts = []

        def complete(self, messages, **_kwargs):
            prompt = messages[0]["content"]
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return '{"thesis": {}, "sections": []}'
            # An empty admitted plan must start over from the original source
            # prompt instead of trying to revise the poisoned plan.
            assert prompt.startswith("You are planning one source-grounded page")
            return valid_plan

    node = {
        "file": "src/core.py",
        "node_name": "Router",
        "type": "class",
        "start_line": 0,
        "end_line": 6,
        "content": (
            "class Router:\n"
            "    def dispatch(self, repository_request):\n"
            "        return handle(repository_request)"
        ),
    }
    llm = LLM()
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=_FakeVectorStore([node]),
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )

    page = AgentWiki(bundle, model="fake-model", llm=llm)._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "How requests move through the repository",
            "keywords": ["dispatch"],
            "files": ["src/core.py"],
        }
    )

    assert len(llm.prompts) == 2
    assert page["generation"]["mode"] == "generated"
    assert page["generation"]["fallback"] is None
    assert page["generation"]["metrics"]["model_calls"] == 2
    assert page["generation"]["metrics"]["fresh_replans"] == 1
    assert page["generation"]["metrics"]["repair_attempts"] == 0
    assert "is indexed from" not in page["markdown"]


def test_structured_page_deduplicates_without_free_form_markdown_repair(tmp_path):
    node = {
        "file": "src/core.py",
        "node_name": "Router",
        "type": "class",
        "start_line": 0,
        "end_line": 8,
        "content": (
            "class Router:\n"
            '    """Repository request boundary."""\n'
            "    def dispatch(self, repository_request):\n"
            "        return handle(repository_request)"
        ),
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=_FakeVectorStore([node]),
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )
    wiki = AgentWiki(bundle, model="fake-model", llm=SimpleNamespace())
    wiki._fact_plan = lambda *_args: (
        {
            "thesis": {
                "statement": "`Router` defines the repository request boundary",
                "evidence": ["E1"],
            },
            "sections": [
                {
                    "title": "Dispatch",
                    "claims": [
                        {
                            "role": "responsibility",
                            "statement": (
                                "`Router` dispatches repository requests through "
                                "`handle`"
                            ),
                            "evidence": ["E1"],
                        },
                        {
                            "role": "responsibility",
                            "statement": (
                                "`Router` dispatches the repository request by "
                                "calling `handle`"
                            ),
                            "evidence": ["E1"],
                        },
                    ],
                }
            ],
        },
        [],
    )

    def reject_free_form_repair(*_args, **_kwargs):
        raise AssertionError("structured pages must not use free-form repair")

    wiki._repair_markdown = reject_free_form_repair
    page = wiki._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "How requests move through the repository",
            "keywords": ["dispatch"],
            "files": ["src/core.py"],
        }
    )

    assert page["quality"]["valid"] is True
    assert page["generation"]["mode"] == "generated"
    assert page["generation"]["repaired"] is False
    assert page["generation"]["renderer"] == "fact_plan"
    assert page["markdown"].count("`Router` dispatches") == 1


def test_generation_separates_soft_composition_and_semantic_diagnostics(tmp_path):
    node = {
        "file": "src/core.py",
        "node_name": "Router",
        "type": "class",
        "start_line": 0,
        "end_line": 6,
        "content": (
            "class Router:\n"
            "    def handle(self, source_backed_request):\n"
            "        return source_backed_request"
        ),
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=_FakeVectorStore([node]),
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )
    wiki = AgentWiki(bundle, model="fake-model", llm=SimpleNamespace())
    plan = {
        "thesis": {
            "statement": ("The `Router` handles source-backed repository requests"),
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Responsibility",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "The `Router` handles source-backed repository requests"
                        ),
                        "evidence": ["E1"],
                    },
                    {
                        "role": "contract",
                        "statement": "`handle` returns the source-backed request",
                        "evidence": ["E1"],
                    },
                ],
            }
        ],
    }
    soft_warning = "page thesis must contain exactly one sentence"
    wiki._fact_plan = lambda *_args: (plan, [soft_warning])

    page = wiki._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "How repository requests are handled",
            "keywords": ["router"],
            "files": ["src/core.py"],
        }
    )

    assert page["grounding"]["valid"] is True
    assert page["quality"]["valid"] is True
    assert page["generation"]["mode"] == "generated"
    assert page["generation"]["plan_warnings"] == [soft_warning]
    assert page["generation"]["reason"] is None

    isolated_warning = (
        "page plan is dominated by isolated operation sections: Responsibility"
    )
    wiki._fact_plan = lambda *_args: (plan, [isolated_warning])

    isolated = wiki._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "How repository requests are handled",
            "keywords": ["router"],
            "files": ["src/core.py"],
        }
    )

    # A composition note says the page could be organised better, not that it
    # says something unsupported. It is recorded and steers repair, but it does
    # not stop the page from being published -- a richer page has more sections
    # and so more surface for these to fire on, and gating on them marked every
    # page degraded while its grounding report was clean.
    assert isolated["grounding"]["valid"] is True
    assert isolated["quality"]["valid"] is True
    assert isolated["generation"]["mode"] == "generated"
    assert isolated["generation"]["reason"] is None
    assert isolated["generation"]["plan_warnings"] == [isolated_warning]

    semantic_warning = (
        "claim 'The Router handles requests' is not supported by concrete terms "
        "in its cited source evidence"
    )
    wiki._fact_plan = lambda *_args: (plan, [semantic_warning])

    guarded = wiki._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "How repository requests are handled",
            "keywords": ["router"],
            "files": ["src/core.py"],
        }
    )

    # An unsupported fact is the tier that still blocks publication.
    assert guarded["grounding"]["valid"] is True
    assert guarded["quality"]["valid"] is True
    assert guarded["generation"]["mode"] == "degraded"
    assert guarded["generation"]["reason"] == "quality_guard"
    assert guarded["generation"]["plan_warnings"] == [semantic_warning]


def test_generation_mode_requires_the_strict_page_quality_gate(tmp_path, monkeypatch):
    import codenib.wiki.agent_wiki as agent_wiki_module

    node = {
        "file": "src/core.py",
        "node_name": "Router",
        "type": "class",
        "start_line": 0,
        "end_line": 5,
        "content": (
            "class Router:\n"
            "    def handle(self, repository_request):\n"
            "        return repository_request"
        ),
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=_FakeVectorStore([node]),
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )
    wiki = AgentWiki(bundle, model="fake-model", llm=SimpleNamespace())
    wiki._fact_plan = lambda *_args: (
        {
            "thesis": {
                "statement": "The `Router` handles repository requests",
                "evidence": ["E1"],
            },
            "sections": [
                {
                    "title": "Request handling",
                    "claims": [
                        {
                            "role": "contract",
                            "statement": (
                                "`Router.handle()` returns the repository request"
                            ),
                            "evidence": ["E1"],
                        }
                    ],
                }
            ],
        },
        [],
    )
    real_report = agent_wiki_module._page_quality_report

    def force_quality_failure(*args, **kwargs):
        report = real_report(*args, **kwargs)
        return {**report, "valid": False}

    monkeypatch.setattr(
        agent_wiki_module,
        "_page_quality_report",
        force_quality_failure,
    )

    page = wiki._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "Repository request handling",
            "keywords": ["router", "request"],
            "files": ["src/core.py"],
        }
    )

    assert page["grounding"]["valid"] is True
    assert page["quality"]["valid"] is False
    assert page["generation"]["mode"] == "degraded"
    assert page["generation"]["reason"] == "quality_guard"


def test_page_reports_model_unavailable_when_fact_planning_falls_back(tmp_path):
    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            raise RuntimeError("provider unavailable")

    node = {
        "file": "src/core.py",
        "node_name": "Router",
        "type": "class",
        "start_line": 0,
        "end_line": 2,
        "content": "class Router:\n    pass",
    }
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=_FakeVectorStore([node]),
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        code_graph=lambda: None,
    )

    llm = LLM()
    page = AgentWiki(bundle, model="fake-model", llm=llm)._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "How requests move through the repository",
            "keywords": ["dispatch"],
            "files": ["src/core.py"],
        }
    )

    assert page["generation"]["mode"] == "degraded"
    assert page["generation"]["reason"] == "model_unavailable"
    assert page["generation"]["renderer"] == "fact_plan"
    assert page["generation"]["fallback"] == "fact_plan"
    assert page["grounding"]["valid"] is True
    assert page["generation"]["metrics"]["model_calls"] == 1
    assert page["generation"]["metrics"]["model_failures"] == 1
    assert llm.calls == 1


def test_agent_wiki_store_entry_id_tracks_view_rebuild_identity(tmp_path):
    view = SimpleNamespace(
        status="fresh",
        commit="abc123",
        built_at_epoch=1.0,
        config={"builder_schema": 1},
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={"bm25": view}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    before = wiki._store_entry_id("outline")
    view.built_at_epoch = 2.0
    view.config = {"builder_schema": 2}

    assert wiki._store_entry_id("outline") != before


def test_agent_wiki_store_entry_id_ignores_equivalent_rebuild_timestamp(tmp_path):
    view = SimpleNamespace(
        status="fresh",
        commit="abc123",
        source_fingerprint="sha256:source",
        built_at_epoch=1.0,
        config={"builder_schema": 1},
        metadata={"artifact_digest": "sha256:artifact"},
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(
            languages=["python"],
            indexes={"bm25": view},
            source_fingerprint="sha256:source",
        ),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    before = wiki._store_entry_id("outline")

    view.built_at_epoch = 2.0

    assert wiki._store_entry_id("outline") == before


def test_agent_wiki_store_entry_id_tracks_artifact_receipt(tmp_path):
    view = SimpleNamespace(
        status="fresh",
        commit="abc123",
        source_fingerprint="sha256:source",
        built_at_epoch=1.0,
        config={"builder_schema": 1},
        metadata={"artifact_digest": "sha256:first"},
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(
            languages=["python"],
            indexes={"bm25": view},
            source_fingerprint="sha256:source",
        ),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    before = wiki._store_entry_id("outline")

    view.metadata["artifact_digest"] = "sha256:second"

    assert wiki._store_entry_id("outline") != before


def test_agent_wiki_store_entry_id_tracks_view_source_identity(tmp_path):
    view = SimpleNamespace(
        status="fresh",
        commit="abc123",
        source_fingerprint="sha256:first",
        built_at_epoch=1.0,
        config={"builder_schema": 1},
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={"bm25": view}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    before = wiki._store_entry_id("outline")
    view.source_fingerprint = "sha256:second"

    assert wiki._store_entry_id("outline") != before


def test_agent_wiki_store_entry_id_tracks_source_selection_identity(tmp_path):
    manifest = SimpleNamespace(
        languages=["python"],
        indexes={},
        source_fingerprint="sha256:source",
        source_selection_digest="sha256:first-selection",
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=manifest,
    )
    wiki = AgentWiki(bundle, model="fake-model")
    before = wiki._store_entry_id("outline")

    manifest.source_selection_digest = "sha256:second-selection"

    assert wiki._store_entry_id("outline") != before


def test_agent_wiki_store_entry_id_is_stable_across_lazy_client_creation(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    before = wiki._store_entry_id("outline")
    wiki._llm = SimpleNamespace(cache_identity="created-lazily")

    assert wiki._store_entry_id("outline") == before


def test_agent_wiki_persists_through_injected_store_without_json_mirror(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    cache_dir = tmp_path / "wiki-cache"
    store = SQLiteWikiStore(cache_dir / "wiki.sqlite3")
    original = AgentWiki(
        bundle,
        model="first-model",
        store=store,
    )
    payload = {"pages": [{"id": "overview", "title": "Overview"}]}

    original._write_cache("outline", payload)

    assert not list(cache_dir.glob("agentwiki_*.json"))

    class UnexpectedLLM:
        cache_identity = "must-not-run"

        def complete(self, *_args, **_kwargs):
            raise AssertionError("a database hit must not invoke the model")

    reloaded = AgentWiki(
        bundle,
        model="different-model",
        store=SQLiteWikiStore(cache_dir / "wiki.sqlite3"),
        llm=UnexpectedLLM(),
    )
    assert reloaded.outline() == payload
    stored = store.read(original._store_entry_id("outline"))
    assert stored is not None
    assert stored.repository_id == "owner__repo-1"
    assert stored.envelope == {
        "model": "first-model",
        "api_base": "",
        "llm_identity": "",
        "data": payload,
    }


def test_page_prompt_upgrade_reuses_outline_without_reusing_old_page(
    tmp_path, monkeypatch
):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    store = SQLiteWikiStore(tmp_path / "wiki-cache" / "wiki.sqlite3")
    meta = {"id": "overview", "title": "Overview"}
    outline = {"pages": [meta]}
    old_page = {"id": "overview", "content": "Cached before the range fix."}
    suffix = AgentWiki._page_cache_suffix(meta)
    with monkeypatch.context() as previous_version:
        previous_version.setattr(agent_wiki_module, "_PAGE_PROMPT_VERSION", "128")
        original = AgentWiki(bundle, model="fake-model", store=store)
        original._write_cache("outline", outline)
        original._write_cache(suffix, old_page)
        old_entry_id = original._store_entry_id(suffix)
        old_entry = store.read(old_entry_id)
        assert original._read_cache(suffix) == old_page

    reloaded = AgentWiki(bundle, model="fake-model", store=store)

    assert reloaded._read_cache("outline") == outline
    assert reloaded._read_cache(suffix) is None
    assert store.read(old_entry_id) == old_entry
    assert reloaded._llm is None


def test_agent_wiki_reads_store_envelope_with_retired_provenance_field(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    store = SQLiteWikiStore(tmp_path / "wiki-cache" / "wiki.sqlite3")
    wiki = AgentWiki(bundle, model="fake-model", store=store)
    payload = {"pages": [{"id": "overview", "title": "Overview"}]}
    entry_id = wiki._store_entry_id("outline")
    store.publish(
        entry_id=entry_id,
        repository_id="owner__repo-1",
        envelope={
            "model": "old-model",
            "data": payload,
            "_codenib_cache_provenance": {
                "schema": 1,
                "legacy_filenames": ["agentwiki_retired.json"],
            },
        },
    )

    assert wiki._read_cache("outline") == payload
    assert wiki.outline() == payload


def test_agent_wiki_without_store_does_not_persist_across_instances(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    payload = {"pages": [{"id": "overview", "title": "Overview"}]}
    first = AgentWiki(bundle, model="fake-model")
    second = AgentWiki(bundle, model="fake-model")

    first._write_cache("outline", payload)

    assert first._read_cache("outline") is None
    assert second._read_cache("outline") is None


def test_agent_wiki_cached_page_tree_reads_store_without_generating(
    tmp_path, monkeypatch
):
    view = SimpleNamespace(
        status="fresh",
        commit="abc123",
        source_fingerprint="",
        built_at_epoch=1.0,
        config={},
        metadata={},
    )
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={"bm25": view}),
    )
    cache_dir = tmp_path / "wiki-cache"
    store = SQLiteWikiStore(cache_dir / "wiki.sqlite3")
    original = AgentWiki(bundle, model="old-model", store=store)
    original._write_cache(
        "outline",
        {"pages": [{"id": "runtime", "title": "Runtime", "children": []}]},
    )
    wiki = AgentWiki(bundle, model="fake-model", store=store)
    monkeypatch.setattr(
        wiki,
        "outline",
        lambda: (_ for _ in ()).throw(AssertionError("must not generate outline")),
    )

    tree = wiki.cached_page_tree()

    assert tree == [
        {
            "id": "runtime",
            "title": "Runtime",
            "cache_state": "cold",
            "children": [],
        }
    ]


def test_agent_wiki_page_cache_key_tracks_outline_metadata():
    original = {
        "id": "runtime",
        "title": "Runtime",
        "summary": "Requests enter through the command router.",
        "keywords": ["router"],
        "files": ["src/router.py"],
        "children": [],
    }
    revised = {
        **original,
        "summary": "The command router passes requests to the runtime.",
        "files": ["src/router.py", "src/runtime.py"],
    }

    assert AgentWiki._page_cache_suffix(original) != AgentWiki._page_cache_suffix(
        revised
    )


def test_agent_wiki_page_tree_reports_ready_cold_and_degraded_cache_states(tmp_path):
    runtime = {
        "id": "runtime",
        "title": "Runtime",
        "children": [
            {"id": "routing", "title": "Routing", "children": []},
        ],
    }
    broken = {"id": "broken", "title": "Broken", "children": []}
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        manifest=SimpleNamespace(languages=["python"], indexes={}),
        vector_store=None,
        bm25=None,
    )
    wiki = AgentWiki(bundle, model="fake-model")
    wiki._outline = {"pages": [runtime, broken]}
    cached = {
        wiki._page_cache_suffix(runtime): {
            "generation": {"mode": "generated"},
            "quality": {"valid": True},
        },
        wiki._page_cache_suffix(broken): {
            "generation": {
                "mode": "degraded",
                "retry": {"attempts": 2},
            },
            "quality": {"valid": False},
        },
    }
    wiki._read_cache = lambda suffix: cached.get(suffix)

    tree = wiki.page_tree()

    assert tree[0]["cache_state"] == "ready"
    assert tree[0]["children"][0]["cache_state"] == "cold"
    assert tree[1]["cache_state"] == "degraded"


def test_agent_wiki_removes_legacy_page_visuals_without_regenerating_prose():
    legacy = {
        "id": "overview",
        "title": "Overview",
        "markdown": "# Overview\n\nSource-grounded prose.",
        "citations": [{"file": "src/api.py"}],
        "story": {
            "beats": [
                {"section": "Enter", "role": "entry", "evidence": ["E1"]},
                {"section": "Run", "role": "outcome", "evidence": ["E2"]},
            ]
        },
        "media_slots": [
            {
                "id": "overview-story-storyboard",
                "kind": "storyboard",
                "placement": "appendix",
            }
        ],
    }

    refreshed = AgentWiki._refresh_media_plan(legacy)

    assert refreshed["markdown"] == legacy["markdown"]
    assert refreshed["media_plan_version"] == MEDIA_PLAN_VERSION
    assert refreshed["media_slots"] == []


def test_agent_wiki_page_citations_never_generate_prose_and_are_cached(tmp_path):
    node = {
        "file": "src/router.py",
        "node_name": "Router.run",
        "type": "method",
        "start_line": 0,
        "end_line": 2,
        "content": "def run(request):\n    return dispatch(request)",
    }
    vector_store = _FakeVectorStore([node])
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=vector_store,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    outline = {
        "pages": [
            {
                "id": "runtime",
                "title": "Runtime",
                "summary": "Request routing",
                "files": ["src/router.py"],
                "children": [],
            }
        ]
    }
    cache_dir = tmp_path / "wiki-cache"
    wiki_store = SQLiteWikiStore(cache_dir / "wiki.sqlite3")
    wiki = AgentWiki(bundle, model="fake-model", store=wiki_store)
    wiki._outline = outline
    wiki._generate_page = lambda _meta: (_ for _ in ()).throw(
        AssertionError("graph evidence must not generate prose")
    )

    citations = wiki.page_citations("runtime")

    assert citations == [
        {
            "file": "src/router.py",
            "start_line": 1,
            "end_line": 3,
            "node_name": "Router.run",
            "type": "method",
            "score": None,
            "content": None,
        }
    ]
    assert len(vector_store.calls) == 1
    assert wiki._pages == {}

    vector_store.calls.clear()
    reloaded = AgentWiki(
        bundle,
        model="other-model",
        store=SQLiteWikiStore(cache_dir / "wiki.sqlite3"),
    )
    reloaded._outline = outline
    assert reloaded.page_citations("runtime") == citations
    assert vector_store.calls == []


def test_agent_wiki_regenerates_cached_diagnostic_fallback(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    meta = {"id": "runtime", "title": "Runtime", "children": []}
    cached = {
        "id": "runtime",
        "markdown": "`Router` is indexed from `src/router.py`.",
        "generation": {
            "mode": "degraded",
            "fallback": "fact_plan",
            "reason": "quality_guard",
        },
    }
    generated = {
        "id": "runtime",
        "markdown": "Readable source-linked explanation.",
        "generation": {"mode": "generated", "fallback": None},
    }
    writes = []
    wiki._find = lambda _page_id: meta
    wiki._read_cache = lambda _suffix: cached
    wiki._generate_page = lambda _meta: generated
    wiki._write_cache = lambda suffix, page: writes.append((suffix, page))

    assert wiki.page("runtime") is generated
    assert writes == [(wiki._page_cache_suffix(meta), generated)]


def test_agent_wiki_rechecks_in_memory_degraded_page_after_cooldown(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    meta = {"id": "runtime", "title": "Runtime", "children": []}
    degraded = {
        "id": "runtime",
        "generation": {
            "mode": "degraded",
            "fallback": "fact_plan",
            "retry": {"attempts": 0, "next_attempt_epoch": 0.0},
        },
        "quality": {"valid": False},
    }
    generated = {
        "id": "runtime",
        "markdown": "Recovered source-linked explanation.",
        "generation": {"mode": "generated", "fallback": None},
        "quality": {"valid": True},
    }
    wiki._pages["runtime"] = degraded
    wiki._find = lambda _page_id: meta
    wiki._read_cache = lambda _suffix: degraded
    wiki._generate_page = lambda _meta: generated
    wiki._write_cache = lambda _suffix, _page: None

    assert wiki.page("runtime") is generated
    assert wiki._pages["runtime"] is generated


def test_agent_wiki_retries_quality_invalid_cache_with_a_cooldown():
    legacy_invalid = {
        "generation": {"mode": "degraded", "fallback": None},
        "quality": {"valid": False},
    }

    assert AgentWiki._cached_page_needs_regeneration(legacy_invalid) is True

    AgentWiki._record_page_retry(legacy_invalid, now_epoch=10_000_000_000.0)
    retry = legacy_invalid["generation"]["retry"]
    assert retry["state"] == "scheduled"
    assert retry["attempts"] == 0
    assert retry["next_attempt_epoch"] > 10_000_000_000.0
    assert AgentWiki._cached_page_needs_regeneration(legacy_invalid) is False


def test_agent_wiki_retries_grounding_invalid_cache_with_a_cooldown():
    grounding_invalid = {
        "generation": {
            "mode": "degraded",
            "fallback": None,
            "reason": "quality_guard",
        },
        "grounding": {"valid": False},
        "quality": {"valid": True},
    }

    assert AgentWiki._cached_page_needs_regeneration(grounding_invalid) is True

    AgentWiki._record_page_retry(grounding_invalid, now_epoch=10_000_000_000.0)
    retry = grounding_invalid["generation"]["retry"]
    assert retry["state"] == "scheduled"
    assert retry["attempts"] == 0
    assert retry["next_attempt_epoch"] > 10_000_000_000.0
    assert AgentWiki._cached_page_needs_regeneration(grounding_invalid) is False


def test_agent_wiki_stops_retrying_after_bounded_failed_attempts():
    previous = {
        "generation": {
            "mode": "degraded",
            "fallback": None,
            "retry": {"attempts": 1},
        },
        "quality": {"valid": False},
    }
    still_invalid = {
        "generation": {"mode": "degraded", "fallback": None},
        "quality": {"valid": False},
    }

    AgentWiki._record_page_retry(
        still_invalid,
        previous=previous,
        now_epoch=100.0,
    )

    assert still_invalid["generation"]["retry"] == {
        "state": "exhausted",
        "attempts": 2,
        "max_attempts": 2,
        "last_attempt_epoch": 100.0,
        "next_attempt_epoch": None,
    }
    assert AgentWiki._cached_page_needs_regeneration(still_invalid) is False


def test_agent_wiki_records_recovery_from_a_bad_cached_page():
    previous = {
        "generation": {"mode": "degraded", "fallback": "fact_plan"},
        "quality": {"valid": False},
    }
    recovered = {
        "generation": {"mode": "generated", "fallback": None},
        "quality": {"valid": True},
    }

    AgentWiki._record_page_retry(recovered, previous=previous, now_epoch=100.0)

    assert recovered["generation"]["retry"]["state"] == "recovered"
    assert recovered["generation"]["retry"]["attempts"] == 1


def test_agent_wiki_coalesces_concurrent_page_generation(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    meta = {"id": "runtime", "title": "Runtime", "children": []}
    generation_started = threading.Event()
    second_lookup_started = threading.Event()
    release_generation = threading.Event()
    lookup_calls = 0
    generation_calls = 0
    calls_guard = threading.Lock()

    def find(_page_id):
        nonlocal lookup_calls
        with calls_guard:
            lookup_calls += 1
            if lookup_calls == 2:
                second_lookup_started.set()
        return meta

    def generate(_meta):
        nonlocal generation_calls
        with calls_guard:
            generation_calls += 1
        generation_started.set()
        assert release_generation.wait(timeout=2)
        return {"id": "runtime", "markdown": "generated once"}

    wiki._find = find
    wiki._generate_page = generate
    wiki._read_cache = lambda _suffix: None
    wiki._write_cache = lambda _suffix, _page: None

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(wiki.page, "runtime")
        assert generation_started.wait(timeout=2)
        second = executor.submit(wiki.page, "runtime")
        assert second_lookup_started.wait(timeout=2)
        release_generation.set()

        assert first.result(timeout=2) == second.result(timeout=2)

    assert generation_calls == 1


def test_agent_wiki_bounds_process_local_generation_wait(tmp_path, monkeypatch):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    wiki._outline = {"pages": [{"id": "runtime", "title": "Runtime", "children": []}]}
    owner = wiki._page_generation_lock("runtime")
    owner.acquire()
    monkeypatch.setattr(
        agent_wiki_module,
        "_GENERATION_LOCK_TIMEOUT_SECONDS",
        0.01,
    )
    try:
        with pytest.raises(WikiStoreError, match="lock wait timed out"):
            wiki.page("runtime")
    finally:
        owner.release()

    wiki._generate_page = lambda _meta: {
        "id": "runtime",
        "markdown": "generated after the owner released the lock",
    }
    wiki._read_cache = lambda _suffix: None
    wiki._write_cache = lambda _suffix, _page: None
    assert wiki.page("runtime")["markdown"] == (
        "generated after the owner released the lock"
    )


def test_agent_wiki_bounds_evidence_generation_wait(tmp_path, monkeypatch):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    meta = {"id": "runtime", "title": "Runtime", "children": []}
    wiki._outline = {"pages": [meta]}
    cache_suffix = wiki._page_cache_suffix(meta)
    monkeypatch.setattr(
        agent_wiki_module,
        "_GENERATION_LOCK_TIMEOUT_SECONDS",
        0.01,
    )

    evidence_owner = wiki._page_evidence_lock(cache_suffix)
    evidence_owner.acquire()
    try:
        with pytest.raises(WikiStoreError, match="lock wait timed out"):
            wiki.page_citations("runtime")
    finally:
        evidence_owner.release()

    wiki._retrieve = lambda *_args, **_kwargs: []
    retrieval_owner = wiki._evidence_retrieval_lock
    retrieval_owner.acquire()
    try:
        with pytest.raises(WikiStoreError, match="lock wait timed out"):
            wiki.page_citations("runtime")
    finally:
        retrieval_owner.release()

    assert wiki.page_citations("runtime") == []


def test_agent_wiki_coalesces_generation_across_store_instances(tmp_path):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    meta = {"id": "runtime", "title": "Runtime", "children": []}
    outline = {"pages": [meta]}
    cache_dir = tmp_path / "wiki-cache"
    builders = [
        AgentWiki(
            bundle,
            model="fake-model",
            store=SQLiteWikiStore(cache_dir / "wiki.sqlite3"),
        )
        for _ in range(2)
    ]
    for wiki in builders:
        wiki._outline = outline
    generation_started = threading.Event()
    release_generation = threading.Event()
    calls_guard = threading.Lock()
    generation_calls = 0

    def generate(_meta):
        nonlocal generation_calls
        with calls_guard:
            generation_calls += 1
        generation_started.set()
        assert release_generation.wait(timeout=2)
        return {
            "id": "runtime",
            "markdown": "generated once across processes",
            "generation": {"mode": "generated"},
            "quality": {"valid": True},
        }

    for wiki in builders:
        wiki._generate_page = generate

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(wiki.page, "runtime") for wiki in builders]
        assert generation_started.wait(timeout=2)
        release_generation.set()
        assert results[0].result(timeout=2) == results[1].result(timeout=2)

    assert generation_calls == 1


def test_flow_step_naming_an_unsupported_symbol_is_dropped():
    """A diagram must not invent a hop the evidence does not show."""

    from codenib.wiki.agent_wiki import _renderable_plan
    from codenib.wiki.evidence import EvidenceItem

    evidence = [
        EvidenceItem(
            id="E1",
            file="a.py",
            start_line=1,
            end_line=9,
            symbol="Router.handle",
            kind="method",
            content="def handle(self):\n    return self.dispatch()",
        )
    ]
    plan = {
        "thesis": {"statement": "`Router.handle()` dispatches.", "evidence": ["E1"]},
        "flow": {
            "title": "Path",
            "steps": [
                {
                    "from": "`Router.handle()`",
                    "to": "`Router.dispatch()`",
                    "evidence": ["E1"],
                },
                {
                    "from": "`Router.dispatch()`",
                    "to": "`Teleporter.beam()`",
                    "evidence": ["E1"],
                },
            ],
        },
        "sections": [
            {
                "title": "S",
                "claims": [
                    {
                        "role": "flow",
                        "statement": "`Router.handle()` calls `Router.dispatch()`.",
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }
    rendered = _renderable_plan(plan, evidence, [])
    # Only one step resolves, and one arrow is not a flow.
    assert "flow" not in rendered


def test_flow_keeps_only_its_connected_path():
    """Disconnected pairs are a relation list drawn with arrows, not a path."""

    from codenib.wiki.agent_wiki import _renderable_plan
    from codenib.wiki.evidence import EvidenceItem

    evidence = [
        EvidenceItem(
            id="E1",
            file="a.py",
            start_line=1,
            end_line=3,
            symbol="Session.dispatch",
            kind="method",
            content="def send(self):\n    prepare()",
        ),
        EvidenceItem(
            id="E2",
            file="a.py",
            start_line=4,
            end_line=6,
            symbol="prepare",
            kind="function",
            content="def prepare():\n    dispatch()",
        ),
        EvidenceItem(
            id="E3",
            file="b.py",
            start_line=1,
            end_line=3,
            symbol="unrelated_one",
            kind="function",
            content="def unrelated_one():\n    unrelated_two()",
        ),
    ]
    plan = {
        "thesis": {"statement": "`Session.send()` sends.", "evidence": ["E1"]},
        "flow": {
            "title": "Path",
            "steps": [
                {"from": "`send()`", "to": "`prepare()`", "evidence": ["E1"]},
                {"from": "`prepare()`", "to": "`dispatch()`", "evidence": ["E2"]},
                {
                    "from": "`unrelated_one()`",
                    "to": "`unrelated_two()`",
                    "evidence": ["E3"],
                },
            ],
        },
        "sections": [
            {
                "title": "S",
                "claims": [
                    {
                        "role": "flow",
                        "statement": "`send()` calls `prepare()`.",
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }
    steps = _renderable_plan(plan, evidence, [])["flow"]["steps"]
    assert len(steps) == 2
    assert all("unrelated" not in str(s) for s in steps)


def test_fact_plan_flow_fallback_caption_is_auditable_and_not_thin():
    evidence = [
        EvidenceItem(
            id="E1",
            file="a.py",
            start_line=1,
            end_line=6,
            symbol="Session.send",
            kind="method",
            content="def dispatch(self):\n    return adapter.send()",
        ),
        EvidenceItem(
            id="E2",
            file="b.py",
            start_line=1,
            end_line=6,
            symbol="Adapter.send",
            kind="method",
            content="def send(self):\n    return response.build()",
        ),
        EvidenceItem(
            id="E3",
            file="c.py",
            start_line=1,
            end_line=6,
            symbol="Response.build",
            kind="method",
            content="def build(self):\n    return Response()",
        ),
    ]
    relations = [
        RelationItem(
            id="R1",
            source="a.py:Session.dispatch()",
            target="b.py:Adapter.send()",
            anchors=("a.py:2",),
        ),
        RelationItem(
            id="R2",
            source="b.py:Adapter.send()",
            target="c.py:Response.build()",
            anchors=("b.py:2",),
        ),
    ]
    plan = {
        "flow": {
            "title": "",
            "steps": [
                {
                    "from": "`Session.dispatch()`",
                    "to": "`Adapter.send()`",
                    "evidence": ["R1"],
                },
                {
                    "from": "`Adapter.send()`",
                    "to": "`Response.build()`",
                    "evidence": ["R2"],
                },
            ],
        },
        "sections": [
            {
                "title": "Dispatch",
                "claims": [
                    {
                        "role": "flow",
                        "statement": "`Session.dispatch()` calls `Adapter.send()`",
                        "evidence": ["R1"],
                    }
                ],
            },
            {
                "title": "Transport",
                "claims": [
                    {
                        "role": "flow",
                        "statement": "`Adapter.send()` calls `Response.build()`",
                        "evidence": ["R2"],
                    }
                ],
            },
            {
                "title": "Response",
                "claims": [
                    {
                        "role": "contract",
                        "statement": "`Response.build()` returns a response",
                        "evidence": ["E3"],
                    }
                ],
            },
        ],
    }
    rendered = _renderable_plan(plan, evidence, relations)
    markdown = _fact_plan_markdown(rendered, evidence, relations)
    quality = _page_quality_report(
        markdown,
        rendered,
        require_dense_sections=True,
        relations=relations,
        evidence_items=evidence,
    )

    assert "## How it fits together" in markdown
    assert (
        "How it fits together. Each arrow is a call site recorded in the "
        "index. [R1] [R2]" in markdown
    )
    assert "How it fits together" not in quality["thin_sections"]


def test_flow_drops_names_that_exist_without_a_proven_relation():
    """Independent definitions must not become a source-checked flow."""

    from codenib.wiki.agent_wiki import _renderable_plan
    from codenib.wiki.evidence import EvidenceItem

    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file="workflow.py",
            start_line=index,
            end_line=index,
            symbol=name,
            kind="function",
            content=f"def {name}():\n    return {index}",
        )
        for index, name in enumerate(("alpha", "beta", "gamma"), start=1)
    ]
    plan = {
        "thesis": {"statement": "`alpha()` starts.", "evidence": ["E1"]},
        "flow": {
            "title": "Invented path",
            "steps": [
                {"from": "`alpha()`", "to": "`beta()`", "evidence": ["E1"]},
                {"from": "`beta()`", "to": "`gamma()`", "evidence": ["E2"]},
            ],
        },
        "sections": [
            {
                "title": "S",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": "`alpha()` returns one",
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }

    assert "flow" not in _renderable_plan(plan, evidence, [])


def test_handoff_label_keeps_only_the_purpose_clause():
    assert (
        _handoff_label(
            "`Session.send()` calls `HTTPAdapter.send()` to hand the prepared "
            "request to urllib3."
        )
        == "hand the prepared request to urllib3"
    )
    assert _handoff_label("`A.run()` calls `B.step()`") == ""
    assert _handoff_label("`A.run()` calls `B.step()`, which returns `C`") == ""
    assert _handoff_label("plain prose without code spans") == ""


def test_interaction_row_uses_leaf_symbols_and_relation_marker():
    relation = RelationItem(
        id="R4",
        source="src/requests/sessions.py:Session.send()",
        target="src/requests/adapters.py:HTTPAdapter.send()",
        anchors=("src/requests/sessions.py:703",),
    )
    assert (
        _interaction_row(relation, "hand the prepared request to urllib3")
        == "- `Session.send()` → `HTTPAdapter.send()`: hand the prepared "
        "request to urllib3 [R4]"
    )
    assert (
        _interaction_row(relation) == "- `Session.send()` → `HTTPAdapter.send()` [R4]"
    )


def test_fact_plan_markdown_moves_relation_claims_out_of_prose():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/requests/sessions.py",
            start_line=680,
            end_line=720,
            symbol="src/requests/sessions.py:Session.send()",
            kind="method",
            content=(
                "def send(self, request, **kwargs):\n"
                "    adapter = self.get_adapter(url=request.url)\n"
                "    r = adapter.send(request, **kwargs)\n"
                "    return r\n"
            ),
        ),
    ]
    relations = [
        RelationItem(
            id="R1",
            source="src/requests/sessions.py:Session.send()",
            target="src/requests/adapters.py:HTTPAdapter.send()",
            anchors=("src/requests/sessions.py:703",),
        )
    ]
    plan = {
        "sections": [
            {
                "title": "Dispatch",
                "claims": [
                    {
                        "role": "responsibility",
                        "statement": (
                            "`Session.send()` picks the adapter for the request "
                            "URL and returns the adapter's response"
                        ),
                        "evidence": ["E1"],
                    },
                    {
                        "role": "flow",
                        "statement": (
                            "`Session.send()` calls `HTTPAdapter.send()` to hand "
                            "the prepared request to the transport"
                        ),
                        "evidence": ["R1"],
                    },
                ],
            }
        ],
    }
    rendered = _renderable_plan(plan, evidence, relations)
    markdown = _fact_plan_markdown(rendered, evidence, relations)

    assert "picks the adapter for the request URL" in markdown
    assert "calls `HTTPAdapter.send()`" not in markdown
    assert (
        "**Interactions**\n- `Session.send()` → `HTTPAdapter.send()`: hand the "
        "prepared request to the transport [R1]" in markdown
    )
    quality = _page_quality_report(
        markdown,
        rendered,
        require_interaction=True,
        relations=relations,
        evidence_items=evidence,
    )
    assert quality["interaction_row_count"] == 1
    assert quality["valid"] is True


def _journey_fixture():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/requests/api.py",
            start_line=14,
            end_line=60,
            symbol="src/requests/api.py:request()",
            kind="function",
            content=(
                "def request(method, url, **kwargs):\n"
                "    with sessions.Session() as session:\n"
                "        return session.request(method=method, url=url, **kwargs)\n"
            ),
        ),
        EvidenceItem(
            id="E2",
            file="src/requests/sessions.py",
            start_line=500,
            end_line=590,
            symbol="src/requests/sessions.py:Session.request()",
            kind="method",
            content=(
                "def request(self, method, url, **kwargs):\n"
                "    req = Request(method=method.upper(), url=url)\n"
                "    prep = self.prepare_request(req)\n"
                "    resp = self.send(prep, **send_kwargs)\n"
                "    return resp\n"
            ),
        ),
        EvidenceItem(
            id="E3",
            file="src/requests/adapters.py",
            start_line=600,
            end_line=700,
            symbol="src/requests/adapters.py:HTTPAdapter.send()",
            kind="method",
            content=(
                "def send(self, request, stream=False, **kwargs):\n"
                "    conn = self.get_connection_with_tls_context(request)\n"
                "    resp = conn.urlopen(method=request.method, url=url)\n"
                "    return self.build_response(request, resp)\n"
            ),
        ),
    ]
    plan = {
        "thesis": {
            "statement": "`request()` opens a session and sends the request",
            "evidence": ["E1"],
        },
        "journey": {
            "title": "From `request()` to a response",
            "stages": [
                {
                    "stage": "`request()`",
                    "statement": "opens a Session and forwards the method and url",
                    "evidence": ["E1"],
                },
                {
                    "stage": "`Session.request()`",
                    "statement": "prepares the Request and sends the prepared request",
                    "evidence": ["E2"],
                },
                {
                    "stage": "`HTTPAdapter.send()`",
                    "statement": "opens the connection and builds the response",
                    "evidence": ["E3"],
                },
                {
                    "stage": "`_private_helper()`",
                    "statement": "must never appear as a stage",
                    "evidence": ["E3"],
                },
            ],
        },
        "sections": [
            {
                "title": "Transport boundary",
                "claims": [
                    {
                        "role": "contract",
                        "statement": (
                            "`HTTPAdapter.send()` opens the connection for the "
                            "request and returns the built response"
                        ),
                        "evidence": ["E3"],
                    }
                ],
            }
        ],
        "see_also": [{"page": "sessions", "title": "Sessions"}],
    }
    context = {
        "topics": [
            {
                "id": "public-api",
                "title": "Public API",
                "summary": "Module-level helpers that open a session per call.",
                "files": ["src/requests/api.py"],
            },
            {
                "id": "sessions",
                "title": "Sessions",
                "summary": "Session state, request preparation, and redirects.",
                "files": ["src/requests/sessions.py"],
            },
        ],
        "entry_points": [
            {"path": "src/requests/__init__.py", "kind": "package", "label": "requests"}
        ],
    }
    return evidence, plan, context


def test_overview_journey_is_admitted_and_rendered_with_topic_links():
    evidence, plan, context = _journey_fixture()
    rendered = _renderable_plan(plan, evidence, [])

    stages = [stage["stage"] for stage in rendered["journey"]["stages"]]
    assert stages == ["request()", "Session.request()", "HTTPAdapter.send()"]

    markdown = _fact_plan_markdown(
        rendered,
        evidence,
        [],
        concise_overview=True,
        overview_context=context,
    )
    assert "## From `request()` to a response" in markdown
    assert (
        "1. **`request()`** · [Public API](?p=public-api): opens a Session and "
        "forwards the method and url. [E1]" in markdown
    )
    assert "3. **`HTTPAdapter.send()`**: opens the connection" in markdown
    assert "_private_helper" not in markdown
    assert "## Explore the system" in markdown
    assert "- [Public API](?p=public-api)" in markdown
    assert "- [Sessions](?p=sessions)" in markdown
    assert "src/requests/__init__.py" not in markdown
    # Compact navigation replaces both the subsystem inventory and related list.
    assert "## Related pages" not in markdown

    quality = _page_quality_report(
        markdown,
        rendered,
        # Journey parsing remains a compatibility surface; newly generated
        # Overview pages use semantic architecture as their only path model.
        require_dense_sections=False,
        require_cited_intro=True,
        evidence_items=evidence,
    )
    assert quality["valid"] is True
    report = grounding_report(markdown, evidence, [])
    assert report["valid"] is True


def test_overview_journey_needs_three_supported_stages():
    evidence, plan, _context = _journey_fixture()
    plan["journey"]["stages"] = plan["journey"]["stages"][:2]
    rendered = _renderable_plan(plan, evidence, [])
    assert "journey" not in rendered


def test_subsystem_table_and_owning_topic_helpers():
    _evidence, _plan, context = _journey_fixture()
    assert (
        _owning_topic(["./src/requests/sessions.py"], context["topics"])["id"]
        == "sessions"
    )
    assert _owning_topic(["src/other.py"], context["topics"]) is None
    assert _subsystem_table({"topics": [], "entry_points": []}) == ""
    navigation = _subsystem_table(context)
    assert navigation.splitlines() == [
        "- [Public API](?p=public-api)",
        "- [Sessions](?p=sessions)",
    ]


def test_excerpt_window_centres_on_the_lines_the_section_names():
    content = "\n".join(
        [
            "def rebuild_auth(self, prepared_request, response):",
            '    """Strip auth on redirect."""',
            *[f"    setup_{index} = {index}" for index in range(20)],
            "    headers = prepared_request.headers",
            '    if "Authorization" in headers and self.should_strip_auth(a, b):',
            '        del headers["Authorization"]',
            "    return None",
        ]
    )
    section = {
        "claims": [
            {
                "statement": (
                    "`rebuild_auth()` deletes the Authorization header when "
                    "`should_strip_auth()` returns true"
                )
            }
        ]
    }
    terms = _excerpt_focus_terms(section, "notice the header removal")
    assert {"rebuild_auth", "should_strip_auth", "authorization", "header"} <= terms
    assert "returns" not in terms

    window, marked = _excerpt_window(content, terms, max_lines=6)
    assert 'del headers["Authorization"]' in "\n".join(window)
    assert "def rebuild_auth" not in "\n".join(window)
    assert marked and all(1 <= line <= 6 for line in marked)
    assert any("should_strip_auth" in window[line - 1] for line in marked)

    # With nothing to look for, the top of the body (its signature) is shown.
    top, unmarked = _excerpt_window(content, set(), max_lines=6)
    assert top[0].startswith("def rebuild_auth")
    assert unmarked == []


def test_excerpt_caption_uses_the_admitted_notice_and_marks_lines():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/requests/sessions.py",
            start_line=309,
            end_line=332,
            symbol="src/requests/sessions.py:SessionRedirectMixin.rebuild_auth()",
            kind="method",
            content=(
                "def rebuild_auth(self, prepared_request, response):\n"
                "    headers = prepared_request.headers\n"
                '    if "Authorization" in headers and self.should_strip_auth(o, u):\n'
                '        del headers["Authorization"]\n'
            ),
        )
    ]
    plan = {
        "sections": [
            {
                "title": "Auth stripping",
                "claims": [
                    {
                        "role": "contract",
                        "statement": (
                            "`SessionRedirectMixin.rebuild_auth()` deletes the "
                            "Authorization header when `should_strip_auth()` "
                            "returns true"
                        ),
                        "evidence": ["E1"],
                    }
                ],
                "excerpt": {
                    "evidence": "E1",
                    "why": "the Authorization header is deleted before the redirect",
                },
            }
        ]
    }
    rendered = _renderable_plan(plan, evidence, [])
    markdown = _fact_plan_markdown(rendered, evidence, [])
    assert "```python hl=" in markdown
    assert (
        "*What to notice:* the Authorization header is deleted before the "
        "redirect. [E1]" in markdown
    )
    assert "Source excerpt." not in markdown

    plan["sections"][0]["excerpt"]["why"] = "an efficient and powerful design"
    markdown = _fact_plan_markdown(_renderable_plan(plan, evidence, []), evidence, [])
    assert "What to notice" not in markdown
    assert "Source excerpt from `SessionRedirectMixin.rebuild_auth()`. [E1]" in markdown


def test_journey_from_path_admits_narration_and_falls_back_to_call_sites():
    evidence, _plan, _context = _journey_fixture()
    relations = [
        RelationItem(
            id="R1",
            source="src/requests/api.py:request()",
            target="src/requests/sessions.py:Session.request()",
            anchors=("src/requests/api.py:59",),
        ),
        RelationItem(
            id="R2",
            source="src/requests/sessions.py:Session.request()",
            target="src/requests/adapters.py:HTTPAdapter.send()",
            anchors=("src/requests/sessions.py:589",),
        ),
    ]
    entry_path = [
        {"evidence": "E1", "symbol": evidence[0].symbol, "relation": None},
        {"evidence": "E2", "symbol": evidence[1].symbol, "relation": "R1"},
        {"evidence": "E3", "symbol": evidence[2].symbol, "relation": "R2"},
    ]
    narration = {
        "E1": "`request()` opens a session and forwards the method and url",
        # Not borne out by E2's source: falls back to the recorded hop.
        "E2": "`Session.request()` negotiates TLS certificates with the proxy",
        # Evaluative wording is rejected like any claim.
        "E3": "`HTTPAdapter.send()` opens the connection with a powerful pool",
    }
    journey = _journey_from_path(entry_path, narration, evidence, relations)

    assert journey["title"] == "From `request()` to `HTTPAdapter.send()`"
    assert journey["stages"] == [
        {
            "stage": "request()",
            "statement": "`request()` opens a session and forwards the method and url",
            "evidence": ["E1"],
        },
        {
            "stage": "Session.request()",
            "statement": "hands off to `HTTPAdapter.send()`",
            "evidence": ["R2"],
            "relation": "R1",
        },
        {
            "stage": "HTTPAdapter.send()",
            "statement": "receives the work from `Session.request()`",
            "evidence": ["R2"],
            "relation": "R2",
        },
    ]
    # The hops become the visual fallback, each arrow cited by its call site.
    flow = _journey_flow(journey)
    assert [(step["from"], step["to"], step["evidence"]) for step in flow["steps"]] == [
        ("`request()`", "`Session.request()`", ["R1"]),
        ("`Session.request()`", "`HTTPAdapter.send()`", ["R2"]),
    ]
    rendered = _renderable_plan(
        {"journey": journey, "sections": []}, evidence, relations
    )
    assert [stage["stage"] for stage in rendered["journey"]["stages"]] == [
        "request()",
        "Session.request()",
        "HTTPAdapter.send()",
    ]
    markdown = _fact_plan_markdown(rendered, evidence, relations, concise_overview=True)
    assert "## From `request()` to `HTTPAdapter.send()`" in markdown
    assert (
        "2. **`Session.request()`**: hands off to `HTTPAdapter.send()`. [R2]"
        in markdown
    )

    assert _journey_from_path(entry_path[:2], narration, evidence, relations) is None


def test_story_review_is_off_by_default_and_parses_when_on(tmp_path):
    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            return (
                '{"problem":{"score":2,"quote":"strips the header"},'
                '"path":{"score":1,"quote":"from send()"},'
                '"decision":{"score":2,"quote":"host changes"},'
                '"failure":{"score":0,"quote":""},'
                '"jargon":false,"repetition":false,"notes":"say what breaks"}'
            )

    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    llm = LLM()
    quiet = AgentWiki(bundle, model="fake-model", llm=llm)
    assert quiet._review_story("# Page\n\nSome prose. [E1]") is None
    assert llm.calls == 0

    loud = AgentWiki(bundle, model="fake-model", llm=llm, story_review=True)
    review = loud._review_story("# Page\n\nSome prose. [E1]")
    assert llm.calls == 1
    assert review["score"] == 5 and review["max_score"] == 8
    assert review["passed"] is False
    assert review["answers"]["failure"] == {"score": 0, "quote": ""}
    assert review["notes"] == "say what breaks"


def test_excerpt_caption_falls_back_when_it_restates_the_claim():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/requests/sessions.py",
            start_line=309,
            end_line=332,
            symbol="src/requests/sessions.py:SessionRedirectMixin.rebuild_auth()",
            kind="method",
            content=(
                "def rebuild_auth(self, prepared_request, response):\n"
                "    headers = prepared_request.headers\n"
                '    if "Authorization" in headers and self.should_strip_auth(o, u):\n'
                '        del headers["Authorization"]\n'
            ),
        )
    ]
    plan = {
        "sections": [
            {
                "title": "Auth stripping",
                "claims": [
                    {
                        "role": "contract",
                        "statement": (
                            "`rebuild_auth()` deletes the Authorization header "
                            "from the prepared request headers before the "
                            "redirect is followed"
                        ),
                        "evidence": ["E1"],
                    }
                ],
                "excerpt": {
                    "evidence": "E1",
                    # The plan's "why" says the claim again in other words —
                    # the exact pattern the sentence-redundancy gate rejects.
                    "why": (
                        "the Authorization header is deleted from the prepared "
                        "request headers before the redirect is followed"
                    ),
                },
            }
        ]
    }
    markdown = _fact_plan_markdown(_renderable_plan(plan, evidence, []), evidence, [])

    assert "```python hl=" in markdown
    assert "What to notice" not in markdown
    assert "Source excerpt from `SessionRedirectMixin.rebuild_auth()`. [E1]" in markdown
    assert section_sentence_redundancy_report(markdown)["sentence_redundancy_valid"]


def test_admissible_excerpt_caption_keeps_a_distinct_notice():
    parts = [
        "`rebuild_auth()` deletes the Authorization header when the redirect "
        "leaves the original host. [E1]"
    ]
    notice = "*What to notice:* `should_strip_auth()` compares scheme and port. [E1]"
    plain = "Source excerpt from `rebuild_auth`. [E1]"

    assert _admissible_excerpt_caption("Auth", parts, [notice, plain]) == notice
    assert _admissible_excerpt_caption("Auth", parts, [plain]) == plain


def test_admissible_excerpt_caption_drops_a_symbol_label_that_echoes_the_claim():
    # `mp_lexer_to_next` contributes "mp", "lexer" and "next" as prose terms,
    # so even the neutral symbol caption shares most words with a claim
    # about that lexer; the bare label is the one that cannot repeat prose.
    parts = [
        "`mp_lexer_to_next()` advances the lexer through whitespace and comments "
        "and reads the next token from the source buffer. [E3]"
    ]
    symbol = "Source excerpt from `mp_lexer_to_next`. [E3]"
    bare = "Source excerpt. [E3]"

    picked = _admissible_excerpt_caption("Lexer", parts, [symbol, bare])
    assert picked == bare
    assert section_sentence_redundancy_report(
        "## Lexer\n\n" + "\n\n".join([*parts, picked])
    )["sentence_redundancy_valid"]


def test_excerpt_fence_outgrows_backticks_inside_the_source():
    evidence = [
        EvidenceItem(
            id="E1",
            file="src/render.rs",
            start_line=1,
            end_line=6,
            symbol="src/render.rs:render",
            kind="function",
            content=(
                "//! Lines are laid out in columns:\n"
                "//! ```text\n"
                "//!  /--- line number\n"
                "//! ```\n"
                "fn render(lines: &[Line]) {}\n"
            ),
        )
    ]
    plan = {
        "sections": [
            {
                "title": "Rendering",
                "claims": [
                    {
                        "role": "contract",
                        "statement": "`render()` lays lines out in columns",
                        "evidence": ["E1"],
                    }
                ],
                "excerpt": {"evidence": "E1", "why": "see the column layout"},
            }
        ]
    }
    markdown = _fact_plan_markdown(_renderable_plan(plan, evidence, []), evidence, [])
    fence_lines = [line for line in markdown.splitlines() if line.startswith("````")]
    assert len(fence_lines) == 2, markdown
    # Nothing from the excerpt leaks into what the gates read as prose.
    assert "line number" not in strip_code_fences(markdown)


def test_purpose_section_is_dropped_when_it_restates_the_thesis():
    evidence = [
        EvidenceItem(
            id="E1",
            file="tsdb/nhcb.go",
            start_line=1,
            end_line=40,
            symbol="tsdb/nhcb.go:ConvertNHCBToClassic",
            kind="function",
            content=(
                "func ConvertNHCBToClassic(h *Histogram) []Sample {\n"
                "\treturn nil\n}\n"
            ),
        )
    ]
    thesis = (
        "`ConvertNHCBToClassic()` converts native histograms with custom "
        "buckets into classic histogram series by emitting cumulative bucket, "
        "count, and sum samples"
    )
    plan = {
        "thesis": {"statement": thesis, "evidence": ["E1"]},
        "purpose": {
            "statements": [
                "This area converts native histograms with custom buckets into "
                "classic histogram series by emitting cumulative bucket, count, "
                "and sum samples"
            ],
            "evidence": ["E1"],
        },
        "sections": [
            {
                "title": "Conversion",
                "claims": [
                    {
                        "role": "contract",
                        "statement": (
                            "`ConvertNHCBToClassic()` validates the histogram "
                            "before emitting samples"
                        ),
                        "evidence": ["E1"],
                    }
                ],
            }
        ],
    }
    markdown = _fact_plan_markdown(_renderable_plan(plan, evidence, []), evidence, [])
    assert "## Purpose and scope" not in markdown
    assert duplicate_prose_blocks(markdown) == []


def test_drop_duplicate_framing_removes_a_transition_that_restates_the_claims():
    transition = (
        "*Plan commands orchestrate the core graph engine, handling cancellation "
        "and targeting as part of the planning lifecycle.* [E3]"
    )
    claims = (
        "Plan commands orchestrate the core graph engine, handling cancellation "
        "and targeting as part of the planning lifecycle, and the command reads "
        "its view from `Meta`. [E3] [E4]"
    )
    distinct = "*The CLI first loads backend state from disk.* [E1]"

    kept = _drop_duplicate_framing("Plan commands", [transition, claims], [transition])
    assert kept == [claims]
    assert duplicate_prose_blocks("## Plan commands\n\n" + "\n\n".join(kept)) == []

    untouched = _drop_duplicate_framing("Plan commands", [distinct, claims], [distinct])
    assert untouched == [distinct, claims]


def _degraded_memory_page(next_attempt_epoch):
    return {
        "id": "runtime",
        "markdown": "diagnostic draft",
        "media_plan_version": MEDIA_PLAN_VERSION,
        "generation": {
            "mode": "degraded",
            "fallback": None,
            "reason": "quality_guard",
            "retry": {
                "state": "scheduled",
                "attempts": 1,
                "max_attempts": 2,
                "last_attempt_epoch": 1.0,
                "next_attempt_epoch": next_attempt_epoch,
            },
        },
        "quality": {"valid": False},
    }


def test_agent_wiki_in_memory_degraded_page_retries_once_its_cooldown_passes(
    tmp_path,
):
    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            instance_id="owner__repo-1",
            commit_short="abc123",
            language="python",
        ),
        vector_store=None,
        bm25=None,
        manifest=SimpleNamespace(languages=["python"], indexes={}),
    )
    wiki = AgentWiki(bundle, model="fake-model")
    meta = {"id": "runtime", "title": "Runtime", "children": []}
    generated = {
        "id": "runtime",
        "markdown": "Readable source-linked explanation.",
        "generation": {"mode": "generated", "fallback": None},
    }
    calls = []
    wiki._find = lambda _page_id: meta
    wiki._generate_page = lambda _meta: calls.append(_meta) or generated
    wiki._write_cache = lambda _suffix, _page: None

    # Inside the cooldown the diagnostic page is served as-is, without a model call.
    cooling = _degraded_memory_page(next_attempt_epoch=10_000_000_000.0)
    wiki._pages["runtime"] = cooling
    wiki._read_cache = lambda _suffix: cooling
    assert wiki.page("runtime")["markdown"] == "diagnostic draft"
    assert calls == []

    # Once the persisted window opens, the in-process copy must not pin the
    # bad page for the life of the process: the read regenerates it.
    due = _degraded_memory_page(next_attempt_epoch=1.0)
    wiki._pages["runtime"] = due
    wiki._read_cache = lambda _suffix: due
    assert wiki.page("runtime") is generated
    assert calls == [meta]
    assert wiki._pages["runtime"] is generated


def test_stub_bodies_are_recognised_but_real_ones_are_not():
    from codenib.wiki.agent_wiki import AgentWiki

    stub = (
        "def send(self, request):\n"
        '    """Sends PreparedRequest object.\n\n    :param request: the request.\n    """\n'
        "    raise NotImplementedError\n"
    )
    assert AgentWiki._is_stub_body(stub) is True
    assert AgentWiki._is_stub_body("def close(self):\n    pass\n") is True
    wrapped = (
        "self,\n        request: PreparedRequest,\n        stream: bool = False,\n"
        "    ) -> Response:\n"
        '        """Sends PreparedRequest object.\n\n        :param stream: flag.\n'
        '        """\n        raise NotImplementedError\n'
    )
    assert AgentWiki._is_stub_body(wrapped) is True
    real = (
        "def send(self, request):\n"
        "    conn = self.get_connection(request)\n"
        "    return conn\n"
    )
    assert AgentWiki._is_stub_body(real) is False
    assert AgentWiki._is_stub_body("def only_signature(self): ...") is False


def test_concrete_override_follows_the_subclass_header_reference():
    from codenib.graph.code_graph import CodeGraph
    from codenib.wiki.agent_wiki import AgentWiki

    graph = CodeGraph()
    for name, kind, line in [
        ("a.py:Base", "class", 0),
        ("a.py:Base.send()", "method", 2),
        ("a.py:HTTP", "class", 10),
        ("a.py:HTTP.send()", "method", 12),
        ("a.py:Other", "class", 20),
        ("a.py:Other.close()", "method", 22),
    ]:
        graph._add_vertex(
            name,
            {
                "type": kind,
                "file": "a.py",
                "start_line": line,
                "end_line": line + 3,
                "unified_name": name,
            },
        )
    for parent, child in [
        ("a.py:Base", "a.py:Base.send()"),
        ("a.py:HTTP", "a.py:HTTP.send()"),
        ("a.py:Other", "a.py:Other.close()"),
    ]:
        graph._add_edge(parent, child, "contain")
    # `class HTTP(Base):` references Base on HTTP's own first line; Other only
    # mentions Base inside a body, which is not inheritance.
    graph._add_edge(
        "a.py:HTTP", "a.py:Base", "reference", anchor_file="a.py", anchor_line=10
    )
    graph._add_edge(
        "a.py:Other", "a.py:Base", "reference", anchor_file="a.py", anchor_line=23
    )

    raw = graph.get_graph()
    vid = {v["unified_name"]: v.index for v in raw.vs}
    assert AgentWiki._concrete_override(raw, vid["a.py:Base.send()"]) == (
        vid["a.py:HTTP.send()"],
        vid["a.py:HTTP"],
    )
    assert AgentWiki._concrete_override(raw, vid["a.py:HTTP.send()"]) is None
