# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import os
import signal
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Iterator

import pytest

import codenib._atomic_directory as atomic_directory
import codenib._captured_directory as captured_directory
import codenib._windows_fs_authority as windows_authority
from codenib._atomic_directory import (
    _TreeOwnership,
    capture_directory_ownership,
    directory_ownership_file_records,
    publication_parent_identity,
)
from codenib._captured_directory import (
    AuthenticatedSnapshotReader,
    CapturedDirectoryReader,
    OwnedDirectoryStage,
    OwnedWorkspaceAuthority,
    PublishedWorkspaceDestinationBinding,
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


def _publish_payload_generation(
    parent: Path,
    *,
    destination_name: str = "published",
    stage_name: str = ".source-stage",
    payload: bytes = b"same bytes",
) -> tuple[Path, WorkspacePlan, PublishedWorkspaceReceiptOwner]:
    """Publish one real generation and retain its caller-owned receipt."""

    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = parent / destination_name
    stage = parent / stage_name
    stage.mkdir(mode=0o700)
    plan = WorkspacePlan(
        subject_digest="f" * 64,
        files=(WorkspaceFile("payload", max_bytes=len(payload)),),
    )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(parent, flags)
    root_descriptor = os.open(stage, flags)
    workspace = OwnedWorkspaceAuthority()
    owner = PublishedWorkspaceReceiptOwner()
    try:
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_descriptor,
            root_descriptor=root_descriptor,
            directory_descriptors={},
            plan=plan,
            destination_binding=None,
        )
        workspace.write_file("payload", (payload,))
        workspace.seal()
        workspace.publish_into(owner)
    except BaseException:
        if owner.state == "empty":
            workspace.close()
        owner.close()
        raise
    finally:
        os.close(root_descriptor)
        os.close(parent_descriptor)
    return destination, plan, owner


def _as_windows_ownership(ownership: object) -> object:
    root_identity = list(atomic_directory.directory_ownership_root_identity(ownership))
    root_identity[3] |= windows_authority.FILE_ATTRIBUTE_DIRECTORY
    root_version = list(
        atomic_directory.directory_ownership_root_version_identity(ownership)
    )
    root_version[4] |= windows_authority.FILE_ATTRIBUTE_DIRECTORY
    entries = []
    for path, kind, identity in atomic_directory.directory_ownership_entry_identities(
        ownership
    ):
        adjusted = list(identity)
        if kind == "directory":
            adjusted[4] |= windows_authority.FILE_ATTRIBUTE_DIRECTORY
        entries.append((path, kind, tuple(adjusted)))
    return replace(
        ownership,
        root_identity=tuple(root_identity),
        root_version_identity=tuple(root_version),
        entry_identities=tuple(entries),
    )


