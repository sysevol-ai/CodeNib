# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from scripts.experimental.hybrid_index import cas as cas_module
from scripts.experimental.hybrid_index.cas import LocalCAS
from scripts.experimental.hybrid_index.contracts import (
    StorageIntegrityError,
    StorageValidationError,
)


def _source(tmp_path: Path, payload: bytes = b"immutable archive") -> Path:
    source = tmp_path / "source.zip"
    source.write_bytes(payload)
    return source


def test_put_file_uses_sha256_key_and_deduplicates(tmp_path: Path) -> None:
    source = _source(tmp_path)
    store = LocalCAS(tmp_path / "objects")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()

    first = store.put_file(source)
    stored = store._object_path(first.digest)
    inode = stored.stat().st_ino
    second = store.put_file(source)

    assert first == second
    assert first.digest == expected
    assert stored.stat().st_ino == inode
    assert stored.read_bytes() == source.read_bytes()
    assert store.verified_path(first.digest, expected_size=first.byte_size) == stored


def test_concurrent_put_of_same_file_publishes_one_object(tmp_path: Path) -> None:
    source = _source(tmp_path, b"concurrent archive" * 100)
    store = LocalCAS(tmp_path / "objects")
    start = threading.Barrier(8)
    results = []
    errors: list[BaseException] = []

    def put() -> None:
        try:
            start.wait(timeout=10)
            results.append(store.put_file(source))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=put) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(set(results)) == 1
    assert len(tuple((store.root / "sha256").glob("*/*"))) == 1


def test_corrupt_existing_object_is_never_replaced(tmp_path: Path) -> None:
    source = _source(tmp_path)
    store = LocalCAS(tmp_path / "objects")
    info = store.put_file(source)
    stored = store._object_path(info.digest)
    inode = stored.stat().st_ino
    stored.write_bytes(b"corrupt")

    with pytest.raises(StorageIntegrityError, match="digest mismatch"):
        store.verify(info.digest)
    with pytest.raises(StorageIntegrityError, match="digest mismatch"):
        store.put_file(source)

    assert stored.stat().st_ino == inode
    assert stored.read_bytes() == b"corrupt"


def test_put_failure_removes_temporary_and_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    store = LocalCAS(tmp_path / "objects")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    def fail_link(_source: Path, _destination: Path) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(cas_module.os, "link", fail_link)

    with pytest.raises(OSError, match="injected publication failure"):
        store.put_file(source)

    assert not store._object_path(digest).exists()
    assert tuple((store.root / "tmp").iterdir()) == ()


def test_constructor_and_put_reject_symlinks(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    actual_source = _source(tmp_path)
    linked_source = tmp_path / "linked.zip"
    linked_source.symlink_to(actual_source)

    with pytest.raises(StorageValidationError, match="real directory"):
        LocalCAS(linked_root)

    store = LocalCAS(tmp_path / "objects")
    with pytest.raises(StorageValidationError, match="regular file"):
        store.put_file(linked_source)


@pytest.mark.parametrize("digest", ["", "A" * 64, "0" * 63, "g" * 64])
def test_verify_rejects_noncanonical_digest(tmp_path: Path, digest: str) -> None:
    store = LocalCAS(tmp_path / "objects")

    with pytest.raises(StorageValidationError, match="64 lowercase"):
        store.verify(digest)


def test_new_object_fsyncs_its_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    store = LocalCAS(tmp_path / "objects")
    fsynced: list[Path] = []
    monkeypatch.setattr(cas_module, "_fsync_directory", fsynced.append)

    info = store.put_file(source)

    assert store._object_path(info.digest).parent in fsynced
