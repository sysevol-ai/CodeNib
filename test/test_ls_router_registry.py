# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for registry-driven LS router delegate selection."""

from __future__ import annotations

import pytest

from codenib import ls_router
from codenib.graph.code_graph import CodeGraph
from codenib.ls_router import (
    LSGraphDecoder,
    LSIndexer,
    build_graph_for_languages,
    build_graph_for_languages_with_report,
)


@pytest.mark.parametrize(
    ("language", "delegate_class"),
    [
        ("python", "SCIPPythonIndexer"),
        ("py", "SCIPPythonIndexer"),
        ("go", "SCIPGoIndexer"),
        ("golang", "SCIPGoIndexer"),
        ("rust", "SCIPRustIndexer"),
        ("rs", "SCIPRustIndexer"),
        ("typescript", "SCIPTypeScriptIndexer"),
        ("javascript", "SCIPTypeScriptIndexer"),
        ("cpp", "ClangdIndexer"),
        ("c", "ClangdIndexer"),
        ("java", "SCIPJavaIndexer"),
        ("csharp", "SCIPCSharpIndexer"),
        ("scala", "SCIPScalaIndexer"),
        ("ruby", "RubyHybridIndexer"),
        ("php", "PHPHybridIndexer"),
        ("kotlin", "SCIPKotlinIndexer"),
    ],
)
def test_ls_indexer_uses_registry_delegate(tmp_path, language, delegate_class):
    indexer = LSIndexer(tmp_path, language=language, output_dir=tmp_path / "out")

    assert indexer._delegate.__class__.__name__ == delegate_class


@pytest.mark.parametrize(
    ("language", "delegate_class"),
    [
        ("java", "SCIPJavaIndexer"),
        ("kotlin", "SCIPKotlinIndexer"),
        ("kt", "SCIPKotlinIndexer"),
        ("scala", "SCIPScalaIndexer"),
        ("csharp", "SCIPCSharpIndexer"),
        ("c#", "SCIPCSharpIndexer"),
        ("ruby", "SCIPRubyIndexer"),
        ("rb", "SCIPRubyIndexer"),
        ("php", "SCIPPHPIndexer"),
    ],
)
def test_ls_indexer_can_opt_into_scip_candidate_route(
    tmp_path, language, delegate_class
):
    indexer = LSIndexer(
        tmp_path,
        language=language,
        output_dir=tmp_path / "out",
        graph_route="scip-candidate",
    )

    assert indexer._delegate.__class__.__name__ == delegate_class


def test_ls_indexer_lsp_route_preserves_java_lsp_backend_after_promotion(tmp_path):
    active = LSIndexer(tmp_path, language="java", output_dir=tmp_path / "active")
    lsp = LSIndexer(
        tmp_path,
        language="java",
        output_dir=tmp_path / "lsp",
        graph_route="lsp",
    )

    assert active._delegate.__class__.__name__ == "SCIPJavaIndexer"
    assert lsp._delegate.__class__.__name__ == "GenericLSPIndexer"


def test_ls_indexer_lsp_route_preserves_kotlin_lsp_backend_after_promotion(tmp_path):
    active = LSIndexer(tmp_path, language="kotlin", output_dir=tmp_path / "active")
    lsp = LSIndexer(
        tmp_path,
        language="kotlin",
        output_dir=tmp_path / "lsp",
        graph_route="lsp",
    )

    assert active._delegate.__class__.__name__ == "SCIPKotlinIndexer"
    assert lsp._delegate.__class__.__name__ == "GenericLSPIndexer"


@pytest.mark.parametrize(
    "language",
    [
        "python",
        "go",
        "rust",
        "typescript",
        "javascript",
        "java",
        "kotlin",
        "csharp",
        "ruby",
        "php",
    ],
)
def test_ls_indexer_can_force_generic_lsp_route(tmp_path, language):
    indexer = LSIndexer(
        tmp_path,
        language=language,
        output_dir=tmp_path / "out",
        graph_route="lsp",
    )

    assert indexer._delegate.__class__.__name__ == "GenericLSPIndexer"


