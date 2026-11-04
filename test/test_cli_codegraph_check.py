# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``codenib codegraph check``."""

from __future__ import annotations

import json
import subprocess
import time
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


@pytest.mark.parametrize("completion", [{"partial": True}, {"complete": False}])
def test_an_incomplete_generation_report_is_refused_without_partial_flags(
    repo, monkeypatch, completion
):
    monkeypatch.setattr(
        cli,
        "_codegraph_index_report",
        lambda _path: {"symbol_graph": True, "source_matches": True, "detail": ""},
    )
    entry = SimpleNamespace(
        config={
            "partial": False,
            "partial_index": False,
            "failed_languages": {},
            "index_generation_reports": {"python": completion},
        }
    )
    monkeypatch.setattr(
        "codenib.compiler.manifest.RepoManifest.load",
        lambda _path: SimpleNamespace(indexes={"symbol_graph": entry}),
    )
    with pytest.raises(cli.CLIError, match="incomplete"):
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


@pytest.mark.parametrize("name", ["café.py", "two words.py", 'quote"tab\t.py'])
def test_real_diff_filenames_preserve_changed_violations(tmp_path, name):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "maintenance.auto", "false")
    (tmp_path / name).write_text("def run():\n    pass\n")
    _git(tmp_path, "add", "--", name)
    _git(tmp_path, "commit", "-qm", "test: seed diff filename")
    (tmp_path / name).write_text("def run():\n    sink()\n")
    changed = cli._changed_lines_since(tmp_path, "HEAD")
    assert changed == {name: {2}}
    from codenib.graph.path_checks import PathRule, check_paths

    graph = _graph(guarded=False)
    graph.graph.vs["file"] = [name] * 3
    graph.graph.es["anchor_file"] = [name]
    result = check_paths(
        graph,
        [PathRule(id="guarded", to=("sink",), through=("guard",))],
        tmp_path,
        changed,
    )
    assert not result.passed and result.rules[0].violations[0].changed is True


@pytest.mark.parametrize("complete", [False, True])
def test_actual_compiler_publication_checks_completion_not_partial_permission(
    tmp_path, monkeypatch, complete
):
    from codenib.compiler.index_builders import SymbolGraphBuilder
    from codenib.compiler.manifest import IndexEntry, RepoManifest
    from codenib.ls_router import GraphBuildResult
    from codenib.repository_source_selection import RepositorySourceSelection
    from codenib.source_fingerprint import fingerprint_repository

    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    (repo / "a.py").write_text("def entry():\n    missing_sink()\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "maintenance.auto", "false")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "test: seed compiler source")
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    graph = CodeGraph(str(repo))
    graph.add_root_node(".")
    graph.add_file_node("a.py")
    graph.build_range_indexes()
    monkeypatch.setattr(
        "codenib.ls_router.build_graph_for_languages_with_report",
        lambda *_a, **_kw: GraphBuildResult(
            graph=graph,
            requested_languages=["python"],
            available_languages=["python"],
            failed_languages={},
            index_generation_reports={
                "python": {"complete": complete, "partial": not complete}
            },
        ),
    )
    status = SymbolGraphBuilder(
        languages=["python"],
        allow_partial_languages=True,
        allow_partial_index=True,
        source_coverage_fallback=True,
    ).build(scope="current_repo", repo_path=str(repo), output_dir=str(cache / "graph"))
    selection = RepositorySourceSelection()
    source = fingerprint_repository(repo, selection=selection)
    entry = IndexEntry(
        index_type="symbol_graph",
        path=status.path,
        built_at="fixture",
        built_at_epoch=time.time(),
        status="fresh",
        commit=commit,
        source_fingerprint=source.value,
        source_selection_digest=selection.digest,
        config=status.metadata,
    )
    manifest = RepoManifest(
        repo_path=str(repo),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=source.value,
        last_indexed_source_fingerprint=source.value,
        source_selection=selection,
        last_indexed_source_selection_digest=selection.digest,
        languages=["python"],
        file_count=source.file_count,
        indexes={"symbol_graph": entry},
    )
    manifest.derive_capabilities()
    manifest.save(cache / "repo_manifest.json")
    monkeypatch.setattr("codenib.paths.repo_index_dir", lambda _path: cache)
    report = cli._codegraph_index_report(repo)
    assert report["symbol_graph"] and report["source_matches"]
    assert report["symbol_graph_complete"] is False  # permission is not an outcome
    if complete:
        loaded, languages = cli._load_checkable_symbol_graph(repo)
        assert loaded.graph.vcount() == 2 and languages == ["python"]
    else:
        assert entry.config["partial_index"] and entry.config["partial"] is False
        with pytest.raises(cli.CLIError, match="incomplete"):
            cli._load_checkable_symbol_graph(repo)
