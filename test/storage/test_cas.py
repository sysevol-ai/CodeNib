# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import hashlib
import multiprocessing
import os
import stat
from pathlib import Path
from queue import Empty

import pytest

import codenib._atomic_directory as atomic_module
import codenib._owned_file_publication as owned_file_module
import codenib.storage.cas as cas_module
from codenib.storage.cas import LocalCAS
from codenib.storage.models import StorageIntegrityError, StorageValidationError


def _descriptor_count() -> int:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        pytest.skip("descriptor accounting requires procfs")
    return len(os.listdir(descriptor_root))


def _assert_bad_descriptor(descriptor: int) -> None:
    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF


def _exception_notes(error: BaseException) -> tuple[str, ...]:
    return (
        *tuple(getattr(error, "__notes__", ())),
        *tuple(getattr(error, "_codenib_cleanup_notes", ())),
    )


def _put_bytes_in_process(
    root: str,
    payload: bytes,
    ready: multiprocessing.Queue,
    start: multiprocessing.Event,
) -> None:
    store = LocalCAS(Path(root), require_preprovisioned=True)
    ready.put(os.getpid())
    if not start.wait(timeout=30):
        raise TimeoutError("CAS publication start was not released")
    store.put_bytes(payload)


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
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stored.stat().st_nlink == 1
    orphans = list(stored.parent.glob(".codenib-file-*"))
    assert len(orphans) == 1
    assert orphans[0].read_bytes() == payload
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
    with pytest.raises(StorageIntegrityError, match="failed integrity"):
        store.put_bytes(payload)


def test_failed_file_fsync_leaves_owned_stage_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS(tmp_path / "objects")
    payload = b"not published"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]

    real_fsync = owned_file_module.os.fsync

    def fail_file_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected file fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(owned_file_module.os, "fsync", fail_file_fsync)

    with pytest.raises(OSError, match="injected file fsync failure"):
        store.put_bytes(payload)

    assert not destination.exists()
    stages = list(destination.parent.glob(".codenib-file-*"))
    assert len(stages) == 1
    assert stages[0].read_bytes() == payload


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
    original = b"first version"
    source.write_bytes(original)
    original_digest = hashlib.sha256(original).hexdigest()
    real_publish = LocalCAS._publish_object

    def mutate_then_publish(self, *args, **kwargs):
        source.write_bytes(b"second version with a different size")
        return real_publish(self, *args, **kwargs)

    monkeypatch.setattr(
        LocalCAS,
        "_publish_object",
        mutate_then_publish,
    )

    with pytest.raises(OSError, match="source changed"):
        store.put_file(source)

    destination = store.root / "sha256" / original_digest[:2] / original_digest[2:]
    assert not destination.exists()
    stages = list(destination.parent.glob(".codenib-file-*"))
    assert len(stages) == 1


def test_second_file_pass_checks_same_size_mutation_before_stop_iteration(
    tmp_path: Path,
    filesystem_ctime_tick,
) -> None:
    source = tmp_path / "source.bin"
    original = b"same-size-original"
    replacement = b"same-size-mutated!"
    assert len(replacement) == len(original)
    source.write_bytes(original)
    digest, byte_size, signature = cas_module._hash_stable_file(source)
    chunks = cas_module._stable_file_chunks(
        source,
        expected_signature=signature,
        expected_digest=digest,
        expected_size=byte_size,
    )

    assert next(chunks) == original
    filesystem_ctime_tick(signature[5])
    source.write_bytes(replacement)

    with pytest.raises(OSError, match="source changed"):
        next(chunks)


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

    assert store.root == Path(os.path.abspath(tmp_path / "relative-objects"))
    assert store.read_bytes(info.digest) == b"stable root"
    assert store.put_bytes(b"another object").byte_size == len(b"another object")


def test_constructor_never_resolves_or_follows_a_lexical_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    resolved = False

    def forbidden_resolve(*_args, **_kwargs):
        nonlocal resolved
        resolved = True
        raise AssertionError("CAS root used Path.resolve")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)
    store = LocalCAS(root)

    assert store.root == root
    assert not resolved


