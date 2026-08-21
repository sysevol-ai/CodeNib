# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

from codenib.repository_source_selection import RepositorySourceSelection
from codenib.source_fingerprint import capture_repository_source
from codenib.wiki.builder import Symbol
from codenib.wiki.outline import (
    _apply_outline_summary_rewrites,
    _fallback_outline,
    _flagged_outline_summaries,
    _merge_outlines,
    _outline_plan_warnings,
    _outline_quality_warnings,
    _outline_score,
    _page_allows_supporting_files,
    _readme_entry_files,
    _required_top_level_pages,
    _validate_outline,
    generate_outline,
)


def _source_paths(root):
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def test_outline_prompt_uses_only_authenticated_source_inventory(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "runtime.py").write_text(
        "def run(): return 'visible'\n",
        encoding="utf-8",
    )
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.txt").write_text(
        "EXCLUDED_PROMPT_SECRET\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "Run src/runtime.py to start the service.\n",
        encoding="utf-8",
    )
    binding = capture_repository_source(
        tmp_path,
        selection=RepositorySourceSelection(("private",)),
    )
    captured = {}

    class LLM:
        def complete(self, messages, **_kwargs):
            captured["prompt"] = messages[0]["content"]
            raise RuntimeError("stop after prompt capture")

    bundle = SimpleNamespace(
        entry=SimpleNamespace(
            repo="owner/repo",
            repo_dir=str(tmp_path),
            language="python",
            commit_short="abc123",
        ),
        manifest=SimpleNamespace(languages=["python"], indexes={}, file_count=2),
        vector_store=None,
        bm25=SimpleNamespace(documents=[]),
        source_reader=binding.borrow_reader(),
    )

    try:
        generate_outline(bundle, "fake-model", llm=LLM())
    finally:
        binding.close()

    assert "src/runtime.py" in captured["prompt"]
    assert "private/secret.txt" not in captured["prompt"]
    assert "EXCLUDED_PROMPT_SECRET" not in captured["prompt"]


def test_outline_quality_rejects_marketing_and_document_meta_summaries():
    weak = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": "This section explains the advanced repository system.",
                "children": [],
            }
        ]
    }
    concrete = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": (
                    "The command compiles repository views and serves them "
                    "through a local Wiki."
                ),
                "children": [],
            }
        ]
    }

    warnings = _outline_quality_warnings(weak)

    assert len(warnings) == 2
    assert _outline_quality_warnings(concrete) == []
    assert _outline_quality_warnings(
        {
            "pages": [
                {
                    "title": "Vector Indexing",
                    "summary": (
                        "Vector embeddings rank code chunks, improving retrieval "
                        "capabilities beyond lexical search."
                    ),
                    "children": [],
                }
            ]
        }
    )


def test_outline_style_repair_applies_only_summary_text():
    original = {
        "pages": [
            {
                "id": "runtime",
                "title": "Runtime",
                "summary": "The runtime handles requests.",
                "keywords": ["runtime"],
                "files": ["src/runtime.py"],
                "children": [],
            }
        ]
    }
    repaired = _apply_outline_summary_rewrites(
        original,
        """
        {"summaries":[
          {"path":"0","summary":"The runtime dispatches requests to registered tools."},
          {"path":"9","summary":"This path is not allowed."}
        ]}
        """,
        allowed_paths={"0"},
    )

    assert repaired is not None
    assert (
        repaired["pages"][0]["summary"]
        == "The runtime dispatches requests to registered tools."
    )
    assert repaired["pages"][0]["files"] == ["src/runtime.py"]
    assert (
        _apply_outline_summary_rewrites(
            original,
            '{"summaries":[{"path":"9","summary":"Not admitted."}]}',
            allowed_paths={"0"},
        )
        is None
    )


def test_outline_style_repair_targets_only_flagged_summaries():
    data = {
        "pages": [
            {
                "title": "Runtime",
                "summary": "This section explains an advanced runtime.",
                "keywords": ["runtime"],
                "files": ["src/runtime.py"],
                "children": [
                    {
                        "title": "Dispatch",
                        "summary": "The dispatcher passes requests to tools.",
                        "keywords": ["dispatch"],
                        "files": ["src/dispatch.py"],
                        "children": [],
                    }
                ],
            }
        ]
    }

    flagged = _flagged_outline_summaries(data)

    assert [item["path"] for item in flagged] == ["0"]


def test_outline_breadth_scales_with_available_repository_evidence():
    assert _required_top_level_pages(4) == 2
    assert _required_top_level_pages(8) == 3
    assert _required_top_level_pages(40) == 5

    sparse = {"pages": [{"files": ["src/a.py"], "children": []}] * 3}
    broad = {"pages": [{"files": [f"src/{i}.py"], "children": []} for i in range(5)]}

    assert _outline_score(broad, 5) > _outline_score(sparse, 5)


