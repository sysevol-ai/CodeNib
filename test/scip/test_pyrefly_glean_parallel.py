# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Parallel reading of pyrefly Glean reports, and the per-pass path filter."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.scip_interface.pyrefly_glean import read_glean_report
from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer

# pyrefly 1.4.0 `check --report-glean` on four modules: definitions, aliased
# imports, a keyword call, and a non-importable module (generate-docs.py).
FIXTURE = Path(__file__).parent / "fixtures" / "pyrefly_glean_small"


def _snapshot(report):
    return {
        "files": sorted(report.files),
        "definitions": [dataclasses.asdict(d) for d in report.definitions],
        "declaration_starts": dict(report.declaration_starts),
        "aliases": report.aliases,
        "xrefs": dict(report.xrefs),
        "calls": dict(report.calls),
    }


@pytest.mark.parametrize(
    "path_allowed", [None, lambda path: path.startswith("pkg/")], ids=["all", "pkg"]
)
def test_parallel_report_matches_sequential(path_allowed):
    sequential = read_glean_report(FIXTURE, path_allowed=path_allowed, workers=1)
    parallel = read_glean_report(FIXTURE, path_allowed=path_allowed, workers=2)
    assert _snapshot(parallel) == _snapshot(sequential)
    assert sequential.definitions and sequential.aliases and sequential.calls


def test_report_renames_a_non_importable_module():
    report = read_glean_report(FIXTURE, workers=2)
    names = {d.qname for d in report.definitions if d.file == "generate-docs.py"}
    assert "generate-docs.main" in names


def test_worker_count_comes_from_the_environment(monkeypatch):
    from codenib.scip_interface import pyrefly_glean

    assert pyrefly_glean._glean_workers(10) == 1  # small reports stay in-process
    monkeypatch.setenv("CODENIB_GLEAN_WORKERS", "3")
    assert pyrefly_glean._glean_workers(10) == 3


def test_project_filter_checks_each_path_once(monkeypatch):
    names = ["src/a.py", "src/a.py:f()", "src/a.py:g()", "build/b.py", "build/b.py:h()"]
    graph = CodeGraph()
    graph.graph.add_vertices(len(names))
    graph.graph.vs["name"] = names
    graph.graph.vs["type"] = ["file", "function", "function", "file", "function"]
    graph.graph.vs["file"] = [name.split(":")[0] for name in names]
    graph.name_to_vertex = {name: index for index, name in enumerate(names)}

    indexer = SCIPPythonIndexer.__new__(SCIPPythonIndexer)
    indexer._target_dir = None
    indexer.exclude_patterns = ["build/**"]
    calls = []
    original = SCIPPythonIndexer._path_allowed

    def counted(self, path, allow_target_ancestor=False):
        calls.append((path, allow_target_ancestor))
        return original(self, path, allow_target_ancestor=allow_target_ancestor)

    monkeypatch.setattr(SCIPPythonIndexer, "_path_allowed", counted)
    filtered = indexer._filter_project_graph(graph)
    assert sorted(filtered.graph.vs["name"]) == [
        "src/a.py",
        "src/a.py:f()",
        "src/a.py:g()",
    ]
    # Two symbols in src/a.py, one in build/b.py: one check per path and kind.
    assert sorted(calls) == [
        ("build/b.py", False),
        ("build/b.py", True),
        ("src/a.py", False),
        ("src/a.py", True),
    ]


def test_failed_workers_fall_back_to_in_process_parsing(monkeypatch):
    import shutil
    import sys

    false = shutil.which("false")
    if false is None:
        pytest.skip("no `false` executable")
    expected = _snapshot(read_glean_report(FIXTURE, workers=1))
    monkeypatch.setattr(sys, "executable", false)  # every worker exits 1
    assert _snapshot(read_glean_report(FIXTURE, workers=2)) == expected


def test_parallel_read_works_from_a_script_without_a_main_guard(tmp_path):
    import subprocess
    import sys

    # multiprocessing's spawn would re-run this script in every worker.
    script = tmp_path / "unguarded.py"
    script.write_text(
        "from codenib.scip_interface.pyrefly_glean import read_glean_report\n"
        f"report = read_glean_report({str(FIXTURE)!r}, workers=2)\n"
        "print(len(report.definitions))\n"
    )
    done = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    expected = len(read_glean_report(FIXTURE, workers=1).definitions)
    assert done.stdout.strip() == str(expected)
