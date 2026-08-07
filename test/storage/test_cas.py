# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import hashlib
import os
import threading
from pathlib import Path

import pytest

import codenib.storage.cas as cas_module
from codenib.storage.cas import LocalCAS
from codenib.storage.models import StorageIntegrityError, StorageValidationError


def test_put_bytes_uses_canonical_key_and_deduplicates(tmp_path: Path) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"immutable artifact"
    digest = hashlib.sha256(payload).hexdigest()

    first = store.put_bytes(payload)
    stored = store.root / first.storage_key
    first_metadata = stored.stat()
    second = store.put_bytes(payload)

    assert first == second
    assert first.digest == digest
    assert first.byte_size == len(payload)
    assert first.storage_key == f"sha256/{digest[:2]}/{digest[2:]}"
    assert stored.stat().st_ino == first_metadata.st_ino
    assert store.has(digest)
    assert store.verify(digest) == first
    assert store.read_bytes(digest) == payload
    with store.open(digest) as handle:
        assert handle.read() == payload


def test_put_file_stores_regular_file(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"file artifact" * 100)
    store = LocalCAS(tmp_path / "objects")

    info = store.put_file(source)

    assert info.byte_size == source.stat().st_size
    assert store.read_bytes(info.digest) == source.read_bytes()


def test_existing_corrupt_object_is_rejected_instead_of_replaced(
    tmp_path: Path,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"correct"
    info = store.put_bytes(payload)
    stored = store.root / info.storage_key
    original_inode = stored.stat().st_ino
    stored.write_bytes(b"corrupt")

    with pytest.raises(StorageIntegrityError, match="digest mismatch"):
        store.verify(info.digest)
    with pytest.raises(StorageIntegrityError, match="digest mismatch"):
        store.read_bytes(info.digest)
    with pytest.raises(StorageIntegrityError, match="failed integrity"):
        store.put_bytes(payload)

    assert stored.stat().st_ino == original_inode
    assert stored.read_bytes() == b"corrupt"


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "0" * 63,
        "0" * 65,
        "A" * 64,
        "g" * 64,
        "sha256:" + "0" * 64,
        "../" + "0" * 61,
        "0" * 64 + "\n",
    ],
)
def test_digest_syntax_is_strict(tmp_path: Path, digest: str) -> None:
    store = LocalCAS(tmp_path / "objects")

    with pytest.raises(StorageValidationError, match="64 lowercase hexadecimal"):
        store.has(digest)
    with pytest.raises(StorageValidationError, match="64 lowercase hexadecimal"):
        store.verify(digest)
    with pytest.raises(StorageValidationError, match="64 lowercase hexadecimal"):
        store.materialize(digest, tmp_path / "out")


def test_existing_special_object_is_rejected(tmp_path: Path) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"special"
    digest = hashlib.sha256(payload).hexdigest()
    parent = store.root / "sha256" / digest[:2]
    parent.mkdir()
    stored = parent / digest[2:]
    stored.symlink_to(tmp_path / "missing")

    with pytest.raises(StorageIntegrityError, match="not a regular file"):
        store.has(digest)
    with pytest.raises(StorageIntegrityError, match="not a regular file"):
        store.put_bytes(payload)


def test_failed_put_cleans_temporary_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"not published"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]

    def fail_link(*_args, **_kwargs):
        raise OSError("injected publish failure")

    monkeypatch.setattr("codenib.storage.cas._link_at", fail_link)

    with pytest.raises(OSError, match="injected publish failure"):
        store.put_bytes(payload)

    assert not destination.exists()
    assert list(destination.parent.glob("*.tmp")) == []


def test_materialize_atomically_replaces_regular_file(tmp_path: Path) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"published view"
    info = store.put_bytes(payload)
    destination = tmp_path / "runtime" / "view.bin"
    destination.parent.mkdir()
    destination.write_bytes(b"previous view")

    result = store.materialize(info.digest, destination)

    assert result == destination
    assert destination.read_bytes() == payload
    assert list(destination.parent.glob("*.tmp")) == []


