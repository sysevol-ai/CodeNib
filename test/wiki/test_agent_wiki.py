# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fast unit tests for the agent wiki retrieval guardrails."""

from types import SimpleNamespace

from codenib.wiki.agent_wiki import (
    AgentWiki,
    _candidate_score,
    _clean_markdown,
    _ensure_cited_intro,
    _fact_plan_markdown,
    _format_supported_literals,
    _page_quality_report,
    _plan_quality_warnings,
    _prepare_evidence_content,
    _prune_uncited_blocks,
    _readme_intro,
    _remove_orphan_headings,
)
from codenib.wiki.builder import Symbol
from codenib.wiki.evidence import EvidenceItem, candidate_key


class _FakeVectorStore:
    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    def search_with_content(self, query, top_k):
        self.calls.append((query, top_k))
        return self.nodes[:top_k]


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
        "The repository contains several subsystems with distinct "
        "responsibilities. [E5] [E6]"
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
            content="class Indexer: pass",
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
            content="def main(): pass",
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
            content="def run(): pass",
        ),
        EvidenceItem(
            id="E2",
            file="src/runtime.py",
            start_line=10,
            end_line=18,
            symbol="load_config",
            kind="function",
            content="def load_config(): pass",
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
            content="def main(): pass",
        ),
        EvidenceItem(
            id="E3",
            file="src/compiler.py",
            start_line=None,
            end_line=None,
            symbol="Compiler",
            kind="class",
            content="class Compiler: pass",
        ),
        EvidenceItem(
            id="E4",
            file="src/server.py",
            start_line=None,
            end_line=None,
            symbol="Server",
            kind="class",
            content="class Server: pass",
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
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "statement": "The CLI accepts a repository path",
                        "evidence": ["E2"],
                    },
                    {
                        "statement": "The Wiki exposes indexed source",
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Flow",
                "claims": [
                    {
                        "statement": "The compiler creates repository indexes",
                        "evidence": ["E3"],
                    },
                    {
                        "statement": "The compiler records repository indexes",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Subsystems",
                "claims": [
                    {"statement": "The server returns Wiki pages", "evidence": ["E4"]},
                    {
                        "statement": "The CLI invokes the compiler",
                        "evidence": ["E2", "E3"],
                    },
                ],
            },
        ]
    }
    meta = {"id": "overview"}

    assert _plan_quality_warnings(meta, sparse, evidence)
    assert _plan_quality_warnings(meta, dense, evidence) == []


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
            (2, "src/cli.py", "The CLI starts the compiler."),
            (3, "src/compiler.py", "The compiler writes a manifest."),
            (4, "src/server.py", "The server returns Wiki pages."),
        ]
    ]
    plan = {
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "statement": "The Wiki accepts a repository path",
                        "evidence": ["E1"],
                    },
                    {
                        "statement": "The Wiki builds repository indexes",
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Execution",
                "claims": [
                    {"statement": "The CLI starts the compiler", "evidence": ["E2"]},
                    {
                        "statement": "The compiler writes a manifest",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Runtime",
                "claims": [
                    {"statement": "The server returns Wiki pages", "evidence": ["E4"]},
                    {
                        "statement": "The CLI starts repository processing",
                        "evidence": ["E2"],
                    },
                ],
            },
        ]
    }

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
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {"statement": "The project builds a Wiki", "evidence": ["E1"]},
                    {
                        "statement": "The Wiki contains source links",
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Execution",
                "claims": [
                    {"statement": "The CLI accepts a path", "evidence": ["E2"]},
                    {
                        "statement": "The compiler writes a manifest",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Runtime",
                "claims": [
                    {"statement": "The server returns pages", "evidence": ["E4"]},
                    {
                        "statement": "The CLI starts compilation",
                        "evidence": ["E2", "E3"],
                    },
                ],
            },
        ]
    }

    warnings = _plan_quality_warnings({"id": "overview"}, plan, evidence)

    assert warnings == []


def test_overview_plan_requires_new_sources_after_public_workflow():
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
            (2, "src/cli.py", "The CLI starts the compiler."),
            (3, "src/compiler.py", "The compiler writes a manifest."),
            (4, "src/server.py", "The server returns Wiki pages."),
        ]
    ]
    plan = {
        "sections": [
            {
                "title": "Workflow",
                "claims": [
                    {
                        "statement": "The Wiki accepts a repository",
                        "evidence": ["E1"],
                    },
                    {
                        "statement": "The Wiki indexes the repository",
                        "evidence": ["E1"],
                    },
                ],
            },
            {
                "title": "Execution",
                "claims": [
                    {"statement": "The CLI starts the compiler", "evidence": ["E2"]},
                    {
                        "statement": "The compiler writes a manifest",
                        "evidence": ["E3"],
                    },
                ],
            },
            {
                "title": "Subsystems",
                "claims": [
                    {"statement": "The CLI owns the entry point", "evidence": ["E2"]},
                    {
                        "statement": "The compiler owns the manifest",
                        "evidence": ["E3"],
                    },
                ],
            },
        ]
    }

    warnings = _plan_quality_warnings({"id": "overview"}, plan, evidence)

    assert (
        "section 'Subsystems' must introduce an implementation source not used "
        "by earlier sections" in warnings
    )


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
            content="def _retrieve(self): pass",
        )
    ]
    plan = {
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
        ]
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