def test_constructor_rejects_symlinked_ancestor_without_foreign_mutation(
    tmp_path: Path,
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(foreign, target_is_directory=True)

    for require_preprovisioned in (False, True):
        with pytest.raises(StorageValidationError, match="not a real directory"):
            LocalCAS(
                alias / "objects",
                require_preprovisioned=require_preprovisioned,
            )

    assert list(foreign.iterdir()) == []


def test_provision_rejects_filesystem_anchor_before_mkdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mkdir_calls: list[tuple[object, ...]] = []

    def forbidden_mkdir(*args, **_kwargs) -> None:
        mkdir_calls.append(args)
        raise AssertionError("provision attempted mkdir before rejecting root")

    monkeypatch.setattr(cas_module.os, "mkdir", forbidden_mkdir)

    with pytest.raises(StorageValidationError, match="lexical parent"):
        LocalCAS.provision(Path(os.path.sep))

    assert mkdir_calls == []


def test_provision_requires_preexisting_parent_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_parent = tmp_path / "missing-parent"
    root = missing_parent / "objects"
    mkdir_calls: list[tuple[object, ...]] = []

    def forbidden_mkdir(*args, **_kwargs) -> None:
        mkdir_calls.append(args)
        raise AssertionError("provision attempted to create a missing ancestor")

    monkeypatch.setattr(cas_module.os, "mkdir", forbidden_mkdir)

    with pytest.raises(StorageValidationError, match="parent must already exist"):
        LocalCAS.provision(root)

    assert mkdir_calls == []
    assert not missing_parent.exists()


def test_provision_rejects_symlinked_parent_without_foreign_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(foreign, target_is_directory=True)
    mkdir_calls: list[tuple[object, ...]] = []

    def forbidden_mkdir(*args, **_kwargs) -> None:
        mkdir_calls.append(args)
        raise AssertionError("provision followed a symlinked root parent")

    monkeypatch.setattr(cas_module.os, "mkdir", forbidden_mkdir)

    with pytest.raises(StorageValidationError, match="real directory"):
        LocalCAS.provision(alias / "objects")

    assert mkdir_calls == []
    assert list(foreign.iterdir()) == []


@pytest.mark.parametrize("entry", ["root", "sha256", "last-shard"])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_provision_retry_unconditionally_replays_complete_fsync_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    phase: str,
) -> None:
    root = tmp_path / "objects"
    sha256_root = root / "sha256"
    real_fsync = cas_module.os.fsync
    injected = False

    def matches_target(descriptor: int) -> bool:
        observed = os.fstat(descriptor)
        if entry == "root":
            target = root.parent
        elif entry == "sha256":
            target = root
        else:
            if not (sha256_root / "ff").is_dir():
                return False
            target = sha256_root
        metadata = target.stat()
        return (observed.st_dev, observed.st_ino) == (
            metadata.st_dev,
            metadata.st_ino,
        )

    def fail_target_fsync(descriptor: int) -> None:
        nonlocal injected
        if not injected and matches_target(descriptor):
            injected = True
            if phase == "after":
                real_fsync(descriptor)
            raise OSError(errno.EIO, f"injected {entry} {phase} fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(cas_module.os, "fsync", fail_target_fsync)
    with pytest.raises(OSError, match=f"injected {entry} {phase}"):
        LocalCAS.provision(root)
    assert injected

    replayed_after_complete_layout = False

    def record_retry_fsync(descriptor: int) -> None:
        nonlocal replayed_after_complete_layout
        real_fsync(descriptor)
        if (
            sha256_root.is_dir()
            and len([path for path in sha256_root.iterdir() if path.is_dir()]) == 256
            and matches_target(descriptor)
        ):
            replayed_after_complete_layout = True

    monkeypatch.setattr(cas_module.os, "fsync", record_retry_fsync)
    store = LocalCAS.provision(root)

    assert replayed_after_complete_layout
    assert store.put_bytes(b"durable retry").byte_size == len(b"durable retry")


def test_provision_replays_durability_chain_in_child_to_parent_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    LocalCAS.provision(root)
    targets = {
        (metadata.st_dev, metadata.st_ino): label
        for label, metadata in (
            ("sha256", (root / "sha256").stat()),
            ("root", root.stat()),
            ("root-parent", root.parent.stat()),
        )
    }
    events: list[str] = []
    real_fsync = cas_module.os.fsync

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        label = targets.get((metadata.st_dev, metadata.st_ino))
        if label is not None:
            events.append(label)
        real_fsync(descriptor)

    monkeypatch.setattr(cas_module.os, "fsync", record_fsync)

    LocalCAS.provision(root)

    assert events == ["sha256", "root", "root-parent"]


def test_constructor_rejects_symlink_and_special_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(real_root, target_is_directory=True)
    file_root = tmp_path / "file-root"
    file_root.write_text("not a directory", encoding="utf-8")

    for require_preprovisioned in (False, True):
        with pytest.raises(StorageValidationError, match="not a real directory"):
            LocalCAS(
                symlink_root,
                require_preprovisioned=require_preprovisioned,
            )
        with pytest.raises(StorageValidationError, match="not a real directory"):
            LocalCAS(
                file_root,
                require_preprovisioned=require_preprovisioned,
            )
    assert list(real_root.iterdir()) == []


def test_provision_builds_complete_strict_layout_and_put_never_mkdirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    LocalCAS.provision(root)
    sha256_root = root / "sha256"

    assert {entry.name for entry in sha256_root.iterdir()} == {
        f"{value:02x}" for value in range(256)
    }
    store = LocalCAS(root, require_preprovisioned=True)
    assert store._strict_root_identity is not None
    assert store._strict_sha256_identity is not None
    assert len(store._strict_shard_identities) == 256
    assert store._strict_resources is not None
    assert len(store._strict_shard_descriptors) == 256

    def forbidden_mkdir(*_args, **_kwargs) -> None:
        raise AssertionError("strict CAS put attempted to create a directory")

    monkeypatch.setattr(cas_module.os, "mkdir", forbidden_mkdir)
    info = store.put_bytes(b"strict preprovisioned object")

    assert store.read_bytes(info.digest) == b"strict preprovisioned object"


def test_strict_constructor_cancellation_during_resource_handoff_closes_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    provisioned = LocalCAS.provision(root)
    provisioned.close()
    retained: list[int] = []
    real_retain = cas_module._retain_directory_descriptor

    def retain(resources, descriptor):
        retained_descriptor = real_retain(resources, descriptor)
        retained.append(retained_descriptor)
        return retained_descriptor

    monkeypatch.setattr(cas_module, "_retain_directory_descriptor", retain)
    interruption = KeyboardInterrupt("strict resource handoff cancellation")

    def interrupt_handoff(store, **state) -> None:
        assert store.root == root
        assert state["require_preprovisioned"] is True
        assert state["strict_root_descriptor"] is not None
        assert state["strict_sha256_descriptor"] is not None
        assert len(state["strict_shard_identities"]) == 256
        assert len(state["strict_shard_descriptors"]) == 256
        assert not state["strict_resources"].closed
        raise interruption

    monkeypatch.setattr(cas_module, "_install_strict_state", interrupt_handoff)
    before = _descriptor_count()

    with pytest.raises(KeyboardInterrupt) as caught:
        LocalCAS(root, require_preprovisioned=True)

    assert caught.value is interruption
    assert len(retained) == 258
    assert len(set(retained)) == len(retained)
    for descriptor in retained:
        _assert_bad_descriptor(descriptor)
    assert _descriptor_count() <= before


def test_strict_put_rejects_preprovisioned_shard_generation_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    store = LocalCAS.provision(root)
    payload = b"generation-bound object"
    digest = hashlib.sha256(payload).hexdigest()
    shard = root / "sha256" / digest[:2]
    previous = root / "sha256" / f"{digest[:2]}-previous"
    shard.rename(previous)
    shard.mkdir()

    with pytest.raises(StorageIntegrityError, match="generation changed"):
        store.put_bytes(payload)

    assert list(shard.iterdir()) == []
    assert list(previous.iterdir()) == []


def test_strict_put_retains_unlinked_shard_generation_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    store = LocalCAS.provision(root)
    payload = b"pinned shard generation"
    digest = hashlib.sha256(payload).hexdigest()
    shard_name = digest[:2]
    shard = root / "sha256" / shard_name
    retained = store._strict_shard_descriptors[shard_name]
    retained_identity = cas_module._directory_resource_identity(
        retained,
        path=shard,
        label="retained test shard",
    )

    shard.rmdir()
    shard.mkdir()

    assert (
        cas_module._directory_resource_identity(
            retained,
            path=shard,
            label="retained test shard",
        )
        == retained_identity
    )
    with pytest.raises(StorageIntegrityError, match="generation changed"):
        store.put_bytes(payload)
    assert list(shard.iterdir()) == []
    store.close()
    _assert_bad_descriptor(retained)


@pytest.mark.parametrize("layer", ["root", "sha256", "shard"])
@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_directory_owner_close_fault_cleans_all_and_preserves_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
    phase: str,
    exception_type: type[BaseException],
) -> None:
    root = tmp_path / "objects"
    store = LocalCAS.provision(root)
    payload = f"{layer}-{phase}-{exception_type.__name__}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    paths = {
        "root": root,
        "sha256": root / "sha256",
        "shard": root / "sha256" / digest[:2],
    }
    target_metadata = paths[layer].stat()
    target_identity = (target_metadata.st_dev, target_metadata.st_ino)
    captured: list[int] = []
    real_open = atomic_module._PosixResourceOwner.open
    real_close = cas_module.os.close
    error = (
        OSError(errno.EIO, f"{phase} {layer} close failure")
        if exception_type is OSError
        else exception_type(f"{phase} {layer} close interruption")
    )
    injected = False

    def capture_target_open(
        owner,
        path,
        flags,
        mode=0o777,
        *,
        dir_fd=None,
    ):
        descriptor = real_open(owner, path, flags, mode, dir_fd=dir_fd)
        metadata = os.fstat(descriptor)
        if not captured and (metadata.st_dev, metadata.st_ino) == target_identity:
            captured.append(descriptor)
        return descriptor

    def fail_target_close(descriptor: int) -> None:
        nonlocal injected
        if captured and descriptor == captured[0] and not injected:
            injected = True
            if phase == "after":
                real_close(descriptor)
            raise error
        real_close(descriptor)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "open",
        capture_target_open,
    )
    monkeypatch.setattr(cas_module.os, "close", fail_target_close)
    before = _descriptor_count()

    with pytest.raises(exception_type) as caught:
        store.put_bytes(payload)

    assert caught.value is error
    assert injected
    assert len(captured) == 1
    _assert_bad_descriptor(captured[0])
    assert _descriptor_count() <= before
    destination = root / "sha256" / digest[:2] / digest[2:]
    committed_inode = destination.stat().st_ino

    monkeypatch.setattr(cas_module.os, "close", real_close)
    assert store.put_bytes(payload).digest == digest
    assert destination.stat().st_ino == committed_inode


