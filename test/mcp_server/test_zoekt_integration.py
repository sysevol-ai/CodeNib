# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end integration tests for Zoekt search via MCP.

Builds a tiny git-tracked repo, runs the real ``zoekt-git-index`` binary to
produce shards, hydrates a ``ServerContext`` against the resulting manifest,
and exercises ``search_zoekt_impl`` against a live ``zoekt-webserver``
subprocess.

These tests require the Zoekt Go binaries on ``$PATH``::

    go install github.com/sourcegraph/zoekt/cmd/zoekt-git-index@latest
    go install github.com/sourcegraph/zoekt/cmd/zoekt-webserver@latest

If either binary is missing, all tests in this module are skipped.  The
unit tests in ``test_zoekt_searcher.py`` and ``test_tools_search.py`` cover
the same code paths against mocks for CI environments without Zoekt.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from codeminer.compiler.index_builders import ZoektIndexBuilder
from codeminer.compiler.manifest import IndexEntry, RepoManifest
from codeminer.mcp.context import ServerContext
from codeminer.mcp.tools.search import search_zoekt_impl

# Skip the entire module when Zoekt binaries are not installed.
pytestmark = pytest.mark.skipif(
    shutil.which("zoekt-git-index") is None or shutil.which("zoekt-webserver") is None,
    reason=(
        "Zoekt binaries (zoekt-git-index, zoekt-webserver) not on $PATH. "
        "Install with: go install github.com/sourcegraph/zoekt/cmd/...@latest"
    ),
)


# ---------------------------------------------------------------------------
# Synthetic repo
# ---------------------------------------------------------------------------

REPO_FILES: dict[str, str] = {
    "src/auth.py": (
        "def login(user, password):\n"
        "    if not user:\n"
        "        raise InvalidTokenError('missing user')\n"
        "    return True\n"
    ),
    "src/billing.py": (
        "TAX_RATE = 0.08\n"
        "\n"
        "def calculate_tax(amount):\n"
        '    """Compute MAGIC_BILLING_MARKER sales tax."""\n'
        "    return amount * TAX_RATE\n"
    ),
    "docs/README.md": (
        "# Project\n"
        "\n"
        "Authentication uses InvalidTokenError when credentials are bad.\n"
        "See src/auth.py for details.\n"
    ),
    "scripts/deploy.sh": ("#!/bin/bash\n" "echo MAGIC_BILLING_MARKER deploy step\n"),
}


def _git(repo_dir: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )


def _materialize_repo(repo_dir: Path) -> None:
    """Write REPO_FILES to disk and create a single-commit git history.

    ``zoekt-git-index`` reads from a git repository, so we need at least
    one commit reachable from HEAD.
    """
    for rel, content in REPO_FILES.items():
        path = repo_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@codeminer.local")
    _git(repo_dir, "config", "user.name", "Codeminer Test")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-q", "-m", "initial")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def manifest_path(tmp_path: Path) -> Path:
    """Build a real Zoekt index for the synthetic repo and return its manifest path."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _materialize_repo(repo_dir)

    shard_dir = tmp_path / "zoekt"
    builder = ZoektIndexBuilder()
    status = builder.build(
        scope="current_repo",
        repo_path=str(repo_dir),
        output_dir=str(shard_dir),
    )
    assert (
        status.metadata["shard_count"] >= 1
    ), f"zoekt-git-index produced no shards: {status.metadata}"

    manifest = RepoManifest(
        repo_path=str(repo_dir),
        commit="zoekt-integration-test",
        languages=["python"],
        indexes={
            "zoekt": IndexEntry(
                index_type="zoekt",
                path=str(shard_dir),
                built_at="2026-01-01T00:00:00",
                built_at_epoch=1735689600.0,
                status="fresh",
            ),
        },
    )
    out_path = tmp_path / "repo_manifest.json"
    manifest.save(out_path)
    return out_path


@pytest.fixture()
def loaded_ctx(manifest_path: Path) -> Iterator[ServerContext]:
    """Yield a ServerContext with a live zoekt-webserver, then tear it down."""
    ctx = ServerContext.load(manifest_path)
    try:
        yield ctx
    finally:
        if ctx.zoekt is not None:
            ctx.zoekt.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_index_builder_creates_shards(tmp_path: Path) -> None:
    """ZoektIndexBuilder.build runs the binary and emits shard files."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _materialize_repo(repo_dir)

    shard_dir = tmp_path / "shards"
    status = ZoektIndexBuilder().build(
        scope="current_repo",
        repo_path=str(repo_dir),
        output_dir=str(shard_dir),
    )

    assert status.index_type == "zoekt"
    assert status.path == str(shard_dir)
    shards = [p for p in shard_dir.iterdir() if p.suffix == ".zoekt"]
    assert shards, f"no .zoekt shards under {shard_dir}: {list(shard_dir.iterdir())}"
    assert status.metadata["shard_count"] == len(shards)