def test_page_plan_rejects_incidental_helper_section():
    evidence = [
        EvidenceItem(
            id=f"E{index}",
            file="src/runtime.py",
            start_line=0,
            end_line=10,
            symbol=symbol,
            kind="method",
            content="def method(): pass",
        )
        for index, symbol in [
            (1, "AgentRunner.run"),
            (2, "AgentRunner.configure"),
            (3, "AgentRunner._serialize"),
        ]
    ]
    plan = {
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
        ]
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


def test_overview_fact_plan_repairs_sparse_plan():
    sparse = (
        '{"thesis":"indexed source","sections":['
        '{"title":"Workflow","claims":[{"statement":"The CLI accepts a path",'
        '"evidence":["E2"]}]},'
        '{"title":"Flow","claims":[{"statement":"The compiler builds indexes",'
        '"evidence":["E3"]}]},'
        '{"title":"Subsystems","claims":[{"statement":"The server returns pages",'
        '"evidence":["E4"]}]}]}'
    )
    dense = (
        '{"thesis":"indexed source","sections":['
        '{"title":"Workflow","claims":['
        '{"statement":"The CLI accepts a repository path","evidence":["E2"]},'
        '{"statement":"The Wiki exposes indexed source","evidence":["E1"]}]},'
        '{"title":"Flow","claims":['
        '{"statement":"The compiler creates repository indexes","evidence":["E3"]},'
        '{"statement":"The compiler records repository indexes","evidence":["E3"]}]},'
        '{"title":"Subsystems","claims":['
        '{"statement":"The server returns Wiki pages","evidence":["E4"]},'
        '{"statement":"The CLI invokes the compiler","evidence":["E2","E3"]}]}]}'
    )

    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.responses = iter((sparse, dense))
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
            (2, "src/cli.py", "def main(): pass"),
            (3, "src/compiler.py", "class Compiler: pass"),
            (4, "src/server.py", "class Server: pass"),
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

    assert llm.calls == 2
    assert warnings == []
    assert len(plan["sections"][2]["claims"]) == 2


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
        "## Workflow\n\n"
        "The command builds a repository Wiki. [E1]"
    )

    rendered = _ensure_cited_intro(draft, evidence, canonical_readme=True)

    assert rendered.startswith(
        "The repository compiles source into reusable views and serves them "
        "through a local developer Wiki. [E1]"
    )
    assert "This document provides" not in rendered
    assert "## Workflow" in rendered


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
        "src/compiler.py": "class Compiler:\n    def build(self): pass",
        "src/server.py": "class Server:\n    def page(self): pass",
    }
    for file, content in source.items():
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    plan = (
        '{"thesis":"repository context","sections":['
        '{"title":"Workflow","claims":['
        '{"statement":"Users run `codenib wiki` with a repository path",'
        '"evidence":["E1"]},'
        '{"statement":"The command detects repository languages","evidence":["E1"]}]},'
        '{"title":"Execution","claims":['
        '{"statement":"The CLI starts repository compilation from parsed command arguments",'
        '"evidence":["E2"]},'
        '{"statement":"The `Compiler` builds repository views for later requests",'
        '"evidence":["E3"]}]},'
        '{"title":"Subsystems","claims":['
        '{"statement":"The `Server` returns source-linked Wiki pages to callers",'
        '"evidence":["E4"]},'
        '{"statement":"The CLI owns the public command and dispatches compilation",'
        '"evidence":["E2"]}]}]}'
    )

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
    page = AgentWiki(bundle, model="fake-model", llm=llm)._generate_page(
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
    assert "## Workflow" in page["markdown"]


def test_generated_page_uses_fact_plan_and_reports_grounding(tmp_path):
    class LLM:
        cache_identity = "fake"

        def __init__(self):
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return (
                '{"thesis":"request routing","sections":[{"title":"Flow",'
                '"claims":[{"statement":"The `Router` dispatches source-backed '
                'repository requests through `dispatch`","evidence":["E1"]}]}]}'
            )

    node = {
        "file": "src/core.py",
        "node_name": "Router",
        "type": "class",
        "start_line": 0,
        "end_line": 6,
        "content": "class Router:\n    def dispatch(self):\n        return handle()",
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
    wiki = AgentWiki(bundle, model="fake-model", llm=llm)

    page = wiki._generate_page(
        {
            "id": "routing",
            "title": "Request Routing",
            "summary": "How requests move through the repository",
            "keywords": ["dispatch"],
            "files": ["src/core.py"],
        }
    )

    assert len(llm.calls) == 1
    assert page["grounding"]["valid"] is True
    assert page["quality"]["valid"] is True
    assert page["generation"]["mode"] == "generated"
    assert page["generation"]["renderer"] == "fact_plan"
    assert page["citations"][0]["start_line"] == 1
    assert page["evidence"]["items"][0]["routes"] == ("outline", "dense")


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
    assert llm.calls == 1


def test_agent_wiki_cache_key_tracks_view_rebuild_identity(tmp_path):
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
    before = wiki._key("outline")
    view.built_at_epoch = 2.0
    view.config = {"builder_schema": 2}

    assert wiki._key("outline") != before