@pytest.mark.parametrize("layer", ["root", "sha256", "shard"])
def test_directory_owner_retries_repeated_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
) -> None:
    root = tmp_path / "objects"
    store = LocalCAS.provision(root)
    payload = f"persistent-{layer}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    paths = {
        "root": root,
        "sha256": root / "sha256",
        "shard": root / "sha256" / digest[:2],
    }
    metadata = paths[layer].stat()
    target_identity = (metadata.st_dev, metadata.st_ino)
    captured: list[int] = []
    real_open = atomic_module._PosixResourceOwner.open
    real_close = cas_module.os.close
    error = OSError(errno.EIO, f"repeated {layer} close failure")
    failures_remaining = 5

    def capture_target_open(
        owner,
        path,
        flags,
        mode=0o777,
        *,
        dir_fd=None,
    ):
        descriptor = real_open(owner, path, flags, mode, dir_fd=dir_fd)
        opened = os.fstat(descriptor)
        if not captured and (opened.st_dev, opened.st_ino) == target_identity:
            captured.append(descriptor)
        return descriptor

    def fail_repeatedly(descriptor: int) -> None:
        nonlocal failures_remaining
        if captured and descriptor == captured[0] and failures_remaining:
            failures_remaining -= 1
            raise error
        real_close(descriptor)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "open",
        capture_target_open,
    )
    monkeypatch.setattr(cas_module.os, "close", fail_repeatedly)
    before = _descriptor_count()

    with pytest.raises(OSError) as caught:
        store.put_bytes(payload)

    assert caught.value is error
    assert failures_remaining == 0
    _assert_bad_descriptor(captured[0])
    assert _descriptor_count() <= before


