# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for route-level graph alignment checks."""

from __future__ import annotations

import json
from pathlib import Path

from codeminer.graph.code_graph import CodeGraph
from codeminer.types import EDGE_TYPE_CONTAIN, NODE_TYPE_CLASS, NODE_TYPE_FILE
from scripts import check_graph_route_alignment


def _graph(project_root: Path, *, include_symbol: bool = True) -> CodeGraph:
    graph = CodeGraph(project_root=str(project_root))
    graph._add_vertex("src/Foo.java", {"type": NODE_TYPE_FILE})
    if include_symbol:
        graph._add_vertex(
            "route:Foo",
            {
                "type": NODE_TYPE_CLASS,
                "file": "src/Foo.java",
                "start_line": 1,
                "end_line": 10,
                "unified_name": "src/Foo.java:Foo",
            },
        )
        graph._add_edge("src/Foo.java", "route:Foo", EDGE_TYPE_CONTAIN)
    graph.build_range_indexes()
    return graph


def test_build_route_alignment_builds_isolated_route_outputs(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    calls = []

    def fake_build_graph_for_languages(
        project_root,
        output_dir,
        *,
        languages,
        project_name,
        skip_level,
        target_dir,
        include_references,
        exclude_patterns,
        graph_route,
    ):
        calls.append(
            {
                "project_root": project_root,
                "output_dir": output_dir,
                "languages": languages,
                "project_name": project_name,
                "skip_level": skip_level,
                "target_dir": target_dir,
                "include_references": include_references,
                "exclude_patterns": exclude_patterns,
                "graph_route": graph_route,
            }
        )
        graph = _graph(project)
        output_dir.mkdir(parents=True, exist_ok=True)
        graph.save_graph(output_dir / "graph.pkl")
        return graph

    monkeypatch.setattr(
        check_graph_route_alignment,
        "build_graph_for_languages",
        fake_build_graph_for_languages,
    )

    result = check_graph_route_alignment.build_route_alignment(
        project,
        "java",
        output_dir=tmp_path / "out",
        reference_route="lsp",
        candidate_route="scip-candidate",
        skip_level="graph",
        target_dir="src/main/java",
        exclude_patterns=("vendor/**",),
        reference_include_references=True,
        clean=True,
    )

    assert result.ok
    assert [call["graph_route"] for call in calls] == ["lsp", "scip-candidate"]
    assert [call["languages"] for call in calls] == [["java"], ["java"]]
    assert [call["skip_level"] for call in calls] == ["graph", "graph"]
    assert [call["target_dir"] for call in calls] == [
        "src/main/java",
        "src/main/java",
    ]
    assert [call["include_references"] for call in calls] == [True, False]
    assert [call["exclude_patterns"] for call in calls] == [
        ["vendor/**"],
        ["vendor/**"],
    ]
    assert calls[0]["output_dir"].name == "reference-lsp"
    assert calls[1]["output_dir"].name == "candidate-scip-candidate"


def test_main_returns_nonzero_json_for_alignment_mismatch(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "repo"
    project.mkdir()

    def fake_build_graph_for_languages(
        project_root,
        output_dir,
        *,
        graph_route,
        **kwargs,
    ):
        graph = _graph(project, include_symbol=graph_route == "lsp")
        output_dir.mkdir(parents=True, exist_ok=True)
        graph.save_graph(output_dir / "graph.pkl")
        return graph

    monkeypatch.setattr(
        check_graph_route_alignment,
        "build_graph_for_languages",
        fake_build_graph_for_languages,
    )

    status = check_graph_route_alignment.main(
        [
            "--project-root",
            str(project),
            "--language",
            "java",
            "--output-dir",
            str(tmp_path / "out"),
            "--json",
        ]
    )

    assert status == 1
    payload = json.loads(capsys.readouterr().out)
    assert not payload["ok"]
    assert payload["alignment"]["missing_symbols"] == ["src/Foo.java:Foo"]


def test_main_reports_build_failure_as_json(tmp_path, monkeypatch, capsys):
    project = tmp_path / "repo"
    project.mkdir()

    def fail_build(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        check_graph_route_alignment,
        "build_graph_for_languages",
        fail_build,
    )

    status = check_graph_route_alignment.main(
        [
            "--project-root",
            str(project),
            "--language",
            "java",
            "--output-dir",
            str(tmp_path / "out"),
            "--json",
        ]
    )

    assert status == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "boom"