class _FakeCapturedWindowsApi:
    """HANDLE-relative tree model with no path-based file access."""

    def __init__(self, ownership: object, payloads: dict[str, bytes]) -> None:
        root_identity = atomic_directory.directory_ownership_root_version_identity(
            ownership
        )
        entries = atomic_directory.directory_ownership_entry_identities(ownership)
        self.nodes: dict[int, dict[str, object]] = {}
        self.handles: dict[int, int] = {}
        self.offsets: dict[int, int] = {}
        self.next_node = 1
        self.next_handle = 100
        self.open_relative_calls: list[tuple[int, str, bool, bool]] = []
        self.fail_next_close = False
        self.root_id = self._add_node(root_identity, data=b"")
        paths: dict[str, int] = {"": self.root_id}
        for path, kind, identity in sorted(
            entries,
            key=lambda item: (item[0].count("/"), item[0]),
        ):
            parent, _, name = path.rpartition("/")
            node = self._add_node(identity, data=payloads.get(path, b""))
            children = self.nodes[paths[parent]]["children"]
            assert isinstance(children, dict)
            children[name] = node
            paths[path] = node
            assert stat.S_ISDIR(identity[2]) is (kind == "directory")

    def _add_node(self, identity: tuple[int, ...], *, data: bytes) -> int:
        node = self.next_node
        self.next_node += 1
        self.nodes[node] = {
            "identity": identity,
            "file_id_128": node.to_bytes(16, "big"),
            "data": data,
            "children": {},
        }
        return node

    def _new_handle(self, node: int) -> int:
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = node
        self.offsets[handle] = 0
        return handle

    def root_handle(self) -> int:
        return self._new_handle(self.root_id)

    def metadata(self, handle: int) -> windows_authority.WindowsHandleMetadata:
        node = self.nodes[self.handles[handle]]
        identity = node["identity"]
        file_id_128 = node["file_id_128"]
        assert isinstance(identity, tuple)
        assert isinstance(file_id_128, bytes)
        return windows_authority.WindowsHandleMetadata(
            st_dev=identity[0],
            st_ino=identity[1],
            st_mode=identity[2],
            st_size=identity[3],
            st_file_attributes=identity[4],
            st_mtime_ns=identity[5],
            st_ctime_ns=identity[6],
            st_nlink=identity[7],
            file_id_128=file_id_128,
        )

    def iter_directory(
        self,
        handle: int,
    ) -> tuple[windows_authority.WindowsDirectoryEntry, ...]:
        node = self.nodes[self.handles[handle]]
        children = node["children"]
        assert isinstance(children, dict)
        output = []
        for name, child_id in children.items():
            child = self.nodes[child_id]
            identity = child["identity"]
            file_id_128 = child["file_id_128"]
            assert isinstance(identity, tuple)
            assert isinstance(file_id_128, bytes)
            output.append(
                windows_authority.WindowsDirectoryEntry(
                    name=name,
                    file_id=identity[1],
                    attributes=identity[4]
                    | (
                        windows_authority.FILE_ATTRIBUTE_DIRECTORY
                        if stat.S_ISDIR(identity[2])
                        else 0
                    ),
                    file_id_128=file_id_128,
                )
            )
        return tuple(output)

    def open_relative(
        self,
        parent_handle: int,
        name: str,
        *,
        desired_access: int,
        is_directory: bool,
        allow_reparse: bool,
    ) -> int:
        del desired_access
        self.open_relative_calls.append(
            (parent_handle, name, is_directory, allow_reparse)
        )
        parent = self.nodes[self.handles[parent_handle]]
        children = parent["children"]
        assert isinstance(children, dict)
        matches = [
            child
            for child_name, child in children.items()
            if child_name.casefold() == name.casefold()
        ]
        if len(matches) != 1:
            raise FileNotFoundError(name)
        child = matches[0]
        identity = self.nodes[child]["identity"]
        assert isinstance(identity, tuple)
        assert stat.S_ISDIR(identity[2]) is is_directory
        assert not allow_reparse
        return self._new_handle(child)

    def read(self, handle: int, size: int) -> bytes:
        node = self.nodes[self.handles[handle]]
        data = node["data"]
        assert isinstance(data, bytes)
        offset = self.offsets[handle]
        block = data[offset : offset + size]
        self.offsets[handle] += len(block)
        return block

    def close(self, handle: int) -> None:
        if self.fail_next_close:
            self.fail_next_close = False
            raise OSError("injected HANDLE close failure")
        self.handles.pop(handle)
        self.offsets.pop(handle)

    def replace_child_with_identical_tree(self, name: str) -> None:
        children = self.nodes[self.root_id]["children"]
        assert isinstance(children, dict)
        old_id = children[name]
        old = self.nodes[old_id]
        identity = old["identity"]
        assert isinstance(identity, tuple)
        replacement_identity = (
            identity[0],
            identity[1] + 10_000,
            *identity[2:],
        )
        replacement = self._add_node(replacement_identity, data=b"")
        replacement_children = self.nodes[replacement]["children"]
        old_children = old["children"]
        assert isinstance(replacement_children, dict)
        assert isinstance(old_children, dict)
        replacement_children.update(old_children)
        children[name] = replacement

    def replace_nested_file_with_identical_bytes(
        self,
        directory: str,
        name: str,
    ) -> None:
        root_children = self.nodes[self.root_id]["children"]
        assert isinstance(root_children, dict)
        directory_node = self.nodes[root_children[directory]]
        children = directory_node["children"]
        assert isinstance(children, dict)
        old = self.nodes[children[name]]
        identity = old["identity"]
        data = old["data"]
        assert isinstance(identity, tuple)
        assert isinstance(data, bytes)
        children[name] = self._add_node(identity, data=data)


class _FakeCapturedWindowsAuthority:
    def __init__(self, api: _FakeCapturedWindowsApi) -> None:
        self.api = api
        self.handles = [api.root_handle()]
        self.closed = False
        self.expected_root = api.root_id

    @property
    def handle(self) -> int:
        if self.closed:
            raise RuntimeError("fake Windows authority is closed")
        return self.handles[-1]

    def verify(self) -> None:
        if self.closed or self.api.root_id != self.expected_root:
            raise RuntimeError("Windows lexical directory binding changed")
        self.api.metadata(self.handle)

    def close(self) -> None:
        if self.closed:
            return
        for handle in reversed(self.handles):
            self.api.close(handle)
        self.handles.clear()
        self.closed = True


def _install_fake_windows_reader(
    monkeypatch: pytest.MonkeyPatch,
    api: _FakeCapturedWindowsApi,
) -> None:
    monkeypatch.setattr(captured_directory, "_use_windows_handles", lambda: True)

    def open_authority(
        _path: Path,
        *,
        cleanup_slot: object,
    ) -> _FakeCapturedWindowsAuthority:
        authority = _FakeCapturedWindowsAuthority(api)
        cleanup_slot.own(authority)  # type: ignore[attr-defined]
        return authority

    monkeypatch.setattr(
        captured_directory._windows_fs,
        "open_lexical_directory_authority",
        open_authority,
    )


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


