# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rebuilding changed files with the graph's builder gives the fresh graph."""

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from codenib.compiler.verification import FactProjectionVerifier
from codenib.graph.incremental.builder_patch import (
    BuilderPatchUnavailable,
    rebuild_changed_files,
)
from codenib.graph.incremental.change_mgr import detect_changed_files
from codenib.scip_interface.pyrefly_glean import SYMBOL_TABLE_NAME
from codenib.scip_interface.scip_indexer_python import SCIPPythonIndexer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("pyrefly") is None, reason="needs pyrefly"),
]

BASE = {
    "shop/__init__.py": "",
    "shop/pricing.py": textwrap.dedent(
        """\
        from typing import overload


        class Cart:
            def __init__(self, items=None):
                self.items = list(items or [])

            @overload
            def total(self, tax: None = None) -> int: ...
            @overload
            def total(self, tax: float) -> float: ...
            def total(self, tax=None):
                return sum(self.items)
        """
    ),
    "shop/checkout.py": textwrap.dedent(
        """\
        from shop.pricing import Cart


        def checkout(items):
            cart = Cart(items)
            return cart.total()
        """
    ),
    "shop/report.py": textwrap.dedent(
        """\
        from shop.checkout import checkout


        def report(items):
            return f"total {checkout(items)}"
        """
    ),
}
# Lines added above the overloads move them, a new function appears, and
# checkout starts calling it.
EDITED = {
    "shop/pricing.py": BASE["shop/pricing.py"].replace(
        "class Cart:",
        "def round_cents(value):\n    return round(value, 2)\n\n\nclass Cart:",
    ),
    "shop/checkout.py": textwrap.dedent(
        """\
        from shop.pricing import Cart, round_cents


        def checkout(items):
            cart = Cart(items)
            return round_cents(cart.total())
        """
    ),
}


def _git(root, *arguments):
    subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True
    )


def _commit(root, files, message):
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        message,
    )


def _build(root, output):
    output.mkdir()
    graph = SCIPPythonIndexer(project_root=root, output_dir=output).run_pipeline(
        output_file=str(output / "graph.pkl"), skip_level=None, report_profile=False
    )
    assert graph is not None
    return graph


def test_patched_graph_states_the_facts_of_a_fresh_build(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_PYTHON_GRAPH_PROVIDER", "pyrefly")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, BASE, "base")
    graph = _build(repo, tmp_path / "base")
    table = tmp_path / "base" / SYMBOL_TABLE_NAME
    assert table.is_file()

    _commit(repo, EDITED, "edit")
    changed = detect_changed_files(str(repo), "HEAD~1", "HEAD", extensions={".py"})
    stats = rebuild_changed_files(graph, repo, changed, symbol_table=table)
    fresh = _build(repo, tmp_path / "fresh")

    result = FactProjectionVerifier().verify(
        index_type="symbol_graph", patched=graph, fresh=fresh
    )
    assert result.verified, result.details
    assert stats["files_rebuilt"] == 2
    # report.py was not rebuilt and still reaches checkout.
    names = set(graph.graph.vs["name"])
    assert any(name.endswith("report.py:report()") for name in names)

    # The table was carried forward, so a second patch follows the first.
    _commit(
        repo,
        {"shop/report.py": BASE["shop/report.py"].replace("total {", "sum {")},
        "second",
    )
    changed = detect_changed_files(str(repo), "HEAD~1", "HEAD", extensions={".py"})
    rebuild_changed_files(graph, repo, changed, symbol_table=table)
    again = _build(repo, tmp_path / "fresh2")
    assert (
        FactProjectionVerifier()
        .verify(index_type="symbol_graph", patched=graph, fresh=again)
        .verified
    )


def test_graph_without_a_symbol_table_is_handed_back(tmp_path):
    from codenib.graph.code_graph import CodeGraph

    with pytest.raises(BuilderPatchUnavailable, match="symbol table"):
        rebuild_changed_files(
            CodeGraph(str(tmp_path)),
            tmp_path,
            {"modified": ["a.py"]},
            symbol_table=tmp_path / SYMBOL_TABLE_NAME,
        )