def test_outline_repairs_merge_distinct_valid_concepts():
    base = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": "Repository entry paths and major subsystems.",
                "keywords": ["entry"],
                "files": ["README.md"],
                "children": [],
            },
            {
                "id": "indexing",
                "title": "Indexing",
                "summary": "The compiler builds repository indexes.",
                "keywords": ["compiler"],
                "files": ["src/compiler.py"],
                "children": [],
            },
        ]
    }
    candidate = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": "Repository entry paths and runtime components.",
                "keywords": ["runtime"],
                "files": ["src/cli.py"],
                "children": [],
            },
            {
                "id": "agent-runtime",
                "title": "Agent Runtime",
                "summary": "The runtime dispatches requests to tools.",
                "keywords": ["runtime"],
                "files": ["src/runner.py"],
                "children": [],
            },
            {
                "id": "graph-navigation",
                "title": "Graph Navigation",
                "summary": "The graph resolves symbols to source locations.",
                "keywords": ["graph"],
                "files": ["src/graph.py"],
                "children": [],
            },
        ]
    }

    merged = _merge_outlines(base, candidate)

    assert [page["id"] for page in merged["pages"]] == [
        "overview",
        "indexing",
        "agent-runtime",
        "graph-navigation",
    ]
    assert merged["pages"][0]["files"] == ["README.md", "src/cli.py"]
    assert merged["pages"][0]["keywords"] == ["entry", "runtime"]


def test_outline_repairs_merge_synonymous_file_backed_concepts():
    base = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "files": ["README.md"],
                "children": [],
            },
            {
                "id": "evaluation-workflows",
                "title": "Evaluation Workflows",
                "files": ["bash/run_all.sh", "bash/run_lc.sh"],
                "children": [],
            },
        ]
    }
    candidate = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "files": ["README.md"],
                "children": [],
            },
            {
                "id": "evaluation-processes",
                "title": "Evaluation Processes",
                "files": ["bash/run_lc.sh", "bash/run_sgl.sh"],
                "children": [],
            },
        ]
    }

    merged = _merge_outlines(base, candidate)

    assert [page["id"] for page in merged["pages"]] == [
        "overview",
        "evaluation-workflows",
    ]
    assert merged["pages"][1]["files"] == [
        "bash/run_all.sh",
        "bash/run_lc.sh",
        "bash/run_sgl.sh",
    ]


def test_outline_requires_primary_readme_workflow_coverage():
    warnings = _outline_plan_warnings(
        {
            "pages": [
                {"id": "overview", "files": ["README.md"], "children": []},
                {
                    "id": "evaluation",
                    "files": ["bash/run_eval.sh"],
                    "children": [],
                },
            ]
        },
        ["tests/test_edit_iterate.py", "bash/run_eval.sh"],
    )

    assert any("primary README quickstart entry" in warning for warning in warnings)


def test_outline_requires_real_source_anchors(tmp_path):
    (tmp_path / "codenib").mkdir()
    (tmp_path / "codenib" / "config.py").write_text("class RuntimeConfig: pass\n")
    (tmp_path / "codenib" / "runner.py").write_text("class AgentRunner: pass\n")
    symbols = [
        Symbol(
            file="codenib/config.py",
            name="RuntimeConfig",
            type="class",
            start_line=0,
            end_line=0,
            content="class RuntimeConfig: pass",
        ),
        Symbol(
            file="codenib/runner.py",
            name="AgentRunner",
            type="class",
            start_line=0,
            end_line=0,
            content="class AgentRunner: pass",
        ),
    ]
    data = {
        "pages": [
            {
                "id": "overview",
                "title": "Overview",
                "summary": "Repository purpose",
                "keywords": ["architecture"],
                "files": [],
                "children": [],
            },
            {
                "id": "configuration",
                "title": "Configuration",
                "summary": "Runtime configuration",
                "keywords": ["RuntimeConfig"],
                "files": ["invented/settings.py"],
                "children": [
                    {
                        "id": "plugins",
                        "title": "Plugin Marketplace",
                        "summary": "A generic unsupported extension page",
                        "keywords": ["marketplace"],
                        "files": [],
                        "children": [],
                    }
                ],
            },
            {
                "id": "communication",
                "title": "Agent Communication",
                "summary": "A generic unsupported section",
                "keywords": ["communication"],
                "files": [],
                "children": [],
            },
        ]
    }

    result = _validate_outline(
        data,
        _source_paths(tmp_path),
        symbols=symbols,
        fallback_files=["codenib/runner.py"],
    )

    assert [page["id"] for page in result["pages"]] == [
        "overview",
        "configuration",
    ]
    assert result["pages"][0]["files"] == [
        "codenib/runner.py",
        "codenib/config.py",
    ]
    assert result["pages"][1]["files"] == ["codenib/config.py"]
    assert result["pages"][1]["children"] == []


