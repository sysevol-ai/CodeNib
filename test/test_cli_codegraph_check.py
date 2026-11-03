# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``codenib codegraph check``."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import igraph as ig
import pytest

from codenib import cli
from codenib.graph.code_graph import CodeGraph

RULES = """\
version: 1
rules:
  - id: guarded
    message: Every call to sink passes guard.
    to: [sink]
    through: [guard]
"""


def _graph(*, guarded: bool) -> CodeGraph:
    g = CodeGraph()
    gg = ig.Graph(directed=True)
    names = ["a.py:api()", "a.py:guard()", "a.py:sink()"]
    gg.add_vertices(3)
    gg.vs["name"] = names
    gg.vs["unified_name"] = names
    gg.vs["type"] = ["function"] * 3
    gg.vs["file"] = ["a.py"] * 3
    gg.vs["start_line"] = [0, 3, 6]
    gg.vs["end_line"] = [1, 4, 7]
    gg.add_edges([(0, 1), (1, 2)] if guarded else [(0, 2)])
    gg.es["type"] = ["reference"] * gg.ecount()
    gg.es["anchor_file"] = ["a.py"] * gg.ecount()
    gg.es["anchor_line"] = [1, 4] if guarded else [1]
    g.graph = gg
    g.name_to_vertex = {name: i for i, name in enumerate(names)}
    return g


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".codenib").mkdir()
    (tmp_path / ".codenib" / "checks.yaml").write_text(RULES)
    monkeypatch.setattr("codenib.toolchains.activate_managed_toolchain", lambda: None)
    return tmp_path


@pytest.mark.parametrize("guarded, code", [(True, 0), (False, 1)])
def test_check_exits_with_the_rule_outcome(repo, monkeypatch, capsys, guarded, code):
    monkeypatch.setattr(
        cli,
        "_load_checkable_symbol_graph",
        lambda path: (_graph(guarded=guarded), ["python"]),
    )
    assert cli.run(["codegraph", "check", str(repo), "--json"]) == code
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is guarded and report["graph_languages"] == ["python"]
    if not guarded:
        (violation,) = report["rules"][0]["violations"]
        assert [hop["name"] for hop in violation["path"]] == [
            "a.py:api()",
            "a.py:sink()",
        ]


def test_text_report_names_the_unguarded_path(repo, monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_load_checkable_symbol_graph",
        lambda path: (_graph(guarded=False), ["python"]),
    )
    assert cli.run(["codegraph", "check", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "FAIL guarded: Every call to sink passes guard." in out
    assert "a.py:2  api() calls sink()" in out and "api() -> sink()" in out


def test_invalid_rules_and_symbols_are_command_errors(repo, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_load_checkable_symbol_graph", lambda path: (_graph(guarded=True), [])
    )
    (repo / "bad.yaml").write_text("version: 1\nrules: []\n")
    assert (
        cli.run(["codegraph", "check", str(repo), "--rules", str(repo / "bad.yaml")])
        == 2
    )
    (repo / "typo.yaml").write_text(RULES.replace("[guard]", "[gaurd]"))
    assert (
        cli.run(["codegraph", "check", str(repo), "--rules", str(repo / "typo.yaml")])
        == 2
    )
    assert "'gaurd' must name exactly one symbol" in capsys.readouterr().err


def test_a_stale_or_partial_graph_is_refused(repo, monkeypatch):
    report = {"symbol_graph": True, "source_matches": False, "detail": "source changed"}
    monkeypatch.setattr(cli, "_codegraph_index_report", lambda path: report)
    with pytest.raises(cli.CLIError, match="does not match this checkout"):
        cli._load_checkable_symbol_graph(repo)
    report["source_matches"] = True
    entry = SimpleNamespace(config={"partial": False, "failed_languages": {"go": "x"}})
    monkeypatch.setattr(
        "codenib.compiler.manifest.RepoManifest.load",
        lambda path: SimpleNamespace(indexes={"symbol_graph": entry}),
    )
    with pytest.raises(cli.CLIError, match=r"incomplete \(go\)"):
        cli._load_checkable_symbol_graph(repo)


def _git(repo, *arguments):
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            *arguments,
        ],
        check=True,
        capture_output=True,
    )


def test_base_reports_changes_since_the_merge_base_in_the_working_tree(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "maintenance.auto", "false")
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "tag", "base")
    (tmp_path / "a.py").write_text("one\nTWO\nthree\nfour\n")
    (tmp_path / "b.py").write_text("new\n")
    _git(tmp_path, "add", "a.py", "b.py")
    _git(tmp_path, "commit", "-qm", "change")
    assert cli._changed_lines_since(tmp_path, "base") == {"a.py": {2, 4}, "b.py": {1}}
    (tmp_path / "b.py").write_text(
        "new\nedited\n"
    )  # uncommitted, as an agent leaves it
    (tmp_path / "c.py").write_text("x\ny\n")  # untracked
    changed = cli._changed_lines_since(tmp_path, "base")
    assert changed == {"a.py": {2, 4}, "b.py": {1, 2}, "c.py": {1, 2}}
    with pytest.raises(cli.CLIError, match="git rev-parse failed"):
        cli._changed_lines_since(tmp_path, "--output=/tmp/x")