def test_nested_directory_cleanup_keeps_first_error_and_attempts_every_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    store = LocalCAS.provision(root)
    payload = b"nested cleanup first primary"
    digest = hashlib.sha256(payload).hexdigest()
    target_paths = [root / "sha256" / digest[:2], root / "sha256"]
    target_identities = {
        (path.stat().st_dev, path.stat().st_ino): index
        for index, path in enumerate(target_paths)
    }
    descriptors: dict[int, int] = {}
    real_open = atomic_module._PosixResourceOwner.open
    real_close = cas_module.os.close
    primary = KeyboardInterrupt("shard close interruption")
    secondary = SystemExit("sha256 close interruption")
    injected: set[int] = set()

    def capture_target_open(
        owner,
        path,
        flags,
        mode=0o777,
        *,
        dir_fd=None,
    ):
        descriptor = real_open(owner, path, flags, mode, dir_fd=dir_fd)
        metadata = os.fstat(descriptor)
        index = target_identities.get((metadata.st_dev, metadata.st_ino))
        if index is not None and index not in descriptors:
            descriptors[index] = descriptor
        return descriptor

    def fail_two_layers(descriptor: int) -> None:
        for index, error in ((0, primary), (1, secondary)):
            if descriptors.get(index) == descriptor and index not in injected:
                injected.add(index)
                raise error
        real_close(descriptor)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "open",
        capture_target_open,
    )
    monkeypatch.setattr(cas_module.os, "close", fail_two_layers)
    before = _descriptor_count()

    with pytest.raises(KeyboardInterrupt) as caught:
        store.put_bytes(payload)

    assert caught.value is primary
    assert injected == {0, 1}
    assert any(
        "sha256 close interruption" in note for note in _exception_notes(primary)
    )
    assert all(descriptor >= 0 for descriptor in descriptors.values())
    for descriptor in descriptors.values():
        _assert_bad_descriptor(descriptor)
    assert _descriptor_count() <= before