def test_non_evaluation_page_drops_supporting_eval_files(tmp_path):
    files = [
        "src/agent/runner.py",
        "src/eval/agent_study_runner.py",
    ]
    for file in files:
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# source\n")
    symbols = [
        Symbol(
            file="src/agent/runner.py",
            name="AgentRunner",
            type="class",
            start_line=0,
            end_line=0,
            content="class AgentRunner: pass",
        )
    ]

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "agent-runtime",
                    "title": "Agent Runtime",
                    "summary": "Agent execution and context handling.",
                    "keywords": ["AgentRunner", "runtime", "evaluation"],
                    "files": files,
                    "children": [],
                }
            ]
        },
        _source_paths(tmp_path),
        symbols=symbols,
        fallback_files=files,
    )

    runtime = next(page for page in result["pages"] if page["id"] == "agent-runtime")
    assert runtime["files"] == ["src/agent/runner.py"]


def test_supporting_file_access_follows_page_identity_not_search_keywords():
    assert not _page_allows_supporting_files(
        {
            "id": "agent-runtime",
            "title": "Agent Runtime",
            "keywords": ["evaluation", "benchmark"],
        }
    )
    assert _page_allows_supporting_files(
        {
            "id": "evaluation",
            "title": "Evaluation Framework",
            "keywords": ["AgentRunner"],
        }
    )
    assert _page_allows_supporting_files(
        {
            "id": "edit-workflow",
            "title": "Edit Iteration Workflow",
            "keywords": ["Editor"],
        }
    )


def test_readme_entry_files_resolve_documented_workflows(tmp_path):
    files = [
        "tests/test_edit_iterate.py",
        "bash/run_eval.sh",
        "src/runtime.py",
    ]
    for file in files:
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# source\n")
    readme = (
        "Run `python tests/test_edit_iterate.py` for the minimum workflow.\n"
        "Use ./bash/run_eval.sh for the full evaluation.\n"
        "The missing path is examples/not_present.py.\n"
    )

    assert _readme_entry_files(_source_paths(tmp_path), readme) == files[:2]


def test_readme_documented_workflow_retains_supporting_source(tmp_path):
    file = "tests/test_edit_iterate.py"
    path = tmp_path / file
    path.parent.mkdir(parents=True)
    path.write_text("def edit_iterate(): pass\n")

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "overview",
                    "title": "Overview",
                    "summary": "Repository purpose and execution flow.",
                    "keywords": ["workflow"],
                    "files": [file],
                    "children": [],
                },
                {
                    "id": "edit-iteration",
                    "title": "Edit Iteration Workflow",
                    "summary": "The workflow iterates edits against a task.",
                    "keywords": ["edit", "iterate"],
                    "files": [file],
                    "children": [],
                },
            ]
        },
        _source_paths(tmp_path),
        fallback_files=[file],
        documented_files=[file],
    )

    assert result["pages"][1]["files"] == [file]


def test_evaluation_page_retains_explicit_eval_files(tmp_path):
    file = "src/eval/agent_study_runner.py"
    path = tmp_path / file
    path.parent.mkdir(parents=True)
    path.write_text("class EvaluationRunner: pass\n")
    symbols = [
        Symbol(
            file=file,
            name="EvaluationRunner",
            type="class",
            start_line=0,
            end_line=0,
            content="class EvaluationRunner: pass",
        )
    ]

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "evaluation",
                    "title": "Evaluation Framework",
                    "summary": "Evaluation studies and measurements.",
                    "keywords": ["evaluation", "EvaluationRunner"],
                    "files": [file],
                    "children": [],
                }
            ]
        },
        _source_paths(tmp_path),
        symbols=symbols,
        fallback_files=[file],
    )

    evaluation = next(page for page in result["pages"] if page["id"] == "evaluation")
    assert evaluation["files"] == [file]


def test_fallback_outline_remains_navigable_without_model():
    result = _fallback_outline(
        [
            "src/core.py",
            "src/config.py",
            "tests/test_core.py",
            "README.md",
        ]
    )

    assert result["mode"] == "fallback"
    assert result["pages"][0]["id"] == "overview"
    assert {page["title"] for page in result["pages"]} >= {
        "Overview",
        "Src",
        "Tests",
    }


