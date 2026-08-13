# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import dis
import hashlib
import inspect
import io
import json
import math
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import codenib.artifacts.github as github_artifacts
import codenib.artifacts.runtime as runtime_module
import codenib.index.sparse_idx.bm25_index as bm25_module
import codenib.mcp.server as server_module
from codenib._atomic_directory import (
    PublicationDirectoryReader,
    reopen_authenticated_directory,
)
from codenib.artifacts import (
    CONTEXT_ARTIFACT_MANIFEST,
    bind_context_artifact,
    extract_context_artifact_archive,
    fetch_github_context_artifact,
    query_context_artifact,
    render_artifact_mcp_config,
    resolve_github_context_artifact,
    stage_context_artifact,
    verify_context_artifact,
)
from codenib.cli import run
from codenib.compiler.index_builders import VectorIndexBuilder
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.vector_store import CodeVectorStore
from codenib.mcp.context import ServerContext
from codenib.mcp.tools.source import read_source_impl
from codenib.source_fingerprint import (
    RepositoryChangedError,
    capture_repository_source,
    fingerprint_repository,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _bm25_artifact(
    tmp_path: Path,
    *,
    source_symlink: bool = False,
    internal_source_symlink: bool = False,
) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "sample.py"
    if source_symlink and internal_source_symlink:
        raise ValueError("source symlink fixture modes are mutually exclusive")
    if source_symlink:
        outside = tmp_path / "outside.py"
        outside.write_text("SECRET = 'outside checkout'\n", encoding="utf-8")
        source.symlink_to(outside)
    elif internal_source_symlink:
        (repo / "real.py").write_text("VALUE = 1\n", encoding="utf-8")
        source.symlink_to("real.py")
    else:
        source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "CodeNib Test")
    _git(repo, "config", "user.email", "codenib@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")

    index_root = tmp_path / "indexes"
    view = index_root / "bm25"
    view.mkdir(parents=True)
    (view / "documents.json").write_text(
        json.dumps(
            [
                {
                    "page_content": "sample py value 1",
                    "metadata": {
                        "file": "sample.py",
                        "name": "VALUE",
                        "node_id": "sample.py:VALUE",
                        "type": "variable",
                        "start_line": 0,
                        "end_line": 0,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (view / "bm25_metadata.json").write_text(
        json.dumps(
            {
                "project_root": str(repo),
                "max_k": 15,
                "language": "english",
            }
        ),
        encoding="utf-8",
    )
    source_identity = fingerprint_repository(repo)
    manifest_path = index_root / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=source_identity.value,
        last_indexed_source_fingerprint=source_identity.value,
        languages=["python"],
        file_count=source_identity.file_count,
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(view),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=1.0,
                status="fresh",
                commit=commit,
                source_fingerprint=source_identity.value,
            )
        },
        capabilities={
            "sparse_search": True,
            "dense_search": False,
            "hybrid_search": False,
            "symbol_navigation": False,
        },
        compiled_at="2026-08-04T00:00:00+00:00",
        compiled_at_epoch=1.0,
    ).save(manifest_path)
    artifact = tmp_path / "artifact"
    stage_context_artifact(
        repo,
        manifest_path,
        artifact,
        repository="example/project",
    )
    return repo, artifact, commit


class _PortableEmbedding:
    def embed_query(self, text: str) -> list[float]:
        values = [
            float(value + 1) for value in hashlib.sha256(text.encode()).digest()[:4]
        ]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]


def _vector_artifact(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "semantic-repo"
    repo.mkdir()
    (repo / "search.py").write_text(
        "def locate_symbol(query):\n    return query.casefold()\n",
        encoding="utf-8",
    )
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "CodeNib Test")
    _git(repo, "config", "user.email", "codenib@example.invalid")
    _git(repo, "add", "search.py")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    config = VectorIndexBuilder(
        languages=["python"],
        embedding_model="test/model",
        embedding_provider="huggingface",
        embedding_dimension=4,
        build_levels=["l2"],
    ).artifact_identity()
    index_root = tmp_path / "semantic-indexes"
    vector = index_root / "vector"
    with patch.object(
        CodeVectorStore,
        "_initialize_embedding_model",
        return_value=_PortableEmbedding(),
    ):
        store = CodeVectorStore(
            embedding_model="test/model",
            embedding_provider="huggingface",
            dimension=4,
            index_metric="ip",
            store_path=str(vector),
            artifact_metadata=config,
        )
        store.add_code_chunks(
            [
                {
                    "content": "def locate_symbol(query): return query.casefold()",
                    "chunk_type": "function",
                    "name": "locate_symbol",
                    "file": str(repo / "search.py"),
                    "start_line": 0,
                    "end_line": 1,
                }
            ],
            level="l2",
        )
        store.save()

    source_identity = fingerprint_repository(repo)
    manifest_path = index_root / "repo_manifest.json"
    RepoManifest(
        repo_path=str(repo),
        commit=commit,
        last_indexed_commit=commit,
        source_fingerprint=source_identity.value,
        last_indexed_source_fingerprint=source_identity.value,
        languages=["python"],
        file_count=source_identity.file_count,
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=1.0,
                status="fresh",
                config=dict(config),
                metadata=dict(config),
                commit=commit,
                source_fingerprint=source_identity.value,
            )
        },
        capabilities={
            "sparse_search": False,
            "dense_search": True,
            "hybrid_search": False,
            "symbol_navigation": False,
        },
        compiled_at="2026-08-04T00:00:00+00:00",
        compiled_at_epoch=1.0,
    ).save(manifest_path)
    artifact = tmp_path / "semantic-artifact"
    stage_context_artifact(
        repo,
        manifest_path,
        artifact,
        repository="example/semantic-project",
    )
    return repo, artifact, commit


def _metadata(artifact: Path) -> dict:
    return json.loads((artifact / CONTEXT_ARTIFACT_MANIFEST).read_text())


def _write_metadata(artifact: Path, metadata: dict) -> None:
    (artifact / CONTEXT_ARTIFACT_MANIFEST).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _refresh_inventory_file(artifact: Path, metadata: dict, relative: str) -> None:
    path = artifact / relative
    record = next(item for item in metadata["files"] if item["path"] == relative)
    record["bytes"] = path.stat().st_size
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def _rewrite_artifact_source_fingerprint(artifact: Path, fingerprint: str) -> None:
    metadata = _metadata(artifact)
    metadata["repository"]["source_fingerprint"] = fingerprint
    manifest_relative = metadata["manifest"]["path"]
    manifest_path = artifact / manifest_relative
    manifest = RepoManifest.load(manifest_path)
    manifest.source_fingerprint = fingerprint
    manifest.last_indexed_source_fingerprint = fingerprint
    for entry in manifest.indexes.values():
        entry.source_fingerprint = fingerprint
    manifest.save(manifest_path)
    _refresh_inventory_file(artifact, metadata, manifest_relative)
    _write_metadata(artifact, metadata)


def _zip_tree(source: Path, output: Path, *, prefix: str = "") -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source).as_posix()
                archive.write(path, f"{prefix}{relative}")


def test_verify_and_bind_artifact_before_loading_bm25(tmp_path: Path) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)

    verified = verify_context_artifact(
        artifact,
        expected_repository="example/project",
        expected_commit=commit,
    )
    binding = bind_context_artifact(
        artifact,
        repo,
        expected_repository="example/project",
        expected_commit=commit,
    )

    assert verified.views == ("bm25",)
    assert binding.manifest.repo_path == str(repo)
    assert binding.manifest.indexes["bm25"].path == str(artifact / "views" / "bm25")
    context = ServerContext.load(
        binding.manifest,
        views=["bm25"],
        artifact_binding=binding,
    )
    assert context.bm25 is not None
    assert context.bm25.project_root == str(repo)
    results = context.bm25.search("value", return_code_content=True)
    assert results[0].file == "sample.py"
    assert "VALUE = 1" in (results[0].content or "")


