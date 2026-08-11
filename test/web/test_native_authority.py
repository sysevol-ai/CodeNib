# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Web runtime's trusted local native-index boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codenib.compiler import cache_lock as cache_lock_module
from codenib.compiler.cache_lock import (
    COMPILER_CACHE_LOCK_FILENAME,
    compiler_cache_lock,
)
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.index.embedding.artifact_integrity import capture_authenticated_vector_view
from codenib.native_index_authorization import (
    InvalidNativeIndexAuthorizationError,
    require_native_index_authorization,
)
from codenib.source_fingerprint import (
    RepositoryChangedError,
    RepositorySourceBinding,
    fingerprint_repository,
)
from codenib.web import native_authority as native_authority_module
from codenib.web.config import RepoEntry
from codenib.web.native_authority import authorize_local_manifest_vector


def _local_vector_manifest(tmp_path):
    source_file = tmp_path / "sample.py"
    source_file.write_text("def sample():\n    return 1\n")
    cache = tmp_path / ".codenib_cache"
    vector = cache / "vector"
    vector.mkdir(parents=True)
    (vector / "config.json").write_text(json.dumps({"dimension": 3}))
    source = fingerprint_repository(tmp_path, exclude_roots=(cache,)).value
    config = {
        "embedding_model": "vendor/model",
        "embedding_provider": "huggingface",
        "embedding_dimension": 3,
        "index_metric": "ip",
    }
    vector_entry = IndexEntry(
        index_type="vector",
        path=str(vector),
        built_at="2026-08-11T00:00:00Z",
        built_at_epoch=0.0,
        status="fresh",
        config=config,
        commit="abc123",
        source_fingerprint=source,
    )
    manifest = RepoManifest(
        repo_path=str(tmp_path),
        commit="abc123",
        source_fingerprint=source,
        file_count=1,
        languages=["python"],
        indexes={"vector": vector_entry},
    )
    manifest_path = cache / "repo_manifest.json"
    manifest.save(manifest_path)
    repo_entry = RepoEntry(
        instance_id="sample",
        repo="owner/sample",
        base_commit="abc123",
        language="python",
        repo_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )
    return repo_entry, manifest, vector_entry, source_file


def test_local_resolver_binds_exact_tree_and_unmodified_manifest_config(tmp_path):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)

    authorization = authorize_local_manifest_vector(
        repo_entry,
        manifest,
        vector_entry,
    )

    with capture_authenticated_vector_view(vector_entry.path) as view:
        require_native_index_authorization(
            authorization,
            view.ownership,
            view_type="vector",
            semantic_contract=vector_entry.config,
        )
        with pytest.raises(
            InvalidNativeIndexAuthorizationError,
            match="does not match captured bytes",
        ):
            require_native_index_authorization(
                authorization,
                view.ownership,
                view_type="vector",
                semantic_contract={**vector_entry.config, "embedding_dimension": 4},
            )


def test_local_resolver_rejects_source_drift_before_vector_capture(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, source_file = _local_vector_manifest(tmp_path)
    source_file.write_text("def changed():\n    return 2\n")
    monkeypatch.setattr(
        "codenib.web.native_authority.capture_authenticated_vector_view",
        lambda _path: pytest.fail("source drift must fail before vector capture"),
    )

    with pytest.raises(ValueError, match="source content does not match"):
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)


def test_local_resolver_rejects_manifest_drift_before_source_capture(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)
    observed = RepoManifest.load(repo_entry.manifest_path)
    observed.indexes["vector"].config["embedding_dimension"] = 4
    observed.save(repo_entry.manifest_path)
    monkeypatch.setattr(
        "codenib.web.native_authority.capture_repository_source",
        lambda *_args, **_kwargs: pytest.fail(
            "manifest drift must fail before source capture"
        ),
    )

    with pytest.raises(ValueError, match="manifest changed"):
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)


