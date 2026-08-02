# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess

from codenib.git_snapshot import GitSourceSurface
from codenib.graph.code_graph import CodeGraph
from codenib.graph.source_coverage import supplement_graph_source_coverage
from codenib.types import EDGE_TYPE_CONTAIN


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def test_source_coverage_supplements_all_missing_tracked_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text(
        "class Service:\n    def run(self):\n        return 1\n",
        encoding="utf-8",
    )
    (repo / "src" / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "src/main.py", "src/other.py")
    _git(repo, "commit", "-m", "initial")

    graph = CodeGraph(str(repo))
    graph.add_root_node(str(repo))
    report = supplement_graph_source_coverage(
        graph,
        repo_root=repo,
        surface=GitSourceSurface.load(repo),
        extensions={".py"},
        represented_paths=(),
    )

    assert report["supplemented_files"] == 2
    assert "src/main.py:Service" in graph.name_to_vertex
    assert "src/main.py:Service.run()" in graph.name_to_vertex
    contain_edges = [
        edge
        for edge in graph.graph.es
        if edge.attributes().get("type") == EDGE_TYPE_CONTAIN
    ]
    assert contain_edges


def test_source_coverage_does_not_use_required_file_labels(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "kept.py").write_text("def kept(): pass\n", encoding="utf-8")
    (repo / "excluded.py").write_text("def excluded(): pass\n", encoding="utf-8")
    _git(repo, "add", "kept.py", "excluded.py")
    _git(repo, "commit", "-m", "initial")
    graph = CodeGraph(str(repo))
    graph.add_root_node(str(repo))

    report = supplement_graph_source_coverage(
        graph,
        repo_root=repo,
        surface=GitSourceSurface.load(repo),
        extensions={".py"},
        represented_paths={"kept.py"},
        exclude_patterns=["excluded.py"],
    )

    assert report["candidate_files"] == 0
    assert report["files"] == []