def test_server_context_starts_zoekt(loaded_ctx: ServerContext) -> None:
    """ServerContext.load brings up zoekt-webserver and reports no errors."""
    assert loaded_ctx.zoekt is not None, f"zoekt failed: {loaded_ctx.errors}"
    assert "zoekt" not in loaded_ctx.errors
    assert loaded_ctx.zoekt.is_running
    assert loaded_ctx.zoekt.port > 0


def test_search_substring_finds_known_token(loaded_ctx: ServerContext) -> None:
    """A unique substring (`MAGIC_BILLING_MARKER`) is found across file types."""
    results = search_zoekt_impl(
        loaded_ctx,
        query="MAGIC_BILLING_MARKER",
        top_k=20,
    )

    assert results, "Zoekt should find at least one occurrence"
    files = {r["file"] for r in results}
    # The token appears in the Python source and the shell script.
    assert "src/billing.py" in files, f"missing src/billing.py: {files}"
    assert "scripts/deploy.sh" in files, f"missing scripts/deploy.sh: {files}"
    # Every result is file-typed.
    assert {r["type"] for r in results} == {"file"}


def test_search_finds_token_in_documentation(loaded_ctx: ServerContext) -> None:
    """Zoekt searches raw text, so it sees identifiers mentioned in Markdown."""
    results = search_zoekt_impl(
        loaded_ctx,
        query="InvalidTokenError",
        top_k=20,
    )

    assert results
    files = {r["file"] for r in results}
    # Shows up in the Python source AND the Markdown docs.
    assert "src/auth.py" in files
    assert "docs/README.md" in files


def test_search_file_filter_scopes_results(loaded_ctx: ServerContext) -> None:
    """``file_filter`` restricts matches to paths satisfying the expression."""
    results = search_zoekt_impl(
        loaded_ctx,
        query="InvalidTokenError",
        top_k=20,
        file_filter=r"\.py$",
    )

    assert results
    for r in results:
        assert r["file"].endswith(
            ".py"
        ), f"unexpected file in scoped results: {r['file']}"


def test_search_regex_atom(loaded_ctx: ServerContext) -> None:
    """Zoekt's ``regex:`` query atom enables regex matching."""
    results = search_zoekt_impl(
        loaded_ctx,
        query=r"regex:calculate_\w+",
        top_k=10,
    )

    assert results
    files = {r["file"] for r in results}
    assert "src/billing.py" in files


def test_search_returns_line_range_and_snippet(loaded_ctx: ServerContext) -> None:
    """Each hit carries a usable line range and a snippet of matched content."""
    results = search_zoekt_impl(
        loaded_ctx,
        query="calculate_tax",
        top_k=5,
    )

    assert results
    hit = next((r for r in results if r["file"] == "src/billing.py"), None)
    assert hit is not None, f"calculate_tax not found in src/billing.py: {results}"
    assert isinstance(hit.get("start_line"), int)
    assert isinstance(hit.get("end_line"), int)
    assert hit["start_line"] >= 1
    assert hit["end_line"] >= hit["start_line"]
    assert "calculate_tax" in (hit.get("content") or "")