def test_local_resolver_rejects_symlinked_compiler_lock_before_source_capture(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)
    cache = Path(repo_entry.manifest_path).parent
    victim = cache / "lock-victim"
    victim.write_bytes(b"victim must remain unchanged")
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    try:
        lock_path.symlink_to(victim)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    monkeypatch.setattr(
        native_authority_module,
        "capture_repository_source",
        lambda *_args, **_kwargs: pytest.fail(
            "an unsafe compiler lock must fail before source capture"
        ),
    )

    with pytest.raises(RuntimeError, match="compiler cache lock"):
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)

    assert victim.read_bytes() == b"victim must remain unchanged"
    assert lock_path.is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="exercises POSIX lock identity")
def test_local_resolver_rejects_compiler_lock_entry_swap_before_source_capture(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)
    cache = Path(repo_entry.manifest_path).parent
    lock_path = cache / COMPILER_CACHE_LOCK_FILENAME
    lock_path.write_bytes(b"original")
    replacement = cache / "replacement-lock"
    replacement.write_bytes(b"replacement")
    stolen = cache / "stolen-lock"
    real_validate = cache_lock_module._validate_posix_lock_entry
    calls = 0

    def swap_after_first_validation(*args, **kwargs):
        nonlocal calls
        calls += 1
        identity = real_validate(*args, **kwargs)
        if calls == 1:
            os.replace(lock_path, stolen)
            os.replace(replacement, lock_path)
        return identity

    monkeypatch.setattr(
        cache_lock_module,
        "_validate_posix_lock_entry",
        swap_after_first_validation,
    )
    monkeypatch.setattr(
        native_authority_module,
        "capture_repository_source",
        lambda *_args, **_kwargs: pytest.fail(
            "a swapped compiler lock must fail before source capture"
        ),
    )

    with pytest.raises(RuntimeError, match="compiler cache lock changed"):
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)

    assert calls == 2
    assert stolen.read_bytes() == b"original"
    assert lock_path.read_bytes() == b"replacement"


def test_local_resolver_rejects_source_drift_after_authorization(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, source_file = _local_vector_manifest(tmp_path)
    real_mint = native_authority_module._mint_trusted_local_admin_authorization

    def mint_then_change_source(*args, **kwargs):
        authorization = real_mint(*args, **kwargs)
        source_file.write_text("def changed_during_authorization():\n    return 2\n")
        return authorization

    monkeypatch.setattr(
        native_authority_module,
        "_mint_trusted_local_admin_authorization",
        mint_then_change_source,
    )

    with pytest.raises(RepositoryChangedError, match="source inventory changed"):
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)


def test_local_resolver_rejects_vector_tree_drift_after_authorization(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)
    vector_config = Path(vector_entry.path) / "config.json"
    real_mint = native_authority_module._mint_trusted_local_admin_authorization

    def mint_then_change_tree(*args, **kwargs):
        authorization = real_mint(*args, **kwargs)
        vector_config.write_text(json.dumps({"dimension": 4}))
        return authorization

    monkeypatch.setattr(
        native_authority_module,
        "_mint_trusted_local_admin_authorization",
        mint_then_change_tree,
    )

    with pytest.raises(ValueError, match="vector view changed"):
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)


def test_local_resolver_propagates_source_cleanup_failure_with_retry_owner(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)
    cleanup_failure = OSError("source cleanup failed")
    captured = []
    real_capture = native_authority_module.capture_repository_source
    real_close = RepositorySourceBinding.close

    def capture(*args, **kwargs):
        binding = real_capture(*args, **kwargs)
        captured.append(binding)
        return binding

    def fail_cleanup(binding):
        if binding in captured:
            raise cleanup_failure
        real_close(binding)

    monkeypatch.setattr(native_authority_module, "capture_repository_source", capture)
    monkeypatch.setattr(RepositorySourceBinding, "close", fail_cleanup)

    with pytest.raises(OSError, match="source cleanup failed") as caught:
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)

    assert caught.value is cleanup_failure
    retry_owner = caught.value.source_cleanup_owner
    assert retry_owner.pending_sources == tuple(captured)
    monkeypatch.setattr(RepositorySourceBinding, "close", real_close)
    retry_owner.close()
    assert retry_owner.closed


