# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib.artifacts.runtime as artifact_runtime_module
import codenib.mcp.server as server_module
import codenib.source_fingerprint as source_fingerprint_module
from codenib._contained_source import read_repository_file
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.mcp.tools._validation import MAX_SOURCE_CONTENT_CHARS
from codenib.mcp.tools.source import read_source_impl
from codenib.source_fingerprint import RepositorySourceBinding, fingerprint_repository


def _context(repo: Path, *, verified: bool = True):
    manifest = RepoManifest(
        repo_path=str(repo),
        commit="a" * 40,
        source_fingerprint="sha256:" + "b" * 64,
        languages=["python"],
    )
    context = SimpleNamespace(
        manifest=manifest,
        artifact={"repository": "example/project"},
        source_verified=verified,
        source_error=None if verified else "source content drifted",
    )
    context.read_source_bytes = lambda relative, max_bytes: read_repository_file(
        repo,
        relative,
        max_bytes=max_bytes,
    )
    return context


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
        "verification_scope": "content-bytes",
        "commit_verified": False,
        "checkout_state": "not-attested",
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


def test_read_source_rejects_absolute_symlink_and_nonregular(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(source)
    (tmp_path / "folder").mkdir()

    with pytest.raises(ValueError, match="readable regular"):
        read_source_impl(_context(tmp_path), "link.py", 1, 1)
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


def test_read_source_requires_content_authenticated_binding(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="source content drifted"):
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
    initial_manifest = asyncio.run(server_module.get_manifest())
    assert initial_manifest["runtime"]["source_read"] == {
        "verified": True,
        "error": None,
        "verification_scope": "content-bytes",
        "commit_verified": False,
        "checkout_state": "not-attested",
    }

    source.write_text("value = 2\n", encoding="utf-8")
    runtime_manifest = asyncio.run(server_module.get_manifest())
    assert runtime_manifest["runtime"]["source_read"]["verified"] is False
    assert runtime_manifest["runtime"]["source_read"]["verification_scope"] is None
    assert runtime_manifest["runtime"]["source_read"]["commit_verified"] is False
    assert (
        runtime_manifest["runtime"]["source_read"]["checkout_state"] == "not-attested"
    )
    assert server_module.get_context().source_verified is False
    assert "inventory changed" in str(
        runtime_manifest["runtime"]["source_read"]["error"]
    )

    server_module.init_server(manifest_path)

    ctx = server_module.get_context()
    assert ctx.source_verified is False
    assert "do not match the indexed content" in str(ctx.source_error)


def test_init_server_capture_return_cancellation_closes_preowned_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo)
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    ).save(manifest_path)
    captured: list[RepositorySourceBinding] = []
    real_capture = source_fingerprint_module.capture_repository_source
    interruption = KeyboardInterrupt("injected after MCP source capture returned")

    def capture(*args, **kwargs):
        source = real_capture(*args, **kwargs)
        captured.append(source)
        raise interruption

    monkeypatch.setattr(
        source_fingerprint_module,
        "capture_repository_source",
        capture,
    )
    monkeypatch.setattr(server_module, "_ctx", None)
    with pytest.raises(KeyboardInterrupt) as caught:
        server_module.init_server(manifest_path)

    assert caught.value is interruption
    assert len(captured) == 1
    assert captured[0].closed
    assert server_module._ctx is None


def test_init_server_persistent_close_eio_preserves_primary_and_retry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source_path = repo / "source.py"
    source_path.write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo)
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    ).save(manifest_path)
    source_path.write_text("value = 2\n", encoding="utf-8")
    captured: list[RepositorySourceBinding] = []
    real_capture = source_fingerprint_module.capture_repository_source
    real_close = RepositorySourceBinding.close

    def capture(*args, **kwargs):
        source = real_capture(*args, **kwargs)
        captured.append(source)
        return source

    def persistent_eio(source: RepositorySourceBinding) -> None:
        if source in captured:
            raise OSError(errno.EIO, "injected MCP source close EIO")
        real_close(source)

    monkeypatch.setattr(
        source_fingerprint_module,
        "capture_repository_source",
        capture,
    )
    monkeypatch.setattr(RepositorySourceBinding, "close", persistent_eio)
    monkeypatch.setattr(server_module, "_ctx", None)
    with pytest.raises(ValueError, match="do not match the indexed content") as caught:
        server_module.init_server(manifest_path)

    assert len(captured) == 1
    owner = caught.value.source_cleanup_owner
    assert owner.pending_sources == (captured[0],)
    assert not captured[0].closed
    assert server_module._ctx is None

    monkeypatch.setattr(RepositorySourceBinding, "close", real_close)
    owner.close()
    assert owner.closed
    assert captured[0].closed


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(
            KeyboardInterrupt("cleanup closed then interrupted"), id="keyboard"
        ),
        pytest.param(SystemExit("cleanup closed then exited"), id="system-exit"),
    ],
)
def test_init_server_propagates_cleanup_cancellation_after_owner_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo)
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    ).save(manifest_path)
    real_capture = source_fingerprint_module.capture_repository_source
    real_owner_close = artifact_runtime_module.SourceBindingCleanupOwner.close
    injected = False

    def fail_capture(*args, **kwargs):
        real_capture(*args, **kwargs)
        raise ValueError("injected capture primary")

    def close_then_interrupt(owner) -> None:
        nonlocal injected
        real_owner_close(owner)
        if not injected:
            injected = True
            raise interruption

    monkeypatch.setattr(
        source_fingerprint_module,
        "capture_repository_source",
        fail_capture,
    )
    monkeypatch.setattr(
        artifact_runtime_module.SourceBindingCleanupOwner,
        "close",
        close_then_interrupt,
    )
    previous_context = SimpleNamespace(marker="previous")
    monkeypatch.setattr(server_module, "_ctx", previous_context)

    with pytest.raises(type(interruption)) as caught:
        server_module.init_server(manifest_path)

    assert caught.value is interruption
    assert server_module._ctx is previous_context


