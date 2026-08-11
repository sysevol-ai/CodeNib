# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dis
import errno
import os
import signal
import stat
import sys
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

import pytest

import codenib._atomic_directory as atomic_directory
import codenib._captured_directory as captured_directory
from codenib._atomic_directory import (
    capture_directory_ownership,
    directory_ownership_file_records,
)
from codenib._captured_directory import (
    AuthenticatedSnapshotReader,
    CapturedDirectoryReader,
    OwnedDirectoryStage,
    OwnedWorkspaceAuthority,
    PublishedWorkspaceReceipt,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
    require_owned_workspace_publication_support,
)


@contextmanager
def _preopened_workspace(
    tmp_path: Path,
    plan: WorkspacePlan,
) -> Iterator[tuple[Path, Path, int, int, dict[str, int]]]:
    parent = tmp_path / "workspace-parent"
    parent.mkdir(mode=0o700)
    destination = parent / "destination"
    stage = parent / "trusted-stage"
    stage.mkdir(mode=plan.root_mode)
    for item in plan.directories:
        (stage / item.path.as_posix()).mkdir(mode=item.mode)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(parent, flags)
    root_descriptor = os.open(stage, flags)
    directories = {
        item.path.as_posix(): os.open(stage / item.path.as_posix(), flags)
        for item in plan.directories
    }
    try:
        yield (
            destination,
            stage,
            parent_descriptor,
            root_descriptor,
            directories,
        )
    finally:
        for descriptor in directories.values():
            os.close(descriptor)
        os.close(root_descriptor)
        os.close(parent_descriptor)


def _interrupt_before_store(
    callback: Callable[[], object],
    *,
    local_name: str,
    error: BaseException,
) -> None:
    target_code = callback.__code__
    instructions = {
        instruction.offset: instruction
        for instruction in dis.get_instructions(target_code)
    }
    previous_trace = sys.gettrace()

    def trace(frame, event, _arg):
        if event == "call" and frame.f_code is target_code:
            frame.f_trace_opcodes = True
            return trace
        if event == "opcode" and frame.f_code is target_code:
            instruction = instructions.get(frame.f_lasti)
            if (
                instruction is not None
                and instruction.opname == "STORE_FAST"
                and instruction.argval == local_name
            ):
                sys.settrace(None)
                raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)


def _interrupt_after_store_attr(
    callback: Callable[[], object],
    *,
    target_code: object,
    attribute: str,
    error: BaseException,
) -> None:
    instructions = {
        instruction.offset: instruction
        for instruction in dis.get_instructions(target_code)
    }
    previous_trace = sys.gettrace()
    armed = False

    def trace(frame, event, _arg):
        nonlocal armed
        if event == "call" and frame.f_code is target_code:
            frame.f_trace_opcodes = True
            return trace
        if event == "opcode" and frame.f_code is target_code:
            if armed:
                sys.settrace(None)
                raise error
            instruction = instructions.get(frame.f_lasti)
            if (
                instruction is not None
                and instruction.opname == "STORE_ATTR"
                and instruction.argval == attribute
            ):
                armed = True
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)


def test_tree_ownership_exposes_canonical_file_records(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "b.txt").write_bytes(b"b")
    (nested / "a.txt").write_bytes(b"alpha")

    records = directory_ownership_file_records(capture_directory_ownership(root))

    assert [(record.path, record.size) for record in records] == [
        ("b.txt", 1),
        ("nested/a.txt", 5),
    ]
    assert all(len(record.sha256) == 64 for record in records)


def test_entry_policy_rejects_before_file_bytes_are_hashed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "documents.json").write_bytes(b"x" * 32)

    def reject_large(_path: str, kind: str, _mode: int, size: int) -> None:
        if kind == "file" and size > 16:
            raise ValueError("prehash size policy")

    def forbidden_read(_descriptor: int, _size: int) -> bytes:
        raise AssertionError("oversized file must be rejected before hashing")

    monkeypatch.setattr(atomic_directory.os, "read", forbidden_read)
    with pytest.raises(ValueError, match="prehash size policy"):
        capture_directory_ownership(root, entry_policy=reject_large)


def test_captured_reader_rejects_identical_nested_a_b_a_replacement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    source = nested / "payload"
    source.write_bytes(b"same bytes")
    ownership = capture_directory_ownership(root)
    reader = CapturedDirectoryReader(root, ownership)
    saved = tmp_path / "saved-nested"

    nested.rename(saved)
    nested.mkdir()
    (nested / "payload").write_bytes(b"same bytes")
    replacement = tmp_path / "replacement-nested"
    nested.rename(replacement)
    saved.rename(nested)

    try:
        with pytest.raises(ValueError, match="captured path is not a real directory"):
            reader.read_bytes("nested/payload", max_bytes=64)
    finally:
        reader.close()

    assert (replacement / "payload").read_bytes() == b"same bytes"


def test_authenticated_descriptor_exposes_only_a_sealed_good_snapshot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "payload"
    source.write_bytes(b"GOOD")
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))
    observed = b""

    try:
        with pytest.raises(ValueError, match="changed after authentication"):
            with reader.authenticated_descriptor("payload", max_bytes=4) as (
                descriptor,
                record,
            ):
                assert record.size == 4
                source.write_bytes(b"EVIL")
                os.lseek(descriptor, 0, os.SEEK_SET)
                observed = os.read(descriptor, 4)
                with pytest.raises(OSError):
                    os.write(descriptor, b"FAIL")
    finally:
        reader.close()

    assert observed == b"GOOD"


def test_authenticated_snapshot_has_bounded_immutable_bytes_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib._captured_directory as captured_directory

    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"first\nsecond")
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))

    def unavailable() -> int:
        raise captured_directory._SnapshotUnavailable("unsupported host")

    monkeypatch.setattr(captured_directory, "_create_sealable_memfd", unavailable)
    try:
        with reader.authenticated_snapshot("payload") as (snapshot, record):
            assert record.size == 12
            assert snapshot.readline() == b"first\n"
            assert snapshot.read() == b"second"
            with pytest.raises(RuntimeError, match="no sealed descriptor"):
                _ = snapshot.descriptor
    finally:
        reader.close()