def test_ls_indexer_lsp_route_rejects_language_without_lsp_command(tmp_path):
    with pytest.raises(ValueError, match="Unsupported LSP graph language"):
        LSIndexer(
            tmp_path,
            language="scala",
            output_dir=tmp_path / "out",
            graph_route="lsp",
        )


def test_ls_indexer_passes_decoder_backend_only_to_scip(tmp_path):
    py_indexer = LSIndexer(
        tmp_path / "py",
        language="python",
        output_dir=tmp_path / "py-out",
        decoder_backend="core",
    )
    assert py_indexer._delegate.decoder_backend == "core"

    cpp_indexer = LSIndexer(
        tmp_path / "cpp",
        language="cpp",
        output_dir=tmp_path / "cpp-out",
        decoder_backend="core",
    )

    assert cpp_indexer._delegate.__class__.__name__ == "ClangdIndexer"
    assert not hasattr(cpp_indexer._delegate, "decoder_backend")


@pytest.mark.parametrize(
    ("language", "delegate_class"),
    [
        ("python", "SCIPPythonGraphDecoder"),
        ("go", "SCIPGoGraphDecoder"),
        ("rust", "SCIPRustGraphDecoder"),
        ("typescript", "SCIPTypeScriptGraphDecoder"),
        ("javascript", "SCIPTypeScriptGraphDecoder"),
        ("cpp", "ClangdGraphDecoder"),
        ("java", "SCIPJavaGraphDecoder"),
        ("csharp", "SCIPCSharpGraphDecoder"),
        ("scala", "SCIPJavaGraphDecoder"),
        ("ruby", "SCIPRubyGraphDecoder"),
        ("php", "SCIPPHPGraphDecoder"),
        ("kotlin", "SCIPKotlinGraphDecoder"),
    ],
)
def test_ls_graph_decoder_uses_registry_delegate(tmp_path, language, delegate_class):
    decoder = LSGraphDecoder(
        str(tmp_path / "index.decoded"),
        project_root=str(tmp_path),
        language=language,
    )

    assert decoder._delegate.__class__.__name__ == delegate_class


def test_ls_indexer_graph_patch_constructs_graph_patcher_without_profiler_kwarg(
    tmp_path, monkeypatch
):
    indexer = LSIndexer(tmp_path, language="python", output_dir=tmp_path / "out")
    calls = {}

    from codenib.graph.incremental import graph_patcher

    def fake_detect_changed_files(project_root, base_commit, target_commit, extensions):
        calls["detect"] = (project_root, base_commit, target_commit, extensions)
        return {"modified": [], "added": [], "deleted": [], "renamed": []}

    def fake_patch_files(
        self,
        changed_files,
        *,
        earlier_commit=None,
        later_commit=None,
    ):
        calls["patch"] = (changed_files, earlier_commit, later_commit)
        return {"ok": True}

    monkeypatch.setattr(
        graph_patcher.GraphPatcher,
        "detect_changed_files",
        staticmethod(fake_detect_changed_files),
    )
    monkeypatch.setattr(graph_patcher.GraphPatcher, "patch_files", fake_patch_files)

    result = indexer.graph_patch(CodeGraph(str(tmp_path)), "base", "HEAD")

    assert result == {"ok": True}
    assert calls["detect"][1:] == ("base", "HEAD", {".py"})
    assert calls["patch"] == (
        {"modified": [], "added": [], "deleted": [], "renamed": []},
        "base",
        "HEAD",
    )


def test_build_graph_for_languages_preserves_single_language_layout(
    tmp_path, monkeypatch
):
    calls = []

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            *,
            output_dir=None,
            profiler=None,
            language=None,
            decoder_backend=None,
            graph_route="active",
        ):
            self.project_root = project_root
            self.output_dir = output_dir
            self.language = language
            self.graph_route = graph_route
            calls.append(self)

        def run_pipeline(self, *, project_name=None, skip_level=None):
            graph = CodeGraph(str(self.project_root))
            graph.add_file_node(f"{self.language}.txt")
            graph.build_range_indexes()
            self.project_name = project_name
            self.skip_level = skip_level
            return graph

    monkeypatch.setattr(ls_router, "LSIndexer", FakeIndexer)

    graph = build_graph_for_languages(
        tmp_path / "repo",
        tmp_path / "out",
        languages=["py"],
        project_name="repo",
        skip_level="graph",
    )

    assert graph is not None
    [call] = calls
    assert call.language == "python"
    assert call.graph_route == "active"
    assert call.output_dir == tmp_path / "out"
    assert call.project_name == "repo"
    assert call.skip_level == "graph"


