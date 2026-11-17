# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The compiler patches a stale Python graph with its builder when asked to."""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from codenib.compiler.graph_artifact import load_authenticated_graph_artifact
from codenib.compiler.index_compiler import GRAPH_INCREMENTAL_ENV
from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
from codenib.compiler.verification import FactProjectionVerifier
from codenib.paths import repo_index_dir

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("pyrefly") is None, reason="needs pyrefly"),
]

BASE = {
    "shop/__init__.py": "",
    "shop/pricing.py": textwrap.dedent("""\
        class Cart:
            def __init__(self, items=None):
                self.items = list(items or [])

            def total(self):
                return sum(self.items)
        """),
    "shop/checkout.py": textwrap.dedent("""\
        from shop.pricing import Cart


        def checkout(items):
            return Cart(items).total()
        """),
}
EDITED = {
    "shop/pricing.py": BASE["shop/pricing.py"].replace(
        "class Cart:",
        "def round_cents(value):\n    return round(value, 2)\n\n\nclass Cart:",
    ),
    "shop/checkout.py": textwrap.dedent("""\
        from shop.pricing import Cart, round_cents


        def checkout(items):
            return round_cents(Cart(items).total())
        """),
}


def _git(root, *arguments):
    subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True
    )


def _commit(root: Path, files, message):
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


def _index(repo: Path, **options):
    from codenib.cli import index_repository

    _, failed = index_repository(
        repo,
        languages=["python"],
        views=["symbol_graph"],
        allow_partial_graph_languages=False,
        **options,
    )
    assert not failed
    manifest = RepoManifest.load(repo_index_dir(repo) / MANIFEST_FILENAME)
    return manifest.indexes["symbol_graph"]


def _surface(graph):
    """Every attribute the query-surface hash covers, without the order."""

    core = graph.graph
    names = core.vs["name"]
    vertices = sorted(
        repr(sorted(vertex.attributes().items(), key=str)) for vertex in core.vs
    )
    edges = sorted(
        repr(
            (
                names[edge.source],
                names[edge.target],
                sorted(edge.attributes().items(), key=str),
            )
        )
        for edge in core.es
    )
    return vertices, edges


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODENIB_PYTHON_GRAPH_PROVIDER", "pyrefly")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _commit(root, BASE, "base")
    return root


def test_stale_graph_is_patched_and_reads_like_a_fresh_build(repo, monkeypatch):
    first = _index(repo)
    assert first.metadata["update_mode"] == "full_rebuild"
    _commit(repo, EDITED, "edit")

    monkeypatch.setenv(GRAPH_INCREMENTAL_ENV, "builder")
    patched_entry = _index(repo)

    assert patched_entry.metadata["update_mode"] == "incremental"
    assert patched_entry.metadata["patch"] == "builder"
    assert patched_entry.metadata["patch_stats"]["files_rebuilt"] == 2
    # Readers that accept a built graph accept this generation.
    patched = load_authenticated_graph_artifact(patched_entry)
    positions = patched.lsp_occurrence_index

    monkeypatch.delenv(GRAPH_INCREMENTAL_ENV)
    fresh_entry = _index(repo, rebuild=True)
    assert fresh_entry.metadata["update_mode"] == "full_rebuild"
    fresh = load_authenticated_graph_artifact(fresh_entry)

    result = FactProjectionVerifier().verify(
        index_type="symbol_graph", patched=patched, fresh=fresh
    )
    assert result.verified, result.details
    # The query-surface hash follows vertex order, which a patch changes.
    # Order aside, everything it covers is the same.
    assert _surface(patched) == _surface(fresh)
    assert sorted(positions.occurrences, key=repr) == sorted(
        fresh.lsp_occurrence_index.occurrences, key=repr
    )


def test_one_patch_can_follow_another(repo, monkeypatch):
    _index(repo)
    monkeypatch.setenv(GRAPH_INCREMENTAL_ENV, "builder")
    _commit(repo, EDITED, "edit")
    assert _index(repo).metadata["update_mode"] == "incremental"

    again = dict(EDITED)
    again["shop/checkout.py"] = EDITED["shop/checkout.py"].replace(
        "return round_cents(Cart(items).total())",
        "cart = Cart(items)\n    return round_cents(cart.total())",
    )
    _commit(repo, again, "second edit")
    second = _index(repo)
    assert second.metadata["update_mode"] == "incremental"
    assert second.metadata["patch_stats"]["files_rebuilt"] == 1
    patched = load_authenticated_graph_artifact(second)

    monkeypatch.delenv(GRAPH_INCREMENTAL_ENV)
    fresh = load_authenticated_graph_artifact(_index(repo, rebuild=True))
    assert _surface(patched) == _surface(fresh)
    assert sorted(patched.lsp_occurrence_index.occurrences, key=repr) == sorted(
        fresh.lsp_occurrence_index.occurrences, key=repr
    )


def test_without_the_setting_a_stale_graph_is_rebuilt(repo):
    _index(repo)
    _commit(repo, EDITED, "edit")

    assert _index(repo).metadata["update_mode"] == "full_rebuild"


def _patched_and_fresh(repo, monkeypatch):
    monkeypatch.setenv(GRAPH_INCREMENTAL_ENV, "builder")
    entry = _index(repo)
    patched = load_authenticated_graph_artifact(entry)
    monkeypatch.delenv(GRAPH_INCREMENTAL_ENV)
    fresh = load_authenticated_graph_artifact(_index(repo, rebuild=True))
    return entry, patched, fresh