def test_captured_reader_rejects_nested_a_b_a_when_ctime_advances(
    tmp_path: Path,
    filesystem_ctime_tick,
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    source = nested / "payload"
    source.write_bytes(b"same bytes")
    ownership = capture_directory_ownership(root)
    reader = CapturedDirectoryReader(root, ownership)
    saved = tmp_path / "saved-nested"

    captured_ctime_ns = nested.stat().st_ctime_ns
    if sys.platform != "win32":
        # POSIX endpoint checks do not provide a namespace event history. Make
        # the metadata-version signal deterministic without mutating the target.
        filesystem_ctime_tick(captured_ctime_ns)

    nested.rename(saved)
    if sys.platform != "win32":
        assert saved.stat().st_ctime_ns != captured_ctime_ns
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


def test_captured_reader_uses_windows_handle_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload").write_bytes(b"captured bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(
        ownership,
        {"nested/payload": b"captured bytes"},
    )
    _install_fake_windows_reader(monkeypatch, api)

    reader = CapturedDirectoryReader(root, ownership)
    try:
        assert reader.record("nested/payload").size == len(b"captured bytes")
        assert reader.read_bytes("nested/payload", max_bytes=64) == b"captured bytes"
        reader.verify_root()
    finally:
        reader.close()

    assert api.open_relative_calls
    assert all(not call[3] for call in api.open_relative_calls)
    assert not api.handles


def test_captured_windows_reader_rejects_identical_nested_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload").write_bytes(b"same bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(
        ownership,
        {"nested/payload": b"same bytes"},
    )
    _install_fake_windows_reader(monkeypatch, api)
    reader = CapturedDirectoryReader(root, ownership)
    api.replace_child_with_identical_tree("nested")

    try:
        with pytest.raises(ValueError, match="captured path is not a real directory"):
            reader.read_bytes("nested/payload", max_bytes=64)
    finally:
        reader.close()

    assert not api.handles


def test_captured_windows_reader_rejects_open_file_namespace_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload").write_bytes(b"same bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(
        ownership,
        {"nested/payload": b"same bytes"},
    )
    _install_fake_windows_reader(monkeypatch, api)
    reader = CapturedDirectoryReader(root, ownership)
    source = reader.open_file("nested/payload", max_bytes=64)
    api.replace_nested_file_with_identical_bytes("nested", "payload")

    try:
        with pytest.raises(ValueError, match="captured file changed while reading"):
            source.authenticate()
    finally:
        source.close()
        reader.close()

    assert not api.handles


def test_captured_windows_reader_preserves_primary_and_retries_handle_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(ownership, {"payload": b"bytes"})
    _install_fake_windows_reader(monkeypatch, api)
    reader = CapturedDirectoryReader(root, ownership)
    source = reader.open_file("payload", max_bytes=64)
    api.fail_next_close = True

    with pytest.raises(RuntimeError, match="primary failure") as caught:
        with source:
            raise RuntimeError("primary failure")

    assert caught.value.captured_directory_cleanup_owner is source
    assert api.handles
    source.close()
    reader.close()
    assert not api.handles


def test_captured_windows_reader_retains_success_path_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(ownership, {"payload": b"bytes"})
    _install_fake_windows_reader(monkeypatch, api)
    reader = CapturedDirectoryReader(root, ownership)
    api.fail_next_close = True

    with pytest.raises(OSError, match="injected HANDLE close failure") as caught:
        reader.read_bytes("payload", max_bytes=64)

    source = caught.value.captured_directory_cleanup_owner
    assert isinstance(source, captured_directory.AuthenticatedFile)
    assert api.handles
    source.close()
    reader.close()
    assert not api.handles


def test_captured_windows_reader_rejects_descriptor_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(ownership, {"payload": b"bytes"})
    _install_fake_windows_reader(monkeypatch, api)
    reader = CapturedDirectoryReader(root, ownership)
    root_handles = set(api.handles)

    try:
        with pytest.raises(RuntimeError, match="available only on POSIX"):
            with reader.authenticated_descriptor("payload", max_bytes=64):
                raise AssertionError("descriptor authority must not be yielded")
        assert set(api.handles) == root_handles
    finally:
        reader.close()

    assert not api.handles


def test_captured_windows_reader_bounds_live_namespace_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(ownership, {"payload": b"bytes"})
    _install_fake_windows_reader(monkeypatch, api)
    reader = CapturedDirectoryReader(root, ownership)
    yielded = 0

    def unbounded_entries(_handle: int):
        nonlocal yielded
        while True:
            yielded += 1
            yield windows_authority.WindowsDirectoryEntry(
                name=f"foreign-{yielded}",
                file_id=10_000 + yielded,
                attributes=0,
                file_id_128=(10_000 + yielded).to_bytes(16, "big"),
            )

    monkeypatch.setattr(api, "iter_directory", unbounded_entries)
    try:
        with pytest.raises(RuntimeError, match="ownership entry limit"):
            reader.open_file("payload", max_bytes=64)
    finally:
        reader.close()

    assert yielded == len(reader._entry_identities) + 1
    assert not api.handles


def test_captured_windows_reader_retains_file_owner_during_return_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(ownership, {"payload": b"bytes"})
    _install_fake_windows_reader(monkeypatch, api)
    reader = CapturedDirectoryReader(root, ownership)
    real_open = captured_directory.CapturedDirectoryReader._open_windows
    interruption = KeyboardInterrupt("after authenticated HANDLE acquisition")

    def open_then_interrupt(
        self: CapturedDirectoryReader,
        relative: object,
    ) -> object:
        real_open(self, relative)  # type: ignore[arg-type]
        raise interruption

    monkeypatch.setattr(
        captured_directory.CapturedDirectoryReader,
        "_open_windows",
        open_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        reader.open_file("payload", max_bytes=64)

    assert caught.value is interruption
    assert len(api.handles) == 2
    reader.close()
    assert not api.handles


def test_captured_windows_copy_chunks_preserves_consumer_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"bytes")
    ownership = _as_windows_ownership(capture_directory_ownership(root))
    api = _FakeCapturedWindowsApi(ownership, {"payload": b"bytes"})
    _install_fake_windows_reader(monkeypatch, api)
    reader = CapturedDirectoryReader(root, ownership)
    chunks = reader.copy_chunks("payload", max_bytes=64)
    primary = KeyboardInterrupt("consumer cancellation")

    assert next(chunks) == b"bytes"
    api.fail_next_close = True
    with pytest.raises(KeyboardInterrupt) as caught:
        chunks.throw(primary)

    assert caught.value is primary
    source = primary.captured_directory_cleanup_owner
    assert isinstance(source, captured_directory.AuthenticatedFile)
    source.close()
    reader.close()
    assert not api.handles


def test_owned_stage_publishes_its_final_full_tree(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    stage = OwnedDirectoryStage(destination)
    stage.write_file("nested/payload", [b"owned bytes"])

    stage.publish(expected_destination_ownership=None)

    assert (destination / "nested" / "payload").read_bytes() == b"owned bytes"


def test_owned_stage_rejects_root_replacement_before_publish(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    stage = OwnedDirectoryStage(destination)
    stage.write_file("payload", [b"owned bytes"])
    stolen = tmp_path / "stolen-stage"
    stage.path.rename(stolen)
    stage.path.mkdir()
    (stage.path / "foreign").write_bytes(b"foreign")

    try:
        with pytest.raises(RuntimeError, match="owned stage root changed"):
            stage.publish(expected_destination_ownership=None)
    finally:
        stage.discard()

    assert not destination.exists()
    assert (stolen / "payload").read_bytes() == b"owned bytes"
    assert (stage.path / "foreign").read_bytes() == b"foreign"


def test_authenticated_descriptor_exposes_only_a_sealed_good_snapshot(
    tmp_path: Path,
    filesystem_ctime_tick,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "payload"
    source.write_bytes(b"GOOD")
    captured_ctime_ns = source.stat().st_ctime_ns
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))
    observed = b""

    try:
        with pytest.raises(ValueError, match="changed after authentication"):
            with reader.authenticated_descriptor("payload", max_bytes=4) as (
                descriptor,
                record,
            ):
                assert record.size == 4
                filesystem_ctime_tick(captured_ctime_ns)
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


@pytest.mark.parametrize("blocked_errno", [errno.ENOSYS, errno.EPERM])
def test_authenticated_snapshot_falls_back_when_memfd_syscall_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_errno: int,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = b"portable snapshot"
    (root / "payload").write_bytes(payload)
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))

    def blocked(*_args, **_kwargs) -> int:
        raise OSError(blocked_errno, "memfd blocked")

    monkeypatch.setattr(captured_directory.os, "memfd_create", blocked, raising=False)
    try:
        with reader.authenticated_snapshot("payload") as (snapshot, _record):
            assert snapshot.read() == payload
            with pytest.raises(RuntimeError, match="no sealed descriptor"):
                _ = snapshot.descriptor
    finally:
        reader.close()