@pytest.mark.parametrize("layer", ["root", "sha256", "shard"])
def test_directory_owner_does_not_close_observably_reused_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
) -> None:
    root = tmp_path / "objects"
    store = LocalCAS.provision(root)
    payload = f"reuse-{layer}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    paths = {
        "root": root,
        "sha256": root / "sha256",
        "shard": root / "sha256" / digest[:2],
    }
    metadata = paths[layer].stat()
    target_identity = (metadata.st_dev, metadata.st_ino)
    foreign = tmp_path / f"foreign-{layer}.bin"
    foreign.write_bytes(b"foreign")
    foreign_source = os.open(foreign, os.O_RDONLY)
    captured: list[int] = []
    real_open = atomic_module._PosixResourceOwner.open
    real_close = cas_module.os.close
    interruption = KeyboardInterrupt(f"{layer} close reused fd")
    reused = False

    def capture_target_open(
        owner,
        path,
        flags,
        mode=0o777,
        *,
        dir_fd=None,
    ):
        descriptor = real_open(owner, path, flags, mode, dir_fd=dir_fd)
        opened = os.fstat(descriptor)
        if not captured and (opened.st_dev, opened.st_ino) == target_identity:
            captured.append(descriptor)
        return descriptor

    def close_then_reuse(descriptor: int) -> None:
        nonlocal reused
        real_close(descriptor)
        if captured and descriptor == captured[0] and not reused:
            os.dup2(foreign_source, descriptor)
            reused = True
            raise interruption

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "open",
        capture_target_open,
    )
    monkeypatch.setattr(cas_module.os, "close", close_then_reuse)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            store.put_bytes(payload)

        assert caught.value is interruption
        assert reused
        assert os.pread(captured[0], len(b"foreign"), 0) == b"foreign"
        assert store.put_bytes(payload).digest == digest
        assert os.pread(captured[0], len(b"foreign"), 0) == b"foreign"
    finally:
        monkeypatch.setattr(cas_module.os, "close", real_close)
        if captured:
            real_close(captured[0])
        real_close(foreign_source)