def test_build_graph_for_languages_merges_alias_deduped_graphs(tmp_path, monkeypatch):
    calls = []

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            *,
            output_dir=None,
            profiler=None,
            language=None,
            decoder_backend=None,
            graph_route="active",
        ):
            self.project_root = project_root
            self.output_dir = output_dir
            self.language = language
            self.graph_route = graph_route
            calls.append(self)

        def run_pipeline(self, *, project_name=None, skip_level=None):
            graph = CodeGraph(str(self.project_root))
            graph.add_file_node(f"{self.language}.txt")
            graph.add_symbol_node(
                f"{self.language}.txt:{self.language}()",
                line=0,
                scope_start_line=0,
                scope_end_line=1,
            )
            graph.add_containment_edge(f"{self.language}.txt:{self.language}()")
            graph.build_range_indexes()
            self.project_name = project_name
            self.skip_level = skip_level
            return graph

    monkeypatch.setattr(ls_router, "LSIndexer", FakeIndexer)

    graph = build_graph_for_languages(
        tmp_path / "repo",
        tmp_path / "out",
        languages=["py", "python", "go"],
        project_name="repo",
        skip_level="graph",
    )

    assert graph is not None
    assert [call.language for call in calls] == ["python", "go"]
    assert [call.graph_route for call in calls] == ["active", "active"]
    assert [call.output_dir for call in calls] == [
        tmp_path / "out" / "graphs" / "python",
        tmp_path / "out" / "graphs" / "go",
    ]
    assert [call.project_name for call in calls] == ["repo-python", "repo-go"]
    assert graph.graph.vcount() == 4
    assert graph.graph.ecount() == 2

    saved = CodeGraph.load_graph(tmp_path / "out" / "graph.pkl")
    assert saved.graph.vcount() == 4
    assert saved.graph.ecount() == 2


def test_build_graph_for_languages_can_preserve_successful_language_graphs(
    tmp_path, monkeypatch
):
    calls = []

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            *,
            output_dir=None,
            profiler=None,
            language=None,
            decoder_backend=None,
            graph_route="active",
        ):
            self.project_root = project_root
            self.output_dir = output_dir
            self.language = language
            calls.append(language)

        def run_pipeline(self, *, project_name=None, skip_level=None):
            if self.language == "cpp":
                raise RuntimeError("compilation database missing")
            graph = CodeGraph(str(self.project_root))
            graph.add_file_node(f"{self.language}.txt")
            graph.build_range_indexes()
            return graph

    monkeypatch.setattr(ls_router, "LSIndexer", FakeIndexer)

    result = build_graph_for_languages_with_report(
        tmp_path / "repo",
        tmp_path / "out",
        languages=["python", "cpp", "go"],
        allow_partial=True,
    )

    assert result.graph is not None
    assert result.requested_languages == ["python", "cpp", "go"]
    assert result.available_languages == ["python", "go"]
    assert result.failed_languages == {"cpp": "compilation database missing"}
    assert result.partial
    assert calls == ["python", "cpp", "go"]

    saved = CodeGraph.load_graph(tmp_path / "out" / "graph.pkl")
    assert saved.graph.vcount() == 2


def test_build_graph_for_languages_can_use_scip_candidate_route(tmp_path, monkeypatch):
    calls = []

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            *,
            output_dir=None,
            profiler=None,
            language=None,
            decoder_backend=None,
            graph_route="active",
        ):
            self.project_root = project_root
            self.output_dir = output_dir
            self.language = language
            self.graph_route = graph_route
            calls.append(self)

        def run_pipeline(self, *, project_name=None, skip_level=None):
            graph = CodeGraph(str(self.project_root))
            graph.add_file_node(f"{self.language}.txt")
            graph.build_range_indexes()
            self.project_name = project_name
            self.skip_level = skip_level
            return graph

    monkeypatch.setattr(ls_router, "LSIndexer", FakeIndexer)

    graph = build_graph_for_languages(
        tmp_path / "repo",
        tmp_path / "out",
        languages=["kt", "scala"],
        project_name="repo",
        skip_level="graph",
        graph_route="scip-candidate",
    )

    assert graph is not None
    assert [call.language for call in calls] == ["kotlin", "scala"]
    assert [call.graph_route for call in calls] == [
        "scip-candidate",
        "scip-candidate",
    ]
    assert [call.output_dir for call in calls] == [
        tmp_path / "out" / "graphs" / "kotlin",
        tmp_path / "out" / "graphs" / "scala",
    ]