def test_a_commit_that_only_adds_a_file_is_patched_exactly(repo, monkeypatch):
    _index(repo)
    added = {"shop/refund.py": textwrap.dedent("""\
            from shop.pricing import Cart


            def refund(items):
                return -Cart(items).total()
            """)}
    _commit(repo, added, "add a file")

    entry, patched, fresh = _patched_and_fresh(repo, monkeypatch)

    assert entry.metadata["update_mode"] == "incremental"
    assert _surface(patched) == _surface(fresh)
    assert sorted(patched.lsp_occurrence_index.occurrences, key=repr) == sorted(
        fresh.lsp_occurrence_index.occurrences, key=repr
    )


def test_files_that_are_not_python_are_not_handed_to_the_builder(repo, monkeypatch):
    _index(repo)
    other = {"docs/notes.md": "# Notes\n", "settings.toml": "[shop]\nopen = true\n"}
    _commit(repo, {**EDITED, **other}, "edit, with notes")

    entry, patched, fresh = _patched_and_fresh(repo, monkeypatch)

    assert entry.metadata["update_mode"] == "incremental"
    assert entry.metadata["patch_stats"]["files_rebuilt"] == 2
    assert _surface(patched) == _surface(fresh)
    assert sorted(patched.lsp_occurrence_index.occurrences, key=repr) == sorted(
        fresh.lsp_occurrence_index.occurrences, key=repr
    )


@pytest.mark.parametrize("change", ["remove a file", "add notes"])
def test_a_change_the_builder_checks_nothing_for_is_rebuilt(repo, monkeypatch, change):
    # Every symbol carries the commit, and only a check brings it up to date.
    _index(repo)
    if change == "remove a file":
        (repo / "shop/checkout.py").unlink()
        _commit(repo, {}, change)
    else:
        _commit(repo, {"docs/notes.md": "# Notes\n"}, change)

    monkeypatch.setenv(GRAPH_INCREMENTAL_ENV, "builder")
    assert _index(repo).metadata["update_mode"] == "full_rebuild"


def test_a_patch_that_fails_is_rebuilt(repo, monkeypatch):
    from codenib.graph.incremental import builder_patch

    def fail(*arguments, **options):
        raise KeyError("Attribute does not exist")

    _index(repo)
    _commit(repo, EDITED, "edit")

    monkeypatch.setattr(builder_patch, "rebuild_changed_files", fail)
    monkeypatch.setenv(GRAPH_INCREMENTAL_ENV, "builder")
    assert _index(repo).metadata["update_mode"] == "full_rebuild"


def test_a_graph_that_differs_from_its_receipt_is_rebuilt(repo, monkeypatch):
    from codenib.graph.code_graph import CodeGraph

    first = _index(repo)
    graph_path = Path(first.path) / "graph.pkl"
    graph = CodeGraph.load_graph(str(graph_path))
    graph.graph.delete_edges([0])
    graph.save_graph(str(graph_path))
    _commit(repo, EDITED, "edit")

    monkeypatch.setenv(GRAPH_INCREMENTAL_ENV, "builder")
    assert _index(repo).metadata["update_mode"] == "full_rebuild"


def test_positions_that_differ_from_their_receipt_are_rebuilt(repo, monkeypatch):
    from codenib.scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex

    first = _index(repo)
    path = Path(first.path) / "lsp_index.pkl"
    positions = SCIPOccurrenceIndex.load(str(path))
    SCIPOccurrenceIndex(
        positions.occurrences[1:], position_encoding=positions.position_encoding
    ).save(str(path))
    _commit(repo, EDITED, "edit")

    monkeypatch.setenv(GRAPH_INCREMENTAL_ENV, "builder")
    assert _index(repo).metadata["update_mode"] == "full_rebuild"


def test_a_symbol_table_without_a_prefix_is_rebuilt(repo, monkeypatch):
    from codenib.scip_interface.pyrefly_glean import SYMBOL_TABLE_NAME

    first = _index(repo)
    path = Path(first.path) / SYMBOL_TABLE_NAME
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        table = json.load(handle)
    # What a build wrote before the table recorded its prefix and files.
    table["schema"] = 1
    del table["prefix"], table["files"]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(table, handle)
    _commit(repo, EDITED, "edit")

    monkeypatch.setenv(GRAPH_INCREMENTAL_ENV, "builder")
    assert _index(repo).metadata["update_mode"] == "full_rebuild"


@pytest.mark.parametrize(
    "config",
    [
        {"pyrightconfig.json": '{"exclude": ["shop/checkout.py"]}\n'},
        {"pyproject.toml": '[tool.pyright]\nexclude = ["shop/checkout.py"]\n'},
    ],
    ids=["pyrightconfig", "pyproject"],
)
def test_a_changed_project_configuration_is_rebuilt(repo, monkeypatch, config):
    _index(repo)
    _commit(repo, config, "narrow the checked files")

    entry, patched, fresh = _patched_and_fresh(repo, monkeypatch)

    assert _surface(patched) == _surface(fresh)
    assert "shop/checkout.py" not in set(fresh.graph.vs["name"])
    assert entry.metadata["update_mode"] == "full_rebuild"