def test_bind_uses_one_detached_repository_identity_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    probe = capture_repository_source(repo, exclude_roots=(artifact,))
    source_type = type(probe)
    real_snapshot = source_type.authenticated_identity_snapshot
    probe.close()
    substituted_root = tmp_path / "substituted"

    def snapshot_then_replace_public_projection(source):
        snapshot = real_snapshot(source)
        source.root = substituted_root
        source.fingerprint = "sha256-v2:" + ("0" * 64)
        source.file_count = 0
        source.file_records = ()
        return snapshot

    monkeypatch.setattr(
        source_type,
        "authenticated_identity_snapshot",
        snapshot_then_replace_public_projection,
    )

    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    try:
        assert binding.repo_path == repo
        assert binding.manifest.repo_path == str(repo)
        assert binding.source_bound
    finally:
        binding.close()


def test_bound_source_reads_poison_after_checkout_changes(tmp_path: Path) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(
        artifact,
        repo,
        expected_repository="example/project",
        expected_commit=commit,
    )
    context = ServerContext.load(
        binding.manifest,
        views=["bm25"],
        artifact_binding=binding,
    )
    assert context.source_verified is True
    (repo / "sample.py").write_text("VALUE = 999\n", encoding="utf-8")

    with pytest.raises(RepositoryChangedError):
        read_source_impl(context, "sample.py", 1, 1)
    assert context.source_verified is False
    with pytest.raises(RepositoryChangedError, match="poisoned"):
        context.bm25.search("value", return_code_content=True)