def test_overview_prefers_repository_root_readme(tmp_path):
    (tmp_path / "landing").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("Repository overview.\n")
    (tmp_path / "landing" / "README.md").write_text("Static landing page.\n")
    (tmp_path / "src" / "core.py").write_text("def run(): pass\n")

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "overview",
                    "title": "Overview",
                    "summary": "Repository purpose",
                    "keywords": ["architecture"],
                    "files": ["landing/README.md", "src/core.py"],
                    "children": [],
                }
            ]
        },
        _source_paths(tmp_path),
        symbols=[],
        fallback_files=["src/core.py"],
    )

    assert result["pages"][0]["files"][0] == "README.md"


def test_first_outline_page_is_canonical_overview(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("Repository overview.\n")
    (tmp_path / "src" / "cli.py").write_text("def main(): pass\n")

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "codenib-overview",
                    "title": "CodeNib Overview",
                    "summary": "Repository purpose and workflow",
                    "keywords": ["workflow"],
                    "files": ["src/cli.py"],
                    "children": [],
                }
            ]
        },
        _source_paths(tmp_path),
        symbols=[],
        fallback_files=["src/cli.py"],
    )

    assert result["pages"][0]["title"] == "Overview"
    assert result["pages"][0]["files"][0] == "README.md"


def test_overview_children_are_promoted_to_top_level(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("Repository overview.\n")
    (tmp_path / "src" / "compiler.py").write_text("class IndexCompiler: pass\n")
    symbols = [
        Symbol(
            file="src/compiler.py",
            name="IndexCompiler",
            type="class",
            start_line=0,
            end_line=0,
            content="class IndexCompiler: pass",
        )
    ]

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "overview",
                    "title": "Overview",
                    "summary": "Repository purpose",
                    "keywords": ["architecture"],
                    "files": ["README.md", "src/compiler.py"],
                    "children": [
                        {
                            "id": "indexing",
                            "title": "Indexing",
                            "summary": "Repository index construction",
                            "keywords": ["index", "compiler"],
                            "files": ["src/compiler.py"],
                            "children": [],
                        }
                    ],
                }
            ]
        },
        _source_paths(tmp_path),
        symbols=symbols,
        fallback_files=["src/compiler.py"],
    )

    assert [page["id"] for page in result["pages"]] == ["overview", "indexing"]
    assert result["pages"][0]["children"] == []


def test_overview_prioritizes_product_entrypoints_over_backend_details(tmp_path):
    files = [
        "README.md",
        "src/cli.py",
        "src/compiler/index_compiler.py",
        "src/runtime/app.py",
        "src/api/server.py",
        "src/wiki/builder.py",
        "src/languages/decoder.py",
        "tests/test_cli.py",
    ]
    for file in files:
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {file}\n")

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "overview",
                    "title": "Overview",
                    "summary": "Repository purpose and architecture",
                    "keywords": ["architecture", "runtime"],
                    "files": [
                        "src/languages/decoder.py",
                        "tests/test_cli.py",
                    ],
                    "children": [],
                }
            ]
        },
        _source_paths(tmp_path),
        symbols=[],
        fallback_files=files[1:],
    )

    selected = result["pages"][0]["files"]
    assert selected[0] == "README.md"
    assert selected.index("src/cli.py") < selected.index("src/languages/decoder.py")
    assert selected.index("src/runtime/app.py") < selected.index(
        "src/languages/decoder.py"
    )
    assert "tests/test_cli.py" not in selected


def test_outline_rejects_generic_concepts_with_unrelated_real_files(tmp_path):
    files = [
        "src/agent/runner.py",
        "src/index/compiler.py",
        "src/graph/code_graph.py",
    ]
    symbols = [
        Symbol(
            file=file,
            name=name,
            type="class",
            start_line=0,
            end_line=0,
            content=f"class {name}: pass",
        )
        for file, name in [
            ("src/agent/runner.py", "AgentRunner"),
            ("src/index/compiler.py", "IndexCompiler"),
            ("src/graph/code_graph.py", "CodeGraph"),
        ]
    ]
    for file in files:
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# source\n")

    result = _validate_outline(
        {
            "pages": [
                {
                    "id": "interceptors",
                    "title": "Interceptors",
                    "summary": "Generic middleware around requests.",
                    "keywords": ["middleware", "request"],
                    "files": ["src/agent/runner.py"],
                    "children": [],
                },
                {
                    "id": "indexing",
                    "title": "Indexing",
                    "summary": "Repository index construction.",
                    "keywords": ["index", "compiler"],
                    "files": ["src/index/compiler.py"],
                    "children": [],
                },
                {
                    "id": "graph",
                    "title": "Graph Structure",
                    "summary": "Repository dependency graph.",
                    "keywords": ["graph", "dependencies"],
                    "files": ["src/graph/code_graph.py"],
                    "children": [],
                },
            ]
        },
        _source_paths(tmp_path),
        symbols=symbols,
        fallback_files=files,
    )

    assert [page["id"] for page in result["pages"]] == [
        "overview",
        "indexing",
        "graph",
    ]
