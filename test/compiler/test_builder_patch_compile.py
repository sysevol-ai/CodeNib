# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The compiler patches a stale Python graph with its builder when asked to."""

from __future__ import annotations

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
    "shop/pricing.py": textwrap.dedent(
        """\
        class Cart:
            def __init__(self, items=None):
                self.items = list(items or [])

            def total(self):
                return sum(self.items)
        """
    ),
    "shop/checkout.py": textwrap.dedent(
        """\
        from shop.pricing import Cart


        def checkout(items):
            return Cart(items).total()
        """
    ),
}
EDITED = {
    "shop/pricing.py": BASE["shop/pricing.py"].replace(
        "class Cart:",
        "def round_cents(value):\n    return round(value, 2)\n\n\nclass Cart:",
    ),
    "shop/checkout.py": textwrap.dedent(
        """\
        from shop.pricing import Cart, round_cents


        def checkout(items):
            return round_cents(Cart(items).total())
        """
    ),
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