def test_strict_constructor_rejects_incomplete_layout_without_mutation(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing"

    with pytest.raises(StorageValidationError, match="does not exist"):
        LocalCAS(missing_root, require_preprovisioned=True)
    assert not missing_root.exists()

    incomplete = tmp_path / "incomplete"
    (incomplete / "sha256").mkdir(parents=True)
    marker = incomplete / "sha256" / "keep"
    marker.write_bytes(b"unchanged")
    before = sorted(path.relative_to(incomplete) for path in incomplete.rglob("*"))

    with pytest.raises(StorageValidationError, match="all 256 digest shards"):
        LocalCAS(incomplete, require_preprovisioned=True)

    after = sorted(path.relative_to(incomplete) for path in incomplete.rglob("*"))
    assert after == before
    assert marker.read_bytes() == b"unchanged"


def test_supported_posix_lazy_path_never_calls_portable_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        cas_module._require_local_cas_support()
    except RuntimeError:
        pytest.skip("host does not support hardened LocalCAS publication")

    def forbidden_portable_helper(*_args, **_kwargs) -> None:
        raise AssertionError("supported POSIX CAS called a portable helper")

    for helper_name in (
        "_open_portable_directory_path",
        "_open_portable_child_directory",
        "_open_or_create_portable_child_directory",
        "_create_portable_temporary_file",
        "_unlink_portable",
    ):
        monkeypatch.setattr(cas_module, helper_name, forbidden_portable_helper)
    for method_name in (
        "_publish_portable_object",
        "_verify_portable_existing",
        "_publish_portable_temporary",
    ):
        monkeypatch.setattr(LocalCAS, method_name, forbidden_portable_helper)

    store = LocalCAS(tmp_path / "objects")
    info = store.put_bytes(b"hardened POSIX lazy object")

    assert not store._portable_lazy
    assert store.read_bytes(info.digest) == b"hardened POSIX lazy object"


def test_missing_directory_fds_use_portable_lazy_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    monkeypatch.setattr(cas_module, "_SAFE_DIRECTORY_FDS", False)

    def forbidden_owned_publication(*_args, **_kwargs) -> None:
        raise AssertionError("portable lazy CAS used owned publication")

    monkeypatch.setattr(cas_module, "publish_owned_file", forbidden_owned_publication)
    store = LocalCAS(root)
    info = store.put_bytes(b"portable directory-fd fallback")
    destination = tmp_path / "view.bin"

    assert store._portable_lazy
    assert store.read_bytes(info.digest) == b"portable directory-fd fallback"
    assert store.materialize(info.digest, destination) == destination
    assert destination.read_bytes() == b"portable directory-fd fallback"

    strict_root = tmp_path / "strict"
    with pytest.raises(RuntimeError, match="directory-fd support"):
        LocalCAS(strict_root, require_preprovisioned=True)
    assert not strict_root.exists()


def test_windows_uses_portable_lazy_backend_but_strict_fails_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(owned_file_module.sys, "platform", "win32")
    lazy_root = tmp_path / "lazy"
    source = tmp_path / "source.bin"
    source.write_bytes(b"portable Windows source")
    destination = tmp_path / "materialized.bin"
    strict_root = tmp_path / "strict"
    provisioned_root = tmp_path / "provisioned"

    def forbidden_owned_publication(*_args, **_kwargs) -> None:
        raise AssertionError("portable Windows CAS used owned publication")

    monkeypatch.setattr(cas_module, "publish_owned_file", forbidden_owned_publication)
    store = LocalCAS(lazy_root)
    bytes_info = store.put_bytes(b"portable Windows bytes")
    file_info = store.put_file(source)

    assert store._portable_lazy
    assert store.read_bytes(bytes_info.digest) == b"portable Windows bytes"
    assert store.read_bytes(file_info.digest) == source.read_bytes()
    assert store.materialize(bytes_info.digest, destination) == destination
    assert destination.read_bytes() == b"portable Windows bytes"

    with pytest.raises(RuntimeError, match="unsupported on Windows"):
        LocalCAS(strict_root, require_preprovisioned=True)
    with pytest.raises(RuntimeError, match="unsupported on Windows"):
        LocalCAS.provision(provisioned_root)

    assert not strict_root.exists()
    assert not provisioned_root.exists()


def test_real_multiprocess_same_digest_publishes_one_canonical_inode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    store = LocalCAS.provision(root)
    payload = b"multiprocess immutable object"
    digest = hashlib.sha256(payload).hexdigest()
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_put_bytes_in_process,
            args=(str(root), payload, ready, start),
        )
        for _index in range(4)
    ]
    for process in processes:
        process.start()
    try:
        for _process in processes:
            try:
                ready.get(timeout=30)
            except Empty as exc:  # pragma: no cover - failure diagnostic
                raise AssertionError("CAS worker did not reach the race") from exc
    finally:
        start.set()
    for process in processes:
        process.join(timeout=30)
    try:
        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    finally:
        for process in processes:
            if process.is_alive():  # pragma: no cover - failure cleanup
                process.terminate()
                process.join(timeout=10)
        ready.close()
        ready.join_thread()

    destination = root / "sha256" / digest[:2] / digest[2:]
    assert destination.read_bytes() == payload
    assert destination.stat().st_nlink == 1
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    stages = list(destination.parent.glob(".codenib-file-*"))
    assert len(stages) == len(processes) - 1
    assert all(stage.read_bytes() == payload for stage in stages)
    assert store.verify(digest).byte_size == len(payload)