def test_authenticated_snapshot_does_not_hide_memfd_resource_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"payload")
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))

    def exhausted(*_args, **_kwargs) -> int:
        raise OSError(errno.EMFILE, "descriptor limit")

    monkeypatch.setattr(
        captured_directory.os,
        "memfd_create",
        exhausted,
        raising=False,
    )
    try:
        with pytest.raises(ValueError, match="copied immutably") as caught:
            with reader.authenticated_snapshot("payload"):
                pass
        assert isinstance(caught.value.__cause__, OSError)
        assert caught.value.__cause__.errno == errno.EMFILE
    finally:
        reader.close()


def test_authenticated_snapshot_falls_back_when_sealing_is_blocked_and_closes_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = b"seal fallback"
    (root / "payload").write_bytes(payload)
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))
    created: list[int] = []

    def create_stage() -> int:
        descriptor = os.open(
            tmp_path / "snapshot-stage",
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created.append(descriptor)
        return descriptor

    def blocked_seal(
        _descriptor: int,
        command: int,
        *_args: object,
    ) -> int:
        assert command == captured_directory._F_ADD_SEALS
        raise OSError(errno.EPERM, "sealing blocked")

    monkeypatch.setattr(captured_directory, "_create_sealable_memfd", create_stage)
    assert captured_directory._fcntl is not None
    monkeypatch.setattr(captured_directory._fcntl, "fcntl", blocked_seal)
    try:
        with reader.authenticated_snapshot("payload") as (snapshot, _record):
            assert snapshot.read() == payload
    finally:
        reader.close()

    assert len(created) == 1
    with pytest.raises(OSError) as caught:
        os.fstat(created[0])
    assert caught.value.errno == errno.EBADF


def test_authenticated_snapshot_blocked_memfd_obeys_memory_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"oversized")
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))
    source = reader.open_file("payload")

    def blocked(*_args, **_kwargs) -> int:
        raise OSError(errno.EPERM, "memfd blocked")

    def forbidden_copy(*_args, **_kwargs) -> None:
        raise AssertionError("oversized fallback copied bytes into memory")

    monkeypatch.setattr(captured_directory.os, "memfd_create", blocked, raising=False)
    monkeypatch.setattr(captured_directory, "_MAX_IN_MEMORY_SNAPSHOT_BYTES", 1)
    monkeypatch.setattr(
        captured_directory.AuthenticatedFile,
        "_copy_authenticated_from_start",
        forbidden_copy,
    )
    try:
        with pytest.raises(ValueError, match="fallback limit"):
            source.immutable_snapshot()
    finally:
        source.close()
        reader.close()


