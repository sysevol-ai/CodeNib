# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import pickle
import subprocess
from types import SimpleNamespace

import faiss
import numpy as np
import pytest

from codenib import compat_pickle
from codenib.compiler.artifact_quality import (
    assess_vector_artifact,
    constrain_and_assess_graph_artifact,
    constrain_graph_to_source_selection,
    vector_artifact_files_match,
)
from codenib.git_snapshot import GitSourceSurface
from codenib.graph.code_graph import CodeGraph
from codenib.index.embedding.artifact_integrity import capture_authenticated_vector_view
from codenib.native_index_authorization import _mint_trusted_local_admin_authorization
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.scip_interface.lsp_occurrence_index import (
    SCIPOccurrence,
    SCIPOccurrenceIndex,
)
from codenib.types import EDGE_TYPE_REFERENCE


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _source_surface(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    _git(repo, "add", "src/main.py")
    _git(repo, "commit", "-m", "initial")
    return repo, GitSourceSurface.load(repo)


def _assess_vector_as_trusted_local_admin(root, **kwargs):
    """Test-only lexical admin boundary for locally constructed fixtures."""

    expected_artifact = kwargs["expected_artifact"]
    with capture_authenticated_vector_view(root) as view:
        authorization = _mint_trusted_local_admin_authorization(
            view.ownership,
            view_type="vector",
            semantic_contract=expected_artifact,
            evidence=(
                "artifact-quality-test-owned-fixture",
                "captured-vector-tree-subject",
            ),
        )
        return assess_vector_artifact(
            root,
            authenticated_view=view,
            native_index_authorization=authorization,
            **kwargs,
        )


def test_graph_quality_removes_paths_outside_commit(tmp_path):
    repo, surface = _source_surface(tmp_path)
    graph = CodeGraph(str(repo))
    graph.add_root_node(str(repo))
    graph.add_directory_node("src")
    graph.add_file_node("src/main.py")
    graph.add_symbol_node("main", 0, 0, 0, "function")
    graph.add_directory_node("lib")
    graph.add_file_node("lib/generated.py")
    graph.add_symbol_node("generated", 0, 0, 0, "function")

    report = constrain_and_assess_graph_artifact(
        graph,
        surface,
        required_files=["src/main.py"],
    )

    assert report["passed"] is True
    assert report["surface"]["removed_outside_paths"] == ["lib/generated.py"]
    assert "lib/generated.py" not in graph.name_to_vertex
    assert "generated" not in graph.name_to_vertex
    assert "src/main.py" in graph.name_to_vertex


def test_graph_quality_rejects_missing_required_source_file(tmp_path):
    repo, surface = _source_surface(tmp_path)
    graph = CodeGraph(str(repo))
    graph.add_root_node(str(repo))
    graph.current_file = "src/main.py"
    graph.add_symbol_node("reference_only", 0, 0, 0, "symbol")

    report = constrain_and_assess_graph_artifact(
        graph,
        surface,
        required_files=["src/main.py"],
    )

    assert report["passed"] is False
    assert "missing_required_source_files" in report["failure_names"]


def test_graph_quality_reports_malformed_paths_before_constraining(tmp_path):
    repo, surface = _source_surface(tmp_path)
    graph = CodeGraph(str(repo))
    graph.add_root_node(str(repo))
    graph.add_file_node("src/main.py")
    graph.add_file_node("../generated.py")

    report = constrain_and_assess_graph_artifact(
        graph,
        surface,
        required_files=["src/main.py"],
    )

    assert report["passed"] is False
    assert "invalid_graph_paths" in report["failure_names"]
    assert report["surface"]["invalid_paths_before"]
    assert report["surface"]["invalid_paths_after"] == []
    assert "../generated.py" not in graph.name_to_vertex


def test_graph_source_selection_constrains_every_record_and_rebuilds_indexes():
    graph = CodeGraph("/repo")
    graph.add_root_node(".")
    for directory in ("src", "generated", "generated-copy", "empty"):
        graph.add_directory_node(directory)

    graph.add_file_node("src/main.py")
    graph.add_symbol_node("kept", 1, 1, 3, "function")
    graph.add_file_node("generated/private.py")
    graph.add_symbol_node("private", 1, 1, 3, "function")
    graph.add_file_node("generated-copy/public.py")
    graph.add_symbol_node("prefix-safe", 1, 1, 3, "function")
    graph._add_edge(
        "kept",
        "prefix-safe",
        EDGE_TYPE_REFERENCE,
        anchor_file="src/main.py",
        anchor_line=2,
    )
    graph._add_edge(
        "kept",
        "prefix-safe",
        EDGE_TYPE_REFERENCE,
        anchor_file="generated/private.py",
        anchor_line=2,
    )
    graph.lsp_occurrence_index = SCIPOccurrenceIndex(
        [
            SCIPOccurrence("src/main.py", 1, 0, 1, 4, "kept"),
            SCIPOccurrence("generated/private.py", 1, 0, 1, 4, "private"),
            SCIPOccurrence("generated-copy/public.py", 1, 0, 1, 4, "prefix-safe"),
        ]
    )
    graph.build_range_indexes()

    selection = RepositorySourceSelection(["generated"])
    report = constrain_graph_to_source_selection(graph, selection)

    assert report["source_selection_digest"] == selection.digest
    assert report["excluded_paths_after"] == []
    assert report["invalid_paths_after"] == []
    assert "generated/private.py" in report["removed_excluded_paths"]
    assert "private" not in graph.name_to_vertex
    assert "generated/private.py" not in graph.name_to_vertex
    assert "generated" not in graph.name_to_vertex
    assert "empty" not in graph.name_to_vertex
    assert "kept" in graph.name_to_vertex
    assert "prefix-safe" in graph.name_to_vertex
    assert "src" in graph.name_to_vertex
    assert "generated-copy" in graph.name_to_vertex
    assert set(graph.symbol_ranges) == {"kept", "prefix-safe"}
    assert set(graph._file_nodes) == {"src/main.py", "generated-copy/public.py"}
    assert set(graph._file_edge_anchors) == {"src/main.py"}
    assert [
        occurrence.file_path for occurrence in graph.lsp_occurrence_index.occurrences
    ] == ["src/main.py", "generated-copy/public.py"]


def test_graph_source_selection_enforces_default_ignored_paths_when_empty():
    graph = CodeGraph("/repo")
    graph.add_root_node(".")
    graph.add_directory_node("src")
    graph.add_file_node("src/main.py")
    graph.add_symbol_node("kept", 1, 1, 3, "function")
    graph.add_directory_node("node_modules")
    graph.add_file_node("node_modules/dependency.js")
    graph.add_symbol_node("dependency", 1, 1, 3, "function")
    graph.build_range_indexes()

    report = constrain_graph_to_source_selection(
        graph,
        RepositorySourceSelection(),
    )

    assert report["excluded_paths_after"] == []
    assert "node_modules/dependency.js" in report["removed_excluded_paths"]
    assert "node_modules/dependency.js" not in graph.name_to_vertex
    assert "dependency" not in graph.name_to_vertex
    assert "src/main.py" in graph.name_to_vertex


def test_graph_source_selection_removes_defined_symbol_without_source_path():
    graph = CodeGraph("/repo")
    graph.add_root_node(".")
    graph.add_directory_node("src")
    graph.add_file_node("src/main.py")
    graph.add_symbol_node("local", 1, 1, 3, "function")
    local = graph.graph.vs.find(name="local")
    local["file"] = None
    local["has_definition"] = True
    graph.add_symbol_node("external", 1, 1, 3, "function")
    external = graph.graph.vs.find(name="external")
    external["file"] = None
    external["has_definition"] = False
    graph.build_range_indexes()

    report = constrain_graph_to_source_selection(
        graph,
        RepositorySourceSelection(),
    )

    assert "local" not in graph.name_to_vertex
    assert "external" in graph.name_to_vertex
    assert any(
        "local" not in item and ".file=None" in item
        for item in report["invalid_paths_before"]
    )
    assert report["invalid_paths_after"] == []


def test_vector_quality_checks_identity_counts_paths_and_l0_coverage(
    tmp_path,
    monkeypatch,
):
    _repo, surface = _source_surface(tmp_path)
    model = "test/model"
    suffix = "test__model"
    identity = {
        "repo": "org/repo",
        "commit": surface.commit,
        "tree": surface.tree,
        "languages": ["python"],
    }
    root = tmp_path / "vectors"
    level = root / "l0"
    level.mkdir(parents=True)
    documents = [
        SimpleNamespace(
            page_content="def main(): pass\n",
            metadata={"file": "src/main.py"},
        )
    ]
    with (level / f"documents_{suffix}.pkl").open("wb") as handle:
        pickle.dump(documents, handle)
    index = faiss.IndexFlatIP(2)
    index.add(np.asarray([[1.0, 0.0]], dtype=np.float32))
    faiss.write_index(index, str(level / f"index_{suffix}.faiss"))
    (level / f"config_{suffix}.json").write_text(
        json.dumps({"num_documents": 1}), encoding="utf-8"
    )
    (root / f"config_{suffix}.json").write_text(
        json.dumps({"embedding_model": model, "artifact": identity}),
        encoding="utf-8",
    )

    def parser_must_not_run(*_args, **_kwargs):
        raise AssertionError("native parser ran before external authorization")

    with capture_authenticated_vector_view(root) as view:
        with monkeypatch.context() as guard:
            guard.setattr(compat_pickle, "load", parser_must_not_run)
            guard.setattr(faiss, "read_index", parser_must_not_run)
            with pytest.raises(ValueError, match="requires external authorization"):
                assess_vector_artifact(
                    root,
                    embedding_model=model,
                    build_levels=["l0"],
                    surface=surface,
                    expected_artifact=identity,
                    required_l0_files=["src/main.py"],
                    authenticated_view=view,
                    native_index_authorization=None,
                )

    with capture_authenticated_vector_view(root) as view:
        authorization = _mint_trusted_local_admin_authorization(
            view.ownership,
            view_type="vector",
            semantic_contract=identity,
            evidence=(
                "artifact-quality-primary-error-test",
                "captured-vector-tree-subject",
            ),
        )

        def fail_assessment(_paths):
            raise ValueError("primary assessment failure")

        def fail_final_verification():
            raise RuntimeError("secondary final verification failure")

        failing_surface = SimpleNamespace(
            commit=surface.commit,
            tree=surface.tree,
            classify=fail_assessment,
        )
        with monkeypatch.context() as guard:
            guard.setattr(
                type(view),
                "verify_final",
                lambda _self: fail_final_verification(),
            )
            with pytest.raises(ValueError, match="primary assessment failure"):
                assess_vector_artifact(
                    root,
                    embedding_model=model,
                    build_levels=["l0"],
                    surface=failing_surface,
                    expected_artifact=identity,
                    required_l0_files=["src/main.py"],
                    authenticated_view=view,
                    native_index_authorization=authorization,
                )

    report = _assess_vector_as_trusted_local_admin(
        root,
        embedding_model=model,
        build_levels=["l0"],
        surface=surface,
        expected_artifact=identity,
        required_l0_files=["src/main.py"],
    )

    assert report["passed"] is True
    assert report["artifact"] == identity
    assert report["levels"]["l0"]["documents"] == 1
    assert report["l0_files"] == ["src/main.py"]
    assert vector_artifact_files_match(
        root,
        embedding_model=model,
        build_levels=["l0"],
        expected_fingerprints=report["file_fingerprints"],
    )

    documents_path = level / f"documents_{suffix}.pkl"
    original_documents = documents_path.read_bytes()
    documents_path.write_bytes(original_documents + b"stale")
    assert not vector_artifact_files_match(
        root,
        embedding_model=model,
        build_levels=["l0"],
        expected_fingerprints=report["file_fingerprints"],
    )

    stale_level = root / "l2"
    stale_level.mkdir()
    stale_index = stale_level / f"index_{suffix}.faiss"
    stale_index.write_bytes(b"stale")
    unexpected = _assess_vector_as_trusted_local_admin(
        root,
        embedding_model=model,
        build_levels=["l0"],
        surface=surface,
        expected_artifact=identity,
    )
    assert unexpected["unexpected_levels"] == ["l2"]
    assert "unexpected_level:l2" in unexpected["failure_names"]
    stale_index.unlink()
    stale_level.rmdir()

    documents[0].metadata = []
    with documents_path.open("wb") as handle:
        pickle.dump(documents, handle)
    invalid = _assess_vector_as_trusted_local_admin(
        root,
        embedding_model=model,
        build_levels=["l0"],
        surface=surface,
        expected_artifact=identity,
        required_l0_files=["src/main.py"],
    )

    assert invalid["passed"] is False
    assert "invalid_document_metadata" in invalid["failure_names"]
    assert invalid["paths"]["invalid_metadata"] == 1

    for metadata in ({}, {"file": ""}):
        documents[0].metadata = metadata
        with documents_path.open("wb") as handle:
            pickle.dump(documents, handle)
        invalid_path = _assess_vector_as_trusted_local_admin(
            root,
            embedding_model=model,
            build_levels=["l0"],
            surface=surface,
            expected_artifact=identity,
        )
        assert "invalid_document_paths" in invalid_path["failure_names"]
        assert invalid_path["paths"]["invalid"]


def test_vector_quality_does_not_report_missing_level_as_empty(tmp_path):
    _repo, surface = _source_surface(tmp_path)
    model = "test/model"
    root = tmp_path / "vectors"
    root.mkdir()
    identity = {"repo": "org/repo", "commit": surface.commit}
    (root / "config_test__model.json").write_text(
        json.dumps({"embedding_model": model, "artifact": identity}),
        encoding="utf-8",
    )

    report = _assess_vector_as_trusted_local_admin(
        root,
        embedding_model=model,
        build_levels=["l0"],
        surface=surface,
        expected_artifact=identity,
    )

    failures = report["levels"]["l0"]["failures"]
    assert any(failure.startswith("missing:") for failure in failures)
    assert "empty_level" not in failures
