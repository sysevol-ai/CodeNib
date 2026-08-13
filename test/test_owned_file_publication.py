# SPDX-FileCopyrightText: 2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import hashlib
import multiprocessing
import os
import signal
import stat
import sys
import threading
from pathlib import Path

import pytest

import codenib._atomic_directory as atomic_module
import codenib._owned_file_publication as publication_module
from codenib._owned_file_publication import (
    OwnedFileConflictError,
    OwnedFilePublicationAuthority,
    PublishedFileReceiptOwner,
    publish_owned_file,
    require_owned_file_publication_support,
)


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


def _set_nonzero_directory_offset(descriptor: int) -> int:
    offset = os.lseek(descriptor, 37, os.SEEK_SET)
    assert offset != 0
    return offset


def _store_publish_result(
    publication: OwnedFilePublicationAuthority,
    receipt_owner: PublishedFileReceiptOwner,
) -> None:
    result = publication.publish_into(receipt_owner)
    assert result is None


def _store_helper_result(
    destination: Path,
    receipt_owner: PublishedFileReceiptOwner,
) -> None:
    result = publication_module.publish_owned_file(
        destination,
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    assert result is None


def _publish_in_process(destination: str, payload: bytes) -> None:
    with PublishedFileReceiptOwner() as receipt_owner:
        publish_owned_file(
            Path(destination),
            [payload],
            max_bytes=len(payload),
            receipt_owner=receipt_owner,
        )
        receipt = receipt_owner.receipt
        assert receipt.read_bytes(max_bytes=len(payload)) == payload


def test_publish_returns_handle_bound_receipt(tmp_path: Path) -> None:
    destination = tmp_path / "object"

    with PublishedFileReceiptOwner() as receipt_owner:
        assert (
            publish_owned_file(
                destination,
                [b"abc", b"def"],
                max_bytes=6,
                mode=0o640,
                receipt_owner=receipt_owner,
            )
            is None
        )
        receipt = receipt_owner.receipt
        assert not receipt.reused
        assert receipt.orphan is None
        assert receipt.record.size == 6
        assert receipt.record.sha256 == hashlib.sha256(b"abcdef").hexdigest()
        assert receipt.record.mode == 0o640
        assert receipt.read_bytes(max_bytes=6) == b"abcdef"
        with pytest.raises(RuntimeError, match="ReceiptOwner"):
            receipt.close()
        assert receipt_owner.active
        assert destination.read_bytes() == b"abcdef"
        assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert receipt_owner.closed


def test_identical_existing_file_is_authenticated_and_reused(tmp_path: Path) -> None:
    destination = tmp_path / "object"
    destination.write_bytes(b"same")
    destination.chmod(0o640)

    with PublishedFileReceiptOwner() as receipt_owner:
        publish_owned_file(
            destination,
            b"same",
            max_bytes=4,
            mode=0o640,
            receipt_owner=receipt_owner,
        )
        receipt = receipt_owner.receipt
        orphan = receipt.orphan
        assert receipt.reused
        assert receipt.record.mode == 0o640
        assert receipt.read_bytes(max_bytes=4) == b"same"
        assert orphan is not None
        assert orphan.path.exists()
        assert orphan.path.read_bytes() == b"same"


def test_borrowed_parent_resource_is_never_closed(tmp_path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_resource = os.open(tmp_path, flags)
    expected_identity = atomic_module.publication_parent_identity(parent_resource)
    expected_offset = _set_nonzero_directory_offset(parent_resource)
    try:
        with PublishedFileReceiptOwner() as receipt_owner:
            publish_owned_file(
                tmp_path / "object",
                b"same-parent",
                max_bytes=len(b"same-parent"),
                parent_resource=parent_resource,
                receipt_owner=receipt_owner,
            )
            receipt = receipt_owner.receipt
            assert receipt.parent_identity == expected_identity
            assert receipt.read_bytes(max_bytes=len(b"same-parent")) == b"same-parent"

        assert atomic_module.publication_parent_identity(parent_resource) == (
            expected_identity
        )
        assert os.lseek(parent_resource, 0, os.SEEK_CUR) == expected_offset
    finally:
        os.close(parent_resource)


def test_borrowed_parent_mismatch_fails_before_stage_or_destination(
    tmp_path: Path,
) -> None:
    destination_parent = tmp_path / "destination-parent"
    borrowed_parent = tmp_path / "borrowed-parent"
    destination_parent.mkdir()
    borrowed_parent.mkdir()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_resource = os.open(borrowed_parent, flags)
    expected_offset = _set_nonzero_directory_offset(parent_resource)
    try:
        with pytest.raises(RuntimeError, match="parent path changed"):
            OwnedFilePublicationAuthority(
                destination_parent / "object",
                max_bytes=1,
                parent_resource=parent_resource,
            )

        assert list(destination_parent.iterdir()) == []
        assert list(borrowed_parent.iterdir()) == []
        os.fstat(parent_resource)
        assert os.lseek(parent_resource, 0, os.SEEK_CUR) == expected_offset
    finally:
        os.close(parent_resource)


def test_borrowed_parent_conflict_does_not_change_caller_offset(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "object"
    destination.write_bytes(b"old")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_resource = os.open(tmp_path, flags)
    expected_offset = _set_nonzero_directory_offset(parent_resource)
    try:
        with pytest.raises(OwnedFileConflictError):
            publish_owned_file(
                destination,
                b"new",
                max_bytes=3,
                parent_resource=parent_resource,
                consume=lambda _receipt: None,
            )

        os.fstat(parent_resource)
        assert os.lseek(parent_resource, 0, os.SEEK_CUR) == expected_offset
        assert destination.read_bytes() == b"old"
    finally:
        os.close(parent_resource)


def test_borrowed_parent_bridge_rejects_fd_replacement_before_openat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_resource = os.open(tmp_path, flags)
    original_alias = os.dup(parent_resource)
    foreign_resource = os.open(foreign, flags)
    expected_offset = _set_nonzero_directory_offset(parent_resource)
    real_open = atomic_module._PosixResourceOwner.open
    replaced = False

    def replace_then_open(
        owner,
        path,
        open_flags,
        mode=0o777,
        *,
        dir_fd=None,
    ):
        nonlocal replaced
        if path == "." and dir_fd == parent_resource and not replaced:
            os.dup2(foreign_resource, parent_resource)
            replaced = True
        return real_open(
            owner,
            path,
            open_flags,
            mode,
            dir_fd=dir_fd,
        )

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "open",
        replace_then_open,
    )
    try:
        with pytest.raises(RuntimeError, match="changed while opened"):
            OwnedFilePublicationAuthority(
                tmp_path / "object",
                max_bytes=1,
                parent_resource=parent_resource,
            )

        assert replaced
        assert atomic_module.publication_parent_identity(parent_resource) == (
            atomic_module.publication_parent_identity(foreign_resource)
        )
        assert not (tmp_path / "object").exists()
        assert not list(tmp_path.glob(".codenib-file-*"))
    finally:
        os.dup2(original_alias, parent_resource)
        assert os.lseek(parent_resource, 0, os.SEEK_CUR) == expected_offset
        os.close(foreign_resource)
        os.close(original_alias)
        os.close(parent_resource)


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("exception_type", [OSError, KeyboardInterrupt, SystemExit])
def test_borrowed_parent_bridge_close_failure_preserves_caller_ofd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    exception_type: type[BaseException],
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_resource = os.open(tmp_path, flags)
    expected_offset = _set_nonzero_directory_offset(parent_resource)
    before = _descriptor_count()
    bridge_descriptors: list[int] = []
    real_open = atomic_module._PosixResourceOwner.open
    real_close = publication_module.os.close
    error = (
        OSError(errno.EIO, f"{phase} bridge close failure")
        if exception_type is OSError
        else exception_type(f"{phase} bridge close interruption")
    )
    injected = False

    def record_bridge_open(
        owner,
        path,
        open_flags,
        mode=0o777,
        *,
        dir_fd=None,
    ):
        descriptor = real_open(
            owner,
            path,
            open_flags,
            mode,
            dir_fd=dir_fd,
        )
        if path == "." and dir_fd == parent_resource:
            bridge_descriptors.append(descriptor)
        return descriptor

    def fail_bridge_close(descriptor: int) -> None:
        nonlocal injected
        if bridge_descriptors and descriptor == bridge_descriptors[0] and not injected:
            injected = True
            if phase == "after":
                real_close(descriptor)
            raise error
        real_close(descriptor)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "open",
        record_bridge_open,
    )
    monkeypatch.setattr(publication_module.os, "close", fail_bridge_close)
    try:
        with pytest.raises(exception_type) as caught:
            OwnedFilePublicationAuthority(
                tmp_path / "object",
                max_bytes=1,
                parent_resource=parent_resource,
            )

        assert caught.value is error
        assert injected
        assert len(bridge_descriptors) == 1
        _assert_bad_descriptor(bridge_descriptors[0])
        os.fstat(parent_resource)
        assert os.lseek(parent_resource, 0, os.SEEK_CUR) == expected_offset
        assert _descriptor_count() <= before
        assert not (tmp_path / "object").exists()
        assert not list(tmp_path.glob(".codenib-file-*"))
    finally:
        os.close(parent_resource)


def test_borrowed_parent_bridge_retries_repeated_eio_without_touching_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_resource = os.open(tmp_path, flags)
    expected_offset = _set_nonzero_directory_offset(parent_resource)
    before = _descriptor_count()
    bridge_descriptors: list[int] = []
    real_open = atomic_module._PosixResourceOwner.open
    real_close = publication_module.os.close
    error = OSError(errno.EIO, "repeated bridge close failure")
    failures_remaining = 4

    def record_bridge_open(
        owner,
        path,
        open_flags,
        mode=0o777,
        *,
        dir_fd=None,
    ):
        descriptor = real_open(
            owner,
            path,
            open_flags,
            mode,
            dir_fd=dir_fd,
        )
        if path == "." and dir_fd == parent_resource:
            bridge_descriptors.append(descriptor)
        return descriptor

    def fail_repeatedly(descriptor: int) -> None:
        nonlocal failures_remaining
        if (
            bridge_descriptors
            and descriptor == bridge_descriptors[0]
            and failures_remaining
        ):
            failures_remaining -= 1
            raise error
        real_close(descriptor)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "open",
        record_bridge_open,
    )
    monkeypatch.setattr(publication_module.os, "close", fail_repeatedly)
    try:
        with pytest.raises(OSError) as caught:
            OwnedFilePublicationAuthority(
                tmp_path / "object",
                max_bytes=1,
                parent_resource=parent_resource,
            )

        assert caught.value is error
        assert failures_remaining == 0
        _assert_bad_descriptor(bridge_descriptors[0])
        os.fstat(parent_resource)
        assert os.lseek(parent_resource, 0, os.SEEK_CUR) == expected_offset
        assert _descriptor_count() <= before
    finally:
        os.close(parent_resource)


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_publish_into_caller_store_cancellation_keeps_owner_and_leaks_no_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    before = _descriptor_count()
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    receipt_owner = PublishedFileReceiptOwner()
    interruption = exception_type("injected before caller receipt-result store")
    real_publish = OwnedFilePublicationAuthority.publish_into

    def publish_then_interrupt(
        candidate: OwnedFilePublicationAuthority,
        owner: PublishedFileReceiptOwner,
    ) -> None:
        real_publish(candidate, owner)
        if candidate is publication:
            raise interruption

    monkeypatch.setattr(
        OwnedFilePublicationAuthority,
        "publish_into",
        publish_then_interrupt,
    )

    try:
        with pytest.raises(exception_type) as caught:
            _store_publish_result(publication, receipt_owner)
        assert caught.value is interruption
        assert publication.state == "published"
        assert receipt_owner.active
        assert receipt_owner.receipt.read_bytes(max_bytes=1) == b"x"
    finally:
        receipt_owner.close()
        publication.close()

    assert receipt_owner.closed
    assert _descriptor_count() <= before


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_helper_caller_store_cancellation_keeps_owner_and_leaks_no_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    before = _descriptor_count()
    destination = tmp_path / "object"
    receipt_owner = PublishedFileReceiptOwner()
    interruption = exception_type("injected before caller helper-result store")
    real_helper = publication_module.publish_owned_file

    def helper_then_interrupt(*args, **kwargs) -> None:
        real_helper(*args, **kwargs)
        raise interruption

    monkeypatch.setattr(
        publication_module,
        "publish_owned_file",
        helper_then_interrupt,
    )

    try:
        with pytest.raises(exception_type) as caught:
            _store_helper_result(destination, receipt_owner)
        assert caught.value is interruption
        assert receipt_owner.active
        assert receipt_owner.receipt.read_bytes(max_bytes=1) == b"x"
    finally:
        receipt_owner.close()

    assert receipt_owner.closed
    assert _descriptor_count() <= before