@pytest.mark.parametrize("force_memory_fallback", [False, True])
def test_authenticated_snapshot_rewinds_after_partial_source_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_memory_fallback: bool,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = b"abcdef"
    (root / "payload").write_bytes(payload)
    reader = CapturedDirectoryReader(root, capture_directory_ownership(root))
    source = reader.open_file("payload", max_bytes=len(payload))

    if force_memory_fallback:

        def unavailable() -> int:
            raise captured_directory._SnapshotUnavailable("unsupported host")

        monkeypatch.setattr(
            captured_directory,
            "_create_sealable_memfd",
            unavailable,
        )

    try:
        assert source.read(2) == b"ab"
        with source.immutable_snapshot() as snapshot:
            assert snapshot.record.size == len(payload)
            assert snapshot.read() == payload
        source.verify_unchanged()
    finally:
        source.close()
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


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX surrogateescape")
def test_workspace_plan_and_publication_preserve_raw_filesystem_paths(
    tmp_path: Path,
) -> None:
    raw_name = os.fsdecode(b"payload-\xff.bin")
    plan = WorkspacePlan(
        subject_digest="c" * 64,
        files=(WorkspaceFile(raw_name, max_bytes=3),),
    )
    same_plan = WorkspacePlan(
        subject_digest="c" * 64,
        files=(WorkspaceFile(PurePosixPath(raw_name), max_bytes=3),),
    )

    assert plan.digest == same_plan.digest
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
            destination_binding=None,
        )
        workspace.write_file(raw_name, [b"raw"])
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        workspace.publish_into(receipt_owner)

        assert os.fsencode(next(destination.iterdir()).name) == os.fsencode(raw_name)
        assert (
            receipt_owner.consume(
                lambda _receipt, reader: reader.read_bytes(raw_name, max_bytes=3)
            )
            == b"raw"
        )
        receipt_owner.close()


@pytest.mark.parametrize(
    ("limit_name", "limit"),
    [
        ("_MAX_OWNERSHIP_ENTRIES", 1),
        ("_MAX_OWNERSHIP_METADATA_BYTES", 1),
    ],
)
def test_workspace_plan_uses_exact_ownership_scanner_budgets(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
) -> None:
    monkeypatch.setattr(atomic_directory, limit_name, limit)

    with pytest.raises(ValueError, match="ownership scanner budget"):
        WorkspacePlan(
            subject_digest="d" * 64,
            files=(
                WorkspaceFile("first", max_bytes=1),
                WorkspaceFile("second", max_bytes=1),
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
            destination_binding=None,
        )
        assert workspace.destination == destination
        assert workspace.expected_destination_binding is None
        root_record = workspace.write_file("root.bin", [b"root"])
        nested_record = workspace.write_file("nested/payload.bin", [b"payload"])
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
                destination_binding=None,
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
                destination_binding=None,
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
                destination_binding=None,
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
                destination_binding=None,
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
            destination_binding=None,
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


def test_preopened_workspace_rejects_darwin_before_state_or_resource_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="4" * 64,
        files=(WorkspaceFile("payload", max_bytes=4),),
    )
    with _preopened_workspace(tmp_path, plan) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()

        def forbidden(*_args, **_kwargs) -> None:
            raise AssertionError("Darwin adoption acquired a lifecycle resource")

        with monkeypatch.context() as patch:
            patch.setattr(captured_directory.sys, "platform", "darwin")
            patch.setattr(
                captured_directory,
                "_open_publication_authority",
                forbidden,
            )
            patch.setattr(
                captured_directory._CancellationSafeRLock,
                "run",
                forbidden,
            )
            with pytest.raises(UnsupportedWorkspaceCreation, match="requires Linux"):
                workspace.adopt(
                    destination=destination,
                    stage_name=stage.name,
                    parent_descriptor=parent_fd,
                    root_descriptor=root_fd,
                    directory_descriptors=directories,
                    plan=plan,
                    destination_binding=None,
                )

        assert workspace.state == "empty"
        assert workspace._destination is None
        assert workspace._stage_path is None
        assert workspace._plan is None
        assert workspace._parent_owner.authority is None
        assert workspace._resources.closed
        assert workspace._root_descriptor == -1
        assert workspace._directory_descriptors == {}
        os.fstat(parent_fd)
        os.fstat(root_fd)


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
            destination_binding=None,
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
            destination_binding=None,
        )
        workspace.write_file("payload", [b"safe"])

        ownership = workspace.seal()

        assert workspace.seal() is ownership
        workspace.close()