def test_build_graph_for_languages_can_force_lsp_route(tmp_path, monkeypatch):
    calls = []

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            *,
            output_dir=None,
            profiler=None,
            language=None,
            decoder_backend=None,
            graph_route="active",
        ):
            self.project_root = project_root
            self.output_dir = output_dir
            self.language = language
            self.graph_route = graph_route
            calls.append(self)

        def run_pipeline(self, *, project_name=None, skip_level=None):
            graph = CodeGraph(str(self.project_root))
            graph.add_file_node(f"{self.language}.txt")
            graph.build_range_indexes()
            self.project_name = project_name
            self.skip_level = skip_level
            return graph

    monkeypatch.setattr(ls_router, "LSIndexer", FakeIndexer)

    graph = build_graph_for_languages(
        tmp_path / "repo",
        tmp_path / "out",
        languages=["python", "java"],
        project_name="repo",
        skip_level="graph",
        graph_route="lsp",
    )

    assert graph is not None
    assert [call.language for call in calls] == ["python", "java"]
    assert [call.graph_route for call in calls] == ["lsp", "lsp"]


def test_build_graph_for_languages_passes_route_filter_options(tmp_path, monkeypatch):
    calls = []

    class FakeIndexer:
        def __init__(
            self,
            project_root,
            *,
            output_dir=None,
            exclude_patterns=None,
            profiler=None,
            language=None,
            decoder_backend=None,
            graph_route="active",
        ):
            self.project_root = project_root
            self.output_dir = output_dir
            self.exclude_patterns = exclude_patterns
            self.language = language
            self.graph_route = graph_route
            calls.append(self)

        def run_pipeline(self, *, project_name=None, skip_level=None, **kwargs):
            graph = CodeGraph(str(self.project_root))
            graph.add_file_node(f"{self.language}.txt")
            graph.build_range_indexes()
            self.project_name = project_name
            self.skip_level = skip_level
            self.pipeline_kwargs = kwargs
            return graph

    monkeypatch.setattr(ls_router, "LSIndexer", FakeIndexer)

    graph = build_graph_for_languages(
        tmp_path / "repo",
        tmp_path / "out",
        languages=["ruby"],
        project_name="repo",
        skip_level=None,
        target_dir="lib",
        include_references=True,
        exclude_patterns=["vendor/**"],
        graph_route="lsp",
        allow_partial_index=True,
    )

    assert graph is not None
    [call] = calls
    assert call.language == "ruby"
    assert call.graph_route == "lsp"
    assert call.exclude_patterns == ["vendor/**"]
    assert call.pipeline_kwargs == {
        "target_dir": "lib",
        "include_references": True,
        "allow_partial_index": True,
    }


def test_build_graph_for_languages_reports_index_generation(tmp_path, monkeypatch):
    class FakeIndexer:
        def __init__(self, project_root, **_kwargs):
            self.project_root = project_root
            self.index_generation_report = {
                "status": "partial",
                "complete": False,
                "partial": True,
                "document_count": 3,
            }

        def run_pipeline(self, **_kwargs):
            graph = CodeGraph(str(self.project_root))
            graph.add_file_node("partial.py")
            graph.build_range_indexes()
            return graph

    monkeypatch.setattr(ls_router, "LSIndexer", FakeIndexer)

    result = ls_router.build_graph_for_languages_with_report(
        tmp_path / "repo",
        tmp_path / "out",
        languages=["python"],
        skip_level=None,
        allow_partial_index=True,
    )

    assert result.graph is not None
    assert result.index_generation_reports == {
        "python": {
            "status": "partial",
            "complete": False,
            "partial": True,
            "document_count": 3,
        }
    }