def test_destination_appearing_during_rename_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS.provision(tmp_path / "objects")
    payload = b"requested object"
    competing = b"competing object"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]
    real_rename = atomic_module._rename_noreplace_at
    raced = False

    def publish_competitor_then_rename(
        source: str,
        target: str,
        source_parent: int,
        target_parent: int,
    ) -> None:
        nonlocal raced
        if not raced:
            raced = True
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(target, flags, 0o600, dir_fd=target_parent)
            try:
                os.write(descriptor, competing)
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        real_rename(source, target, source_parent, target_parent)

    monkeypatch.setattr(
        atomic_module,
        "_rename_noreplace_at",
        publish_competitor_then_rename,
    )

    with pytest.raises(StorageIntegrityError) as caught:
        store.put_bytes(payload)

    assert isinstance(caught.value.__cause__, owned_file_module.OwnedFileConflictError)
    assert destination.read_bytes() == competing
    assert len(list(destination.parent.glob(".codenib-file-*"))) == 1


@pytest.mark.parametrize("defect", ["mode", "link-count"])
def test_existing_object_metadata_conflict_is_not_healed(
    tmp_path: Path,
    defect: str,
) -> None:
    store = LocalCAS.provision(tmp_path / "objects")
    payload = b"canonical payload"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]
    destination.write_bytes(payload)
    destination.chmod(0o600)
    sibling = destination.parent / "extra-link"
    if defect == "mode":
        destination.chmod(0o644)
    else:
        os.link(destination, sibling)
    before = destination.stat()

    with pytest.raises(StorageIntegrityError) as caught:
        store.put_bytes(payload)

    after = destination.stat()
    assert isinstance(caught.value.__cause__, owned_file_module.OwnedFileConflictError)
    assert after.st_ino == before.st_ino
    assert after.st_nlink == before.st_nlink
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert destination.read_bytes() == payload
    if defect == "link-count":
        assert sibling.stat().st_ino == before.st_ino


def test_rename_failure_before_commit_leaves_stage_and_fresh_retry_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS.provision(tmp_path / "objects")
    payload = b"retry before rename"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]
    real_rename = atomic_module._rename_noreplace_at

    def fail_before_rename(*_args, **_kwargs) -> None:
        raise OSError(errno.EIO, "injected before rename")

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", fail_before_rename)
    with pytest.raises(OSError, match="injected before rename"):
        store.put_bytes(payload)

    assert not destination.exists()
    stages = list(destination.parent.glob(".codenib-file-*"))
    assert len(stages) == 1
    failed_inode = stages[0].stat().st_ino

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", real_rename)
    info = store.put_bytes(payload)

    assert info.digest == digest
    assert destination.read_bytes() == payload
    assert destination.stat().st_ino != failed_inode
    assert stages[0].exists()


def test_rename_failure_after_commit_reuses_same_inode_on_fresh_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS.provision(tmp_path / "objects")
    payload = b"retry after rename"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]
    real_rename = atomic_module._rename_noreplace_at

    def rename_then_fail(*args, **kwargs) -> None:
        real_rename(*args, **kwargs)
        raise OSError(errno.EIO, "injected after rename")

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", rename_then_fail)
    with pytest.raises(OSError, match="injected after rename"):
        store.put_bytes(payload)

    committed_inode = destination.stat().st_ino
    assert list(destination.parent.glob(".codenib-file-*")) == []

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", real_rename)
    info = store.put_bytes(payload)

    assert info.digest == digest
    assert destination.stat().st_ino == committed_inode
    assert len(list(destination.parent.glob(".codenib-file-*"))) == 1