def test_artifact_binding_close_releases_unconsumed_source_authority(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    source = binding.source_binding
    assert source is not None and source.usable

    binding.close()
    binding.close()

    assert binding.source_bound is False
    assert not source.usable


@pytest.mark.parametrize(
    ("timing", "interruption"),
    [
        pytest.param(
            "before",
            KeyboardInterrupt("context binding close interrupted before source close"),
            id="before-close",
        ),
        pytest.param(
            "after",
            SystemExit("context binding close interrupted after source close"),
            id="after-close",
        ),
    ],
)
def test_artifact_binding_close_retains_cleanup_owner_through_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timing: str,
    interruption: BaseException,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    source = binding.source_binding
    assert source is not None
    source_type = type(source)
    real_close = source_type.close
    injected = False

    def interrupt_source_close(instance) -> None:
        nonlocal injected
        if instance is source and not injected:
            injected = True
            if timing == "before":
                raise interruption
            real_close(instance)
            raise interruption
        real_close(instance)

    monkeypatch.setattr(source_type, "close", interrupt_source_close)
    with pytest.raises(type(interruption)) as caught:
        binding.close()

    assert caught.value is interruption
    assert injected
    assert binding.source_bound is False
    monkeypatch.setattr(source_type, "close", real_close)
    binding.close()
    assert source.closed


def test_artifact_binding_source_authority_install_is_linear_one_shot(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    barrier = threading.Barrier(3)
    installed = []
    failures: list[BaseException] = []

    def install() -> None:
        target = []
        barrier.wait()
        try:
            binding.install_source_binding(target.append)
            installed.extend(target)
        except BaseException as exc:  # noqa: B036 - record thread result
            failures.append(exc)

    threads = [threading.Thread(target=install) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(installed) == 1
    assert installed[0] is not None
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "already transferred" in str(failures[0])
    installed[0].close()


def test_artifact_binding_rejected_expected_source_retains_original_owner(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    source = binding.source_binding
    assert source is not None
    other = capture_repository_source(repo, exclude_roots=(artifact,))

    with pytest.raises(ValueError, match="does not match"):
        binding.install_source_binding(
            lambda _source: pytest.fail("mismatched source must not be installed"),
            expected=other,
            require_expected=True,
        )

    assert binding.source_binding is source
    assert source.usable
    installed = []
    binding.install_source_binding(installed.append)
    assert installed == [source]
    source.close()
    other.close()


def test_server_context_losing_install_does_not_close_winners_source(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    winner = []
    binding.install_source_binding(winner.append)
    source = winner[0]
    assert source is not None and source.usable

    with pytest.raises(RuntimeError, match="already transferred"):
        ServerContext.load(
            binding.manifest,
            views=[],
            artifact_binding=binding,
            source_binding=source,
        )

    assert source.usable
    source.close()


def test_server_context_expected_witness_mismatch_does_not_close_witness(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = query_context_artifact(artifact, expected_commit=commit)
    witness = capture_repository_source(repo, exclude_roots=(artifact,))

    with pytest.raises(ValueError, match="does not match"):
        ServerContext.load(
            binding.manifest,
            views=[],
            artifact_binding=binding,
            source_binding=witness,
        )

    assert witness.usable
    witness.close()
    binding.close()


def test_artifact_binding_install_cancellation_closes_claimed_authority(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    source = binding.source_binding
    assert source is not None
    method = type(binding).install_source_binding
    lines, first_line = inspect.getsourcelines(method)
    target_line = first_line + next(
        index for index, line in enumerate(lines) if "install(source)" in line
    )
    interruption = KeyboardInterrupt("injected after source authority claim")
    injected = False

    def interrupt_after_claim(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and frame.f_code is method.__code__
            and event == "line"
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interruption
        return interrupt_after_claim

    sys.settrace(interrupt_after_claim)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            binding.install_source_binding(lambda _source: None)
    finally:
        sys.settrace(None)

    assert caught.value is interruption
    assert injected
    assert source.closed
    assert binding.source_bound is False


def test_server_context_install_handoff_cancellation_closes_source(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    source = binding.source_binding
    assert source is not None
    method = ServerContext.load.__func__
    source_lines, first_line = inspect.getsourcelines(method)
    call_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "artifact_binding.install_source_binding(" in line
    )
    current_line = None
    target_offset = None
    for instruction in dis.get_instructions(method):
        if instruction.starts_line is not None:
            current_line = instruction.starts_line
        if instruction.opname == "POP_TOP" and current_line in range(
            call_line, call_line + 5
        ):
            target_offset = instruction.offset
            break
    assert target_offset is not None
    interruption = KeyboardInterrupt("injected after destination install")
    injected = False

    def interrupt_after_install(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and frame.f_code is method.__code__
            and event == "opcode"
            and frame.f_lasti == target_offset
        ):
            injected = True
            raise interruption
        frame.f_trace_opcodes = True
        return interrupt_after_install

    sys.settrace(interrupt_after_install)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            ServerContext.load(
                binding.manifest,
                views=[],
                artifact_binding=binding,
            )
    finally:
        sys.settrace(None)

    assert caught.value is interruption
    assert injected
    assert source.closed
    assert binding.source_bound is False


def test_bind_checkout_cancellation_closes_captured_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    captured = []
    real_capture = runtime_module.capture_repository_source
    interruption = KeyboardInterrupt("injected checkout cancellation")
    descriptor_root = Path("/proc/self/fd")
    before = len(tuple(descriptor_root.iterdir())) if descriptor_root.is_dir() else None

    def capture(*args, **kwargs):
        source = real_capture(*args, **kwargs)
        captured.append(source)
        return source

    def interrupt_checkout(_repo: Path) -> str:
        raise interruption

    monkeypatch.setattr(runtime_module, "capture_repository_source", capture)
    monkeypatch.setattr(runtime_module, "checkout_commit", interrupt_checkout)
    with pytest.raises(KeyboardInterrupt) as caught:
        bind_context_artifact(artifact, repo, expected_commit=commit)

    assert caught.value is interruption
    assert len(captured) == 1
    assert captured[0].closed
    if before is not None:
        assert len(tuple(descriptor_root.iterdir())) <= before + 1


def test_bind_capture_return_cancellation_uses_preinstalled_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    captured = []
    real_capture = runtime_module.capture_repository_source
    interruption = KeyboardInterrupt("injected after capture returned")

    def capture(*args, **kwargs):
        source = real_capture(*args, **kwargs)
        captured.append(source)
        raise interruption

    monkeypatch.setattr(runtime_module, "capture_repository_source", capture)
    with pytest.raises(KeyboardInterrupt) as caught:
        bind_context_artifact(artifact, repo, expected_commit=commit)

    assert caught.value is interruption
    assert len(captured) == 1
    assert captured[0].closed


def test_bind_persistent_close_failure_exposes_retryable_source_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    captured = []
    real_capture = runtime_module.capture_repository_source
    interruption = KeyboardInterrupt("injected bind cancellation")

    def capture(*args, **kwargs):
        source = real_capture(*args, **kwargs)
        captured.append(source)
        return source

    def interrupt_checkout(_repo: Path) -> str:
        raise interruption

    monkeypatch.setattr(runtime_module, "capture_repository_source", capture)
    monkeypatch.setattr(runtime_module, "checkout_commit", interrupt_checkout)
    probe = capture_repository_source(repo, exclude_roots=(artifact,))
    source_type = type(probe)
    probe.close()
    real_close = source_type.close

    def persistent_close(source) -> None:
        if source in captured:
            raise OSError("injected persistent source close failure")
        real_close(source)

    monkeypatch.setattr(source_type, "close", persistent_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        bind_context_artifact(artifact, repo, expected_commit=commit)

    assert caught.value is interruption
    assert len(captured) == 1
    owner = caught.value.source_cleanup_owner
    assert owner.pending_sources == (captured[0],)
    assert not captured[0].closed

    monkeypatch.setattr(source_type, "close", real_close)
    owner.close()
    assert owner.closed
    assert captured[0].closed


def test_bind_promotes_source_cleanup_cancellation_over_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    captured = []
    real_capture = runtime_module.capture_repository_source
    cleanup_interruption = KeyboardInterrupt("source cleanup interrupted")

    def capture(*args, **kwargs):
        source = real_capture(*args, **kwargs)
        captured.append(source)
        source.fingerprint = "sha256-v2:" + ("0" * 64)
        return source

    monkeypatch.setattr(runtime_module, "capture_repository_source", capture)
    probe = capture_repository_source(repo, exclude_roots=(artifact,))
    source_type = type(probe)
    probe.close()
    real_close = source_type.close

    def interrupt_close(source) -> None:
        if source in captured:
            raise cleanup_interruption
        real_close(source)

    monkeypatch.setattr(source_type, "close", interrupt_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        bind_context_artifact(artifact, repo, expected_commit=commit)

    assert caught.value is cleanup_interruption
    assert caught.value.source_cleanup_owner.pending_sources == (captured[0],)
    monkeypatch.setattr(source_type, "close", real_close)
    caught.value.source_cleanup_owner.close()
    assert captured[0].closed


def test_bind_rejects_artifact_root_replaced_after_internal_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    saved = tmp_path / "verified-root"
    real_verify = runtime_module.verify_context_artifact

    def verify_then_replace(*args: object, **kwargs: object):
        verified = real_verify(*args, **kwargs)
        artifact.rename(saved)
        shutil.copytree(saved, artifact)
        return verified

    monkeypatch.setattr(
        runtime_module,
        "verify_context_artifact",
        verify_then_replace,
    )
    with pytest.raises(ValueError, match="changed between verification"):
        bind_context_artifact(
            artifact,
            repo,
            expected_commit=commit,
        )


def test_bind_rejects_checkout_replaced_after_source_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    foreign = tmp_path / "foreign-repo"
    shutil.copytree(repo, foreign, symlinks=True)
    captured = tmp_path / "captured-repo"
    real_checkout_commit = runtime_module.checkout_commit

    def replace_before_commit(path: Path) -> str:
        repo.rename(captured)
        foreign.rename(repo)
        return real_checkout_commit(path)

    monkeypatch.setattr(runtime_module, "checkout_commit", replace_before_commit)
    with pytest.raises(ValueError, match="changed between verification"):
        bind_context_artifact(
            artifact,
            repo,
            expected_repository="example/project",
            expected_commit=commit,
        )

    assert repo.is_dir()
    assert captured.is_dir()


def test_artifact_binding_rejects_root_replaced_before_runtime_load(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    saved = tmp_path / "bound-root"
    artifact.rename(saved)
    shutil.copytree(saved, artifact)
    documents = artifact / "views/bm25/documents.json"
    documents.write_text(
        '[{"page_content":"FOREIGN","metadata":{"file":"sample.py"}}]',
        encoding="utf-8",
    )

    context = ServerContext.load(
        binding.manifest,
        views=["bm25"],
        artifact_binding=binding,
    )

    assert context.bm25 is None
    assert "captured ownership" in context.errors["bm25"]


def test_artifact_bm25_reader_never_reads_path_swapped_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)
    binding = bind_context_artifact(artifact, repo, expected_commit=commit)
    foreign = tmp_path / "foreign-artifact"
    shutil.copytree(artifact, foreign)
    (foreign / "views/bm25/documents.json").write_text(
        '[{"page_content":"FOREIGN","metadata":{"file":"sample.py"}}]',
        encoding="utf-8",
    )
    saved = tmp_path / "authorized-artifact"
    real_open = PublicationDirectoryReader.open_authenticated_file
    swapped = False
    from codenib.index.sparse_idx import BM25CodeIndexer

    real_load_values = BM25CodeIndexer.load_index_values
    loaded_page_content: list[str] = []

    def record_loaded_values(self, documents, metadata, **kwargs):
        result = real_load_values(self, documents, metadata, **kwargs)
        loaded_page_content.extend(document.page_content for document in self.documents)
        return result

    @contextmanager
    def open_during_root_aba(self, relative, *, max_bytes=None):
        nonlocal swapped
        normalized = Path(relative).as_posix()
        if normalized != "views/bm25/documents.json":
            with real_open(self, relative, max_bytes=max_bytes) as source:
                yield source
            return
        swapped = True
        artifact.rename(saved)
        foreign.rename(artifact)
        try:
            with real_open(self, relative, max_bytes=max_bytes) as source:
                yield source
        finally:
            artifact.rename(foreign)
            saved.rename(artifact)

    monkeypatch.setattr(
        PublicationDirectoryReader,
        "open_authenticated_file",
        open_during_root_aba,
    )
    monkeypatch.setattr(
        BM25CodeIndexer,
        "load_index_values",
        record_loaded_values,
    )
    context = ServerContext.load(
        binding.manifest,
        views=["bm25"],
        artifact_binding=binding,
    )

    assert swapped
    assert context.bm25 is None
    assert context.errors["bm25"]
    assert loaded_page_content == ["sample py value 1"]


def test_stage_bind_query_and_mcp_accept_contained_source_symlink(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _bm25_artifact(
        tmp_path,
        internal_source_symlink=True,
    )
    binding = bind_context_artifact(
        artifact,
        repo,
        expected_repository="example/project",
        expected_commit=commit,
    )
    context = ServerContext.load(
        binding.manifest,
        views=["bm25"],
        artifact={"repository": "example/project"},
        artifact_binding=binding,
    )

    assert context.bm25 is not None
    results = context.bm25.search("value", return_code_content=True)
    assert "VALUE = 1" in (results[0].content or "")
    source = read_source_impl(context, "sample.py", 1, 1)
    assert source["content"] == "VALUE = 1\n"


def test_bound_portable_semantic_artifact_remains_native_inert_by_default(
    tmp_path: Path,
) -> None:
    repo, artifact, commit = _vector_artifact(tmp_path)
    assert not list(artifact.rglob("*.pkl"))
    binding = bind_context_artifact(
        artifact,
        repo,
        expected_repository="example/semantic-project",
        expected_commit=commit,
    )

    with patch.object(
        CodeVectorStore,
        "_initialize_embedding_model",
        return_value=_PortableEmbedding(),
    ) as initialize_embedding:
        context = ServerContext.load(
            binding.manifest,
            views=["vector"],
            artifact_binding=binding,
        )
        assert context.vector is None

    initialize_embedding.assert_not_called()
    assert "external authorization" in context.errors["vector"]


def test_verify_rejects_digest_mismatch(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    (artifact / "views" / "bm25" / "documents.json").write_text("[]\n")

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_context_artifact(artifact)


def test_verify_rejects_extra_file_and_symlink(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    (artifact / "extra.txt").write_text("not inventoried")
    with pytest.raises(ValueError, match="file set differs"):
        verify_context_artifact(artifact)

    (artifact / "extra.txt").unlink()
    (artifact / "link").symlink_to(artifact / "repo_manifest.json")
    with pytest.raises(ValueError, match="symbolic link"):
        verify_context_artifact(artifact)


def test_verify_rejects_pickle_even_when_inventoried(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    payload = b"not executable, but the format is forbidden"
    (artifact / "views" / "bm25" / "documents.pkl").write_bytes(payload)
    metadata = _metadata(artifact)
    metadata["files"].append(
        {
            "path": "views/bm25/documents.pkl",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    _write_metadata(artifact, metadata)

    with pytest.raises(ValueError, match="must not contain pickle"):
        verify_context_artifact(artifact)


def test_verify_rejects_inventory_path_escape(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    metadata = _metadata(artifact)
    metadata["files"][0]["path"] = "../outside"
    _write_metadata(artifact, metadata)

    with pytest.raises(ValueError, match="normalized relative POSIX path"):
        verify_context_artifact(artifact)


def test_verify_rejects_repository_and_commit_mismatch(tmp_path: Path) -> None:
    _repo, artifact, commit = _bm25_artifact(tmp_path)
    with pytest.raises(ValueError, match="repository mismatch"):
        verify_context_artifact(
            artifact,
            expected_repository="different/project",
        )
    with pytest.raises(ValueError, match="commit mismatch"):
        verify_context_artifact(
            artifact,
            expected_commit="f" * 40 if commit != "f" * 40 else "e" * 40,
        )


def test_v1_artifact_supports_inert_bm25_query_but_cannot_bind(
    tmp_path: Path,
) -> None:
    repo, artifact, _commit = _bm25_artifact(tmp_path)
    legacy = f"sha256:{'a' * 64}"
    _rewrite_artifact_source_fingerprint(artifact, legacy)

    query_binding = query_context_artifact(artifact)
    verified = query_binding.artifact
    context = ServerContext.load(
        query_binding.manifest,
        views=["bm25"],
        artifact_binding=query_binding,
    )
    try:
        assert verified.source_fingerprint == legacy
        assert context.source_verified is False
        assert context.bm25 is not None
        assert context.bm25.project_root is None
        assert context.bm25.search("value", return_code_content=False)
    finally:
        context.close()

    with pytest.raises(ValueError, match="query-only"):
        bind_context_artifact(artifact, repo)


def test_verify_rejects_unsafe_bm25_source_contract(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    metadata_path = artifact / "views" / "bm25" / "bm25_metadata.json"
    metadata_path.write_text('{"project_root": "/tmp"}\n', encoding="utf-8")
    metadata = _metadata(artifact)
    _refresh_inventory_file(
        artifact,
        metadata,
        "views/bm25/bm25_metadata.json",
    )
    _write_metadata(artifact, metadata)
    with pytest.raises(ValueError, match="project root"):
        verify_context_artifact(artifact)

    metadata_path.write_text('{"project_root": "source"}\n', encoding="utf-8")
    documents_path = artifact / "views" / "bm25" / "documents.json"
    documents = json.loads(documents_path.read_text())
    documents[0]["metadata"]["file"] = "../../outside.py"
    documents_path.write_text(json.dumps(documents), encoding="utf-8")
    metadata = _metadata(artifact)
    _refresh_inventory_file(
        artifact,
        metadata,
        "views/bm25/bm25_metadata.json",
    )
    _refresh_inventory_file(
        artifact,
        metadata,
        "views/bm25/documents.json",
    )
    _write_metadata(artifact, metadata)
    with pytest.raises(ValueError, match="repository-relative POSIX path"):
        verify_context_artifact(artifact)


def test_runtime_source_path_budget_has_exact_utf8_and_component_boundaries() -> None:
    exact_bytes = "a" * 4_096
    exact_components = "/".join("a" for _ in range(256))

    assert runtime_module._source_path(exact_bytes, label="source") == exact_bytes
    assert (
        runtime_module._source_path(exact_components, label="source")
        == exact_components
    )
    for invalid in (
        "a" * 4_097,
        "/".join("a" for _ in range(257)),
        "\ud800.py",
    ):
        with pytest.raises(ValueError, match="repository-relative POSIX path"):
            runtime_module._source_path(invalid, label="source")


def test_stage_rejects_source_symlink_outside_checkout(tmp_path: Path) -> None:
    with pytest.raises(
        (ValueError, RepositoryChangedError),
        match="stable contained source|resolves outside|could not be read consistently",
    ):
        _bm25_artifact(tmp_path, source_symlink=True)


def test_bind_rejects_checkout_content_drift_even_after_commit(tmp_path: Path) -> None:
    repo, artifact, _commit = _bm25_artifact(tmp_path)
    (repo / "second.py").write_text("SECOND = 2\n", encoding="utf-8")
    _git(repo, "add", "second.py")
    _git(repo, "commit", "--quiet", "-m", "second")

    with pytest.raises(ValueError, match="source files do not match"):
        bind_context_artifact(artifact, repo)


def test_bind_treats_same_content_new_head_as_unattested_display_provenance(
    tmp_path: Path,
) -> None:
    repo, artifact, indexed_commit = _bm25_artifact(tmp_path)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "same tree, new head")
    current_commit = _git(repo, "rev-parse", "HEAD")
    assert current_commit != indexed_commit

    binding = bind_context_artifact(artifact, repo)
    try:
        assert binding.source_bound
        assert binding.source_verification_scope == "content-bytes"
        assert binding.commit_verified is False
        assert binding.checkout_commit_observed == current_commit
        assert binding.manifest.commit == indexed_commit
    finally:
        binding.close()


def test_bind_never_attests_head_changed_after_diagnostic_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, artifact, indexed_commit = _bm25_artifact(tmp_path)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "same tree c2")
    second_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--quiet", "--hard", indexed_commit)
    real_checkout_commit = runtime_module.checkout_commit

    def observe_then_change_head(path: Path) -> str:
        observed = real_checkout_commit(path)
        _git(repo, "reset", "--quiet", "--hard", second_commit)
        return observed

    monkeypatch.setattr(
        runtime_module,
        "checkout_commit",
        observe_then_change_head,
    )
    binding = bind_context_artifact(artifact, repo)
    try:
        assert binding.source_bound
        assert binding.checkout_commit_observed == indexed_commit
        assert _git(repo, "rev-parse", "HEAD") == second_commit
        assert binding.commit_verified is False
        assert binding.source_verification_scope == "content-bytes"
    finally:
        binding.close()


def test_bind_rejects_checkout_source_drift(tmp_path: Path) -> None:
    repo, artifact, _commit = _bm25_artifact(tmp_path)
    (repo / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source files do not match"):
        bind_context_artifact(artifact, repo)


def test_extract_archive_verifies_and_removes_single_root_prefix(
    tmp_path: Path,
) -> None:
    _repo, artifact, commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive, prefix="download/")

    verified = extract_context_artifact_archive(
        archive,
        tmp_path / "extracted",
        expected_repository="example/project",
        expected_commit=commit,
    )

    assert verified.commit == commit
    assert verified.metadata_path == tmp_path / "extracted" / (
        CONTEXT_ARTIFACT_MANIFEST
    )


def test_extract_archive_rejects_final_symlink_and_fifo(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive)
    linked = tmp_path / "linked.zip"
    linked.symlink_to(archive)
    with pytest.raises(ValueError, match="bounded regular file"):
        extract_context_artifact_archive(linked, tmp_path / "linked-output")

    if not hasattr(os, "mkfifo"):
        return
    fifo = tmp_path / "archive.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="bounded regular file"):
        extract_context_artifact_archive(fifo, tmp_path / "fifo-output")


def test_archive_snapshot_allows_sibling_mutation_but_rejects_parent_rebind(
    tmp_path: Path,
) -> None:
    from codenib.artifacts import archive as archive_module

    anchor = tmp_path / "anchor"
    anchor.mkdir()
    archive = anchor / "artifact.zip"
    archive.write_bytes(b"snapshot")

    with archive_module._authenticated_archive_snapshot(
        archive,
        max_files=1,
        max_bytes=1024,
    ):
        (anchor / "legitimate-sibling").mkdir()

    held = tmp_path / "held-anchor"
    foreign = tmp_path / "foreign-anchor"
    foreign.mkdir()
    try:
        with pytest.raises(ValueError, match="archive parent changed"):
            with archive_module._authenticated_archive_snapshot(
                archive,
                max_files=1,
                max_bytes=1024,
            ):
                anchor.rename(held)
                foreign.rename(anchor)
    finally:
        if anchor.exists():
            anchor.rename(foreign)
        if held.exists():
            held.rename(anchor)


def test_archive_snapshot_applies_independent_compressed_input_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.artifacts import archive as archive_module

    archive = tmp_path / "oversized.zip"
    archive.write_bytes(b"x" * 1025)
    monkeypatch.setattr(
        archive_module,
        "_create_archive_snapshot",
        lambda: pytest.fail("oversized archive must fail before allocating a memfd"),
    )

    with pytest.raises(ValueError, match="bounded regular file"):
        with archive_module._authenticated_archive_snapshot(
            archive,
            max_files=100,
            max_bytes=64 * 1024 * 1024 * 1024,
            max_archive_bytes=1024,
        ):
            pytest.fail("oversized archive snapshot must not be yielded")


def test_posix_archive_snapshot_close_retry_does_not_close_reused_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.artifacts import archive as archive_module

    archive = tmp_path / "artifact.zip"
    archive.write_bytes(b"snapshot")
    replacement_path = tmp_path / "replacement"
    replacement_path.write_bytes(b"foreign")
    real_fdopen = os.fdopen
    snapshot_descriptor = -1
    interruption = KeyboardInterrupt("snapshot wrapper close interrupted")

    class InterruptingSnapshot:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped
            self.interrupt = True

        @property
        def closed(self) -> bool:
            return self.wrapped.closed

        def __getattr__(self, name: str):
            return getattr(self.wrapped, name)

        def close(self) -> None:
            if self.interrupt:
                self.interrupt = False
                raise interruption
            self.wrapped.close()

    wrapper: InterruptingSnapshot | None = None

    def interrupting_fdopen(descriptor: int, mode: str, *, closefd: bool):
        nonlocal snapshot_descriptor, wrapper
        assert closefd is False
        snapshot_descriptor = descriptor
        wrapper = InterruptingSnapshot(real_fdopen(descriptor, mode, closefd=closefd))
        return wrapper

    monkeypatch.setattr(archive_module.os, "fdopen", interrupting_fdopen)
    with pytest.raises(KeyboardInterrupt) as caught:
        with archive_module._authenticated_archive_snapshot(
            archive,
            max_files=1,
            max_bytes=1024,
        ) as (_display, snapshot):
            assert snapshot.read() == b"snapshot"

    assert caught.value is interruption
    assert wrapper is not None
    replacement = os.open(replacement_path, os.O_RDONLY)
    if replacement != snapshot_descriptor:
        os.dup2(replacement, snapshot_descriptor)
        os.close(replacement)
        replacement = snapshot_descriptor
    try:
        caught.value.archive_cleanup_owner.close()
        assert os.read(replacement, 7) == b"foreign"
    finally:
        os.close(replacement)


def test_archive_snapshot_routes_windows_through_handle_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.artifacts import archive as archive_module

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
    observed = {}

    @contextmanager
    def fake_windows_snapshot(lexical: Path, *, physical_limit: int):
        observed.update(lexical=lexical, physical_limit=physical_limit)
        with archive_module._ImmutableArchiveSnapshot((payload.getvalue(),)) as source:
            yield source

    monkeypatch.setattr(archive_module, "_archive_os_name", lambda: "nt")
    monkeypatch.setattr(
        archive_module,
        "_authenticated_windows_archive_snapshot",
        fake_windows_snapshot,
    )

    path = tmp_path / "artifact.zip"
    with archive_module._authenticated_archive_snapshot(
        path,
        max_files=1,
        max_bytes=1024,
        max_archive_bytes=2048,
    ) as (display, snapshot):
        with zipfile.ZipFile(snapshot) as archive:
            assert archive.namelist() == [CONTEXT_ARTIFACT_MANIFEST]
        with pytest.raises(io.UnsupportedOperation):
            snapshot.write(b"tamper")

    assert display == path
    assert observed == {"lexical": path, "physical_limit": 2048}


def test_windows_archive_snapshot_reads_pinned_handle_and_closes_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib import _windows_fs_authority as windows_fs
    from codenib.artifacts import archive as archive_module

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
    archive_bytes = payload.getvalue()
    file_id = b"f" * 16
    metadata = windows_fs.WindowsHandleMetadata(
        st_dev=7,
        st_ino=1,
        st_mode=stat.S_IFREG | 0o444,
        st_size=len(archive_bytes),
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_nlink=1,
        st_file_attributes=0,
        file_id_128=file_id,
    )
    entry = windows_fs.WindowsDirectoryEntry(
        name="artifact.zip",
        file_id=1,
        attributes=0,
        file_id_128=file_id,
    )

    class Api:
        def __init__(self) -> None:
            self.offset = 0
            self.open_handles = {200}

        def enumerate_directory(self, handle):
            assert handle == 100
            return (entry,)

        def open_relative(self, parent, name, **kwargs):
            assert (parent, name) == (100, "artifact.zip")
            assert kwargs["allow_reparse"] is False
            self.open_handles.add(200)
            return 200

        def metadata(self, handle):
            if handle not in self.open_handles:
                raise KeyError(handle)
            return metadata

        def read(self, handle, size):
            assert handle == 200
            block = archive_bytes[self.offset : self.offset + size]
            self.offset += len(block)
            return block

        def close(self, handle):
            self.open_handles.remove(handle)

    api = Api()
    authority = SimpleNamespace(
        api=api,
        handle=100,
        verify=MagicMock(),
        close=MagicMock(),
    )
    monkeypatch.setattr(
        archive_module._windows_fs,
        "open_lexical_directory_authority",
        lambda _parent, *, cleanup_slot: (cleanup_slot.own(authority) or authority),
    )

    with archive_module._authenticated_windows_archive_snapshot(
        tmp_path / "artifact.zip",
        physical_limit=len(archive_bytes),
    ) as snapshot:
        with zipfile.ZipFile(snapshot) as archive:
            assert archive.namelist() == [CONTEXT_ARTIFACT_MANIFEST]

    assert api.open_handles == set()
    assert authority.verify.call_count == 2
    authority.close.assert_called_once_with()


@pytest.mark.parametrize("interruption_stage", ["authority", "child"])
def test_windows_archive_snapshot_closes_interrupted_handle_handoffs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption_stage: str,
) -> None:
    from codenib import _windows_fs_authority as windows_fs
    from codenib.artifacts import archive as archive_module

    file_id = b"f" * 16
    metadata = windows_fs.WindowsHandleMetadata(
        st_dev=7,
        st_ino=1,
        st_mode=stat.S_IFREG | 0o444,
        st_size=1,
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_nlink=1,
        st_file_attributes=0,
        file_id_128=file_id,
    )
    entry = windows_fs.WindowsDirectoryEntry(
        name="artifact.zip",
        file_id=1,
        attributes=0,
        file_id_128=file_id,
    )

    class _Api:
        def __init__(self) -> None:
            self.open_handles: set[int] = set()

        def enumerate_directory(self, _handle):
            return (entry,)

        def open_relative(self, _parent, _name, **_kwargs):
            self.open_handles.add(200)
            return 200

        def metadata(self, handle):
            if handle not in self.open_handles:
                raise KeyError(handle)
            return metadata

        def close(self, handle):
            self.open_handles.remove(handle)

    api = _Api()
    authority = SimpleNamespace(
        api=api,
        handle=100,
        verify=MagicMock(),
        close=MagicMock(),
    )
    interruption = KeyboardInterrupt(f"injected {interruption_stage} handoff")

    def open_authority(_parent, *, cleanup_slot):
        cleanup_slot.own(authority)
        if interruption_stage == "authority":
            raise interruption
        return authority

    monkeypatch.setattr(
        archive_module._windows_fs,
        "open_lexical_directory_authority",
        open_authority,
    )
    if interruption_stage == "child":
        real_append = windows_fs._append_owned_windows_handle
        interrupted = False

        def interrupt_after_append(handles, handle, deferred=None):
            nonlocal interrupted
            result = real_append(handles, handle, deferred)
            if not interrupted:
                interrupted = True
                raise interruption
            return result

        monkeypatch.setattr(
            archive_module._windows_fs,
            "_append_owned_windows_handle",
            interrupt_after_append,
        )

    with pytest.raises(KeyboardInterrupt) as caught:
        with archive_module._authenticated_windows_archive_snapshot(
            tmp_path / "artifact.zip",
            physical_limit=1,
        ):
            pytest.fail("interrupted archive snapshot must not be yielded")

    assert caught.value is interruption
    assert api.open_handles == set()
    authority.close.assert_called_once_with()


def test_windows_archive_entry_uses_exact_unicode_name() -> None:
    from codenib import _windows_fs_authority as windows_fs
    from codenib.artifacts import archive as archive_module

    exact = windows_fs.WindowsDirectoryEntry(
        name="ss.zip",
        file_id=1,
        attributes=0,
        file_id_128=b"1" * 16,
    )
    near = windows_fs.WindowsDirectoryEntry(
        name="ß.zip",
        file_id=2,
        attributes=0,
        file_id_128=b"2" * 16,
    )
    authority = SimpleNamespace(
        handle=100,
        api=SimpleNamespace(enumerate_directory=lambda _handle: (near, exact)),
    )

    assert archive_module._windows_archive_entry(authority, "ss.zip") is exact
    with pytest.raises(ValueError, match="missing or ambiguous"):
        archive_module._windows_archive_entry(authority, "SS.zip")


def test_extract_archive_reads_one_pinned_generation_during_path_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive)
    foreign = tmp_path / "foreign.zip"
    foreign.write_bytes(b"not the authorized archive")
    saved = tmp_path / "saved.zip"
    from codenib.artifacts import archive as archive_module

    real_read = archive_module._read_archive
    swapped = False

    def read_during_aba(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            archive.rename(saved)
            # Some filesystems do not advance ctime for a rapid rename-away/
            # rename-back cycle. Force a distinct retained-file version so the
            # version-bound snapshot check remains deterministic there too.
            saved_metadata = saved.stat()
            os.utime(
                saved,
                ns=(
                    saved_metadata.st_atime_ns,
                    saved_metadata.st_mtime_ns + 1_000_000_000,
                ),
            )
            foreign.rename(archive)
            try:
                return real_read(descriptor, size)
            finally:
                archive.rename(foreign)
                saved.rename(archive)
        return real_read(descriptor, size)

    monkeypatch.setattr(archive_module, "_read_archive", read_during_aba)
    with pytest.raises(ValueError, match="changed while reading"):
        extract_context_artifact_archive(
            archive,
            tmp_path / "output",
        )

    assert swapped
    assert foreign.read_bytes() == b"not the authorized archive"
    assert not (tmp_path / "output").exists()


def test_extract_archive_native_parser_consumes_only_sealed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _repo, artifact, _commit = _bm25_artifact(fixture)
    archive_a = tmp_path / "artifact-a.zip"
    _zip_tree(artifact, archive_a)

    artifact_b = tmp_path / "artifact-b"
    shutil.copytree(artifact, artifact_b)
    documents = artifact_b / "views" / "bm25" / "documents.json"
    documents.write_text(
        documents.read_text(encoding="utf-8").replace("value 1", "value 9"),
        encoding="utf-8",
    )
    metadata = _metadata(artifact_b)
    _refresh_inventory_file(
        artifact_b,
        metadata,
        "views/bm25/documents.json",
    )
    _write_metadata(artifact_b, metadata)
    archive_b = tmp_path / "artifact-b.zip"
    _zip_tree(artifact_b, archive_b)
    replacement = archive_b.read_bytes()

    from codenib.artifacts import archive as archive_module

    real_zip_file = archive_module.zipfile.ZipFile
    attempted = False
    replaced = False

    def tamper_before_parse(snapshot, *args, **kwargs):
        nonlocal attempted, replaced
        attempted = True
        descriptor = snapshot.fileno()
        try:
            os.ftruncate(descriptor, 0)
            os.write(descriptor, replacement)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError:
            pass
        else:  # pragma: no cover - vulnerable writable snapshot implementation
            replaced = True
        return real_zip_file(snapshot, *args, **kwargs)

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", tamper_before_parse)
    output = tmp_path / "output"
    extract_context_artifact_archive(archive_a, output)

    assert attempted
    assert not replaced
    assert b"value 1" in (output / "views/bm25/documents.json").read_bytes()
    assert b"value 9" not in (output / "views/bm25/documents.json").read_bytes()


def test_extract_archive_returns_published_authority_not_refreshed_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_a = tmp_path / "a"
    fixture_b = tmp_path / "b"
    fixture_a.mkdir()
    fixture_b.mkdir()
    _repo, artifact, commit = _bm25_artifact(fixture_a)
    _other_repo, foreign_artifact, _other_commit = _bm25_artifact(fixture_b)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive)
    output = tmp_path / "output"
    saved = tmp_path / "published-a"
    from codenib.artifacts import archive as archive_module

    real_publish = archive_module.OwnedDirectoryStage.publish

    def publish_then_replace(self, **kwargs: object):
        result = real_publish(self, **kwargs)
        output.rename(saved)
        shutil.copytree(foreign_artifact, output)
        return result

    monkeypatch.setattr(
        archive_module.OwnedDirectoryStage,
        "publish",
        publish_then_replace,
    )
    verified = extract_context_artifact_archive(archive, output)

    assert verified.commit == commit
    with pytest.raises((RuntimeError, ValueError), match="captured ownership"):
        reopen_authenticated_directory(
            output,
            verified.ownership,
            lambda _reader: None,
        )


def test_extract_archive_restores_previous_output_on_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive)
    output = tmp_path / "extracted"
    extract_context_artifact_archive(archive, output)
    marker = output / "previous-output.marker"
    marker.write_text("preserve", encoding="utf-8")
    from codenib import _atomic_directory as atomic_directory

    real_rename = atomic_directory._rename_noreplace_at

    def fail_final_publish(source, target, source_dir_fd, target_dir_fd):
        if source.startswith(".extracted.normalize-") and target == output.name:
            raise OSError("injected archive publish failure")
        return real_rename(source, target, source_dir_fd, target_dir_fd)

    monkeypatch.setattr(
        "codenib._atomic_directory._rename_noreplace_at",
        fail_final_publish,
    )

    with pytest.raises(OSError, match="injected archive publish failure"):
        extract_context_artifact_archive(archive, output)

    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not list(output.parent.glob(".extracted.normalize-*"))


def test_extract_archive_preserves_output_changed_during_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive)
    output = tmp_path / "extracted"
    extract_context_artifact_archive(archive, output)
    previous_metadata = (output / CONTEXT_ARTIFACT_MANIFEST).read_bytes()

    from codenib.artifacts import archive as archive_module

    real_extract = archive_module._extract_member
    mutated = False

    def mutate_old_output(*args, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            nested = output / "late"
            nested.mkdir()
            (nested / "preserve.txt").write_text("preserve", encoding="utf-8")
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(archive_module, "_extract_member", mutate_old_output)

    with pytest.raises(RuntimeError, match="changed before directory publication"):
        extract_context_artifact_archive(archive, output)

    assert (output / CONTEXT_ARTIFACT_MANIFEST).read_bytes() == previous_metadata
    assert (output / "late" / "preserve.txt").read_text(encoding="utf-8") == (
        "preserve"
    )


def test_extract_archive_rejects_output_symlink(tmp_path: Path) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive)
    victim = tmp_path / "victim"
    extract_context_artifact_archive(archive, victim)
    previous = (victim / CONTEXT_ARTIFACT_MANIFEST).read_bytes()
    output = tmp_path / "output"
    output.symlink_to(victim, target_is_directory=True)

    with pytest.raises(ValueError, match="not a directory or is a link"):
        extract_context_artifact_archive(archive, output)

    assert output.is_symlink()
    assert (victim / CONTEXT_ARTIFACT_MANIFEST).read_bytes() == previous


def test_extract_archive_does_not_follow_swapped_stage_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "artifact.zip"
    _zip_tree(artifact, archive)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    stolen = tmp_path / "stolen-stage"

    from codenib.artifacts import archive as archive_module

    real_stage = archive_module.OwnedDirectoryStage
    swapped: Path | None = None

    class SwappedStage(real_stage):
        def __init__(self, destination, **kwargs):
            nonlocal swapped
            super().__init__(destination, **kwargs)
            if Path(destination).name == "output" and swapped is None:
                self.path.rename(stolen)
                self.path.symlink_to(victim, target_is_directory=True)
                swapped = self.path

    monkeypatch.setattr(archive_module, "OwnedDirectoryStage", SwappedStage)

    with pytest.raises(RuntimeError, match="owned stage root changed"):
        extract_context_artifact_archive(archive, tmp_path / "output")

    assert swapped is not None and swapped.is_symlink()
    assert stolen.is_dir()
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_extract_archive_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        archive.writestr("../outside", "bad")
    with pytest.raises(ValueError, match="path is unsafe"):
        extract_context_artifact_archive(traversal, tmp_path / "traversal-output")

    linked = tmp_path / "linked.zip"
    with zipfile.ZipFile(linked, "w") as archive:
        archive.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "repo_manifest.json")
    with pytest.raises(ValueError, match="symbolic link"):
        extract_context_artifact_archive(linked, tmp_path / "linked-output")


def test_extract_archive_enforces_expanded_size_before_writing(tmp_path: Path) -> None:
    archive = tmp_path / "large.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        bundle.writestr("large.bin", "x" * 1024)

    with pytest.raises(ValueError, match="expanded bytes"):
        extract_context_artifact_archive(
            archive,
            tmp_path / "large-output",
            max_bytes=128,
        )
    assert not (tmp_path / "large-output").exists()


def test_extract_archive_limits_directory_entries(tmp_path: Path) -> None:
    archive = tmp_path / "directories.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        for index in range(67):
            bundle.writestr(f"directory-{index}/", "")

    with pytest.raises(ValueError, match="archive exceeds .* entries"):
        extract_context_artifact_archive(
            archive,
            tmp_path / "directory-output",
            max_files=1,
        )


def test_extract_archive_checks_member_count_before_zipfile_allocates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.artifacts import archive as archive_module

    source = tmp_path / "oversized-envelope.zip"
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
    payload = bytearray(source.read_bytes())
    end = payload.rfind(b"PK\x05\x06")
    assert end >= 0
    excessive_count = 67  # max_files=1 permits at most 66 raw entries.
    payload[end + 8 : end + 10] = excessive_count.to_bytes(2, "little")
    payload[end + 10 : end + 12] = excessive_count.to_bytes(2, "little")
    source.write_bytes(payload)

    def should_not_parse(*_args, **_kwargs):
        raise AssertionError("ZipFile allocated entries before envelope validation")

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", should_not_parse)
    with pytest.raises(ValueError, match="exceeds 66 entries"):
        extract_context_artifact_archive(
            source,
            tmp_path / "output",
            max_files=1,
        )


def test_extract_archive_rejects_lying_member_count_before_zipfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.artifacts import archive as archive_module

    source = tmp_path / "lying-count.zip"
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
        bundle.writestr("one.txt", "one")
        bundle.writestr("two.txt", "two")
    payload = bytearray(source.read_bytes())
    end = payload.rfind(b"PK\x05\x06")
    assert end >= 0
    payload[end + 8 : end + 10] = (1).to_bytes(2, "little")
    payload[end + 10 : end + 12] = (1).to_bytes(2, "little")
    source.write_bytes(payload)

    def should_not_parse(*_args, **_kwargs):
        raise AssertionError("ZipFile parsed a lying central-directory count")

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", should_not_parse)
    with pytest.raises(ValueError, match="not a valid ZIP"):
        extract_context_artifact_archive(
            source,
            tmp_path / "output",
            max_files=1,
        )


def test_extract_archive_transfers_pending_owner_from_bad_zip_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.artifacts import archive as archive_module

    source = tmp_path / "malformed.zip"
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr(CONTEXT_ARTIFACT_MANIFEST, "{}")
    owner = object()
    malformed = zipfile.BadZipFile("injected malformed central directory")
    malformed.archive_cleanup_owner = owner

    def fail_parse(*_args, **_kwargs):
        raise malformed

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", fail_parse)
    with pytest.raises(ValueError, match="not a valid ZIP") as caught:
        extract_context_artifact_archive(source, tmp_path / "output")

    assert caught.value.archive_cleanup_owner is owner
    assert caught.value.__cause__ is malformed


def _github_record(
    *,
    artifact_id: int,
    commit: str,
    digest: str,
    expired: bool = False,
) -> dict:
    return {
        "id": artifact_id,
        "name": f"codenib-context-example-project-{commit[:12]}",
        "size_in_bytes": 1024,
        "archive_download_url": (
            "https://api.github.com/repos/example/project/actions/artifacts/"
            f"{artifact_id}/zip"
        ),
        "digest": digest,
        "expired": expired,
        "created_at": f"2026-08-0{artifact_id}T00:00:00Z",
        "workflow_run": {"head_sha": commit},
    }


def test_resolve_github_artifact_uses_exact_head_sha_and_newest_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, _artifact, commit = _bm25_artifact(tmp_path)
    digest = f"sha256:{'a' * 64}"
    observed: dict = {}

    def fake_json(url: str, *, token: str | None) -> dict:
        observed.update(url=url, token=token)
        return {
            "artifacts": [
                _github_record(artifact_id=1, commit="f" * 40, digest=digest),
                _github_record(artifact_id=2, commit=commit, digest=digest),
                _github_record(artifact_id=3, commit=commit, digest=digest),
                _github_record(
                    artifact_id=4,
                    commit=commit,
                    digest=digest,
                    expired=True,
                ),
            ]
        }

    monkeypatch.setattr(github_artifacts, "_github_json", fake_json)
    record = resolve_github_context_artifact(
        "Example/Project",
        commit,
        token="runtime-token",
    )

    assert record.artifact_id == 3
    assert record.head_sha == commit
    assert observed["token"] == "runtime-token"
    assert "name=codenib-context-example-project-" in observed["url"]


def test_fetch_github_artifact_verifies_archive_digest_and_reuses_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "source.zip"
    _zip_tree(artifact, archive)
    archive_bytes = archive.read_bytes()
    archive_digest = hashlib.sha256(archive_bytes).hexdigest()
    record = _github_record(
        artifact_id=7,
        commit=commit,
        digest=f"sha256:{archive_digest}",
    )
    record["size_in_bytes"] = len(archive_bytes)
    calls = {"list": 0, "download": 0}

    def fake_json(_url: str, *, token: str | None) -> dict:
        assert token == "runtime-token"
        calls["list"] += 1
        return {"artifacts": [record]}

    def fake_download(
        _url: str,
        output: Path,
        *,
        token: str,
        max_bytes: int,
    ) -> tuple[int, str]:
        assert token == "runtime-token"
        assert len(archive_bytes) < max_bytes
        calls["download"] += 1
        shutil.copyfile(archive, output)
        return len(archive_bytes), archive_digest

    monkeypatch.setattr(github_artifacts, "_github_json", fake_json)
    monkeypatch.setattr(github_artifacts, "_download_archive", fake_download)
    output = tmp_path / "cache" / commit
    first = fetch_github_context_artifact(
        "example/project",
        commit,
        output_dir=output,
        token="runtime-token",
    )
    second = fetch_github_context_artifact(
        "example/project",
        commit,
        output_dir=output,
    )

    assert first.downloaded is True
    assert first.record is not None and first.record.artifact_id == 7
    assert second.downloaded is False
    assert second.record is None
    assert calls == {"list": 1, "download": 1}
    with pytest.raises(
        ValueError,
        match="inventory exceeds 2 files|contains more than 2 inventoried files",
    ):
        fetch_github_context_artifact(
            "example/project",
            commit,
            output_dir=output,
            max_files=2,
        )


def test_fetch_github_artifact_rejects_archive_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, commit = _bm25_artifact(tmp_path)
    archive = tmp_path / "source.zip"
    _zip_tree(artifact, archive)
    record = _github_record(
        artifact_id=8,
        commit=commit,
        digest=f"sha256:{'0' * 64}",
    )

    monkeypatch.setattr(
        github_artifacts,
        "_github_json",
        lambda _url, *, token: {"artifacts": [record]},
    )

    def fake_download(
        _url: str,
        output: Path,
        *,
        token: str,
        max_bytes: int,
    ) -> tuple[int, str]:
        shutil.copyfile(archive, output)
        return output.stat().st_size, "f" * 64

    monkeypatch.setattr(github_artifacts, "_download_archive", fake_download)
    output = tmp_path / "cache" / commit
    with pytest.raises(ValueError, match="archive digest"):
        fetch_github_context_artifact(
            "example/project",
            commit,
            output_dir=output,
            token="runtime-token",
        )
    assert not output.exists()


def test_github_download_does_not_forward_token_to_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"zip-payload"
    requests: list[urllib.request.Request] = []

    class RedirectingOpener:
        def open(self, request: urllib.request.Request, timeout: int):
            requests.append(request)
            headers = {"Location": "https://objects.example/artifact.zip"}
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                headers,
                None,
            )

    class ObjectResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        requests.append(request)
        return ObjectResponse(payload)

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: RedirectingOpener(),
    )
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    output = tmp_path / "artifact.zip"
    size, digest = github_artifacts._download_archive(
        "https://api.github.com/repos/example/project/actions/artifacts/1/zip",
        output,
        token="never-forward-this-token",
        max_bytes=1024,
    )

    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert requests[0].get_header("Authorization") == (
        "Bearer never-forward-this-token"
    )
    assert requests[1].get_header("Authorization") is None


def test_github_api_refuses_redirect_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []

    class RedirectingOpener:
        def open(self, request: urllib.request.Request, timeout: int):
            requests.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://attacker.example/artifacts"},
                None,
            )

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: RedirectingOpener(),
    )

    with pytest.raises(RuntimeError, match="HTTP 302"):
        github_artifacts._github_json(
            "https://api.github.com/repos/example/project/actions/artifacts",
            token="never-forward-this-token",
        )

    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == (
        "Bearer never-forward-this-token"
    )


def test_artifact_cli_verifies_binding_and_renders_configs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)

    assert (
        run(
            [
                "artifact",
                "verify",
                str(artifact),
                "--repo",
                str(repo),
                "--repository",
                "example/project",
                "--commit",
                commit,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Repository:       example/project" in output
    assert f"Commit:           {commit}" in output
    assert f"Checkout:         {repo}" in output

    assert (
        run(
            [
                "artifact",
                "mcp-config",
                str(artifact),
                "--repo",
                str(repo),
                "--repository",
                "example/project",
                "--host",
                "json",
            ]
        )
        == 0
    )
    config = json.loads(capsys.readouterr().out)
    args = config["mcpServers"]["codenib"]["args"]
    assert args[:2] == ["mcp", "--artifact"]
    assert args[-2:] == ["--repository", "example/project"]


def test_render_artifact_mcp_host_commands_are_shell_safe(tmp_path: Path) -> None:
    repo, artifact, _commit = _bm25_artifact(tmp_path)
    codex = shlex.split(
        render_artifact_mcp_config(
            artifact,
            repo,
            host="codex",
            server_name="repository-context",
        )
    )
    claude = shlex.split(
        render_artifact_mcp_config(
            artifact,
            repo,
            host="claude",
            server_name="repository-context",
        )
    )

    assert codex[:6] == [
        "codex",
        "mcp",
        "add",
        "repository-context",
        "--",
        "codenib",
    ]
    assert claude[:7] == [
        "claude",
        "mcp",
        "add-json",
        "--scope",
        "project",
        "repository-context",
        json.dumps(
            {
                "type": "stdio",
                "command": "codenib",
                "args": codex[6:],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    ]


def test_render_artifact_mcp_config_closes_source_authority(tmp_path: Path) -> None:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        pytest.skip("descriptor accounting needs /proc/self/fd")
    repo, artifact, _commit = _bm25_artifact(tmp_path)
    before = len(tuple(descriptor_root.iterdir()))

    for _ in range(20):
        render_artifact_mcp_config(artifact, repo, host="json")

    assert len(tuple(descriptor_root.iterdir())) <= before + 1


def test_mcp_server_starts_from_verified_artifact(tmp_path: Path) -> None:
    repo, artifact, commit = _bm25_artifact(tmp_path)

    with patch.object(server_module.mcp, "run") as run_server:
        server_module.main(
            [
                "--artifact",
                str(artifact),
                "--repo",
                str(repo),
                "--repository",
                "example/project",
                "--log-level",
                "ERROR",
            ]
        )

    run_server.assert_called_once_with(transport="stdio")
    context = server_module.get_context()
    assert context.bm25 is not None
    manifest = asyncio.run(server_module.get_manifest())
    assert manifest["repo"]["commit"] == commit
    assert manifest["artifact"] == {
        "verified": True,
        "schema": "codenib.context-artifact.v1",
        "repository": "example/project",
        "commit": commit,
        "views": ["bm25"],
    }
    assert manifest["runtime"]["loaded_views"] == ["bm25"]


def test_mcp_server_query_only_v1_artifact_loads_bm25_without_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo, artifact, _commit = _bm25_artifact(tmp_path)
    _rewrite_artifact_source_fingerprint(artifact, f"sha256:{'a' * 64}")

    with patch.object(server_module.mcp, "run") as run_server:
        server_module.main(
            [
                "--artifact",
                str(artifact),
                "--repository",
                "example/project",
                "--log-level",
                "ERROR",
            ]
        )

    run_server.assert_called_once_with(transport="stdio")
    context = server_module.get_context()
    assert context.source_verified is False
    assert context.bm25 is not None
    assert context.bm25.source_mode == bm25_module.SOURCE_MODE_PERSISTED_ONLY
    cwd_source = tmp_path / "sample.py"
    cwd_source.write_text("API_KEY = 'QUERY_ONLY_CWD_SECRET'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        bm25_module,
        "_read_source_text",
        lambda *_args: pytest.fail("query-only artifact must not read source paths"),
    )

    relative = context.bm25.search("value", return_code_content=True)
    assert relative
    assert all(result.content is None for result in relative)
    assert "QUERY_ONLY_CWD_SECRET" not in repr(relative)
    assert context.bm25.search_identifier_occurrences("value")

    context.bm25.documents[0].metadata["file"] = str(cwd_source)
    absolute = context.bm25.search("value", return_code_content=True)
    assert absolute
    assert all(result.content is None for result in absolute)
    assert "QUERY_ONLY_CWD_SECRET" not in repr(absolute)
    assert context.bm25.search_identifier_occurrences("value")
    with pytest.raises(RuntimeError, match="source binding has not been verified"):
        read_source_impl(context, "sample.py", 1, 1)


def test_mcp_server_query_only_v1_vector_artifact_never_parses_native_bytes(
    tmp_path: Path,
) -> None:
    _repo, artifact, _commit = _vector_artifact(tmp_path)
    _rewrite_artifact_source_fingerprint(artifact, f"sha256:{'a' * 64}")

    with (
        patch.object(server_module.mcp, "run") as run_server,
        patch(
            "codenib.index.embedding.artifact_integrity."
            "capture_authenticated_vector_view"
        ) as capture_vector,
        patch(
            "codenib.native_index_authorization."
            "_mint_trusted_local_admin_authorization"
        ) as mint_authorization,
        patch.object(
            CodeVectorStore,
            "_initialize_embedding_model",
        ) as initialize_embedding,
    ):
        server_module.main(
            [
                "--artifact",
                str(artifact),
                "--repository",
                "example/semantic-project",
                "--log-level",
                "ERROR",
            ]
        )

    run_server.assert_called_once_with(transport="stdio")
    capture_vector.assert_not_called()
    mint_authorization.assert_not_called()
    initialize_embedding.assert_not_called()
    context = server_module.get_context()
    assert context.source_verified is False
    assert context.vector is None
    assert "external authorization" in context.errors["vector"]