def test_publication_requires_precreated_owner_before_any_rename(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "object"
    publication = OwnedFilePublicationAuthority(destination, max_bytes=1)
    publication.write([b"x"])
    try:
        with pytest.raises(TypeError, match="receipt_owner"):
            publication.publish()
        assert publication.state == "written"
        assert not destination.exists()
    finally:
        publication.close()

    with pytest.raises(TypeError, match="exactly one"):
        publish_owned_file(tmp_path / "other", [b"x"], max_bytes=1)
    assert not (tmp_path / "other").exists()


def test_helper_consumer_gets_borrowed_receipt_and_closes_it_on_return(
    tmp_path: Path,
) -> None:
    observed: list[publication_module.PublishedFileReceipt] = []

    def consume(receipt: publication_module.PublishedFileReceipt) -> None:
        observed.append(receipt)
        assert receipt.read_bytes(max_bytes=1) == b"x"

    assert (
        publish_owned_file(
            tmp_path / "object",
            [b"x"],
            max_bytes=1,
            consume=consume,
        )
        is None
    )
    assert len(observed) == 1
    assert observed[0].closed


def test_helper_consumer_primary_wins_persistent_close_and_leaks_no_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _descriptor_count()
    primary = KeyboardInterrupt("consumer cancellation")
    cleanup = OSError(errno.EIO, "persistent helper receipt close failure")
    captured: list[publication_module.PublishedFileReceipt] = []
    captured_descriptors: list[int] = []
    real_close = publication_module.os.close

    def fail_receipt_close(candidate: int) -> None:
        if captured_descriptors and candidate == captured_descriptors[0]:
            raise cleanup
        real_close(candidate)

    def consume(receipt: publication_module.PublishedFileReceipt) -> None:
        captured.append(receipt)
        captured_descriptors.append(receipt._descriptor)
        raise primary

    monkeypatch.setattr(publication_module.os, "close", fail_receipt_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        publish_owned_file(
            tmp_path / "object",
            [b"x"],
            max_bytes=1,
            consume=consume,
        )

    assert caught.value is primary
    assert len(captured) == 1
    assert captured[0].closed
    assert any(
        "descriptor cleanup also failed" in note
        and "persistent helper receipt close failure" in note
        for note in _exception_notes(primary)
    )
    assert _descriptor_count() <= before


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_existing_open_cancellation_invalidates_owner_without_fd_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    before = _descriptor_count()
    destination = tmp_path / "object"
    destination.write_bytes(b"same")
    publication = OwnedFilePublicationAuthority(destination, max_bytes=4)
    publication.write([b"same"])
    interruption = exception_type("injected after existing descriptor ownership")
    real_open = publication_module._DescriptorOwner.open

    def open_then_interrupt(owner, path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(owner, path, flags, mode, dir_fd=dir_fd)
        if path == destination.name:
            raise interruption
        return descriptor

    monkeypatch.setattr(
        publication_module._DescriptorOwner,
        "open",
        open_then_interrupt,
    )
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(exception_type) as caught:
                publication.publish_into(receipt_owner)
            assert caught.value is interruption
        finally:
            publication.close()

    assert destination.read_bytes() == b"same"
    assert _descriptor_count() <= before


def test_receipt_constructor_cancellation_invalidates_every_fd_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _descriptor_count()
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    interruption = SystemExit("injected after receipt construction")
    real_receipt_type = publication_module.PublishedFileReceipt
    captured_receipts = []
    captured_descriptors: list[int] = []

    class InterruptingReceipt(real_receipt_type):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured_receipts.append(self)
            captured_descriptors.append(self._descriptor)
            raise interruption

    monkeypatch.setattr(
        publication_module,
        "PublishedFileReceipt",
        InterruptingReceipt,
    )
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(SystemExit) as caught:
                publication.publish_into(receipt_owner)
            assert caught.value is interruption
        finally:
            publication.close()

    assert len(captured_receipts) == 1
    assert captured_receipts[0].closed
    stale_descriptor = captured_descriptors[0]
    _assert_bad_descriptor(stale_descriptor)

    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    opened = os.open(foreign, os.O_RDONLY)
    replacement = stale_descriptor
    if opened != replacement:
        os.dup2(opened, replacement)
        os.close(opened)
    try:
        with pytest.raises(RuntimeError, match="ReceiptOwner"):
            captured_receipts[0].close()
        assert os.pread(replacement, 7, 0) == b"foreign"
    finally:
        os.close(replacement)
    assert _descriptor_count() <= before


@pytest.mark.parametrize("existing_mode", [0o600, 0o666])
def test_existing_mode_mismatch_or_unsafe_mode_is_a_conflict(
    tmp_path: Path,
    existing_mode: int,
) -> None:
    destination = tmp_path / "object"
    destination.write_bytes(b"same")
    destination.chmod(existing_mode)

    with PublishedFileReceiptOwner() as receipt_owner:
        with pytest.raises(OwnedFileConflictError):
            publish_owned_file(
                destination,
                b"same",
                max_bytes=4,
                mode=0o640,
                receipt_owner=receipt_owner,
            )

    assert stat.S_IMODE(destination.stat().st_mode) == existing_mode


def test_different_existing_file_is_a_non_destructive_conflict(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "object"
    destination.write_bytes(b"old")
    destination.chmod(0o644)

    with PublishedFileReceiptOwner() as receipt_owner:
        with pytest.raises(OwnedFileConflictError) as caught:
            publish_owned_file(
                destination,
                [b"new"],
                max_bytes=3,
                receipt_owner=receipt_owner,
            )

    assert destination.read_bytes() == b"old"
    assert caught.value.observed is not None
    assert caught.value.orphan.path.read_bytes() == b"new"


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory"])
def test_existing_non_regular_destination_is_a_conflict(
    tmp_path: Path,
    kind: str,
) -> None:
    destination = tmp_path / "object"
    if kind == "fifo":
        os.mkfifo(destination)
    elif kind == "symlink":
        destination.symlink_to("missing-target")
    else:
        destination.mkdir()

    with PublishedFileReceiptOwner() as receipt_owner:
        with pytest.raises(OwnedFileConflictError) as caught:
            publish_owned_file(
                destination,
                [b"new"],
                max_bytes=3,
                receipt_owner=receipt_owner,
            )

    assert destination.lstat()
    assert caught.value.observed is None
    assert caught.value.orphan.path.read_bytes() == b"new"


def test_destination_appearing_at_rename_is_never_replaced(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "object"
    publication = OwnedFilePublicationAuthority(destination, max_bytes=3)
    publication.write([b"new"])
    assert publication._authority is not None
    original = publication._authority._rename_callback

    def race(source: str, final: str) -> None:
        assert publication._authority is not None
        descriptor = os.open(
            final,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
            dir_fd=publication._authority.resource,
        )
        try:
            os.write(descriptor, b"old")
        finally:
            os.close(descriptor)
        original(source, final)

    publication._authority._rename_callback = race
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(OwnedFileConflictError):
                publication.publish_into(receipt_owner)
        finally:
            publication.close()

    assert destination.read_bytes() == b"old"


def test_stage_create_uses_exclusive_nofollow_cloexec_and_exact_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_flags: list[int] = []
    real_open = publication_module.os.open

    def record_open(path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, str) and path.startswith(".codenib-file-"):
            observed_flags.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(publication_module.os, "open", record_open)
    publication = OwnedFilePublicationAuthority(
        tmp_path / "object",
        max_bytes=1,
        mode=0o640,
    )
    try:
        metadata = os.fstat(publication._descriptor)
        assert stat.S_ISREG(metadata.st_mode)
        assert metadata.st_nlink == 1
        assert stat.S_IMODE(metadata.st_mode) == 0o640
        assert observed_flags
        assert observed_flags[0] & os.O_CREAT
        assert observed_flags[0] & os.O_EXCL
        assert observed_flags[0] & os.O_NOFOLLOW
        assert observed_flags[0] & os.O_CLOEXEC
    finally:
        publication.close()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_constructor_cancellation_after_authority_acquire_closes_all_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    before = _descriptor_count()
    interruption = exception_type("injected after authority acquisition")
    real_acquire = publication_module._PublicationAuthorityOwner.acquire

    def acquire_then_interrupt(owner, path: Path) -> None:
        real_acquire(owner, path)
        raise interruption

    monkeypatch.setattr(
        publication_module._PublicationAuthorityOwner,
        "acquire",
        acquire_then_interrupt,
    )

    with pytest.raises(exception_type) as caught:
        OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)

    assert caught.value is interruption
    assert _descriptor_count() <= before
    assert not list(tmp_path.glob(".codenib-file-*"))


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_borrowed_parent_authority_cancellation_closes_only_owned_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_resource = os.open(tmp_path, flags)
    before = _descriptor_count()
    expected_offset = _set_nonzero_directory_offset(parent_resource)
    interruption = exception_type("injected after borrowed authority acquisition")
    real_acquire = publication_module._PublicationAuthorityOwner.acquire

    def acquire_then_interrupt(
        owner,
        path: Path,
        *,
        parent_resource: int | None = None,
    ) -> None:
        real_acquire(owner, path, parent_resource=parent_resource)
        raise interruption

    monkeypatch.setattr(
        publication_module._PublicationAuthorityOwner,
        "acquire",
        acquire_then_interrupt,
    )
    try:
        with pytest.raises(exception_type) as caught:
            OwnedFilePublicationAuthority(
                tmp_path / "object",
                max_bytes=1,
                parent_resource=parent_resource,
            )

        assert caught.value is interruption
        os.fstat(parent_resource)
        assert os.lseek(parent_resource, 0, os.SEEK_CUR) == expected_offset
        assert _descriptor_count() <= before
        assert not list(tmp_path.glob(".codenib-file-*"))
    finally:
        os.close(parent_resource)


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_authority_factory_return_cancellation_uses_preinstalled_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    before = _descriptor_count()
    interruption = exception_type("injected before authority factory handoff")
    real_open = atomic_module._open_publication_authority
    installed_owners: list[publication_module._PublicationAuthorityOwner] = []

    def open_then_interrupt(*args, **kwargs):
        owner = kwargs.get("authority_owner")
        assert isinstance(owner, publication_module._PublicationAuthorityOwner)
        authority = real_open(*args, **kwargs)
        assert owner.authority is authority
        installed_owners.append(owner)
        raise interruption

    monkeypatch.setattr(
        atomic_module,
        "_open_publication_authority",
        open_then_interrupt,
    )

    with pytest.raises(exception_type) as caught:
        OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)

    assert caught.value is interruption
    assert installed_owners
    assert installed_owners[0].authority is None
    assert _descriptor_count() <= before
    assert not list(tmp_path.glob(".codenib-file-*"))


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_constructor_cancellation_after_stage_open_closes_all_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    before = _descriptor_count()
    interruption = exception_type("injected after stage descriptor ownership")
    real_open = publication_module._DescriptorOwner.open

    def open_then_interrupt(owner, path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(owner, path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, str) and path.startswith(".codenib-file-"):
            raise interruption
        return descriptor

    monkeypatch.setattr(
        publication_module._DescriptorOwner,
        "open",
        open_then_interrupt,
    )

    with pytest.raises(exception_type) as caught:
        OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)

    assert caught.value is interruption
    assert _descriptor_count() <= before
    stages = list(tmp_path.glob(".codenib-file-*"))
    assert len(stages) == 1
    assert stages[0].read_bytes() == b""


def test_stage_with_a_second_link_is_rejected(tmp_path: Path) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)

    class LinkingProducer:
        def __iter__(self):
            yield b"x"
            assert publication._authority is not None
            os.link(
                publication._stage_name,
                "foreign-link",
                src_dir_fd=publication._authority.resource,
                dst_dir_fd=publication._authority.resource,
                follow_symlinks=False,
            )

    try:
        with pytest.raises(RuntimeError, match="identity changed"):
            publication.write(LinkingProducer())
    finally:
        publication.close()

    assert not (tmp_path / "object").exists()
    assert (tmp_path / "foreign-link").read_bytes() == b"x"


def test_same_size_stage_mutation_is_caught_by_pre_publish_rehash(
    tmp_path: Path,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=3)
    publication.write([b"abc"])
    os.pwrite(publication._descriptor, b"x", 0)
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(RuntimeError, match="producer digest"):
                publication.publish_into(receipt_owner)
        finally:
            publication.close()

    assert not (tmp_path / "object").exists()


def test_same_size_mutation_after_rename_is_caught_before_receipt(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "object"
    publication = OwnedFilePublicationAuthority(destination, max_bytes=3)
    publication.write([b"abc"])
    assert publication._authority is not None
    original = publication._authority._rename_callback

    def rename_then_mutate(source: str, final: str) -> None:
        original(source, final)
        os.pwrite(publication._descriptor, b"x", 0)

    publication._authority._rename_callback = rename_then_mutate
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(RuntimeError, match="producer digest"):
                publication.publish_into(receipt_owner)
        finally:
            publication.close()

    assert destination.read_bytes() == b"xbc"


def test_first_receipt_is_duplicated_from_held_stage_not_reopened_by_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])

    def forbidden_reopen(*_args, **_kwargs):
        raise AssertionError("first publication reopened the final path")

    monkeypatch.setattr(
        OwnedFilePublicationAuthority,
        "_open_regular",
        forbidden_reopen,
    )
    with PublishedFileReceiptOwner() as receipt_owner:
        publication.publish_into(receipt_owner)
        receipt = receipt_owner.receipt
        assert receipt.read_bytes(max_bytes=1) == b"x"
    try:
        assert receipt_owner.closed
    finally:
        publication.close()


@pytest.mark.parametrize("swap", ["parent", "ancestor"])
def test_parent_path_aba_fails_before_publication(
    tmp_path: Path,
    swap: str,
) -> None:
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "parent"
    parent.mkdir(parents=True)
    publication = OwnedFilePublicationAuthority(parent / "object", max_bytes=1)
    publication.write([b"x"])

    if swap == "parent":
        moved = ancestor / "moved-parent"
        parent.rename(moved)
        parent.mkdir()
        replacement_parent = parent
    else:
        moved = tmp_path / "moved-ancestor"
        ancestor.rename(moved)
        ancestor.mkdir()
        replacement_parent = ancestor / "parent"
        replacement_parent.mkdir()

    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(RuntimeError, match="parent path changed"):
                publication.publish_into(receipt_owner)
        finally:
            publication.close()

    assert not (replacement_parent / "object").exists()


def test_producer_error_closes_iterator_and_preserves_owned_orphan(
    tmp_path: Path,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=2)

    class BrokenProducer:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"x"
            raise LookupError("producer failed")

        def close(self) -> None:
            self.closed = True

    producer = BrokenProducer()
    try:
        with pytest.raises(LookupError, match="producer failed"):
            publication.write(producer)
        assert producer.closed
        assert publication.state == "failed"
        assert publication.orphan is not None
        assert publication.orphan.path.read_bytes() == b"x"
    finally:
        publication.close()


def test_iterator_close_error_prevents_publication(tmp_path: Path) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)

    def fail_close() -> None:
        raise OSError(errno.EIO, "close failed")

    class CloseableIterator:
        def __init__(self) -> None:
            self.done = False

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            if self.done:
                raise StopIteration
            self.done = True
            return b"x"

        close = staticmethod(fail_close)

    try:
        with pytest.raises(OSError, match="close failed"):
            publication.write(CloseableIterator())
        assert publication.state == "failed"
        assert publication.orphan is not None
        assert publication.orphan.path.read_bytes() == b"x"
    finally:
        publication.close()