def test_parent_fsync_failure_returns_no_blob_and_retry_reuses_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS.provision(tmp_path / "objects")
    payload = b"retry after parent fsync"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]
    real_fsync = owned_file_module.os.fsync
    failed = False

    def fail_first_directory_fsync(descriptor: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            failed = True
            raise OSError(errno.EIO, "injected parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(owned_file_module.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(OSError, match="injected parent fsync failure"):
        store.put_bytes(payload)

    assert failed
    committed_inode = destination.stat().st_ino

    monkeypatch.setattr(owned_file_module.os, "fsync", real_fsync)
    info = store.put_bytes(payload)

    assert info.digest == digest
    assert destination.stat().st_ino == committed_inode
    assert len(list(destination.parent.glob(".codenib-file-*"))) == 1


def test_blob_info_is_created_only_after_durable_receipt_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS.provision(tmp_path / "objects")
    events: list[str] = []
    real_fsync = owned_file_module.os.fsync
    real_rename = atomic_module._rename_noreplace_at
    real_consume = owned_file_module.PublishedFileReceiptOwner.consume
    real_receipt_close = owned_file_module.PublishedFileReceipt._close_from_owner
    real_owner_close = owned_file_module.PublishedFileReceiptOwner.close
    real_blob_info = LocalCAS._blob_info

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        real_fsync(descriptor)
        events.append(
            "parent-fsync" if stat.S_ISDIR(metadata.st_mode) else "file-fsync"
        )

    def record_rename(*args, **kwargs) -> None:
        real_rename(*args, **kwargs)
        events.append("rename")

    def record_consume(self, callback) -> None:
        events.append("consume-enter")
        real_consume(self, callback)
        events.append("consume-return")

    def record_receipt_close(self, authority) -> None:
        events.append("receipt-close-enter")
        real_receipt_close(self, authority)
        events.append("receipt-close-return")

    def record_owner_close(self) -> None:
        events.append("owner-close-enter")
        real_owner_close(self)
        assert self.closed
        events.append("owner-terminal")

    def record_blob_info(digest: str, byte_size: int):
        events.append("blob-info")
        return real_blob_info(digest, byte_size)

    monkeypatch.setattr(owned_file_module.os, "fsync", record_fsync)
    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", record_rename)
    monkeypatch.setattr(
        owned_file_module.PublishedFileReceiptOwner,
        "consume",
        record_consume,
    )
    monkeypatch.setattr(
        owned_file_module.PublishedFileReceipt,
        "_close_from_owner",
        record_receipt_close,
    )
    monkeypatch.setattr(
        owned_file_module.PublishedFileReceiptOwner,
        "close",
        record_owner_close,
    )
    monkeypatch.setattr(LocalCAS, "_blob_info", staticmethod(record_blob_info))

    store.put_bytes(b"ordered publication")

    assert events == [
        "file-fsync",
        "rename",
        "parent-fsync",
        "consume-enter",
        "consume-return",
        "owner-close-enter",
        "receipt-close-enter",
        "receipt-close-return",
        "owner-terminal",
        "blob-info",
    ]


def test_unsupported_no_replace_never_uses_legacy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalCAS.provision(tmp_path / "objects")
    payload = b"no fallback publication"
    digest = hashlib.sha256(payload).hexdigest()
    destination = store.root / "sha256" / digest[:2] / digest[2:]
    replaced = False

    def unsupported_rename(*_args, **_kwargs) -> None:
        raise RuntimeError("filesystem no-replace unsupported")

    def forbidden_replace(*_args, **_kwargs) -> None:
        nonlocal replaced
        replaced = True
        raise AssertionError("legacy replace fallback was used")

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", unsupported_rename)
    monkeypatch.setattr(cas_module.os, "replace", forbidden_replace)

    with pytest.raises(RuntimeError, match="no-replace unsupported"):
        store.put_bytes(payload)

    assert not replaced
    assert not destination.exists()
    assert len(list(destination.parent.glob(".codenib-file-*"))) == 1
    for symbol in (
        "_create_temporary_file",
        "_link_at",
        "_replace_at",
        "_unlink_at",
    ):
        assert not hasattr(cas_module, symbol)