def test_authenticated_snapshot_reader_caps_oversized_requests(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = b"x" * (8 * 1024 * 1024 + 17)
    (root / "payload").write_bytes(payload)
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))

    try:
        with reader.authenticated_snapshot("payload") as (snapshot, _record):
            source = AuthenticatedSnapshotReader(snapshot)
            first = source.read(1 << 60)
            second = source.read(1 << 60)
            assert len(first) == 8 * 1024 * 1024
            assert first + second == payload
            assert source.read(1 << 60) == b""
        with reader.authenticated_snapshot("payload") as (snapshot, _record):
            source = AuthenticatedSnapshotReader(snapshot)
            assert len(source.readline(1 << 60)) == 8 * 1024 * 1024
    finally:
        reader.close()


def test_owned_stage_noreplace_never_overwrites_raced_foreign_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = OwnedDirectoryStage(tmp_path / "destination")
    valuable = tmp_path / "valuable"
    valuable.write_bytes(b"foreign")
    real_rename = captured_directory._rename_noreplace_at

    def race_rename(source, destination, src_dir_fd, dst_dir_fd) -> None:
        valuable.rename(stage.path / destination)
        real_rename(
            source,
            destination,
            src_dir_fd,
            dst_dir_fd,
        )

    monkeypatch.setattr(captured_directory, "_rename_noreplace_at", race_rename)
    try:
        with pytest.raises(ValueError, match="already exists"):
            stage.write_file("payload", [b"new"])
        assert (stage.path / "payload").read_bytes() == b"foreign"
    finally:
        stage.close()


def test_owned_stage_does_not_reacquire_foreign_nested_directory(
    tmp_path: Path,
) -> None:
    stage = OwnedDirectoryStage(tmp_path / "destination")
    foreign = stage.path / "nested"
    foreign.mkdir()
    try:
        with pytest.raises(ValueError, match="was not created here"):
            stage.write_file("nested/payload", [b"new"])
        assert not (foreign / "payload").exists()
    finally:
        stage.close()


