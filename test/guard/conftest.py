# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the guard tests: hand-built symbol graphs and git repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

import igraph as ig
import pytest

# name, type, file, 0-based start line, 0-based end line
Node = Tuple[str, str, Optional[str], Optional[int], Optional[int]]
# source, target, type, 0-based anchor line
Edge = Tuple[str, str, str, Optional[int]]


def build_graph(nodes: Sequence[Node], edges: Sequence[Edge]) -> ig.Graph:
    graph = ig.Graph(directed=True)
    graph.add_vertices(len(nodes))
    graph.vs["name"] = [n[0] for n in nodes]
    graph.vs["type"] = [n[1] for n in nodes]
    graph.vs["file"] = [n[2] for n in nodes]
    graph.vs["start_line"] = [n[3] for n in nodes]
    graph.vs["end_line"] = [n[4] for n in nodes]
    graph.vs["has_definition"] = [n[1] not in ("file", "directory") for n in nodes]
    index: Dict[str, int] = {n[0]: i for i, n in enumerate(nodes)}
    graph.add_edges([(index[s], index[t]) for s, t, _, _ in edges])
    graph.es["type"] = [kind for _, _, kind, _ in edges]
    graph.es["anchor_line"] = [line for _, _, _, line in edges]
    return graph


def vertex(graph: ig.Graph, name: str) -> int:
    return graph.vs.find(name=name).index


def names(graph: ig.Graph, vertices: List[int]) -> List[str]:
    return [graph.vs[v]["name"] for v in vertices]


@pytest.fixture
def make_graph():
    return build_graph


def write(root: Path, files: Dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path):
    """A git repository with automatic maintenance off, and a commit helper."""

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
        ("gc.auto", "0"),
        ("maintenance.auto", "false"),
    ):
        git(root, "config", key, value)

    def commit(files: Dict[str, str], message: str = "change") -> str:
        write(root, files)
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", message)
        return git(root, "rev-parse", "HEAD").strip()

    return SimpleNamespace(root=root, commit=commit, git=lambda *a: git(root, *a))


@pytest.fixture
def write_files():
    return write
