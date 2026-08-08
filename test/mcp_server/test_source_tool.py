# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib.mcp.server as server_module
from codenib.compiler.manifest import RepoManifest
from codenib.mcp.tools._validation import MAX_SOURCE_CONTENT_CHARS
from codenib.mcp.tools.source import read_source_impl
from codenib.source_fingerprint import fingerprint_repository


def _context(repo: Path, *, verified: bool = True):
    manifest = RepoManifest(
        repo_path=str(repo),
        commit="a" * 40,
        source_fingerprint="sha256:" + "b" * 64,
        languages=["python"],
    )
    return SimpleNamespace(
        manifest=manifest,
        artifact={"repository": "example/project"},
        source_verified=verified,
        source_error=None if verified else "checkout drifted",
    )


def test_read_source_returns_bounded_verified_window(tmp_path: Path) -> None:
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = read_source_impl(_context(tmp_path), "src/module.py", 2, 3)

    assert result["content"] == "two\nthree\n"
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["content_projection"]["truncated"] is False
    assert result["source"] == {
        "repository": "example/project",
        "commit": "a" * 40,
        "source_fingerprint": "sha256:" + "b" * 64,
        "verified": True,
    }


@pytest.mark.parametrize(
    "file_path",
    ["../secret.py", "/etc/passwd", "./src/module.py", "src\\module.py"],
)
def test_read_source_rejects_noncanonical_paths(
    tmp_path: Path,
    file_path: str,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="repository-relative POSIX"):
        read_source_impl(_context(tmp_path), file_path)


def test_read_source_rejects_absolute_symlinks_and_nonregular_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(source)
    (tmp_path / "folder").mkdir()

    with pytest.raises(ValueError, match="readable regular"):
        read_source_impl(_context(tmp_path), "link.py")
    with pytest.raises(ValueError, match="readable regular"):
        read_source_impl(_context(tmp_path), "folder")


def test_read_source_accepts_relative_symlink_contained_in_repository(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real" / "source.py"
    real.parent.mkdir()
    real.write_text("contained = True\n", encoding="utf-8")
    (tmp_path / "alias.py").symlink_to("real/source.py")

    result = read_source_impl(_context(tmp_path), "alias.py", 1, 1)

    assert result["content"] == "contained = True\n"


def test_read_source_accepts_contained_intermediate_symlink_with_parent(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real" / "source.py"
    real.parent.mkdir()
    real.write_text("contained = True\n", encoding="utf-8")
    links = tmp_path / "links"
    links.mkdir()
    (links / "package").symlink_to("../real", target_is_directory=True)

    result = read_source_impl(_context(tmp_path), "links/package/source.py", 1, 1)

    assert result["content"] == "contained = True\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
def test_read_source_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")

    with pytest.raises(ValueError, match="readable regular"):
        read_source_impl(_context(tmp_path), "pipe")


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (0, 1, "start_line must be between"),
        (3, 2, "end_line must be greater"),
        (1, 201, "at most 200 lines"),
        (20, 20, "exceeds the source file length"),
    ],
)
def test_read_source_rejects_invalid_windows(
    tmp_path: Path,
    start: int,
    end: int,
    message: str,
) -> None:
    (tmp_path / "source.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        read_source_impl(_context(tmp_path), "source.py", start, end)


def test_read_source_requires_verified_checkout(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checkout drifted"):
        read_source_impl(_context(tmp_path, verified=False), "source.py")


def test_read_source_bounds_large_windows_and_serialized_response(
    tmp_path: Path,
) -> None:
    (tmp_path / "source.py").write_text(
        "".join(f"line_{line} = {'x' * 200!r}\n" for line in range(200)),
        encoding="utf-8",
    )

    result = read_source_impl(_context(tmp_path), "source.py", 1, 200)

    assert len(result["content"]) <= MAX_SOURCE_CONTENT_CHARS
    assert result["content_projection"]["truncated"] is True
    assert result["content_projection"]["next_start_line"] > 1
    assert len(json.dumps(result).encode("utf-8")) < 20_000


def test_init_server_disables_source_reads_after_checkout_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fingerprint = fingerprint_repository(repo)
    manifest = RepoManifest(
        repo_path=str(repo),
        commit=commit,
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    )
    manifest_path = tmp_path / "repo_manifest.json"
    manifest.save(manifest_path)
    monkeypatch.setattr(server_module, "_ctx", None)

    server_module.init_server(manifest_path)
    assert server_module.get_context().source_verified is True

    source.write_text("value = 2\n", encoding="utf-8")
    server_module.init_server(manifest_path)

    ctx = server_module.get_context()
    assert ctx.source_verified is False
    assert "do not match the indexed content" in str(ctx.source_error)