def test_preopened_workspace_seal_return_interruption_keeps_token_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            destination_binding=None,
        )
        workspace.write_file("payload", [b"safe"])

        real_seal = workspace.seal
        interruption = KeyboardInterrupt("seal return")
        interrupted = False

        def seal_then_interrupt() -> object:
            nonlocal interrupted
            ownership = real_seal()
            if not interrupted:
                interrupted = True
                raise interruption
            return ownership

        monkeypatch.setattr(workspace, "seal", seal_then_interrupt)

        with pytest.raises(KeyboardInterrupt, match="seal return"):
            workspace.seal()

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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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


def test_published_destination_binding_is_private_and_active_owner_derived(
    tmp_path: Path,
) -> None:
    empty_owner = PublishedWorkspaceReceiptOwner()
    with pytest.raises(RuntimeError, match="empty, expected active"):
        _ = empty_owner.destination_binding
    empty_owner.close()
    with pytest.raises(RuntimeError, match="closed, expected active"):
        _ = empty_owner.destination_binding

    destination, _plan, owner = _publish_payload_generation(tmp_path / "binding-parent")
    try:
        binding = owner.destination_binding
        receipt = owner.receipt
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        parent_descriptor = os.open(destination.parent, flags)
        try:
            assert type(binding) is PublishedWorkspaceDestinationBinding
            assert binding.destination == destination
            assert binding.parent_identity == publication_parent_identity(
                parent_descriptor
            )
        finally:
            os.close(parent_descriptor)
        assert type(binding.ownership) is _TreeOwnership
        assert binding.ownership == capture_directory_ownership(destination)
        assert binding is owner.destination_binding
        assert (
            owner.consume(lambda _receipt, _reader: owner.destination_binding)
            is binding
        )

        with pytest.raises(TypeError, match="minted by an active receipt owner"):
            PublishedWorkspaceDestinationBinding(
                destination=destination,
                parent_identity=binding.parent_identity,
                ownership=binding.ownership,
            )
        with pytest.raises(AttributeError):
            binding.destination = tmp_path / "forged"  # type: ignore[misc]

        original_path = receipt.path
        original_parent = receipt.parent_identity
        original_ownership = receipt.ownership
        object.__setattr__(receipt, "path", tmp_path / "wrong-receipt-path")
        object.__setattr__(receipt, "parent_identity", (0, 0))
        object.__setattr__(
            receipt,
            "ownership",
            capture_directory_ownership(destination.parent, allow_empty_root=True),
        )
        try:
            assert owner.destination_binding is binding
            assert binding.destination == destination
            assert binding.parent_identity != receipt.parent_identity
            assert binding.ownership != receipt.ownership
        finally:
            object.__setattr__(receipt, "path", original_path)
            object.__setattr__(receipt, "parent_identity", original_parent)
            object.__setattr__(receipt, "ownership", original_ownership)
    finally:
        owner.close()

    with pytest.raises(RuntimeError, match="closed, expected active"):
        _ = owner.destination_binding


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_published_destination_binding_cannot_cross_pid_boundary(
    tmp_path: Path,
) -> None:
    _destination, _plan, owner = _publish_payload_generation(
        tmp_path / "binding-parent"
    )
    child = os.fork()
    if child == 0:  # pragma: no branch - exact child result via exit
        try:
            _ = owner.destination_binding
        except RuntimeError as error:
            os._exit(0 if "PID boundary" in str(error) else 81)
        except BaseException:  # noqa: B036 - child reports exact failure
            os._exit(82)
        os._exit(83)

    _pid, status = os.waitpid(child, 0)
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert owner.active
    owner.close()