def test_materialize_failure_preserves_previous_file_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    info = store.put_bytes(b"new view")
    destination = tmp_path / "view.bin"
    destination.write_bytes(b"old view")

    def fail_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]):
        raise OSError("injected materialization failure")

    monkeypatch.setattr("codenib.storage.cas.os.replace", fail_replace)

    with pytest.raises(OSError, match="injected materialization failure"):
        store.materialize(info.digest, destination)

    assert destination.read_bytes() == b"old view"
    assert list(tmp_path.glob("*.tmp")) == []


def test_materialize_rejects_symlink_and_special_target(tmp_path: Path) -> None:
    store = LocalCAS(tmp_path / "objects")
    info = store.put_bytes(b"view")
    real_target = tmp_path / "real.bin"
    real_target.write_bytes(b"keep")
    symlink = tmp_path / "link.bin"
    symlink.symlink_to(real_target)

    with pytest.raises(StorageValidationError, match="not a regular file"):
        store.materialize(info.digest, symlink)
    with pytest.raises(StorageValidationError, match="not a regular file"):
        store.materialize(info.digest, tmp_path)

    assert real_target.read_bytes() == b"keep"


def test_put_file_rejects_source_changed_between_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    source = tmp_path / "source.bin"
    source.write_bytes(b"first version")
    real_create_temporary = cas_module._create_temporary_file

    def mutate_then_create(*args, **kwargs):
        source.write_bytes(b"second version with a different size")
        return real_create_temporary(*args, **kwargs)

    monkeypatch.setattr(
        "codenib.storage.cas._create_temporary_file",
        mutate_then_create,
    )

    with pytest.raises(OSError, match="source changed"):
        store.put_file(source)

    assert list((store.root / "sha256").rglob("*.tmp")) == []


@pytest.mark.parametrize("swapped_component", ["sha256", "shard"])
def test_reads_reject_symlinked_internal_directory(
    tmp_path: Path,
    swapped_component: str,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"valid bytes outside the CAS root"
    digest = hashlib.sha256(payload).hexdigest()
    outside = tmp_path / "outside"
    outside.mkdir()

    if swapped_component == "sha256":
        external_shard = outside / digest[:2]
        external_shard.mkdir()
        (external_shard / digest[2:]).write_bytes(payload)
        (store.root / "sha256").rmdir()
        (store.root / "sha256").symlink_to(outside, target_is_directory=True)
    else:
        (outside / digest[2:]).write_bytes(payload)
        (store.root / "sha256" / digest[:2]).symlink_to(
            outside,
            target_is_directory=True,
        )

    with pytest.raises(StorageIntegrityError, match="not a real directory"):
        store.has(digest)
    with pytest.raises(StorageIntegrityError, match="not a real directory"):
        store.open(digest)
    with pytest.raises(StorageIntegrityError, match="not a real directory"):
        store.verify(digest)
    destination = tmp_path / "materialized.bin"
    with pytest.raises(StorageIntegrityError, match="not a real directory"):
        store.materialize(digest, destination)
    assert not destination.exists()


def test_new_digest_shard_fsyncs_sha256_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    fsynced: list[Path] = []
    real_fsync = cas_module._fsync_directory_handle

    def record_fsync(descriptor: int | None, path: Path) -> None:
        fsynced.append(path)
        real_fsync(descriptor, path)

    monkeypatch.setattr(cas_module, "_fsync_directory_handle", record_fsync)

    info = store.put_bytes(b"first object in its digest shard")

    assert store.root / "sha256" in fsynced
    assert (store.root / info.storage_key).is_file()


def test_relative_root_remains_stable_after_working_directory_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = LocalCAS(Path("relative-objects"))
    info = store.put_bytes(b"stable root")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.chdir(elsewhere)

    assert store.root == (tmp_path / "relative-objects").resolve()
    assert store.read_bytes(info.digest) == b"stable root"
    assert store.put_bytes(b"another object").byte_size == len(b"another object")


def test_new_root_fsyncs_each_created_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    root = existing / "level-one" / "level-two" / "objects"
    fsynced: list[Path] = []

    monkeypatch.setattr(
        cas_module,
        "_fsync_directory",
        lambda path: fsynced.append(path),
    )
    monkeypatch.setattr(
        cas_module,
        "_fsync_directory_handle",
        lambda _descriptor, _path: None,
    )

    store = LocalCAS(root)

    assert store.root == root
    assert fsynced == [
        existing,
        existing / "level-one",
        existing / "level-one" / "level-two",
    ]


def test_constructor_rejects_symlink_and_special_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(real_root, target_is_directory=True)
    file_root = tmp_path / "file-root"
    file_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StorageValidationError, match="not a real directory"):
        LocalCAS(symlink_root)
    with pytest.raises(StorageValidationError, match="not a real directory"):
        LocalCAS(file_root)


