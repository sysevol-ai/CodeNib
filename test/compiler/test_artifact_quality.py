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

from codenib.compiler.artifact_quality import (
    assess_graph_artifact,
    assess_vector_artifact,
)
from codenib.git_snapshot import GitSourceSurface
from codenib.graph.code_graph import CodeGraph


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

    report = assess_graph_artifact(
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

    report = assess_graph_artifact(
        graph,
        surface,
        required_files=["src/main.py"],
    )

    assert report["passed"] is False
    assert "missing_required_source_files" in report["failure_names"]


def test_vector_quality_checks_identity_counts_paths_and_l0_coverage(tmp_path):
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

    report = assess_vector_artifact(
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