@pytest.mark.parametrize(
    ("mutation", "error_type", "message"),
    [
        ("wrong-path", ValueError, "binding differs from its destination"),
        ("wrong-parent", RuntimeError, "parent differs from its binding"),
        ("wrong-tree", RuntimeError, "destination changed"),
        ("same-bytes-new-inode", RuntimeError, "destination changed"),
        ("raw-tree-token", TypeError, "destination binding is invalid"),
    ],
)
def test_owned_workspace_rejects_inauthentic_destination_binding(
    tmp_path: Path,
    mutation: str,
    error_type: type[Exception],
    message: str,
) -> None:
    payload = b"same bytes"
    parent = tmp_path / "binding-parent"
    destination, plan, owner = _publish_payload_generation(
        parent,
        payload=payload,
    )
    binding: object = owner.destination_binding
    adopted_destination = destination

    if mutation == "wrong-path":
        adopted_destination = parent / "wrong-destination"
    elif mutation == "wrong-parent":
        parked_parent = tmp_path / "original-binding-parent"
        parent.rename(parked_parent)
        parent.mkdir(mode=0o700)
        destination.mkdir(mode=0o700)
        (destination / "payload").write_bytes(payload)
    elif mutation == "wrong-tree":
        (destination / "payload").write_bytes(b"wrong tree")
    elif mutation == "same-bytes-new-inode":
        original_inode = os.stat(destination, follow_symlinks=False).st_ino
        destination.rename(parent / "original-generation")
        destination.mkdir(mode=0o700)
        (destination / "payload").write_bytes(payload)
        assert os.stat(destination, follow_symlinks=False).st_ino != original_inode
    else:
        assert mutation == "raw-tree-token"
        binding = capture_directory_ownership(destination)

    stage = adopted_destination.parent / ".replacement-stage"
    stage.mkdir(mode=plan.root_mode)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(adopted_destination.parent, flags)
    root_descriptor = os.open(stage, flags)
    workspace = OwnedWorkspaceAuthority()
    try:
        with pytest.raises(error_type, match=message):
            workspace.adopt(
                destination=adopted_destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors={},
                plan=plan,
                destination_binding=binding,  # type: ignore[arg-type]
            )
    finally:
        workspace.close()
        os.close(root_descriptor)
        os.close(parent_descriptor)
        owner.close()


def test_owned_workspace_rejects_binding_from_wrong_active_receipt(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "binding-parent"
    destination, plan, owner = _publish_payload_generation(parent)
    _other, _other_plan, wrong_owner = _publish_payload_generation(
        parent,
        destination_name="other-published",
        stage_name=".other-source-stage",
    )
    stage = parent / ".replacement-stage"
    stage.mkdir(mode=plan.root_mode)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(parent, flags)
    root_descriptor = os.open(stage, flags)
    workspace = OwnedWorkspaceAuthority()
    try:
        with pytest.raises(ValueError, match="binding differs from its destination"):
            workspace.adopt(
                destination=destination,
                stage_name=stage.name,
                parent_descriptor=parent_descriptor,
                root_descriptor=root_descriptor,
                directory_descriptors={},
                plan=plan,
                destination_binding=wrong_owner.destination_binding,
            )
        assert owner.active
        assert wrong_owner.active
    finally:
        workspace.close()
        os.close(root_descriptor)
        os.close(parent_descriptor)
        wrong_owner.close()
        owner.close()


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
            destination_binding=None,
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


def test_published_validator_cannot_intercept_destination_binding_mint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            destination_binding=None,
        )
        workspace.write_file("payload", (b"safe",))
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()

        forged = object.__new__(PublishedWorkspaceDestinationBinding)
        object.__setattr__(forged, "destination", tmp_path / "forged")
        object.__setattr__(forged, "parent_identity", (0, 0))
        object.__setattr__(
            forged,
            "ownership",
            capture_directory_ownership(stage, allow_empty_root=True),
        )
        intercepted: list[dict[str, object]] = []

        def intercept_freeze(**kwargs: object) -> object:
            intercepted.append(kwargs)
            return lambda _ownership: forged

        class ForgedPublicBinding:
            pass

        def replace_public_symbols(_reader: object) -> None:
            monkeypatch.setattr(
                captured_directory,
                "_freeze_published_workspace_destination_binding_minter",
                intercept_freeze,
            )
            monkeypatch.setattr(
                captured_directory,
                "PublishedWorkspaceDestinationBinding",
                ForgedPublicBinding,
            )

        workspace.publish_into(
            receipt_owner,
            validate_published_destination=replace_public_symbols,
        )

        binding = receipt_owner.destination_binding
        assert intercepted == []
        assert type(binding) is PublishedWorkspaceDestinationBinding
        assert binding is not forged
        assert binding.destination == destination
        assert binding.parent_identity == publication_parent_identity(parent_fd)
        assert binding.ownership == capture_directory_ownership(destination)
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
            destination_binding=None,
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