def test_concurrent_same_digest_put_publishes_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"concurrent immutable object"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]
    gate = threading.Barrier(2)
    local = threading.local()
    real_verify = LocalCAS._verify_existing_at

    def synchronize_publish_check(self, *args, **kwargs):
        result = real_verify(self, *args, **kwargs)
        count = getattr(local, "count", 0) + 1
        local.count = count
        if count == 2 and result is None:
            gate.wait(timeout=5)
        return result

    successful_inodes: list[int] = []
    real_link = cas_module._link_at

    def record_link(*args, **kwargs):
        real_link(*args, **kwargs)
        successful_inodes.append(destination.stat().st_ino)

    monkeypatch.setattr(LocalCAS, "_verify_existing_at", synchronize_publish_check)
    monkeypatch.setattr(cas_module, "_link_at", record_link)

    errors: list[BaseException] = []

    def put() -> None:
        try:
            store.put_bytes(payload)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=put) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(successful_inodes) == 1
    assert destination.stat().st_ino == successful_inodes[0]
    assert store.read_bytes(digest) == payload


def test_deduplicated_put_flushes_concurrent_publish_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"concurrent durability receipt"
    digest = hashlib.sha256(payload).hexdigest()
    shard = store.root / "sha256" / digest[:2]
    linked = threading.Event()
    release_first_writer = threading.Event()
    dedupe_flushed = threading.Event()
    real_link = cas_module._link_at
    real_fsync = cas_module._fsync_directory_handle

    def link_then_pause(*args, **kwargs) -> None:
        real_link(*args, **kwargs)
        linked.set()
        if not release_first_writer.wait(timeout=5):
            raise TimeoutError("first CAS publisher was not released")

    def record_fsync(descriptor: int | None, path: Path) -> None:
        if linked.is_set() and path == shard:
            dedupe_flushed.set()
        real_fsync(descriptor, path)

    monkeypatch.setattr(cas_module, "_link_at", link_then_pause)
    monkeypatch.setattr(cas_module, "_fsync_directory_handle", record_fsync)
    errors: list[BaseException] = []

    def put() -> None:
        try:
            store.put_bytes(payload)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=put)
    first.start()
    assert linked.wait(timeout=5)
    second = threading.Thread(target=put)
    second.start()
    second.join(timeout=5)

    try:
        assert not second.is_alive()
        assert dedupe_flushed.is_set()
        assert errors == []
    finally:
        release_first_writer.set()
        first.join(timeout=5)

    assert not first.is_alive()
    assert errors == []
    assert store.read_bytes(digest) == payload


def test_put_falls_back_to_atomic_replace_without_hard_link_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    replacements: list[str] = []
    real_replace = cas_module._replace_at

    def unsupported_link(*_args, **_kwargs) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    def record_replace(*args, **kwargs) -> None:
        real_replace(*args, **kwargs)
        replacements.append("replaced")

    monkeypatch.setattr(cas_module, "_link_at", unsupported_link)
    monkeypatch.setattr(cas_module, "_replace_at", record_replace)

    info = store.put_bytes(b"fallback publication")

    assert replacements == ["replaced"]
    assert store.read_bytes(info.digest) == b"fallback publication"
    assert list((store.root / "sha256").rglob("*.tmp")) == []