def test_init_server_primary_cancellation_wins_and_retains_eio_retry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo)
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    ).save(manifest_path)
    real_capture = source_fingerprint_module.capture_repository_source
    real_close = RepositorySourceBinding.close
    captured: list[RepositorySourceBinding] = []
    primary = KeyboardInterrupt("injected capture cancellation")

    def interrupt_capture(*args, **kwargs):
        source = real_capture(*args, **kwargs)
        captured.append(source)
        raise primary

    def persistent_eio(source: RepositorySourceBinding) -> None:
        if source in captured:
            raise OSError(errno.EIO, "injected cleanup EIO")
        real_close(source)

    monkeypatch.setattr(
        source_fingerprint_module,
        "capture_repository_source",
        interrupt_capture,
    )
    monkeypatch.setattr(RepositorySourceBinding, "close", persistent_eio)
    previous_context = SimpleNamespace(marker="previous")
    monkeypatch.setattr(server_module, "_ctx", previous_context)

    with pytest.raises(KeyboardInterrupt) as caught:
        server_module.init_server(manifest_path)

    assert caught.value is primary
    owner = caught.value.source_cleanup_owner
    assert owner.pending_sources == (captured[0],)
    assert server_module._ctx is previous_context

    monkeypatch.setattr(RepositorySourceBinding, "close", real_close)
    owner.close()
    assert owner.closed


def test_init_server_does_not_downgrade_capture_error_with_nested_pending_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo)
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    ).save(manifest_path)

    class PendingOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    pending = PendingOwner()
    primary = ValueError("capture failed with partial owner")
    primary.source_cleanup_owner = pending  # type: ignore[attr-defined]

    def fail_capture(*_args, **_kwargs):
        raise primary

    monkeypatch.setattr(
        source_fingerprint_module,
        "capture_repository_source",
        fail_capture,
    )
    previous_context = SimpleNamespace(marker="previous")
    monkeypatch.setattr(server_module, "_ctx", previous_context)

    with pytest.raises(ValueError) as caught:
        server_module.init_server(manifest_path)

    assert caught.value is primary
    assert caught.value.source_cleanup_owner is pending
    assert server_module._ctx is previous_context
    pending.close()


def test_init_server_attaches_nested_owner_to_actual_cleanup_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
    fingerprint = fingerprint_repository(repo)
    manifest_path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint.value,
        file_count=fingerprint.file_count,
        languages=["python"],
    ).save(manifest_path)

    class PendingOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    pending = PendingOwner()
    primary = ValueError("capture failed with partial owner")
    primary.source_cleanup_owner = pending  # type: ignore[attr-defined]
    interruption = SystemExit("cleanup cancellation")
    real_owner_close = artifact_runtime_module.SourceBindingCleanupOwner.close
    injected = False

    def fail_capture(*_args, **_kwargs):
        raise primary

    def interrupt_empty_owner(owner) -> None:
        nonlocal injected
        real_owner_close(owner)
        if not injected:
            injected = True
            raise interruption

    monkeypatch.setattr(
        source_fingerprint_module,
        "capture_repository_source",
        fail_capture,
    )
    monkeypatch.setattr(
        artifact_runtime_module.SourceBindingCleanupOwner,
        "close",
        interrupt_empty_owner,
    )
    previous_context = SimpleNamespace(marker="previous")
    monkeypatch.setattr(server_module, "_ctx", previous_context)

    with pytest.raises(SystemExit) as caught:
        server_module.init_server(manifest_path)

    assert caught.value is interruption
    assert caught.value.source_cleanup_owner is pending
    assert server_module._ctx is previous_context
    pending.close()


def test_init_server_never_treats_explicit_legacy_identity_as_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_fingerprint = "sha256:" + "a" * 64
    manifest = RepoManifest(
        repo_path="/unavailable",
        source_fingerprint=legacy_fingerprint,
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path="/must-not-be-read/vector",
                built_at="2026-01-01T00:00:00",
                built_at_epoch=0.0,
                status="fresh",
                source_fingerprint=legacy_fingerprint,
            )
        },
    )
    from codenib import native_index_authorization
    from codenib.index.embedding import artifact_integrity

    capture_calls: list[str] = []
    mint_calls: list[str] = []
    monkeypatch.setattr(
        artifact_integrity,
        "capture_authenticated_vector_view",
        lambda _path: capture_calls.append("capture"),
    )
    monkeypatch.setattr(
        native_index_authorization,
        "_mint_trusted_local_admin_authorization",
        lambda *_args, **_kwargs: mint_calls.append("mint"),
    )
    monkeypatch.setattr(server_module, "_ctx", None)

    server_module.init_server(manifest)

    ctx = server_module.get_context()
    assert ctx.source_verified is False
    assert "source binding has not been verified" in str(ctx.source_error)
    assert ctx.vector is None
    assert "external authorization" in ctx.errors["vector"]
    assert capture_calls == []
    assert mint_calls == []