def test_owned_workspace_detaches_caller_and_public_plan_mutation(
    tmp_path: Path,
) -> None:
    source_directory = WorkspaceDirectory("nested")
    source_file = WorkspaceFile("nested/payload", max_bytes=8)
    plan = WorkspacePlan(
        subject_digest="3" * 64,
        directories=(source_directory,),
        files=(source_file,),
    )
    alternate = WorkspacePlan(
        subject_digest="4" * 64,
        files=(WorkspaceFile("alternate", max_bytes=8),),
    )
    original_digest = plan.digest
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
            destination_binding=None,
        )

        object.__setattr__(source_directory, "path", PurePosixPath("rebound"))
        object.__setattr__(source_file, "path", PurePosixPath("rebound/payload"))
        object.__setattr__(source_file, "max_bytes", 1)
        object.__setattr__(plan, "subject_digest", alternate.subject_digest)
        object.__setattr__(plan, "directories", alternate.directories)
        object.__setattr__(plan, "files", alternate.files)
        object.__setattr__(plan, "digest", alternate.digest)
        authority_projection = workspace.plan
        assert authority_projection.digest == original_digest
        assert authority_projection.files[0].path.as_posix() == "nested/payload"
        assert authority_projection.files[0].max_bytes == 8

        object.__setattr__(authority_projection.files[0], "path", PurePosixPath("evil"))
        object.__setattr__(authority_projection, "files", alternate.files)
        object.__setattr__(authority_projection, "digest", alternate.digest)
        assert workspace.plan.digest == original_digest
        assert workspace.plan.files[0].path.as_posix() == "nested/payload"
        assert workspace.plan.files[0].max_bytes == 8

        workspace.write_file("nested/payload", [b"durable"])
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()
        workspace.publish_into(receipt_owner)
        receipt = receipt_owner.receipt
        assert receipt.plan_digest == original_digest

        receipt_projection = receipt.plan
        object.__setattr__(receipt_projection.files[0], "path", PurePosixPath("evil"))
        object.__setattr__(receipt_projection.files[0], "max_bytes", 1)
        object.__setattr__(receipt_projection, "directories", ())
        object.__setattr__(receipt_projection, "digest", alternate.digest)
        assert receipt.plan_digest == original_digest
        assert receipt.plan.files[0].path.as_posix() == "nested/payload"
        assert receipt.plan.files[0].max_bytes == 8
        assert (
            receipt_owner.consume(lambda borrowed, _reader: borrowed.plan_digest)
            == original_digest
        )
        receipt_owner.close()


def test_owned_workspace_rejects_polluted_plan_before_resource_acquisition(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(subject_digest="5" * 64)
    object.__setattr__(plan, "files", [])
    with _preopened_workspace(
        tmp_path, WorkspacePlan(subject_digest="5" * 64)
    ) as opened:
        destination, stage, parent_fd, root_fd, directories = opened
        workspace = OwnedWorkspaceAuthority()

        with pytest.raises(TypeError, match="exact types"):
            workspace.adopt(
                destination=destination,
                stage_name=stage.name,
                parent_descriptor=parent_fd,
                root_descriptor=root_fd,
                directory_descriptors=directories,
                plan=plan,
                destination_binding=None,
            )

        assert workspace.state == "empty"
        assert workspace._parent_owner.authority is None
        assert workspace._resources.closed
        os.fstat(parent_fd)
        os.fstat(root_fd)


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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
    parent = tmp_path / "workspace-parent"
    destination, _initial_plan, initial_owner = _publish_payload_generation(
        parent,
        destination_name="destination",
        payload=b"old",
    )
    plan = WorkspacePlan(
        subject_digest="a" * 64,
        files=(WorkspaceFile("new", max_bytes=3),),
    )
    stage = parent / ".replacement-stage"
    stage.mkdir(mode=plan.root_mode)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(parent, flags)
    root_fd = os.open(stage, flags)
    workspace = OwnedWorkspaceAuthority()
    receipt_owner = PublishedWorkspaceReceiptOwner()
    binding = initial_owner.destination_binding
    try:
        workspace.adopt(
            destination=destination,
            stage_name=stage.name,
            parent_descriptor=parent_fd,
            root_descriptor=root_fd,
            directory_descriptors={},
            plan=plan,
            destination_binding=binding,
        )
        assert workspace.destination == destination
        assert workspace.expected_destination_binding is binding
        workspace.write_file("new", [b"new"])
        workspace.seal()

        workspace.publish_into(receipt_owner)

        orphan = receipt_owner.receipt.orphan
        assert orphan is not None
        assert (
            orphan.reopen(lambda reader: reader.read_bytes("payload", max_bytes=3))
            == b"old"
        )
        assert (
            receipt_owner.consume(
                lambda _receipt, reader: reader.read_bytes("new", max_bytes=3)
            )
            == b"new"
        )
    finally:
        if receipt_owner.state == "empty":
            workspace.close()
        receipt_owner.close()
        initial_owner.close()
        os.close(root_fd)
        os.close(parent_fd)


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
            destination_binding=None,
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
            destination_binding=None,
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
            destination_binding=None,
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
    monkeypatch: pytest.MonkeyPatch,
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
            destination_binding=None,
        )
        workspace.seal()
        receipt_owner = PublishedWorkspaceReceiptOwner()

        real_store = OwnedWorkspaceAuthority._store_publication_transfer_locked
        interruption = SystemExit("first transfer store interruption")

        def store_then_interrupt(self, transfer) -> None:
            real_store(self, transfer)
            raise interruption

        monkeypatch.setattr(
            OwnedWorkspaceAuthority,
            "_store_publication_transfer_locked",
            store_then_interrupt,
        )
        with pytest.raises(SystemExit, match="first transfer store") as caught:
            workspace.publish_into(receipt_owner)

        assert caught.value is interruption

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
            destination_binding=None,
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
            destination_binding=None,
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