def test_owned_stage_rejects_preexisting_ancestor_symlink(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    parent = foreign / "parent"
    parent.mkdir(parents=True)
    valuable = parent / "valuable.txt"
    valuable.write_text("preserve", encoding="utf-8")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (trusted / "alias").symlink_to(foreign, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        OwnedDirectoryStage(trusted / "alias" / "parent" / "published")

    assert valuable.read_text(encoding="utf-8") == "preserve"
    assert not list(parent.glob(".*.normalize-*"))


def test_owned_stage_prepare_creates_nested_parent_and_publishes(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "new" / "nested" / "published"
    stage = OwnedDirectoryStage.prepare(
        destination,
        required_destination_file="marker.txt",
        allow_empty_destination=True,
    )
    stage.write_file("marker.txt", [b"owned"])

    stage.publish()

    assert (destination / "marker.txt").read_bytes() == b"owned"


def test_owned_stage_prepare_rejects_parent_replacement_before_write(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "parent" / "published"
    stage = OwnedDirectoryStage.prepare(destination)
    saved = tmp_path / "saved-parent"
    destination.parent.rename(saved)
    destination.parent.mkdir()
    valuable = destination.parent / "valuable.txt"
    valuable.write_text("preserve", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="parent path changed"):
            stage.write_file("marker.txt", [b"owned"])
        assert valuable.read_text(encoding="utf-8") == "preserve"
        assert not (destination.parent / "marker.txt").exists()
    finally:
        stage.close()


def test_owned_stage_prepare_keeps_pinned_parent_across_reversible_swap(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "parent" / "published"
    stage = OwnedDirectoryStage.prepare(destination)
    saved = tmp_path / "saved-parent"
    destination.parent.rename(saved)
    destination.parent.mkdir()
    valuable = destination.parent / "valuable.txt"
    valuable.write_text("preserve", encoding="utf-8")
    foreign = tmp_path / "foreign-parent"
    destination.parent.rename(foreign)
    saved.rename(destination.parent)
    try:
        stage.write_file("marker.txt", [b"owned"])
        stage.publish()
        assert (destination / "marker.txt").read_bytes() == b"owned"
        assert (foreign / "valuable.txt").read_text(encoding="utf-8") == "preserve"
    finally:
        stage.close()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires procfs")
def test_owned_stage_closes_parent_fd_when_iterator_creation_fails(
    tmp_path: Path,
) -> None:
    class RaisingIterable:
        def __iter__(self):
            raise RuntimeError("iterator creation failed")

    stage = OwnedDirectoryStage(tmp_path / "destination")
    baseline = len(list(Path("/proc/self/fd").iterdir()))
    try:
        with pytest.raises(RuntimeError, match="iterator creation failed"):
            stage.write_file("payload", RaisingIterable())
        assert len(list(Path("/proc/self/fd").iterdir())) == baseline
    finally:
        stage.close()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires procfs")
def test_owned_stage_closes_all_fds_when_iterator_close_fails(tmp_path: Path) -> None:
    class RaisingCloseIterator:
        yielded = False

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            if self.yielded:
                raise StopIteration
            self.yielded = True
            return b"new"

        def close(self) -> None:
            raise RuntimeError("iterator close failed")

    stage = OwnedDirectoryStage(tmp_path / "destination")
    baseline = len(list(Path("/proc/self/fd").iterdir()))
    try:
        with pytest.raises(RuntimeError, match="iterator close failed"):
            stage.write_file("payload", RaisingCloseIterator())
        assert len(list(Path("/proc/self/fd").iterdir())) == baseline
        assert (stage.path / "payload").read_bytes() == b"new"
    finally:
        stage.close()


def test_owned_stage_rejects_root_swap_between_capture_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = captured_directory.os.open
    swapped: dict[str, str] = {}

    def race_open(path, flags, mode=0o777, *, dir_fd=None):
        if (
            not swapped
            and dir_fd is not None
            and isinstance(path, str)
            and ".normalize-" in path
            and flags & os.O_DIRECTORY
        ):
            stolen = f"{path}.stolen"
            os.rename(path, stolen, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.mkdir(path, mode=0o700, dir_fd=dir_fd)
            swapped.update(active=path, stolen=stolen)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(captured_directory.os, "open", race_open)
    with pytest.raises(RuntimeError, match="root changed"):
        OwnedDirectoryStage(tmp_path / "destination")

    assert swapped
    assert (tmp_path / swapped["active"]).is_dir()
    assert (tmp_path / swapped["stolen"]).is_dir()


def test_workspace_plan_is_canonical_and_bound_to_its_subject() -> None:
    first = WorkspacePlan(
        subject_digest="a" * 64,
        directories=(
            WorkspaceDirectory("nested/deeper"),
            WorkspaceDirectory("nested"),
        ),
        files=(
            WorkspaceFile("root.bin", max_bytes=8),
            WorkspaceFile("nested/deeper/payload.bin", mode=0o644, max_bytes=16),
        ),
    )
    second = WorkspacePlan(
        subject_digest="a" * 64,
        directories=(
            WorkspaceDirectory("nested"),
            WorkspaceDirectory("nested/deeper"),
        ),
        files=tuple(reversed(first.files)),
    )

    assert first == second
    assert first.digest == second.digest
    assert len(first.digest) == 64
    assert (
        WorkspacePlan(
            subject_digest="b" * 64,
            directories=first.directories,
            files=first.files,
        ).digest
        != first.digest
    )


def test_workspace_plan_rejects_atomic_scanner_overflow_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_provisioning(*_args, **_kwargs) -> None:
        raise AssertionError("oversized workspace plan reached provisioning")

    monkeypatch.setattr(
        captured_directory,
        "_open_publication_authority",
        forbidden_provisioning,
    )

    with pytest.raises(ValueError, match="too many entries"):
        WorkspacePlan(
            subject_digest="c" * 64,
            directories=(WorkspaceDirectory("entry"),) * 100_001,
        )


def test_workspace_plan_rejects_metadata_budget_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(captured_directory, "_MAX_OWNERSHIP_METADATA_BYTES", 100)

    with pytest.raises(ValueError, match="path metadata exceeds"):
        WorkspacePlan(
            subject_digest="c" * 64,
            directories=(
                WorkspaceDirectory("a" * 48),
                WorkspaceDirectory("b" * 48),
            ),
        )


def test_owned_workspace_publication_support_gate_is_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_checks: list[str] = []

    def forbid_provisioning(*_args, **_kwargs) -> None:
        raise AssertionError("support check attempted workspace provisioning")

    monkeypatch.setattr(captured_directory.sys, "platform", "linux")
    monkeypatch.setattr(captured_directory, "_SAFE_OWNERSHIP_DIRECTORY_FDS", True)
    monkeypatch.setattr(
        captured_directory,
        "_require_rename_noreplace_platform",
        lambda: capability_checks.append("rename-noreplace"),
    )
    monkeypatch.setattr(
        captured_directory,
        "_open_publication_authority",
        forbid_provisioning,
    )

    assert require_owned_workspace_publication_support() is None
    assert capability_checks == ["rename-noreplace"]
    assert "require_owned_workspace_publication_support" in captured_directory.__all__


@pytest.mark.parametrize("platform", ["darwin", "win32", "freebsd14"])
def test_owned_workspace_publication_support_rejects_non_linux_before_probe(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    def forbidden_probe() -> None:
        raise AssertionError("unsupported platform reached rename capability probe")

    monkeypatch.setattr(captured_directory.sys, "platform", platform)
    monkeypatch.setattr(
        captured_directory,
        "_require_rename_noreplace_platform",
        forbidden_probe,
    )

    with pytest.raises(UnsupportedWorkspaceCreation, match="Linux"):
        require_owned_workspace_publication_support()


def test_owned_workspace_publication_support_requires_anchored_directory_fds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_probe() -> None:
        raise AssertionError("unsafe directory-fd host reached rename capability probe")

    monkeypatch.setattr(captured_directory.sys, "platform", "linux")
    monkeypatch.setattr(captured_directory, "_SAFE_OWNERSHIP_DIRECTORY_FDS", False)
    monkeypatch.setattr(
        captured_directory,
        "_require_rename_noreplace_platform",
        forbidden_probe,
    )

    with pytest.raises(UnsupportedWorkspaceCreation, match="directory-fd"):
        require_owned_workspace_publication_support()


def test_owned_workspace_publication_support_wraps_missing_rename_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported_rename() -> None:
        raise RuntimeError("renameat2 is missing")

    monkeypatch.setattr(captured_directory.sys, "platform", "linux")
    monkeypatch.setattr(captured_directory, "_SAFE_OWNERSHIP_DIRECTORY_FDS", True)
    monkeypatch.setattr(
        captured_directory,
        "_require_rename_noreplace_platform",
        unsupported_rename,
    )

    with pytest.raises(
        UnsupportedWorkspaceCreation,
        match="no-replace rename",
    ) as raised:
        require_owned_workspace_publication_support()

    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.parametrize(
    ("directories", "files", "message"),
    [
        ((), (WorkspaceFile("nested/payload", max_bytes=1),), "ancestor"),
        (
            (WorkspaceDirectory("A"), WorkspaceDirectory("a")),
            (),
            "portable path collision",
        ),
        (
            (WorkspaceDirectory("entry"),),
            (WorkspaceFile("entry", max_bytes=1),),
            "both file and directory",
        ),
    ],
)
def test_workspace_plan_rejects_ambiguous_skeletons(
    directories: tuple[WorkspaceDirectory, ...],
    files: tuple[WorkspaceFile, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        WorkspacePlan(
            subject_digest="c" * 64,
            directories=directories,
            files=files,
        )


def test_preopened_workspace_writes_only_planned_files_without_mkdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="d" * 64,
        directories=(WorkspaceDirectory("nested"),),
        files=(
            WorkspaceFile("root.bin", max_bytes=4),
            WorkspaceFile("nested/payload.bin", mode=0o644, max_bytes=8),
        ),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        os.lseek(parent_fd, 37, os.SEEK_SET)
        os.lseek(root_fd, 37, os.SEEK_SET)
        workspace = OwnedWorkspaceAuthority()

        def forbidden_mkdir(*_args, **_kwargs) -> None:
            raise AssertionError("strict workspace runtime must not create directories")

        monkeypatch.setattr(captured_directory.os, "mkdir", forbidden_mkdir)
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        assert workspace.destination == destination
        assert workspace.expected_destination_ownership is None
        root_record = workspace.write_file("root.bin", [b"root"])
        nested_record = workspace.write_file(
            "nested/payload.bin",
            [b"payload"],
        )
        ownership = workspace.seal()

        expected_records = (
            atomic_directory.TreeFileRecord(
                path="nested/payload.bin",
                mode=0o644,
                size=7,
                sha256=(
                    "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5"
                ),
            ),
            atomic_directory.TreeFileRecord(
                path="root.bin",
                mode=0o600,
                size=4,
                sha256=(
                    "4813494d137e1631bba301d5acab6e7bb7aa74ce1185d456565ef51d737677b2"
                ),
            ),
        )
        assert (nested_record, root_record) == expected_records
        assert directory_ownership_file_records(ownership) == expected_records
        assert os.lseek(parent_fd, 0, os.SEEK_CUR) == 37
        assert os.lseek(root_fd, 0, os.SEEK_CUR) == 37
        assert (stage / "nested" / "payload.bin").read_bytes() == b"payload"
        workspace.close()
        os.fstat(parent_fd)
        os.fstat(root_fd)


def test_preopened_workspace_rejects_root_name_replacement(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="e" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        saved = stage.with_name("saved-stage")
        stage.rename(saved)
        stage.mkdir(mode=0o700)
        workspace = OwnedWorkspaceAuthority()

        with pytest.raises(RuntimeError, match="root handle"):
            workspace.adopt(
                destination=destination,
                stage_name=stage.name,
                parent_descriptor=parent_fd,
                root_descriptor=root_fd,
                directory_descriptors=directories,
                plan=plan,
                expected_destination=None,
            )

        assert not (stage / "payload").exists()
        os.fstat(root_fd)


def test_preopened_workspace_rejects_nested_handle_replacement(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="f" * 64,
        directories=(WorkspaceDirectory("nested"),),
        files=(WorkspaceFile("nested/payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        nested = stage / "nested"
        saved = tmp_path / "saved-nested"
        nested.rename(saved)
        nested.mkdir(mode=0o700)
        workspace = OwnedWorkspaceAuthority()

        with pytest.raises(RuntimeError, match="directory handle differs"):
            workspace.adopt(
                destination=destination,
                stage_name=stage.name,
                parent_descriptor=parent_fd,
                root_descriptor=root_fd,
                directory_descriptors=directories,
                plan=plan,
                expected_destination=None,
            )

        assert not (nested / "payload").exists()


def test_preopened_workspace_rejects_any_unplanned_skeleton_file(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="1" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        (stage / "foreign").write_bytes(b"preserve")
        workspace = OwnedWorkspaceAuthority()

        with pytest.raises(ValueError, match="contain no files"):
            workspace.adopt(
                destination=destination,
                stage_name=stage.name,
                parent_descriptor=parent_fd,
                root_descriptor=root_fd,
                directory_descriptors=directories,
                plan=plan,
                expected_destination=None,
            )

        assert (stage / "foreign").read_bytes() == b"preserve"


def test_preopened_workspace_rejects_existing_destination_when_missing_expected(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="2" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        destination.mkdir(mode=0o700)
        workspace = OwnedWorkspaceAuthority()

        with pytest.raises(RuntimeError, match="expected missing"):
            workspace.adopt(
                destination=destination,
                stage_name=stage.name,
                parent_descriptor=parent_fd,
                root_descriptor=root_fd,
                directory_descriptors=directories,
                plan=plan,
                expected_destination=None,
            )


def test_preopened_workspace_noreplace_refuses_foreign_planned_leaf(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="3" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        (stage / "payload").write_bytes(b"evil")

        with pytest.raises(RuntimeError, match="unplanned file"):
            workspace.write_file("payload", [b"good"])

        assert (stage / "payload").read_bytes() == b"evil"


def test_strict_workspace_create_fails_before_filesystem_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_mkdir(*_args, **_kwargs) -> None:
        raise AssertionError("strict create must fail before mutation")

    monkeypatch.setattr(captured_directory.os, "mkdir", forbidden_mkdir)
    with pytest.raises(UnsupportedWorkspaceCreation, match="pre-opened skeleton"):
        OwnedWorkspaceAuthority.create()


def test_preopened_workspace_seal_rejects_external_hardlink(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="4" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.write_file("payload", [b"safe"])
        os.link(stage / "payload", tmp_path / "external-alias")

        with pytest.raises(RuntimeError, match="file changed"):
            workspace.seal()


def test_preopened_workspace_seal_is_idempotently_recoverable(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="5" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.write_file("payload", [b"safe"])

        ownership = workspace.seal()

        assert workspace.seal() is ownership
        workspace.close()


def test_preopened_workspace_seal_return_interruption_keeps_token_recoverable(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="8" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.write_file("payload", [b"safe"])

        def seal_and_store() -> object:
            ownership = workspace.seal()
            return ownership

        with pytest.raises(KeyboardInterrupt, match="seal return"):
            _interrupt_before_store(
                seal_and_store,
                local_name="ownership",
                error=KeyboardInterrupt("seal return"),
            )

        ownership = workspace.seal()
        assert directory_ownership_file_records(ownership)[0].path == "payload"
        workspace.close()


def test_preopened_workspace_rejects_producer_reentrant_close(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="6" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )

        def chunks() -> Iterator[bytes]:
            with pytest.raises(RuntimeError, match="close is reentrant"):
                workspace.close()
            yield b"safe"

        workspace.write_file("payload", chunks())
        workspace.seal()
        assert (stage / "payload").read_bytes() == b"safe"
        workspace.close()


def test_preopened_workspace_retains_file_owner_after_persistent_close_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="7" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        real_close = os.close
        retained: set[int] = set()

        def persistent_file_eio(descriptor: int) -> None:
            metadata = os.fstat(descriptor)
            if stat.S_ISREG(metadata.st_mode):
                retained.add(descriptor)
                raise OSError(errno.EIO, "persistent file close failure")
            real_close(descriptor)

        monkeypatch.setattr(captured_directory.os, "close", persistent_file_eio)
        with pytest.raises(OSError, match="persistent file close failure"):
            workspace.write_file("payload", [b"safe"])

        assert retained
        descriptor = next(iter(retained))
        os.fstat(descriptor)
        assert workspace.state == "failed"

        monkeypatch.setattr(captured_directory.os, "close", real_close)
        workspace.close()
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_preopened_workspace_close_reconciliation_preserves_first_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="9" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        real_close = os.close
        armed = [False]

        def persistent_file_eio(descriptor: int) -> None:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                armed[0] = True
                raise OSError(errno.EIO, "first file close failure")
            real_close(descriptor)

        owner_type = type(workspace._resources)
        real_closed = owner_type.closed

        def interrupted_reconciliation(owner) -> bool:
            if armed[0] and owner is workspace._resources:
                raise SystemExit("secondary close-state interruption")
            return real_closed.fget(owner)

        monkeypatch.setattr(captured_directory.os, "close", persistent_file_eio)
        monkeypatch.setattr(
            owner_type,
            "closed",
            property(interrupted_reconciliation),
        )
        with pytest.raises(OSError, match="first file close failure") as caught:
            workspace.write_file("payload", [b"safe"])

        assert not isinstance(caught.value, SystemExit)
        assert workspace.state == "failed"

        monkeypatch.setattr(owner_type, "closed", real_closed)
        monkeypatch.setattr(captured_directory.os, "close", real_close)
        workspace.close()


def test_preopened_workspace_iterator_close_lookup_preserves_primary_and_fds(
    tmp_path: Path,
) -> None:
    class RaisingCloseLookup:
        yielded = False

        def __iter__(self):
            return self

        def __next__(self) -> bytes:
            if not self.yielded:
                self.yielded = True
                return b"x"
            raise RuntimeError("producer primary")

        @property
        def close(self):
            raise SystemExit("close lookup cancellation")

    plan = WorkspacePlan(
        subject_digest="0" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        with pytest.raises(RuntimeError, match="producer primary") as caught:
            workspace.write_file("payload", RaisingCloseLookup())

        assert any(
            "close lookup cancellation" in note
            for note in tuple(getattr(caught.value, "__notes__", ()))
            + tuple(getattr(caught.value, "_codenib_cleanup_notes", ()))
        )
        assert all(record.owner.descriptor < 0 for record in workspace._file_owners)
        assert workspace.state == "closed"
        assert (stage / "payload").read_bytes() == b"x"


def test_preopened_workspace_record_install_interruption_terminally_fails(
    tmp_path: Path,
) -> None:
    class InterruptingRecords(dict):
        def __setitem__(self, key, value) -> None:
            super().__setitem__(key, value)
            raise KeyboardInterrupt("record install interruption")

    plan = WorkspacePlan(
        subject_digest="a" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace._written_files = InterruptingRecords()

        with pytest.raises(KeyboardInterrupt, match="record install interruption"):
            workspace.write_file("payload", [b"safe"])

        assert workspace.state == "closed"
        assert (stage / "payload").read_bytes() == b"safe"
        with pytest.raises(RuntimeError, match="cannot seal while closed"):
            workspace.seal()


def test_preopened_workspace_record_construction_interruption_terminally_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="a" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )

        def interrupt_record(*_args, **_kwargs):
            raise KeyboardInterrupt("record construction interruption")

        monkeypatch.setattr(captured_directory, "TreeFileRecord", interrupt_record)
        with pytest.raises(KeyboardInterrupt, match="record construction interruption"):
            workspace.write_file("payload", [b"safe"])

        assert workspace.state == "closed"
        assert (stage / "payload").read_bytes() == b"safe"
        with pytest.raises(RuntimeError, match="cannot seal while closed"):
            workspace.seal()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_owned_workspace_child_closes_inherited_resources_without_parent_effect(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="f" * 64,
        directories=(WorkspaceDirectory("nested"),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        authority = workspace._parent_owner.authority
        assert authority is not None
        workspace_descriptors = {
            authority._resource,
            *(record.descriptor for record in workspace._resources._records),
        }
        workspace_descriptors.discard(-1)

        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def hold_lifecycle_lock() -> None:
            def wait_for_release() -> None:
                lock_acquired.set()
                assert release_lock.wait(timeout=5)

            workspace._lock.run(wait_for_release)

        holder = threading.Thread(target=hold_lifecycle_lock)
        holder.start()
        assert lock_acquired.wait(timeout=5)
        child = os.fork()
        if child == 0:  # pragma: no branch - assertions report through exit code
            signal.signal(signal.SIGALRM, lambda *_args: os._exit(90))
            signal.alarm(3)
            real_close = os.close
            target_descriptor = workspace._root_descriptor
            target_barrier = threading.Barrier(2)
            target_close_calls: list[int] = []
            close_errors: list[BaseException] = []

            def counted_close(descriptor: int) -> None:
                if descriptor == target_descriptor:
                    try:
                        target_barrier.wait(timeout=0.25)
                    except threading.BrokenBarrierError:
                        pass
                    target_close_calls.append(descriptor)
                real_close(descriptor)

            captured_directory.os.close = counted_close

            def close_in_child() -> None:
                try:
                    workspace.close()
                except RuntimeError as exc:
                    if "PID boundary" not in str(exc):
                        close_errors.append(exc)
                except BaseException as exc:  # noqa: B036 - child reports failure
                    close_errors.append(exc)
                else:
                    close_errors.append(AssertionError("PID boundary was accepted"))

            closers = [threading.Thread(target=close_in_child) for _ in range(2)]
            for closer in closers:
                closer.start()
            for closer in closers:
                closer.join(timeout=2)
            if any(closer.is_alive() for closer in closers):
                os._exit(91)
            if close_errors:
                os._exit(92)
            if target_close_calls != [target_descriptor]:
                os._exit(95)
            try:
                for descriptor in workspace_descriptors:
                    try:
                        os.fstat(descriptor)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            os._exit(93)
                    else:
                        os._exit(94)
            except BaseException:  # noqa: B036 - child reports exact failure
                os._exit(96)
            os._exit(0)

        release_lock.set()
        holder.join(timeout=5)
        assert not holder.is_alive()
        _pid, status = os.waitpid(child, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
        for descriptor in workspace_descriptors:
            os.fstat(descriptor)
        workspace.close()


def test_owned_workspace_adopt_skeleton_validation_is_linear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = 64
    plan = WorkspacePlan(
        subject_digest="1" * 64,
        directories=tuple(
            WorkspaceDirectory(f"directory-{index:03d}") for index in range(count)
        ),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        calls = 0
        real_as_posix = PurePosixPath.as_posix

        def counted_as_posix(path: PurePosixPath) -> str:
            nonlocal calls
            calls += 1
            return real_as_posix(path)

        monkeypatch.setattr(PurePosixPath, "as_posix", counted_as_posix)
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )

        assert count <= calls < count * 20
        workspace.close()


def test_owned_workspace_seal_flushes_directories_bottom_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="2" * 64,
        directories=(
            WorkspaceDirectory("nested"),
            WorkspaceDirectory("nested/deeper"),
        ),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        descriptor_paths = {
            descriptor: path
            for path, descriptor in workspace._directory_descriptors.items()
        }
        observed: list[str] = []
        real_fsync = captured_directory.os.fsync

        def record_fsync(descriptor: int) -> None:
            path = descriptor_paths.get(descriptor)
            if path is not None:
                observed.append(path)
            real_fsync(descriptor)

        monkeypatch.setattr(captured_directory.os, "fsync", record_fsync)

        workspace.seal()

        assert observed == ["nested/deeper", "nested", ""]
        workspace.close()


def test_owned_workspace_publish_installs_fresh_durable_receipt(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="3" * 64,
        files=(WorkspaceFile("payload", max_bytes=8),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.write_file("payload", [b"durable"])
        sealed = workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()

        workspace.publish_into(receipt_owner)

        receipt = receipt_owner.receipt
        assert isinstance(receipt, PublishedWorkspaceReceipt)
        assert receipt.durable is True
        assert receipt.plan_digest == plan.digest
        assert receipt.sealed_ownership == sealed
        assert receipt.ownership == capture_directory_ownership(destination)
        assert receipt.ownership != sealed
        assert not stage.exists()
        assert (destination / "payload").read_bytes() == b"durable"
        assert receipt_owner.consume(
            lambda borrowed, reader: (
                borrowed.plan_digest,
                reader.read_bytes("payload", max_bytes=8),
            )
        ) == (plan.digest, b"durable")
        with pytest.raises(RuntimeError, match="ReceiptOwner"):
            workspace.close()
        receipt_owner.close()
        assert receipt_owner.closed
        assert receipt.closed


def test_owned_workspace_publish_runs_staged_and_published_validators(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="3" * 64,
        files=(WorkspaceFile("payload", max_bytes=9),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        record = workspace.write_file("payload", [b"validated"])
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        observed: list[tuple[str, tuple[atomic_directory.TreeFileRecord, ...]]] = []

        def validate_staged(
            reader: atomic_directory.PublicationDirectoryReader,
        ) -> None:
            assert stage.is_dir()
            assert not destination.exists()
            assert reader.read_bytes("payload", max_bytes=9) == b"validated"
            observed.append(("staged", reader.file_records()))

        def validate_published(
            reader: atomic_directory.PublicationDirectoryReader,
        ) -> None:
            assert not stage.exists()
            assert destination.is_dir()
            assert reader.read_bytes("payload", max_bytes=9) == b"validated"
            observed.append(("published", reader.file_records()))

        workspace.publish_into(
            receipt_owner,
            validate_staged_directory=validate_staged,
            validate_published_destination=validate_published,
        )

        assert observed == [
            ("staged", (record,)),
            ("published", (record,)),
        ]
        assert receipt_owner.active
        receipt_owner.close()


@pytest.mark.parametrize(
    ("validator_name", "message"),
    [
        ("validate_staged_directory", "staged workspace validator"),
        ("validate_published_destination", "published workspace validator"),
    ],
)
def test_owned_workspace_rejects_noncallable_validator_before_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator_name: str,
    message: str,
) -> None:
    plan = WorkspacePlan(subject_digest="3" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()

        def forbidden_publish(*_args, **_kwargs) -> None:
            raise AssertionError("noncallable validator reached atomic publication")

        monkeypatch.setattr(
            captured_directory,
            "_publish_staged_directory_with_authority",
            forbidden_publish,
        )

        with pytest.raises(TypeError, match=message):
            workspace.publish_into(
                receipt_owner,
                **{validator_name: object()},
            )

        assert workspace.state == "sealed"
        assert workspace._publication_transfer is None
        assert workspace._resources_transferred is False
        assert receipt_owner.state == "empty"
        assert stage.is_dir()
        assert not destination.exists()
        workspace.close()
        receipt_owner.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_published_workspace_child_closes_transfer_without_parent_effect(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(subject_digest="3" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        workspace.publish_into(receipt_owner)
        authority = workspace._parent_owner.authority
        assert authority is not None
        internal_descriptors = {
            authority.resource,
            *(record.descriptor for record in workspace._resources._records),
        }
        internal_descriptors.discard(-1)

        child = os.fork()
        if child == 0:  # pragma: no branch - exact child result via exit
            signal.signal(signal.SIGALRM, lambda *_signal_args: os._exit(70))
            signal.alarm(3)
            failures: list[BaseException] = []
            target_descriptor = workspace._root_descriptor
            target_barrier = threading.Barrier(2)
            target_close_calls: list[int] = []
            real_close = os.close

            def counted_close(descriptor: int) -> None:
                if descriptor == target_descriptor:
                    try:
                        target_barrier.wait(timeout=0.25)
                    except threading.BrokenBarrierError:
                        pass
                    target_close_calls.append(descriptor)
                real_close(descriptor)

            captured_directory.os.close = counted_close

            def close_in_child() -> None:
                try:
                    receipt_owner.close()
                except RuntimeError as exc:
                    if "PID boundary" not in str(exc):
                        failures.append(exc)
                except BaseException as exc:  # noqa: B036 - child report
                    failures.append(exc)
                else:
                    failures.append(AssertionError("PID boundary was accepted"))

            closers = [threading.Thread(target=close_in_child) for _ in range(2)]
            for closer in closers:
                closer.start()
            for closer in closers:
                closer.join(timeout=2)
            if any(closer.is_alive() for closer in closers):
                os._exit(71)
            if failures:
                os._exit(72)
            if target_close_calls != [target_descriptor]:
                os._exit(75)
            for descriptor in internal_descriptors:
                try:
                    os.fstat(descriptor)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        os._exit(73)
                else:
                    os._exit(74)
            os._exit(0)

        _pid, status = os.waitpid(child, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
        for descriptor in internal_descriptors:
            os.fstat(descriptor)
        assert receipt_owner.active
        receipt_owner.close()


def test_owned_workspace_publish_reserves_transfer_before_atomic_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(subject_digest="4" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        real_publish = captured_directory._publish_staged_directory_with_authority
        observed: list[tuple[str, bool]] = []

        def inspect_reservation(*args, **kwargs):
            observed.append((receipt_owner.state, workspace._resources_transferred))
            return real_publish(*args, **kwargs)

        monkeypatch.setattr(
            captured_directory,
            "_publish_staged_directory_with_authority",
            inspect_reservation,
        )

        workspace.publish_into(receipt_owner)

        assert observed == [("reserved", True)]
        receipt_owner.close()


def test_owned_workspace_install_after_store_interruption_keeps_active_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="5" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.write_file("payload", [b"safe"])
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        real_install = PublishedWorkspaceReceiptOwner._install

        def install_then_interrupt(self, reservation, receipt) -> None:
            real_install(self, reservation, receipt)
            raise KeyboardInterrupt("receipt install return interruption")

        monkeypatch.setattr(
            PublishedWorkspaceReceiptOwner,
            "_install",
            install_then_interrupt,
        )

        with pytest.raises(KeyboardInterrupt, match="install return"):
            workspace.publish_into(receipt_owner)

        assert receipt_owner.active
        assert workspace.state == "published"
        assert (
            receipt_owner.consume(
                lambda _receipt, reader: reader.read_bytes("payload", max_bytes=4)
            )
            == b"safe"
        )
        receipt_owner.close()


def test_owned_workspace_parent_fsync_failure_installs_no_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(subject_digest="6" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        authority = workspace._parent_owner.authority
        assert authority is not None
        target = authority.resource
        real_fsync = captured_directory.os.fsync

        def fail_parent_fsync(descriptor: int) -> None:
            if descriptor == target:
                raise OSError(errno.EIO, "parent fsync failed")
            real_fsync(descriptor)

        monkeypatch.setattr(captured_directory.os, "fsync", fail_parent_fsync)

        with pytest.raises(OSError, match="parent fsync failed"):
            workspace.publish_into(receipt_owner)

        assert receipt_owner.state == "cleanup"
        assert workspace.state == "failed"
        assert destination.is_dir()
        assert not stage.exists()
        with pytest.raises(RuntimeError, match="expected active"):
            _ = receipt_owner.receipt
        receipt_owner.close()
        assert receipt_owner.closed


def test_owned_workspace_receipt_rejects_suppressed_authentication_failure(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="7" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.write_file("payload", [b"safe"])
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        workspace.publish_into(receipt_owner)

        def suppress_failure(
            _receipt: PublishedWorkspaceReceipt,
            reader: atomic_directory.PublicationDirectoryReader,
        ) -> None:
            with pytest.raises(ValueError, match="absent"):
                reader.read_bytes("missing", max_bytes=1)

        with pytest.raises(RuntimeError, match="suppressed authentication failure"):
            receipt_owner.consume(suppress_failure)
        assert receipt_owner.active
        receipt_owner.close()


def test_owned_workspace_receipt_rejects_post_publish_root_change(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(subject_digest="8" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        workspace.publish_into(receipt_owner)
        destination.chmod(0o500)

        with pytest.raises(RuntimeError, match="root handle changed"):
            receipt_owner.consume(lambda _receipt, _reader: None)
        receipt_owner.close()


def test_owned_workspace_receipt_close_persistent_eio_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(subject_digest="9" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        workspace.publish_into(receipt_owner)
        target = workspace._root_descriptor
        real_close = captured_directory.os.close
        injected = False

        def fail_once(descriptor: int) -> None:
            nonlocal injected
            if descriptor == target and not injected:
                injected = True
                raise OSError(errno.EIO, "persistent workspace close")
            real_close(descriptor)

        monkeypatch.setattr(captured_directory.os, "close", fail_once)

        with pytest.raises(OSError, match="persistent workspace close"):
            receipt_owner.close()
        assert receipt_owner.state == "close-failed"
        os.fstat(target)
        receipt_owner.close()
        assert receipt_owner.closed


def test_owned_workspace_existing_destination_receipt_retains_exact_orphan(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="a" * 64,
        files=(WorkspaceFile("new", max_bytes=3),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        destination.mkdir(mode=0o700)
        (destination / "old").write_bytes(b"old")
        expected_destination = capture_directory_ownership(destination)
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=expected_destination,
        )
        assert workspace.destination == destination
        assert workspace.expected_destination_ownership == expected_destination
        workspace.write_file("new", [b"new"])
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()

        workspace.publish_into(receipt_owner)

        orphan = receipt_owner.receipt.orphan
        assert orphan is not None
        assert (
            orphan.reopen(lambda reader: reader.read_bytes("old", max_bytes=3))
            == b"old"
        )
        assert (
            receipt_owner.consume(
                lambda _receipt, reader: reader.read_bytes("new", max_bytes=3)
            )
            == b"new"
        )
        receipt_owner.close()


def test_owned_workspace_receipt_constructor_interruption_keeps_cleanup_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(subject_digest="b" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()

        def interrupt_constructor(self, **kwargs) -> None:
            self._transfer = kwargs["transfer"]
            raise SystemExit("workspace receipt constructor interruption")

        monkeypatch.setattr(
            PublishedWorkspaceReceipt,
            "__init__",
            interrupt_constructor,
        )

        with pytest.raises(SystemExit, match="constructor interruption"):
            workspace.publish_into(receipt_owner)

        assert destination.is_dir()
        assert receipt_owner.state == "cleanup"
        with pytest.raises(RuntimeError, match="ReceiptOwner"):
            workspace.close()
        receipt_owner.close()
        assert receipt_owner.closed


def test_owned_workspace_reservation_after_store_interruption_is_cleanup_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(subject_digest="c" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        real_reserve = PublishedWorkspaceReceiptOwner._reserve

        def reserve_then_interrupt(self, reservation) -> None:
            real_reserve(self, reservation)
            raise KeyboardInterrupt("reservation return interruption")

        monkeypatch.setattr(
            PublishedWorkspaceReceiptOwner,
            "_reserve",
            reserve_then_interrupt,
        )

        with pytest.raises(KeyboardInterrupt, match="reservation return"):
            workspace.publish_into(receipt_owner)

        assert stage.is_dir()
        assert not destination.exists()
        assert receipt_owner.state == "cleanup"
        receipt_owner.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_owned_workspace_child_closes_reserved_publication_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(subject_digest="c" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()

        def fork_while_reserved(*_args, **_kwargs) -> None:
            authority = workspace._parent_owner.authority
            assert authority is not None
            internal_descriptors = {
                authority.resource,
                *(record.descriptor for record in workspace._resources._records),
            }
            internal_descriptors.discard(-1)
            child = os.fork()
            if child == 0:  # pragma: no branch - exact child result via exit
                signal.signal(signal.SIGALRM, lambda *_signal_args: os._exit(80))
                signal.alarm(3)
                try:
                    receipt_owner.close()
                except RuntimeError as exc:
                    if "PID boundary" not in str(exc):
                        os._exit(81)
                except BaseException:  # noqa: B036 - child reports by status
                    os._exit(82)
                else:
                    os._exit(83)
                for descriptor in internal_descriptors:
                    try:
                        os.fstat(descriptor)
                    except OSError as exc:
                        if exc.errno != errno.EBADF:
                            os._exit(84)
                    else:
                        os._exit(85)
                os._exit(0)
            _pid, status = os.waitpid(child, 0)
            assert os.WIFEXITED(status)
            assert os.WEXITSTATUS(status) == 0
            for descriptor in internal_descriptors:
                os.fstat(descriptor)
            raise RuntimeError("stop after reserved child cleanup")

        monkeypatch.setattr(
            captured_directory,
            "_publish_staged_directory_with_authority",
            fork_while_reserved,
        )

        with pytest.raises(RuntimeError, match="reserved child cleanup"):
            workspace.publish_into(receipt_owner)

        assert receipt_owner.state == "cleanup"
        receipt_owner.close()


def test_owned_workspace_first_transfer_store_interruption_is_reconciled(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(subject_digest="e" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()

        with pytest.raises(SystemExit, match="first transfer store"):
            _interrupt_after_store_attr(
                lambda: workspace.publish_into(receipt_owner),
                target_code=OwnedWorkspaceAuthority._publish_into_locked.__code__,
                attribute="_publication_transfer",
                error=SystemExit("first transfer store interruption"),
            )

        assert workspace.state == "closed"
        assert receipt_owner.state == "empty"
        assert stage.is_dir()
        assert not destination.exists()
        receipt_owner.close()


def test_owned_workspace_post_install_failure_does_not_deadlock_owner_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(subject_digest="f" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        installed = threading.Event()
        observer_entered_workspace = threading.Event()
        observed_states: list[str] = []
        failures: list[BaseException] = []
        real_install = PublishedWorkspaceReceiptOwner._install
        real_closed = OwnedWorkspaceAuthority._publication_transfer_closed

        def install_then_interrupt(self, reservation, receipt) -> None:
            real_install(self, reservation, receipt)
            installed.set()
            assert observer_entered_workspace.wait(timeout=5)
            raise KeyboardInterrupt("post-install publication interruption")

        def observe_workspace_close(self, transfer) -> bool:
            observer_entered_workspace.set()
            return real_closed(self, transfer)

        monkeypatch.setattr(
            PublishedWorkspaceReceiptOwner,
            "_install",
            install_then_interrupt,
        )
        monkeypatch.setattr(
            OwnedWorkspaceAuthority,
            "_publication_transfer_closed",
            observe_workspace_close,
        )

        def observe_owner() -> None:
            try:
                assert installed.wait(timeout=5)
                observed_states.append(receipt_owner.state)
            except BaseException as exc:  # noqa: B036 - report worker failure
                failures.append(exc)

        observer = threading.Thread(target=observe_owner, daemon=True)
        publisher_errors: list[BaseException] = []

        def publish() -> None:
            try:
                workspace.publish_into(receipt_owner)
            except BaseException as exc:  # noqa: B036 - report worker failure
                publisher_errors.append(exc)

        publisher = threading.Thread(target=publish, daemon=True)
        observer.start()
        publisher.start()
        publisher.join(timeout=5)
        observer.join(timeout=5)

        assert not publisher.is_alive()
        assert not observer.is_alive()
        assert not failures
        assert len(publisher_errors) == 1
        assert isinstance(publisher_errors[0], KeyboardInterrupt)
        assert "post-install publication" in str(publisher_errors[0])
        assert observed_states == ["active"]
        assert receipt_owner.active
        receipt_owner.close()


def test_owned_workspace_consume_and_close_are_linear(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(subject_digest="d" * 64)
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors=directories,
            plan=plan,
            expected_destination=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        workspace.publish_into(receipt_owner)
        consuming = threading.Event()
        release_consumer = threading.Event()
        consumer_done = threading.Event()
        close_done = threading.Event()
        failures: list[BaseException] = []

        def consume() -> None:
            try:

                def hold_reader(_receipt, _reader) -> None:
                    consuming.set()
                    assert release_consumer.wait(timeout=5)

                receipt_owner.consume(hold_reader)
                consumer_done.set()
            except BaseException as exc:  # noqa: B036 - report from worker
                failures.append(exc)

        def close() -> None:
            try:
                receipt_owner.close()
                close_done.set()
            except BaseException as exc:  # noqa: B036 - report from worker
                failures.append(exc)

        consumer = threading.Thread(target=consume)
        closer = threading.Thread(target=close)
        consumer.start()
        assert consuming.wait(timeout=5)
        closer.start()
        assert not close_done.wait(timeout=0.1)
        release_consumer.set()
        consumer.join(timeout=5)
        closer.join(timeout=5)

        assert not failures
        assert consumer_done.is_set()
        assert close_done.is_set()
        assert receipt_owner.closed