def test_iterator_primary_cancellation_wins_and_close_cancellation_is_noted(
    tmp_path: Path,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    primary = KeyboardInterrupt("producer interrupted")
    secondary = SystemExit("iterator close cancelled")

    class DoublyInterruptedIterator:
        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            raise primary

        def close(self) -> None:
            raise secondary

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            publication.write(DoublyInterruptedIterator())
        assert caught.value is primary
        assert any(
            "publication iterator cleanup also failed" in note
            and "iterator close cancelled" in note
            for note in _exception_notes(primary)
        )
        assert publication.state == "failed"
    finally:
        publication.close()


def test_file_fsync_error_leaves_only_an_owned_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    stage_descriptor = publication._descriptor
    real_fsync = publication_module.os.fsync

    def fail_file_fsync(descriptor: int) -> None:
        if descriptor == stage_descriptor:
            raise OSError(errno.EIO, "file fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(publication_module.os, "fsync", fail_file_fsync)
    try:
        with pytest.raises(OSError, match="file fsync failed"):
            publication.write([b"x"])
        assert publication.orphan is not None
        assert not (tmp_path / "object").exists()
    finally:
        publication.close()


def test_parent_fsync_error_returns_no_receipt_after_committed_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    assert publication._authority is not None
    parent_descriptor = publication._authority.resource
    real_fsync = publication_module.os.fsync

    def fail_parent_fsync(descriptor: int) -> None:
        if descriptor == parent_descriptor:
            raise OSError(errno.EIO, "parent fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(publication_module.os, "fsync", fail_parent_fsync)
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(OSError, match="parent fsync failed"):
                publication.publish_into(receipt_owner)
            assert publication.state == "failed"
        finally:
            publication.close()

    assert (tmp_path / "object").read_bytes() == b"x"


def test_rename_error_before_mutation_preserves_stage(
    tmp_path: Path,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    assert publication._authority is not None

    def fail_rename(_source: str, _destination: str) -> None:
        raise OSError(errno.EIO, "rename failed")

    publication._authority._rename_callback = fail_rename
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(OSError, match="rename failed"):
                publication.publish_into(receipt_owner)
            assert publication.orphan is not None
            assert publication.orphan.path.read_bytes() == b"x"
        finally:
            publication.close()


def test_rename_error_after_mutation_returns_no_receipt_and_retry_reuses(
    tmp_path: Path,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    assert publication._authority is not None
    original = publication._authority._rename_callback

    def rename_then_fail(source: str, destination: str) -> None:
        original(source, destination)
        raise OSError(errno.EIO, "injected after rename")

    publication._authority._rename_callback = rename_then_fail
    with PublishedFileReceiptOwner() as failed_owner:
        try:
            with pytest.raises(OSError, match="injected after rename"):
                publication.publish_into(failed_owner)
            assert publication.orphan is None
        finally:
            publication.close()

    with PublishedFileReceiptOwner() as receipt_owner:
        publish_owned_file(
            tmp_path / "object",
            [b"x"],
            max_bytes=1,
            receipt_owner=receipt_owner,
        )
        receipt = receipt_owner.receipt
        assert receipt.reused
        assert receipt.read_bytes(max_bytes=1) == b"x"


def test_rename_reconciliation_cancellation_is_secondary_and_conservative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    assert publication._authority is not None
    primary = KeyboardInterrupt("rename interrupted")
    secondary = SystemExit("rename reconciliation cancelled")
    orphan = publication.orphan
    assert orphan is not None
    stage_path = orphan.path

    def interrupt_rename(_source: str, _destination: str) -> None:
        raise primary

    def interrupt_reconciliation() -> bool:
        raise secondary

    publication._authority._rename_callback = interrupt_rename
    monkeypatch.setattr(
        OwnedFilePublicationAuthority,
        "_rename_committed",
        lambda _publication: interrupt_reconciliation(),
    )
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                publication.publish_into(receipt_owner)
            assert caught.value is primary
            assert any(
                "rename outcome reconciliation also failed" in note
                and "rename reconciliation cancelled" in note
                for note in _exception_notes(primary)
            )
            # A cancelled reconciliation cannot safely advertise either
            # namespace name as an owned orphan, even though this injected
            # pre-rename case leaves the diagnostic path visible.
            assert publication.orphan is None
            assert stage_path.read_bytes() == b"x"
        finally:
            publication.close()


def test_stage_close_error_prevents_a_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    stage_descriptor = publication._descriptor
    real_close = publication_module.os.close

    def fail_stage_close(descriptor: int) -> None:
        if descriptor == stage_descriptor:
            raise OSError(errno.EIO, "stage close failed")
        real_close(descriptor)

    monkeypatch.setattr(publication_module.os, "close", fail_stage_close)
    with PublishedFileReceiptOwner() as receipt_owner:
        try:
            with pytest.raises(OSError, match="stage close failed"):
                publication.publish_into(receipt_owner)
            with pytest.raises(OSError) as caught:
                os.fstat(stage_descriptor)
            assert caught.value.errno == errno.EBADF
        finally:
            monkeypatch.setattr(publication_module.os, "close", real_close)
            publication.close()


@pytest.mark.parametrize(
    ("phase", "interruption"),
    [
        pytest.param(
            "before",
            OSError(errno.EIO, "before close failure"),
            id="oserror-before",
        ),
        pytest.param(
            "after",
            OSError(errno.EIO, "after close failure"),
            id="oserror-after",
        ),
        pytest.param(
            "before",
            KeyboardInterrupt("before close interrupt"),
            id="keyboardinterrupt-before",
        ),
        pytest.param(
            "after",
            SystemExit("after close cancellation"),
            id="systemexit-after",
        ),
    ],
)
def test_receipt_owner_close_reconciles_before_and_after_real_close_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    interruption: BaseException,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    receipt = receipt_owner.receipt
    descriptor = receipt._descriptor
    real_close = publication_module.os.close

    def injected_close(candidate: int) -> None:
        if candidate != descriptor:
            real_close(candidate)
            return
        if phase == "after":
            real_close(candidate)
        raise interruption

    monkeypatch.setattr(publication_module.os, "close", injected_close)
    with pytest.raises(type(interruption)) as caught:
        receipt_owner.close()

    assert caught.value is interruption
    if phase == "before":
        assert receipt_owner.active
        assert not receipt.closed
        assert os.fstat(descriptor)
        monkeypatch.setattr(publication_module.os, "close", real_close)
        receipt_owner.close()
    else:
        assert receipt_owner.closed
    assert receipt.closed
    _assert_bad_descriptor(descriptor)


def test_receipt_owner_close_interrupted_while_arming_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    receipt = receipt_owner.receipt
    descriptor = receipt._descriptor
    interruption = SystemExit("injected during close ownership arming")
    real_arm = publication_module._arm_descriptor_for_close
    injected = False

    def interrupt_first_arm(candidate: int, **kwargs) -> int:
        nonlocal injected
        if candidate == descriptor and not injected:
            injected = True
            raise interruption
        return real_arm(candidate, **kwargs)

    monkeypatch.setattr(
        publication_module,
        "_arm_descriptor_for_close",
        interrupt_first_arm,
    )
    with pytest.raises(SystemExit) as caught:
        receipt_owner.close()

    assert caught.value is interruption
    assert injected
    assert receipt_owner.active
    assert not receipt.closed
    monkeypatch.setattr(
        publication_module,
        "_arm_descriptor_for_close",
        real_arm,
    )
    receipt_owner.close()
    assert receipt_owner.closed
    _assert_bad_descriptor(descriptor)


@pytest.mark.parametrize("replacement_kind", ["different-inode", "same-inode-new-ofd"])
def test_receipt_close_preserves_observable_fd_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    destination = tmp_path / "object"
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        destination,
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    receipt = receipt_owner.receipt
    descriptor = receipt._descriptor
    replacement_path = destination
    if replacement_kind == "different-inode":
        replacement_path = tmp_path / "foreign"
        replacement_path.write_bytes(b"foreign")
    expected = replacement_path.read_bytes()
    real_close = publication_module.os.close
    real_open = publication_module.os.open
    interruption = SystemExit("injected after close and observable fd reuse")

    def close_then_reuse(candidate: int) -> None:
        if candidate != descriptor:
            real_close(candidate)
            return
        real_close(candidate)
        opened = real_open(replacement_path, os.O_RDONLY)
        if opened != candidate:
            os.dup2(opened, candidate)
            real_close(opened)
        raise interruption

    monkeypatch.setattr(publication_module.os, "close", close_then_reuse)
    try:
        with pytest.raises(SystemExit) as caught:
            receipt_owner.close()
        assert caught.value is interruption
        assert receipt_owner.closed
        assert receipt.closed
        assert os.pread(descriptor, len(expected), 0) == expected

        # The invalidated owner must not touch the replacement on a retry.
        receipt_owner.close()
        assert os.pread(descriptor, len(expected), 0) == expected
    finally:
        real_close(descriptor)


def test_exact_same_ofd_aba_is_retained_but_foreign_reuse_is_not_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    receipt = receipt_owner.receipt
    descriptor = receipt._descriptor
    alias = os.dup(descriptor)
    real_close = publication_module.os.close
    interruption = KeyboardInterrupt("injected exact same-OFD dup2 ABA")

    def close_then_restore_exact_alias(candidate: int) -> None:
        if candidate != descriptor:
            real_close(candidate)
            return
        real_close(candidate)
        os.dup2(alias, candidate)
        raise interruption

    monkeypatch.setattr(
        publication_module.os,
        "close",
        close_then_restore_exact_alias,
    )
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            receipt_owner.close()
        assert caught.value is interruption
        assert receipt_owner.active
        assert not receipt.closed

        # Identity plus the shared offset cookie cannot distinguish this ABA,
        # so the retryable owner conservatively retains it.  A later observable
        # reuse is rejected before another close syscall touches that number.
        assert os.fstat(descriptor)
        assert os.fstat(alias)

        monkeypatch.setattr(publication_module.os, "close", real_close)
        foreign = tmp_path / "foreign-after-aba"
        foreign.write_bytes(b"foreign")
        opened = os.open(foreign, os.O_RDONLY)
        if opened != descriptor:
            os.dup2(opened, descriptor)
            real_close(opened)
        with pytest.raises(RuntimeError, match="changed before close retry"):
            receipt_owner.close()
        assert receipt_owner.closed
        assert os.pread(descriptor, 7, 0) == b"foreign"
        real_close(descriptor)
    finally:
        real_close(alias)


def test_receipt_owner_persistent_close_eio_retains_owner_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    descriptor = receipt_owner.receipt._descriptor
    real_close = publication_module.os.close
    error = OSError(errno.EIO, "persistent receipt close failure")
    close_calls = 0

    def fail_receipt_close(candidate: int) -> None:
        nonlocal close_calls
        if candidate == descriptor:
            close_calls += 1
            raise error
        real_close(candidate)

    try:
        monkeypatch.setattr(publication_module.os, "close", fail_receipt_close)
        for _attempt in range(2):
            with pytest.raises(OSError) as caught:
                receipt_owner.close()
            assert caught.value is error
            assert receipt_owner.active
            assert os.fstat(descriptor)
    finally:
        monkeypatch.setattr(publication_module.os, "close", real_close)
        receipt_owner.close()

    assert close_calls == 2
    assert receipt_owner.closed
    _assert_bad_descriptor(descriptor)


def test_receipt_owner_close_after_error_preserves_primary_and_retry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    descriptor = receipt_owner.receipt._descriptor
    real_close = publication_module.os.close
    primary = KeyboardInterrupt("primary cancellation")
    cleanup = OSError(errno.EIO, "persistent receipt cleanup failure")

    def fail_receipt_close(candidate: int) -> None:
        if candidate == descriptor:
            raise cleanup
        real_close(candidate)

    try:
        monkeypatch.setattr(publication_module.os, "close", fail_receipt_close)
        with pytest.raises(KeyboardInterrupt) as caught:
            try:
                raise primary
            except BaseException as body_error:
                receipt_owner.close_after_error(body_error)
                raise
        assert caught.value is primary
        assert any(
            "published receipt owner cleanup also failed" in note
            and "persistent receipt cleanup failure" in note
            for note in _exception_notes(primary)
        )
        assert receipt_owner.active
        assert os.fstat(descriptor)
    finally:
        monkeypatch.setattr(publication_module.os, "close", real_close)
        receipt_owner.close()

    assert receipt_owner.closed
    _assert_bad_descriptor(descriptor)


def test_publish_error_after_install_preserves_primary_and_installed_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "object"
    publication = OwnedFilePublicationAuthority(destination, max_bytes=1)
    publication.write([b"x"])
    receipt_owner = PublishedFileReceiptOwner()
    primary = SystemExit("injected after receipt owner install")
    cleanup = OSError(errno.EIO, "installed receipt close failure")
    installed_descriptor = -1
    real_install = PublishedFileReceiptOwner._install
    real_close = publication_module.os.close

    def install_then_interrupt(owner, reservation, receipt) -> None:
        nonlocal installed_descriptor
        real_install(owner, reservation, receipt)
        installed_descriptor = receipt._descriptor
        raise primary

    def fail_installed_close(candidate: int) -> None:
        if candidate == installed_descriptor:
            raise cleanup
        real_close(candidate)

    monkeypatch.setattr(PublishedFileReceiptOwner, "_install", install_then_interrupt)
    monkeypatch.setattr(publication_module.os, "close", fail_installed_close)
    try:
        with pytest.raises(SystemExit) as caught:
            publication.publish_into(receipt_owner)
        assert caught.value is primary
        assert installed_descriptor >= 0
        assert receipt_owner.active
        assert os.fstat(installed_descriptor)
        assert any(
            "published receipt owner cleanup also failed" in note
            and "installed receipt close failure" in note
            for note in _exception_notes(primary)
        )
    finally:
        monkeypatch.setattr(publication_module.os, "close", real_close)
        receipt_owner.close()
        publication.close()

    assert destination.read_bytes() == b"x"
    assert receipt_owner.closed
    _assert_bad_descriptor(installed_descriptor)


def test_receipt_install_reentry_is_rejected_without_double_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    receipt_owner = PublishedFileReceiptOwner()
    real_install = PublishedFileReceiptOwner._install
    reentry_errors: list[BaseException] = []

    def install_with_reentry(owner, reservation, receipt) -> None:
        try:
            publication.publish_into(owner)
        except RuntimeError as error:
            reentry_errors.append(error)
        real_install(owner, reservation, receipt)

    monkeypatch.setattr(PublishedFileReceiptOwner, "_install", install_with_reentry)
    try:
        publication.publish_into(receipt_owner)
        assert len(reentry_errors) == 1
        assert isinstance(reentry_errors[0], RuntimeError)
        assert "publishing, expected written" in str(reentry_errors[0])
        assert receipt_owner.receipt.read_bytes(max_bytes=1) == b"x"
    finally:
        receipt_owner.close()
        publication.close()

    assert receipt_owner.closed


def test_owner_lifecycle_lock_recovers_acquire_success_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    real_acquire = publication_module._CancellationSafeRLock._acquire
    interruption = KeyboardInterrupt("after lifecycle lock acquire")
    injected = False

    def acquire_then_interrupt(lock) -> None:
        nonlocal injected
        real_acquire(lock)
        if lock is receipt_owner._lock and not injected:
            injected = True
            raise interruption

    monkeypatch.setattr(
        publication_module._CancellationSafeRLock,
        "_acquire",
        acquire_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        _ = receipt_owner.state
    assert caught.value is interruption
    assert injected

    monkeypatch.setattr(
        publication_module._CancellationSafeRLock,
        "_acquire",
        real_acquire,
    )
    observed: list[str] = []
    observer = threading.Thread(target=lambda: observed.append(receipt_owner.state))
    observer.start()
    observer.join(timeout=2)
    assert not observer.is_alive()
    assert observed == ["empty"]


def test_owner_lifecycle_lock_release_cancellation_keeps_closed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    descriptor = receipt_owner.receipt._descriptor
    real_release = publication_module._CancellationSafeRLock._release
    interruption = SystemExit("after lifecycle lock release")
    injected = False

    def release_then_interrupt(lock) -> None:
        nonlocal injected
        real_release(lock)
        if lock is receipt_owner._lock and not injected:
            injected = True
            raise interruption

    monkeypatch.setattr(
        publication_module._CancellationSafeRLock,
        "_release",
        release_then_interrupt,
    )
    with pytest.raises(SystemExit) as caught:
        receipt_owner.close()
    assert caught.value is interruption
    assert injected

    monkeypatch.setattr(
        publication_module._CancellationSafeRLock,
        "_release",
        real_release,
    )
    assert receipt_owner.closed
    _assert_bad_descriptor(descriptor)
    receipt_owner.close()


def test_owner_lifecycle_release_failure_is_secondary_to_callback_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    real_release = publication_module._CancellationSafeRLock._release
    primary = KeyboardInterrupt("lifecycle callback primary")
    secondary = SystemExit("before lifecycle lock release")
    injected = False

    def release_interrupt_once(lock) -> None:
        nonlocal injected
        if lock is receipt_owner._lock and not injected:
            injected = True
            raise secondary
        real_release(lock)

    def fail_callback() -> None:
        raise primary

    monkeypatch.setattr(
        publication_module._CancellationSafeRLock,
        "_release",
        release_interrupt_once,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        receipt_owner._lock.run(fail_callback)
    assert caught.value is primary
    assert injected
    assert any(
        "receipt lifecycle lock release was interrupted" in note
        and "before lifecycle lock release" in note
        for note in _exception_notes(primary)
    )

    monkeypatch.setattr(
        publication_module._CancellationSafeRLock,
        "_release",
        real_release,
    )
    observed: list[str] = []
    observer = threading.Thread(target=lambda: observed.append(receipt_owner.state))
    observer.start()
    observer.join(timeout=2)
    assert not observer.is_alive()
    assert observed == ["empty"]


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_consume_return_cleanup_cancellation_does_not_strand_reentrancy_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    descriptor = receipt_owner.receipt._descriptor
    callback_returned = False
    injected = False
    interruption = exception_type("after consume callback return")
    real_normalize = PublishedFileReceiptOwner._normalize_closed_locked

    def normalize_after_callback(owner) -> None:
        nonlocal injected
        if owner is receipt_owner and callback_returned and not injected:
            injected = True
            raise interruption
        real_normalize(owner)

    def consume(receipt: publication_module.PublishedFileReceipt) -> None:
        nonlocal callback_returned
        assert receipt.read_bytes(max_bytes=1) == b"x"
        callback_returned = True

    monkeypatch.setattr(
        PublishedFileReceiptOwner,
        "_normalize_closed_locked",
        normalize_after_callback,
    )
    with pytest.raises(exception_type) as caught:
        receipt_owner.consume(consume)
    assert caught.value is interruption
    assert callback_returned
    assert injected

    monkeypatch.setattr(
        PublishedFileReceiptOwner,
        "_normalize_closed_locked",
        real_normalize,
    )
    receipt_owner.close()
    assert receipt_owner.closed
    _assert_bad_descriptor(descriptor)


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_persistent_close_error_wins_state_cleanup_cancellation_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    receipt = receipt_owner.receipt
    descriptor = receipt._descriptor
    real_close = publication_module.os.close
    real_closed = publication_module.PublishedFileReceipt.closed.fget
    assert real_closed is not None
    close_error = OSError(errno.EIO, "persistent close before real syscall")
    interruption = exception_type("receipt close-state observation cancelled")
    close_attempted = False
    injected = False

    def fail_close(candidate: int) -> None:
        nonlocal close_attempted
        if candidate == descriptor:
            close_attempted = True
            raise close_error
        real_close(candidate)

    def interrupt_closed(candidate) -> bool:
        nonlocal injected
        if candidate is receipt and close_attempted and not injected:
            injected = True
            raise interruption
        return real_closed(candidate)

    monkeypatch.setattr(publication_module.os, "close", fail_close)
    monkeypatch.setattr(
        publication_module.PublishedFileReceipt,
        "closed",
        property(interrupt_closed),
    )
    with pytest.raises(OSError) as caught:
        receipt_owner.close()

    assert caught.value is close_error
    assert close_attempted
    assert injected
    assert any(
        "receipt owner close-state cleanup also failed" in note
        and "close-state observation cancelled" in note
        for note in _exception_notes(close_error)
    )
    assert receipt_owner.active
    assert os.fstat(descriptor)

    monkeypatch.setattr(publication_module.os, "close", real_close)
    monkeypatch.setattr(
        publication_module.PublishedFileReceipt,
        "closed",
        property(real_closed),
    )
    receipt_owner.close()
    assert receipt_owner.closed
    _assert_bad_descriptor(descriptor)


@pytest.mark.parametrize("phase", ["before", "after"])
def test_after_real_close_state_transition_cancellation_converges_by_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    descriptor = receipt_owner.receipt._descriptor
    interruption = SystemExit(f"{phase} receipt close-state transition")
    real_settle = PublishedFileReceiptOwner._settle_receipt_locked
    injected = False

    def settle_then_interrupt(owner, receipt):
        nonlocal injected
        if owner is receipt_owner and not injected:
            injected = True
            if phase == "after":
                real_settle(owner, receipt)
            raise interruption
        return real_settle(owner, receipt)

    monkeypatch.setattr(
        PublishedFileReceiptOwner,
        "_settle_receipt_locked",
        settle_then_interrupt,
    )
    with pytest.raises(SystemExit) as caught:
        receipt_owner.close()

    assert caught.value is interruption
    assert injected
    _assert_bad_descriptor(descriptor)
    assert receipt_owner.closed
    receipt_owner.close()


@pytest.mark.parametrize(
    ("phase", "exception_type"),
    [
        pytest.param("before", KeyboardInterrupt, id="before-keyboardinterrupt"),
        pytest.param("after", SystemExit, id="after-systemexit"),
    ],
)
def test_publication_primary_wins_cancel_reservation_transition_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    exception_type: type[BaseException],
) -> None:
    destination = tmp_path / "object"
    publication = OwnedFilePublicationAuthority(destination, max_bytes=1)
    publication.write([b"x"])
    receipt_owner = PublishedFileReceiptOwner()
    primary = LookupError("publication authentication primary")
    interruption = exception_type(f"{phase} terminal reservation slot store")
    real_bind = OwnedFilePublicationAuthority._bind_stage
    real_cancel = PublishedFileReceiptOwner._cancel_reservation_locked
    injected = False

    def fail_publish_bind(candidate, record) -> None:
        if candidate is publication:
            raise primary
        real_bind(candidate, record)

    def cancel_then_interrupt(owner, reservation) -> bool:
        nonlocal injected
        if owner is receipt_owner and not injected:
            injected = True
            if phase == "after":
                real_cancel(owner, reservation)
            raise interruption
        return real_cancel(owner, reservation)

    monkeypatch.setattr(OwnedFilePublicationAuthority, "_bind_stage", fail_publish_bind)
    monkeypatch.setattr(
        PublishedFileReceiptOwner,
        "_cancel_reservation_locked",
        cancel_then_interrupt,
    )
    try:
        with pytest.raises(LookupError) as caught:
            publication.publish_into(receipt_owner)
        assert caught.value is primary
        assert injected
        assert any(
            "receipt reservation cleanup also failed" in note
            and "terminal reservation slot store" in note
            for note in _exception_notes(primary)
        )
        assert receipt_owner.closed
        receipt_owner.close()
        assert not destination.exists()
    finally:
        publication.close()


def test_helper_consumer_primary_wins_terminal_close_cancellation_and_no_fd_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _descriptor_count()
    primary = KeyboardInterrupt("helper consumer primary")
    interruption = SystemExit("before terminal descriptor close")
    captured: list[publication_module.PublishedFileReceipt] = []
    descriptors: list[int] = []
    real_close = publication_module.os.close
    injected = False
    close_calls = 0

    def interrupt_first_close(candidate: int) -> None:
        nonlocal close_calls, injected
        if descriptors and candidate == descriptors[0]:
            close_calls += 1
            if not injected:
                injected = True
                raise interruption
        real_close(candidate)

    def consume(receipt: publication_module.PublishedFileReceipt) -> None:
        captured.append(receipt)
        descriptors.append(receipt._descriptor)
        raise primary

    monkeypatch.setattr(publication_module.os, "close", interrupt_first_close)
    with pytest.raises(KeyboardInterrupt) as caught:
        publish_owned_file(
            tmp_path / "object",
            [b"x"],
            max_bytes=1,
            consume=consume,
        )

    assert caught.value is primary
    assert injected
    assert close_calls == 2
    assert len(captured) == 1
    assert captured[0].closed
    _assert_bad_descriptor(descriptors[0])
    assert any(
        "descriptor terminal cleanup was interrupted" in note
        and "before terminal descriptor close" in note
        for note in _exception_notes(primary)
    )
    assert _descriptor_count() <= before


def test_concurrent_owner_close_calls_are_linear_after_one_close_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    descriptor = receipt_owner.receipt._descriptor
    real_close = publication_module.os.close
    interruption = OSError(errno.EIO, "first concurrent close failure")
    start = threading.Barrier(3)
    outcomes: list[BaseException | None] = []
    close_calls = 0

    def fail_first_close(candidate: int) -> None:
        nonlocal close_calls
        if candidate == descriptor:
            close_calls += 1
            if close_calls == 1:
                raise interruption
        real_close(candidate)

    def close() -> None:
        start.wait()
        try:
            receipt_owner.close()
        except BaseException as error:  # noqa: B036 - report thread outcome
            outcomes.append(error)
        else:
            outcomes.append(None)

    monkeypatch.setattr(publication_module.os, "close", fail_first_close)
    workers = [threading.Thread(target=close) for _index in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=3)
        assert not worker.is_alive()

    assert len(outcomes) == 2
    assert sum(error is interruption for error in outcomes) == 1
    assert sum(error is None for error in outcomes) == 1
    assert close_calls == 2
    assert receipt_owner.closed
    _assert_bad_descriptor(descriptor)


def test_one_owner_concurrent_reservations_allow_exactly_one_publication(
    tmp_path: Path,
) -> None:
    before = _descriptor_count()
    receipt_owner = PublishedFileReceiptOwner()
    publications = [
        OwnedFilePublicationAuthority(tmp_path / f"object-{index}", max_bytes=1)
        for index in range(2)
    ]
    for index, publication in enumerate(publications):
        publication.write([bytes([ord("a") + index])])
    start = threading.Barrier(3)
    outcomes: list[tuple[int, str, BaseException | None]] = []

    def publish(index: int) -> None:
        publication = publications[index]
        start.wait()
        try:
            publication.publish_into(receipt_owner)
        except RuntimeError as error:
            outcomes.append((index, "error", error))
        else:
            outcomes.append((index, "published", None))
        finally:
            publication.close()

    workers = [threading.Thread(target=publish, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    published = [entry for entry in outcomes if entry[1] == "published"]
    rejected = [entry for entry in outcomes if entry[1] == "error"]
    assert len(published) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0][2], RuntimeError)
    assert receipt_owner.active
    winning_index = published[0][0]
    assert receipt_owner.receipt.path == tmp_path / f"object-{winning_index}"
    assert sum((tmp_path / f"object-{index}").exists() for index in range(2)) == 1

    receipt_owner.close()
    assert receipt_owner.closed
    assert _descriptor_count() <= before


def test_owner_close_cannot_cancel_a_reserved_publication_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "object"
    publication = OwnedFilePublicationAuthority(destination, max_bytes=1)
    publication.write([b"x"])
    receipt_owner = PublishedFileReceiptOwner()
    reserved = threading.Event()
    continue_publication = threading.Event()
    errors: list[Exception] = []
    real_reserve = PublishedFileReceiptOwner._reserve

    def reserve_then_wait(owner, reservation) -> None:
        real_reserve(owner, reservation)
        if owner is receipt_owner:
            reserved.set()
            assert continue_publication.wait(timeout=2)

    def publish() -> None:
        try:
            publication.publish_into(receipt_owner)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(PublishedFileReceiptOwner, "_reserve", reserve_then_wait)
    worker = threading.Thread(target=publish)
    worker.start()
    assert reserved.wait(timeout=2)
    assert receipt_owner.state == "reserved"
    assert not destination.exists()
    with pytest.raises(RuntimeError, match="publication is in progress"):
        receipt_owner.close()
    assert receipt_owner.state == "reserved"
    assert not destination.exists()

    continue_publication.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    try:
        assert errors == []
        assert receipt_owner.active
        assert destination.read_bytes() == b"x"
    finally:
        receipt_owner.close()
        publication.close()


def test_owner_consume_and_close_are_linear_across_threads(tmp_path: Path) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"x"],
        max_bytes=1,
        receipt_owner=receipt_owner,
    )
    descriptor = receipt_owner.receipt._descriptor
    consume_started = threading.Event()
    allow_consume_return = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()
    consumed: list[bytes] = []
    errors: list[BaseException] = []

    def consume(receipt: publication_module.PublishedFileReceipt) -> None:
        try:
            consume_started.set()
            consumed.append(receipt.read_bytes(max_bytes=1))
            assert allow_consume_return.wait(timeout=2)
            consumed.append(receipt.read_bytes(max_bytes=1))
        except Exception as error:
            errors.append(error)

    def close() -> None:
        try:
            close_started.set()
            receipt_owner.close()
        except Exception as error:
            errors.append(error)
        finally:
            close_finished.set()

    consumer = threading.Thread(target=lambda: receipt_owner.consume(consume))
    consumer.start()
    assert consume_started.wait(timeout=2)
    closer = threading.Thread(target=close)
    closer.start()
    assert close_started.wait(timeout=2)
    assert not close_finished.wait(timeout=0.05)
    allow_consume_return.set()
    consumer.join(timeout=2)
    closer.join(timeout=2)

    assert not consumer.is_alive()
    assert not closer.is_alive()
    assert errors == []
    assert consumed == [b"x", b"x"]
    assert receipt_owner.closed
    _assert_bad_descriptor(descriptor)


@pytest.mark.parametrize(
    ("phase", "exception_type"),
    [
        pytest.param("before", KeyboardInterrupt, id="keyboardinterrupt-before"),
        pytest.param("after", SystemExit, id="systemexit-after"),
    ],
)
def test_parent_authority_close_defers_interrupt_and_closes_all_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    exception_type: type[BaseException],
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    assert publication._authority is not None
    parent_descriptor = publication._authority.resource
    real_close = publication_module.os.close
    interruption = exception_type(f"{phase} parent close interrupt")

    def interrupt_parent_close(descriptor: int) -> None:
        if descriptor != parent_descriptor:
            real_close(descriptor)
            return
        if phase == "after":
            real_close(descriptor)
        raise interruption

    monkeypatch.setattr(publication_module.os, "close", interrupt_parent_close)
    with PublishedFileReceiptOwner() as receipt_owner:
        with pytest.raises(exception_type) as caught:
            publication.publish_into(receipt_owner)

        assert caught.value is interruption
        _assert_bad_descriptor(parent_descriptor)
        publication.close()


def test_body_error_wins_while_close_visits_stage_and_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _descriptor_count()
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    stage_descriptor = publication._descriptor
    assert publication._authority is not None
    parent_descriptor = publication._authority.resource
    real_close = publication_module.os.close
    stage_interruption = KeyboardInterrupt("stage cleanup interrupted")
    parent_cancellation = SystemExit("parent cleanup cancelled")
    stage_injected = False
    parent_injected = False

    def interrupt_two_closes(descriptor: int) -> None:
        nonlocal stage_injected, parent_injected
        if descriptor == stage_descriptor and not stage_injected:
            stage_injected = True
            raise stage_interruption
        if descriptor == parent_descriptor and not parent_injected:
            parent_injected = True
            real_close(descriptor)
            raise parent_cancellation
        real_close(descriptor)

    monkeypatch.setattr(publication_module.os, "close", interrupt_two_closes)

    class BrokenProducer:
        def __iter__(self):
            raise LookupError("primary producer failure")

    with pytest.raises(LookupError, match="primary producer failure") as caught:
        with publication:
            publication.write(BrokenProducer())

    assert stage_injected
    assert parent_injected
    assert publication.state == "closed"
    _assert_bad_descriptor(stage_descriptor)
    _assert_bad_descriptor(parent_descriptor)
    assert any(
        "publication cleanup also failed" in note
        and "stage cleanup interrupted" in note
        for note in _exception_notes(caught.value)
    )
    assert _descriptor_count() <= before


def test_linear_state_and_pid_binding(tmp_path: Path, monkeypatch) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    publication.write([b"x"])
    receipt_owner = PublishedFileReceiptOwner()
    with pytest.raises(RuntimeError, match="expected staged"):
        publication.write([b"x"])

    owner_pid = os.getpid()
    monkeypatch.setattr(publication_module.os, "getpid", lambda: owner_pid + 1)
    with pytest.raises(RuntimeError, match="PID boundary"):
        publication.publish_into(receipt_owner)
    with pytest.raises(RuntimeError, match="PID boundary"):
        publication.close()
    with pytest.raises(RuntimeError, match="PID boundary"):
        _ = receipt_owner.state
    with pytest.raises(RuntimeError, match="PID boundary"):
        receipt_owner.close()

    monkeypatch.setattr(publication_module.os, "getpid", lambda: owner_pid)
    assert receipt_owner.closed


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_receipt_child_close_uses_process_local_lifecycle_lock(
    tmp_path: Path,
) -> None:
    receipt_owner = PublishedFileReceiptOwner()
    publish_owned_file(
        tmp_path / "object",
        [b"payload"],
        max_bytes=7,
        receipt_owner=receipt_owner,
    )
    receipt_descriptor = receipt_owner.receipt._descriptor
    consuming = threading.Event()
    release = threading.Event()

    def hold_receipt() -> None:
        receipt_owner.consume(
            lambda _receipt: (
                consuming.set(),
                release.wait(timeout=10),
            )
        )

    holder = threading.Thread(target=hold_receipt)
    holder.start()
    assert consuming.wait(timeout=5)

    child = os.fork()
    if child == 0:  # pragma: no branch - child reports exact outcome by status
        signal.signal(signal.SIGALRM, lambda *_args: os._exit(70))
        signal.alarm(3)
        try:
            receipt_owner.close()
        except RuntimeError as exc:
            if "PID boundary" not in str(exc):
                os._exit(71)
        except BaseException:  # noqa: B036 - child reports by status
            os._exit(72)
        else:
            os._exit(73)
        try:
            os.fstat(receipt_descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                os._exit(74)
        else:
            os._exit(75)
        os._exit(0)

    _pid, status = os.waitpid(child, 0)
    release.set()
    holder.join(timeout=5)
    assert not holder.is_alive()
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert receipt_owner.receipt.read_bytes(max_bytes=7) == b"payload"
    receipt_owner.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_publication_child_close_does_not_seek_parent_stage(
    tmp_path: Path,
) -> None:
    publication = OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=6)
    stage_descriptor = publication._descriptor

    def chunks():
        yield b"abc"
        assert os.lseek(stage_descriptor, 0, os.SEEK_CUR) == 3
        child = os.fork()
        if child == 0:  # pragma: no branch - child reports exact outcome by status
            signal.signal(signal.SIGALRM, lambda *_args: os._exit(80))
            signal.alarm(3)
            try:
                publication.close()
            except RuntimeError as exc:
                if "PID boundary" not in str(exc):
                    os._exit(81)
            except BaseException:  # noqa: B036 - child reports by status
                os._exit(82)
            else:
                os._exit(83)
            try:
                os.fstat(stage_descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    os._exit(84)
            else:
                os._exit(85)
            os._exit(0)
        _pid, status = os.waitpid(child, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
        assert os.lseek(stage_descriptor, 0, os.SEEK_CUR) == 3
        yield b"def"

    record = publication.write(chunks())
    assert record.size == 6
    receipt_owner = PublishedFileReceiptOwner()
    publication.publish_into(receipt_owner)
    assert receipt_owner.receipt.read_bytes(max_bytes=6) == b"abcdef"
    receipt_owner.close()


def test_missing_parent_and_noncanonical_leaf_do_not_create_paths(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        OwnedFilePublicationAuthority(missing / "object", max_bytes=1)
    assert not missing.exists()

    with pytest.raises(ValueError, match="safe final component"):
        OwnedFilePublicationAuthority(tmp_path / "..", max_bytes=1)


def test_publication_uses_no_destructive_or_resolving_path_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("destructive or resolving path call")

    for name in ("resolve", "mkdir", "unlink", "rmdir"):
        monkeypatch.setattr(Path, name, forbidden)
    for name in ("unlink", "remove", "rmdir"):
        monkeypatch.setattr(publication_module.os, name, forbidden)

    with PublishedFileReceiptOwner() as receipt_owner:
        publish_owned_file(
            tmp_path / "object",
            [b"x"],
            max_bytes=1,
            receipt_owner=receipt_owner,
        )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process gate")
def test_concurrent_processes_publish_identical_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "object"
    payload = b"shared bytes"
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(
            target=_publish_in_process,
            args=(os.fspath(destination), payload),
        )
        for _index in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    assert destination.read_bytes() == payload
    assert len(list(tmp_path.glob(".codenib-file-*"))) == 3


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires procfs")
def test_failure_and_receipt_close_do_not_leak_descriptors(tmp_path: Path) -> None:
    before = len(list(Path("/proc/self/fd").iterdir()))
    destination = tmp_path / "object"
    destination.write_bytes(b"old")
    destination.chmod(0o644)
    for index in range(12):
        with PublishedFileReceiptOwner() as failed_owner:
            with pytest.raises(OwnedFileConflictError):
                publish_owned_file(
                    destination,
                    [f"new-{index}".encode()],
                    max_bytes=16,
                    receipt_owner=failed_owner,
                )
    with PublishedFileReceiptOwner() as receipt_owner:
        publish_owned_file(
            destination,
            [b"old"],
            max_bytes=3,
            receipt_owner=receipt_owner,
        )
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after <= before + 1


def test_darwin_rename_dispatch_uses_existing_renameatx_np(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Rename:
        argtypes = None
        restype = None

        def __call__(self, *args):
            calls.append(args)
            return 0

    class Libc:
        renameatx_np = Rename()

    monkeypatch.setattr(atomic_module.sys, "platform", "darwin")
    monkeypatch.setattr(atomic_module.ctypes, "CDLL", lambda *_args, **_kwargs: Libc())

    atomic_module._rename_noreplace_at("stage", "final", 10, 11)

    assert calls == [(10, b"stage", 11, b"final", atomic_module._RENAME_EXCL)]


def test_windows_fails_before_opening_or_mutating_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = False

    def forbidden_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("Windows publication authority was opened")

    monkeypatch.setattr(publication_module.sys, "platform", "win32")
    monkeypatch.setattr(
        publication_module._atomic,
        "_open_publication_authority",
        forbidden_open,
    )

    with pytest.raises(RuntimeError, match="unsupported on Windows"):
        OwnedFilePublicationAuthority(tmp_path / "object", max_bytes=1)
    assert not opened
    assert not (tmp_path / "object").exists()


@pytest.mark.parametrize(
    ("platform", "message"),
    (("win32", "unsupported on Windows"), ("freebsd", "unsupported on this host")),
)
def test_support_gate_fails_before_opening_or_mutating_any_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    message: str,
) -> None:
    touched = False

    def forbidden(*_args, **_kwargs):
        nonlocal touched
        touched = True
        raise AssertionError("support gate touched filesystem publication state")

    monkeypatch.setattr(publication_module.sys, "platform", platform)
    monkeypatch.setattr(
        publication_module._atomic,
        "_require_rename_noreplace_platform",
        forbidden,
    )
    monkeypatch.setattr(
        publication_module._atomic,
        "_open_publication_authority",
        forbidden,
    )

    with pytest.raises(RuntimeError, match=message):
        require_owned_file_publication_support()

    assert not touched
    assert list(tmp_path.iterdir()) == []


def test_support_gate_covers_atomic_directory_fd_prerequisites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_checked = False

    def forbidden_backend_check() -> None:
        nonlocal backend_checked
        backend_checked = True
        raise AssertionError("rename backend was checked after a failed fd gate")

    monkeypatch.setattr(
        publication_module._atomic,
        "_SAFE_OWNERSHIP_DIRECTORY_FDS",
        False,
    )
    monkeypatch.setattr(
        publication_module._atomic,
        "_require_rename_noreplace_platform",
        forbidden_backend_check,
    )

    with pytest.raises(RuntimeError, match="anchored directory-fd support"):
        require_owned_file_publication_support()

    assert not backend_checked
    assert list(tmp_path.iterdir()) == []