def test_local_resolver_rejects_incomplete_source_cleanup(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)
    captured = []
    real_capture = native_authority_module.capture_repository_source
    real_close = RepositorySourceBinding.close

    def capture(*args, **kwargs):
        binding = real_capture(*args, **kwargs)
        captured.append(binding)
        return binding

    def leave_open(binding):
        if binding not in captured:
            real_close(binding)

    monkeypatch.setattr(native_authority_module, "capture_repository_source", capture)
    monkeypatch.setattr(RepositorySourceBinding, "close", leave_open)

    with pytest.raises(RuntimeError, match="source cleanup did not finish") as caught:
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)

    retry_owner = caught.value.source_cleanup_owner
    assert retry_owner.pending_sources == tuple(captured)
    monkeypatch.setattr(RepositorySourceBinding, "close", real_close)
    retry_owner.close()
    assert retry_owner.closed


def test_local_resolver_preserves_first_base_exception_when_cleanup_also_fails(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)
    cache = Path(repo_entry.manifest_path).parent
    primary = KeyboardInterrupt("authorization interrupted")
    cleanup_failure = OSError("source cleanup also failed")
    captured = []
    real_capture = native_authority_module.capture_repository_source
    real_close = RepositorySourceBinding.close

    def capture(*args, **kwargs):
        binding = real_capture(*args, **kwargs)
        captured.append(binding)
        return binding

    def interrupt_vector_capture(_path):
        raise primary

    def fail_cleanup(binding):
        if binding in captured:
            raise cleanup_failure
        real_close(binding)

    monkeypatch.setattr(native_authority_module, "capture_repository_source", capture)
    monkeypatch.setattr(
        native_authority_module,
        "capture_authenticated_vector_view",
        interrupt_vector_capture,
    )
    monkeypatch.setattr(RepositorySourceBinding, "close", fail_cleanup)

    with pytest.raises(KeyboardInterrupt, match="authorization interrupted") as caught:
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)

    assert caught.value is primary
    assert caught.value.__cause__ is cleanup_failure
    retry_owner = caught.value.source_cleanup_owner
    assert retry_owner.pending_sources == tuple(captured)
    monkeypatch.setattr(RepositorySourceBinding, "close", real_close)
    retry_owner.close()
    assert retry_owner.closed
    with compiler_cache_lock(cache):
        pass


@pytest.mark.skipif(os.name != "posix", reason="exercises the reported POSIX probe")
def test_local_resolver_preserves_body_interrupt_when_lock_close_fails_afterward(
    tmp_path,
    monkeypatch,
):
    repo_entry, manifest, vector_entry, _source_file = _local_vector_manifest(tmp_path)
    cache = Path(repo_entry.manifest_path).parent
    primary = KeyboardInterrupt("resolver body interrupted")
    cleanup = SystemExit("lock cleanup interrupted after close")
    real_close = cache_lock_module._close_posix_lock_file

    def interrupt_source_capture(*_args, **_kwargs):
        raise primary

    def close_then_interrupt(descriptor_owner, *, locked):
        real_close(descriptor_owner, locked=locked)
        raise cleanup

    monkeypatch.setattr(
        native_authority_module,
        "capture_repository_source",
        interrupt_source_capture,
    )
    monkeypatch.setattr(
        cache_lock_module,
        "_close_posix_lock_file",
        close_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        authorize_local_manifest_vector(repo_entry, manifest, vector_entry)

    assert caught.value is primary
    notes = tuple(getattr(primary, "__notes__", ())) + tuple(
        getattr(primary, "_codenib_cleanup_notes", ())
    )
    diagnostic = "\n".join(notes)
    assert "compiler cache lock cleanup also failed" in diagnostic
    assert repr(cleanup) in diagnostic
    assert cache_lock_module._ACTIVE_POSIX_LOCK_DESCRIPTORS == set()

    monkeypatch.setattr(
        cache_lock_module,
        "_close_posix_lock_file",
        real_close,
    )
    with compiler_cache_lock(cache):
        pass
