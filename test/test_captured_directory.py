# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import dis
import errno
import gc
import hashlib
import inspect
import os
import signal
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Iterator

import pytest

import codenib._atomic_directory as atomic_directory
import codenib._captured_directory as captured_directory
import codenib._windows_fs_authority as windows_authority
import codenib._workspace_owner as workspace_owner
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
    OwnedPathBuildDirectory,
    OwnedWorkspaceAuthority,
    PublishedWorkspaceDestinationBinding,
    PublishedWorkspaceReceipt,
    PublishedWorkspaceReceiptOwner,
    UnsupportedWorkspaceCreation,
    WorkspaceDirectory,
    WorkspaceFile,
    WorkspacePlan,
    require_owned_workspace_publication_support,
    retry_retained_owned_path_build_cleanup,
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


def _replacement_deadline_ns() -> int:
    return time.monotonic_ns() + 10_000_000_000


def _leased_native_replacement_owner(parent: Path, destination: Path) -> object:
    if not sys.platform.startswith("linux"):
        pytest.skip("native workspace replacement is Linux-only")
    if not workspace_owner._workspace_owner_protocol_available:
        pytest.skip("native workspace-owner protocol v5 is unavailable")
    owner = workspace_owner.create_owner()
    try:
        workspace_owner.capture_owner_destination(
            owner,
            os.fsencode(parent),
            os.fsencode(destination.name),
            _replacement_deadline_ns(),
        )
        workspace_owner.acquire_owner_replacement_lease(
            owner,
            _replacement_deadline_ns(),
        )
    except BaseException:
        if not workspace_owner.owner_closed(owner):
            workspace_owner.abort_owner(owner)
        raise
    return owner


@contextmanager
def _bound_native_replacement(
    tmp_path: Path,
    *,
    old_payload: bytes = b"old",
) -> Iterator[SimpleNamespace]:
    parent = tmp_path / "native-replacement-parent"
    destination, plan, source_owner = _publish_payload_generation(
        parent,
        destination_name="published",
        payload=old_payload,
    )
    native_owner = _leased_native_replacement_owner(parent, destination)
    workspace = OwnedWorkspaceAuthority()
    output_owner = PublishedWorkspaceReceiptOwner()
    binding = source_owner.destination_binding
    try:
        workspace.bind_replacement_source(
            source_owner,
            destination_binding=binding,
            native_owner=native_owner,
            stage_name=".replacement-stage",
            plan=plan,
        )
        yield SimpleNamespace(
            binding=binding,
            destination=destination,
            native_owner=native_owner,
            output_owner=output_owner,
            parent=parent,
            plan=plan,
            source_owner=source_owner,
            stage=parent / ".replacement-stage",
            workspace=workspace,
        )
    finally:
        if not output_owner.closed:
            output_owner.close()
        if workspace.state != "closed":
            workspace.close()
        if not workspace_owner.owner_closed(native_owner):
            workspace_owner.abort_owner(native_owner)
        if not source_owner.closed:
            source_owner.close()


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


def test_owned_stage_close_retains_descriptor_after_preclose_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = OwnedDirectoryStage(tmp_path / "destination")
    descriptor = stage._descriptor
    real_close = captured_directory.os.close
    interrupted = False
    close_error = OSError(errno.EIO, "injected descriptor close failure")

    def fail_before_close(candidate: int) -> None:
        nonlocal interrupted
        if candidate == descriptor and not interrupted:
            interrupted = True
            raise close_error
        real_close(candidate)

    monkeypatch.setattr(captured_directory.os, "close", fail_before_close)

    with pytest.raises(OSError, match="descriptor close failure") as caught:
        stage.close()

    assert caught.value is close_error
    assert caught.value.captured_directory_cleanup_owner is stage
    os.fstat(descriptor)
    assert stage._descriptor == descriptor

    stage.close()
    assert stage._descriptor == -1
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF


def _retry_owned_path_build_cleanup() -> tuple[atomic_directory.DirectoryOrphan, ...]:
    orphans: list[atomic_directory.DirectoryOrphan] = []
    retry_retained_owned_path_build_cleanup(orphans.append)
    return tuple(orphans)


def _retry_owned_path_build_cleanup_for_group(
    retention_group: object,
) -> tuple[atomic_directory.DirectoryOrphan, ...]:
    orphans: list[atomic_directory.DirectoryOrphan] = []
    captured_directory._retry_retained_owned_path_build_cleanup_for_group(
        retention_group,
        orphans.append,
    )
    return tuple(orphans)


class _OpaqueRetentionGroup:
    """An unhashable route token whose value comparison must never run."""

    def __eq__(self, _other: object) -> bool:
        raise AssertionError("retention groups must be compared by identity")


def test_owned_path_build_rejects_subclass_construction_before_mutation(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()

    class ChildOwnedPathBuildDirectory(OwnedPathBuildDirectory):
        pass

    destination = tmp_path / "missing" / "attempt"

    with pytest.raises(TypeError, match="does not support subclass construction"):
        ChildOwnedPathBuildDirectory.prepare(destination, create_parent=True)
    with pytest.raises(TypeError, match="does not support subclass construction"):
        ChildOwnedPathBuildDirectory(
            destination,
            require_private_parent=True,
        )

    assert not destination.parent.exists()
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


def test_owned_path_build_captures_and_isolates_arbitrary_partial_files(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "attempt"
    owner = OwnedPathBuildDirectory.prepare(destination)

    assert owner.path.parent == parent
    assert owner.path != destination
    assert not destination.exists()
    assert "OwnedPathBuildDirectory" in captured_directory.__all__
    assert stat.S_IMODE(owner.path.stat().st_mode) == 0o700
    with owner.path_operation("partial path writer") as writer_path:
        nested = writer_path / "nested"
        nested.mkdir()
        (nested / "payload.bin").write_bytes(b"partial bytes")
        (writer_path / ".writer.lock").write_bytes(b"lock")

    ownership = owner.capture_ownership()
    assert directory_ownership_file_records(ownership) == (
        atomic_directory.TreeFileRecord(
            path=".writer.lock",
            mode=0o644,
            size=4,
            sha256=hashlib.sha256(b"lock").hexdigest(),
        ),
        atomic_directory.TreeFileRecord(
            path="nested/payload.bin",
            mode=0o644,
            size=13,
            sha256=hashlib.sha256(b"partial bytes").hexdigest(),
        ),
    )

    orphan = owner.isolate()

    assert not owner.closed
    assert not owner.path.exists()
    assert not destination.exists()
    assert (
        orphan.reopen(
            lambda reader: reader.read_bytes("nested/payload.bin", max_bytes=32)
        )
        == b"partial bytes"
    )
    assert owner.isolate() is orphan
    owner.close()
    assert owner.closed


def test_owned_path_build_requires_a_missing_destination(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "attempt"
    destination.write_bytes(b"foreign")

    with pytest.raises(FileExistsError, match="already exists"):
        OwnedPathBuildDirectory.prepare(destination)

    assert destination.read_bytes() == b"foreign"
    assert not tuple(parent.glob(".attempt.normalize-*"))


def test_owned_path_build_binds_expected_parent_identity_before_root_creation(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        expected = atomic_directory.publication_parent_identity(descriptor)
    finally:
        os.close(descriptor)
    displaced = tmp_path / "displaced"
    parent.rename(displaced)
    parent.mkdir(mode=0o700)

    with pytest.raises(
        RuntimeError,
        match="publication parent identity does not match authority",
    ):
        OwnedPathBuildDirectory.prepare(
            parent / "attempt",
            expected_parent_identity=expected,
        )

    assert not tuple(parent.iterdir())
    missing_parent = tmp_path / "missing"
    with pytest.raises(TypeError, match="parent identity is invalid"):
        OwnedPathBuildDirectory.prepare(
            missing_parent / "attempt",
            create_parent=True,
            expected_parent_identity=(1,),
        )
    assert not missing_parent.exists()
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


def test_owned_path_build_isolate_return_handoff_redelivers_retained_orphan(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    interruption = KeyboardInterrupt("isolate return handoff")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            OwnedPathBuildDirectory.isolate,
            lambda: OwnedPathBuildDirectory.prepare(parent / "attempt").isolate(),
            predicate=lambda _frame, result: isinstance(
                result,
                atomic_directory.DirectoryOrphan,
            ),
            error=interruption,
        )

    assert caught.value is interruption
    gc.collect()
    retained = tuple(captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES)
    assert len(retained) == 1
    owner = retained[0]
    assert owner._state == "closed"
    assert not owner.closed
    assert not owner.path.exists()

    orphans = _retry_owned_path_build_cleanup()

    assert len(orphans) == 1
    assert owner.closed
    assert orphans[0].reopen(lambda reader: reader.inventory()) == ()


def test_owned_path_build_requires_private_parent_before_root_creation(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    destination = parent / "attempt"

    with pytest.raises(PermissionError, match="private directory"):
        OwnedPathBuildDirectory.prepare(destination)

    assert not destination.exists()
    assert not tuple(parent.glob(".attempt.normalize-*"))

    owner = OwnedPathBuildDirectory.prepare(
        destination,
        require_private_parent=False,
    )
    owner.close()
    assert owner.closed


def test_owned_path_build_relaxed_parent_still_rejects_shared_writes(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "shared-writable"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    destination = parent / "attempt"

    with pytest.raises(PermissionError, match="owner-controlled"):
        OwnedPathBuildDirectory.prepare(
            destination,
            require_private_parent=False,
        )

    assert not tuple(parent.glob(".attempt.normalize-*"))
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs",
)
def test_owned_path_build_privacy_rejection_releases_read_only_authority(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "non-private"
    parent.mkdir(mode=0o755)
    before = len(tuple(Path("/proc/self/fd").iterdir()))

    with pytest.raises(PermissionError, match="private directory"):
        OwnedPathBuildDirectory.prepare(parent / "attempt")

    assert not tuple(parent.glob(".attempt.normalize-*"))
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


def test_owned_path_build_support_gate_precedes_parent_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "missing" / "nested" / "attempt"
    monkeypatch.setattr(captured_directory.sys, "platform", "win32")

    with pytest.raises(RuntimeError, match="unsupported"):
        OwnedPathBuildDirectory.prepare(destination, create_parent=True)

    assert not (tmp_path / "missing").exists()
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs",
)
def test_owned_path_build_stage_name_gate_precedes_parent_mutation(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "missing"
    destination_name = "a" * 220
    destination = parent / destination_name
    stage_name = f".{destination_name}.normalize-{'0' * 24}"
    assert (
        len(os.fsencode(destination_name))
        <= atomic_directory._MAX_OWNERSHIP_COMPONENT_BYTES
    )
    assert (
        len(os.fsencode(stage_name)) > atomic_directory._MAX_OWNERSHIP_COMPONENT_BYTES
    )
    before = len(tuple(Path("/proc/self/fd").iterdir()))

    with pytest.raises(ValueError, match="owned stage must be one bounded file name"):
        OwnedPathBuildDirectory.prepare(destination, create_parent=True)

    assert not parent.exists()
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert _retry_owned_path_build_cleanup() == ()
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs",
)
def test_owned_path_build_invalid_generated_stage_name_releases_opening_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "missing"
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    monkeypatch.setattr(
        captured_directory.secrets,
        "token_hex",
        lambda _: "x" * 256,
    )

    with pytest.raises(ValueError, match="owned stage must be one bounded file name"):
        OwnedPathBuildDirectory.prepare(parent / "attempt", create_parent=True)

    assert parent.is_dir()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert not tuple(parent.iterdir())
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert _retry_owned_path_build_cleanup() == ()
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs",
)
def test_owned_path_build_mkdir_race_leaves_unknown_child_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    real_mkdir = captured_directory.os.mkdir
    raced: list[Path] = []

    def race_mkdir(path, mode=0o777, *, dir_fd=None):
        if (
            not raced
            and isinstance(path, str)
            and path.startswith(".attempt.normalize-")
            and dir_fd is not None
        ):
            real_mkdir(path, mode=0o700, dir_fd=dir_fd)
            candidate = parent / path
            candidate.joinpath("valuable.bin").write_bytes(b"foreign")
            raced.append(candidate)
            raise FileExistsError(errno.EEXIST, "injected mkdir race", path)
        return real_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(captured_directory.os, "mkdir", race_mkdir)

    with pytest.raises(FileExistsError, match="mkdir race"):
        OwnedPathBuildDirectory.prepare(parent / "attempt")

    assert len(raced) == 1
    assert raced[0].joinpath("valuable.bin").read_bytes() == b"foreign"
    assert not tuple(parent.glob("*discarded-*"))
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before


def test_owned_path_build_post_mkdir_interrupt_leaves_unauthenticated_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    real_stat = captured_directory.os.stat
    interruption = KeyboardInterrupt("before created identity handoff")
    interrupted = False

    def interrupt_created_stat(path, *args, **kwargs):
        nonlocal interrupted
        if (
            not interrupted
            and isinstance(path, str)
            and path.startswith(".attempt.normalize-")
            and kwargs.get("dir_fd") is not None
        ):
            try:
                real_stat(path, *args, **kwargs)
            except FileNotFoundError:
                pass
            else:
                interrupted = True
                raise interruption
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(captured_directory.os, "stat", interrupt_created_stat)

    with pytest.raises(KeyboardInterrupt) as caught:
        OwnedPathBuildDirectory.prepare(parent / "attempt")

    assert caught.value is interruption
    roots = tuple(parent.glob(".attempt.normalize-*"))
    assert len(roots) == 1
    assert roots[0].is_dir()
    assert not tuple(parent.glob("*discarded-*"))
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


def test_owned_path_build_ambiguous_cleanup_reconciles_after_close_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    real_mkdir = captured_directory.os.mkdir
    real_close_resources = OwnedPathBuildDirectory._close_resources
    raced: list[Path] = []
    interruption = KeyboardInterrupt("after exact resource close")
    interrupted = False

    def race_mkdir(path, mode=0o777, *, dir_fd=None):
        if (
            not raced
            and isinstance(path, str)
            and path.startswith(".attempt.normalize-")
            and dir_fd is not None
        ):
            real_mkdir(path, mode=0o700, dir_fd=dir_fd)
            candidate = parent / path
            candidate.joinpath("valuable.bin").write_bytes(b"foreign")
            raced.append(candidate)
            raise FileExistsError(errno.EEXIST, "injected mkdir race", path)
        return real_mkdir(path, mode=mode, dir_fd=dir_fd)

    def close_then_interrupt(
        owner: OwnedPathBuildDirectory,
        *,
        validate_binding: bool,
    ) -> None:
        nonlocal interrupted
        real_close_resources(owner, validate_binding=validate_binding)
        if not interrupted:
            interrupted = True
            raise interruption

    monkeypatch.setattr(captured_directory.os, "mkdir", race_mkdir)
    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_close_resources",
        close_then_interrupt,
    )

    with pytest.raises(FileExistsError, match="mkdir race"):
        OwnedPathBuildDirectory.prepare(parent / "attempt")

    assert interrupted
    assert raced[0].joinpath("valuable.bin").read_bytes() == b"foreign"
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(),
    reason="requires procfs",
)
def test_owned_path_build_stage_constructor_return_interrupt_has_full_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    before = len(tuple(Path("/proc/self/fd").iterdir()))
    real_init = OwnedDirectoryStage.__init__
    interruption = KeyboardInterrupt("after stage construction")
    resources: list[OwnedPathBuildDirectory] = []

    def construct_then_interrupt(stage: OwnedDirectoryStage, *args, **kwargs) -> None:
        resource = kwargs.get("_construction_owner")
        assert isinstance(resource, OwnedPathBuildDirectory)
        resources.append(resource)
        real_init(stage, *args, **kwargs)
        raise interruption

    monkeypatch.setattr(OwnedDirectoryStage, "__init__", construct_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        OwnedPathBuildDirectory.prepare(parent / "attempt")

    assert caught.value is interruption
    assert len(resources) == 1
    owner = resources[0]
    assert owner.closed
    assert not tuple(parent.glob(".attempt.normalize-*"))
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before
    orphan = owner.isolate()
    assert orphan.reopen(lambda reader: reader.inventory()) == ()


def _interrupt_owned_path_on_line(
    function: object,
    source_fragment: str,
    callback: object,
    *,
    predicate: object | None = None,
    error: BaseException,
) -> None:
    assert callable(function)
    assert callable(callback)
    assert predicate is None or callable(predicate)
    source, first_line = inspect.getsourcelines(function)
    target_lines = {
        first_line + offset
        for offset, line in enumerate(source)
        if line.strip() == source_fragment
    }
    assert len(target_lines) == 1
    code = function.__code__
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and event == "line"
            and frame.f_code is code
            and frame.f_lineno in target_lines
            and (predicate is None or predicate(frame.f_locals))
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, f"failed to interrupt before {source_fragment}"


def _interrupt_owned_path_after_attribute_store(
    function: object,
    attribute_name: str,
    callback: object,
    *,
    next_line_fragment: str,
    error: BaseException,
) -> None:
    assert callable(function)
    instructions = tuple(dis.get_instructions(function))
    indexes = tuple(
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_ATTR" and instruction.argval == attribute_name
    )
    assert len(indexes) == 1
    opcode_offsets = {instructions[indexes[0] + 1].offset}
    source, first_line = inspect.getsourcelines(function)
    line_numbers = {
        first_line + offset
        for offset, line in enumerate(source)
        if line.strip() == next_line_fragment
    }
    assert len(line_numbers) == 1
    code = function.__code__
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is code
            and (
                event == "opcode"
                and frame.f_lasti in opcode_offsets
                or event == "line"
                and frame.f_lineno in line_numbers
            )
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, f"failed to interrupt after {attribute_name} store"


def _interrupt_owned_path_after_global_store(
    function: object,
    global_name: str,
    callback: object,
    *,
    error: BaseException,
) -> None:
    assert callable(function)
    assert callable(callback)
    instructions = tuple(dis.get_instructions(function))
    indexes = tuple(
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_GLOBAL" and instruction.argval == global_name
    )
    assert len(indexes) == 1
    opcode_offset = instructions[indexes[0] + 1].offset
    code = function.__code__
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            return trace
        if (
            not injected
            and event == "opcode"
            and frame.f_code is code
            and frame.f_lasti == opcode_offset
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, f"failed to interrupt after {global_name} store"


def _call_owned_path_after_global_store(
    function: object,
    global_name: str,
    callback: object,
) -> None:
    assert callable(function)
    assert callable(callback)
    instructions = tuple(dis.get_instructions(function))
    indexes = tuple(
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_GLOBAL" and instruction.argval == global_name
    )
    assert len(indexes) == 1
    opcode_offset = instructions[indexes[0] + 1].offset
    code = function.__code__
    previous_trace = sys.gettrace()
    called = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal called
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            return trace
        if (
            not called
            and event == "opcode"
            and frame.f_code is code
            and frame.f_lasti == opcode_offset
        ):
            called = True
            sys.settrace(None)
            callback()
        return trace

    sys.settrace(trace)
    try:
        function()
    finally:
        sys.settrace(previous_trace)
        assert called, f"failed to call after {global_name} store"


def _call_owned_path_before_global_load(
    function: object,
    global_name: str,
    callback: object,
    *,
    occurrence: int = 0,
) -> None:
    assert callable(function)
    assert callable(callback)
    offsets = tuple(
        instruction.offset
        for instruction in dis.get_instructions(function)
        if instruction.opname == "LOAD_GLOBAL" and instruction.argval == global_name
    )
    assert 0 <= occurrence < len(offsets)
    opcode_offset = offsets[occurrence]
    code = function.__code__
    previous_trace = sys.gettrace()
    called = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal called
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            return trace
        if (
            not called
            and event == "opcode"
            and frame.f_code is code
            and frame.f_lasti == opcode_offset
        ):
            called = True
            sys.settrace(None)
            callback()
        return trace

    sys.settrace(trace)
    try:
        function()
    finally:
        sys.settrace(previous_trace)
        assert called, f"failed to call before loading {global_name}"


def _call_owned_path_after_named_load(
    function: object,
    name: str,
    callback: object,
) -> None:
    assert callable(function)
    assert callable(callback)
    instructions = tuple(dis.get_instructions(function))
    indexes = tuple(
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.argval == name
        and instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
    )
    assert len(indexes) == 1
    opcode_offset = instructions[indexes[0] + 1].offset
    code = function.__code__
    previous_trace = sys.gettrace()
    called = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal called
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            return trace
        if (
            not called
            and event == "opcode"
            and frame.f_code is code
            and frame.f_lasti == opcode_offset
        ):
            called = True
            sys.settrace(None)
            callback()
        return trace

    sys.settrace(trace)
    try:
        function()
    finally:
        sys.settrace(previous_trace)
        assert called, f"failed to call after loading {name}"


def _call_owned_path_on_return(function: object, callback: object) -> None:
    assert callable(function)
    assert callable(callback)
    code = function.__code__
    previous_trace = sys.gettrace()
    called = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal called
        if event == "call" and frame.f_code is code:
            return trace
        if not called and event == "return" and frame.f_code is code:
            called = True
            sys.settrace(None)
            callback()
        return trace

    sys.settrace(trace)
    try:
        function()
    finally:
        sys.settrace(previous_trace)
        assert called, "failed to call at the return handoff"


def _prime_owned_path_opcode_tracing(function: object, callback: object) -> None:
    assert callable(function)
    assert callable(callback)
    code = function.__code__
    previous_trace = sys.gettrace()
    observed = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal observed
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            return trace
        if event == "opcode" and frame.f_code is code:
            observed = True
        return trace

    try:
        for _attempt in range(2):
            sys.settrace(trace)
            callback()
            sys.settrace(previous_trace)
            if observed:
                break
    finally:
        sys.settrace(previous_trace)
        assert observed, "failed to prime opcode tracing"


def _interrupt_owned_path_on_opcode(
    function: object,
    opname: str,
    callback: object,
    *,
    occurrence: int = 0,
    error: BaseException,
) -> None:
    assert callable(function)
    assert callable(callback)
    offsets = tuple(
        instruction.offset
        for instruction in dis.get_instructions(function)
        if instruction.opname == opname
    )
    assert 0 <= occurrence < len(offsets)
    target_offset = offsets[occurrence]
    code = function.__code__
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            return trace
        if (
            not injected
            and event == "opcode"
            and frame.f_code is code
            and frame.f_lasti == target_offset
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, f"failed to interrupt at {opname}"


def _interrupt_owned_path_return(
    function: object,
    callback: object,
    *,
    predicate: object,
    error: BaseException,
) -> None:
    assert callable(function)
    assert callable(callback)
    assert callable(predicate)
    code = function.__code__
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            return trace
        if (
            not injected
            and event == "return"
            and frame.f_code is code
            and predicate(frame, arg)
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, "failed to interrupt the return handoff"


def test_owned_path_build_prepare_acquire_interrupt_runs_preinstalled_cleanup(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    interruption = KeyboardInterrupt("after acquire")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_on_line(
            OwnedPathBuildDirectory.prepare.__func__,
            "return resource",
            lambda: OwnedPathBuildDirectory.prepare(parent / "attempt"),
            error=interruption,
        )

    assert caught.value is interruption
    assert not tuple(parent.glob(".attempt.normalize-*"))
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


def test_owned_path_build_prepare_cleanup_boundary_preserves_exact_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    real_acquire = OwnedPathBuildDirectory._acquire
    primary = RuntimeError("exact acquire primary")
    interruption = KeyboardInterrupt("cleanup runner entry")

    def acquire_then_fail(
        owner: OwnedPathBuildDirectory,
        *,
        create_parent: bool,
    ) -> None:
        real_acquire(owner, create_parent=create_parent)
        raise primary

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_acquire",
        acquire_then_fail,
    )

    with pytest.raises(RuntimeError, match="exact acquire primary") as caught:
        _interrupt_owned_path_on_line(
            atomic_directory._run_context_with_cleanup_actions.__wrapped__,
            "_run_ordered_actions(failures)",
            lambda: OwnedPathBuildDirectory.prepare(parent / "attempt"),
            predicate=lambda local: local.get("context_error") is primary,
            error=interruption,
        )

    assert caught.value is primary
    retained = tuple(captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES)
    assert len(retained) == 1
    owner = retained[0]
    assert owner.path.is_dir()

    orphans = _retry_owned_path_build_cleanup()

    assert owner.closed
    assert len(orphans) == 1
    assert orphans[0].reopen(lambda reader: reader.inventory()) == ()


def test_owned_path_build_prepare_return_handoff_is_quiescently_retryable(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    interruption = KeyboardInterrupt("prepare return handoff")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            OwnedPathBuildDirectory.prepare.__func__,
            lambda: OwnedPathBuildDirectory.prepare(parent / "attempt"),
            predicate=lambda _frame, result: isinstance(
                result,
                OwnedPathBuildDirectory,
            ),
            error=interruption,
        )

    assert caught.value is interruption
    retained = tuple(captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES)
    assert len(retained) == 1
    owner = retained[0]
    assert owner.path.is_dir()

    orphans = _retry_owned_path_build_cleanup()

    assert owner.closed
    assert not owner.path.exists()
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert len(orphans) == 1
    assert orphans[0].reopen(lambda reader: reader.inventory()) == ()


def test_owned_path_build_group_is_installed_before_constructor_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    retention_group = _OpaqueRetentionGroup()
    other_group = _OpaqueRetentionGroup()
    interruption = KeyboardInterrupt("constructor assignment handoff")
    real_init = OwnedPathBuildDirectory.__init__

    def construct_then_interrupt(owner, *args, **kwargs) -> None:
        real_init(owner, *args, **kwargs)
        raise interruption

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "__init__",
        construct_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        OwnedPathBuildDirectory.prepare(
            parent / "attempt",
            _retention_group=retention_group,
        )

    assert caught.value is interruption
    retained = tuple(captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES)
    assert len(retained) == 1
    owner = retained[0]
    assert owner._retention_group is retention_group
    assert owner._state == "opening"
    assert owner._stage is None
    assert not tuple(parent.iterdir())

    assert _retry_owned_path_build_cleanup_for_group(other_group) == ()
    assert not owner.closed
    assert owner in captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES

    assert _retry_owned_path_build_cleanup_for_group(retention_group) == ()
    assert owner.closed
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


def test_owned_path_build_group_survives_prepare_return_handoff(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    retention_group = _OpaqueRetentionGroup()
    other_group = _OpaqueRetentionGroup()
    interruption = KeyboardInterrupt("grouped prepare return handoff")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            OwnedPathBuildDirectory.prepare.__func__,
            lambda: OwnedPathBuildDirectory.prepare(
                parent / "attempt",
                _retention_group=retention_group,
            ),
            predicate=lambda _frame, result: isinstance(
                result,
                OwnedPathBuildDirectory,
            ),
            error=interruption,
        )

    assert caught.value is interruption
    retained = tuple(captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES)
    assert len(retained) == 1
    owner = retained[0]
    assert owner._retention_group is retention_group
    assert owner.path.is_dir()

    assert _retry_owned_path_build_cleanup_for_group(other_group) == ()
    assert not owner.closed
    orphans = _retry_owned_path_build_cleanup_for_group(retention_group)

    assert len(orphans) == 1
    assert owner.closed
    assert not owner.path.exists()
    assert orphans[0].reopen(lambda reader: reader.inventory()) == ()


def test_owned_path_build_quiescent_acceptor_failure_retains_exact_receipt(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    owner.path.joinpath("payload.bin").write_bytes(b"bytes")
    accepted: list[atomic_directory.DirectoryOrphan] = []
    failure = RuntimeError("orphan sink failed")

    def reject(orphan: atomic_directory.DirectoryOrphan) -> None:
        accepted.append(orphan)
        raise failure

    with pytest.raises(RuntimeError, match="orphan sink failed") as caught:
        retry_retained_owned_path_build_cleanup(reject)

    assert caught.value is failure
    assert len(accepted) == 1
    assert owner._state == "closed"
    assert not owner.closed
    assert owner in captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES

    redelivered = _retry_owned_path_build_cleanup()

    assert redelivered == (accepted[0],)
    assert owner.closed
    assert (
        accepted[0].reopen(
            lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
        )
        == b"bytes"
    )


def test_owned_path_build_quiescent_acceptor_return_interrupt_redelivers(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    accepted: list[atomic_directory.DirectoryOrphan] = []
    interruption = KeyboardInterrupt("orphan accept return")

    def accept(orphan: atomic_directory.DirectoryOrphan) -> None:
        accepted.append(orphan)

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            accept,
            lambda: retry_retained_owned_path_build_cleanup(accept),
            predicate=lambda _frame, result: result is None,
            error=interruption,
        )

    assert caught.value is interruption
    assert len(accepted) == 1
    assert not owner.closed
    assert owner in captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES

    redelivered = _retry_owned_path_build_cleanup()

    assert redelivered == (accepted[0],)
    assert owner.closed


def test_owned_path_build_quiescent_acceptor_rejects_recursive_cleanup(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    accepted: list[atomic_directory.DirectoryOrphan] = []

    def accept(orphan: atomic_directory.DirectoryOrphan) -> None:
        accepted.append(orphan)
        assert not owner.closed
        with pytest.raises(RuntimeError, match="cleanup retry is reentrant"):
            retry_retained_owned_path_build_cleanup(accept)
        with pytest.raises(RuntimeError, match="lifecycle operation is reentrant"):
            owner.close()

    retry_retained_owned_path_build_cleanup(accept)

    assert len(accepted) == 1
    assert owner.closed
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES


def test_owned_path_build_quiescent_cleanup_rejects_concurrent_retry(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    owner.path.joinpath("payload.bin").write_bytes(b"bytes")
    accepting = threading.Event()
    release_acceptor = threading.Event()
    accepted: list[atomic_directory.DirectoryOrphan] = []
    worker_errors: list[BaseException] = []

    def accept(orphan: atomic_directory.DirectoryOrphan) -> None:
        accepted.append(orphan)
        accepting.set()
        if not release_acceptor.wait(timeout=10):
            raise RuntimeError("test acceptor was not released")

    def cleanup() -> None:
        try:
            retry_retained_owned_path_build_cleanup(accept)
        except BaseException as exc:  # noqa: B036 - report thread failure
            worker_errors.append(exc)

    worker = threading.Thread(target=cleanup)
    worker.start()
    assert accepting.wait(timeout=5)
    try:
        with pytest.raises(RuntimeError, match="cleanup retry is already active"):
            retry_retained_owned_path_build_cleanup(accepted.append)
    finally:
        release_acceptor.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert not worker_errors
    assert len(accepted) == 1
    assert owner.closed
    assert (
        accepted[0].reopen(
            lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
        )
        == b"bytes"
    )


@pytest.mark.parametrize("seam", ["acquire", "release"])
def test_owned_path_build_quiescent_retry_lease_interrupt_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    interruption = KeyboardInterrupt(f"retry lease {seam}")
    interrupted = False
    lease_type = captured_directory._OwnedPathBuildRetryLease
    method_name = "acquire_nonblocking" if seam == "acquire" else "close"
    real_method = getattr(lease_type, method_name)

    def interrupt_after_transition(lease) -> None:
        nonlocal interrupted
        real_method(lease)
        if (
            lease._lock is captured_directory._RETAINED_OWNED_PATH_BUILD_RETRY_LOCK
            and not interrupted
        ):
            interrupted = True
            raise interruption

    monkeypatch.setattr(lease_type, method_name, interrupt_after_transition)

    with pytest.raises(KeyboardInterrupt) as caught:
        retry_retained_owned_path_build_cleanup(lambda _orphan: None)

    assert caught.value is interruption
    if seam == "acquire":
        assert not owner.closed
        assert owner in captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    else:
        assert owner.closed

    monkeypatch.setattr(lease_type, method_name, real_method)
    recovered = _retry_owned_path_build_cleanup()

    if seam == "acquire":
        assert len(recovered) == 1
        assert owner.closed
    else:
        assert recovered == ()


def test_owned_path_build_lifecycle_failure_handler_interrupt_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    natural_failure = ValueError("natural isolation failure")
    interruption = KeyboardInterrupt("lifecycle failure handler")
    real_isolate = OwnedPathBuildDirectory._isolate_and_close

    def fail_isolation(
        _owner: OwnedPathBuildDirectory,
        *,
        require_orphan: bool,
        _release_retention: bool = True,
    ) -> None:
        raise natural_failure

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_isolate_and_close",
        fail_isolation,
    )

    with pytest.raises(ValueError, match="natural isolation failure") as caught:
        _interrupt_owned_path_on_line(
            captured_directory._capture_owned_path_build_lease_outcome,
            "outcome.error = error",
            owner.close,
            predicate=lambda values: values.get("error") is natural_failure,
            error=interruption,
        )

    assert caught.value is natural_failure
    assert caught.value.__cause__ is interruption or any(
        str(interruption) in note for note in getattr(caught.value, "__notes__", ())
    )
    acquired: list[bool] = []

    def probe_lifecycle_lock() -> None:
        locked = owner._lifecycle_lock.acquire(timeout=1)
        acquired.append(locked)
        if locked:
            owner._lifecycle_lock.release()

    probe = threading.Thread(target=probe_lifecycle_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_isolate_and_close",
        real_isolate,
    )
    owner.close()
    assert owner.closed


def test_owned_path_build_lifecycle_return_interrupt_releases_lock(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    interruption = KeyboardInterrupt("lifecycle return")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            OwnedPathBuildDirectory._run_lifecycle,
            lambda: owner._run_lifecycle(lambda: None),
            predicate=lambda _frame, result: result is None,
            error=interruption,
        )

    assert caught.value is interruption
    acquired: list[bool] = []

    def probe_lifecycle_lock() -> None:
        locked = owner._lifecycle_lock.acquire(timeout=1)
        acquired.append(locked)
        if locked:
            owner._lifecycle_lock.release()

    probe = threading.Thread(target=probe_lifecycle_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]
    owner.close()
    assert owner.closed


def test_owned_path_build_lifecycle_normal_settle_interrupt_releases_lock(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    interruption = KeyboardInterrupt("lifecycle normal settlement")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_on_line(
            captured_directory._run_context_with_cleanup_actions.__wrapped__,
            "_run_ordered_actions(failures)",
            lambda: owner._run_lifecycle(lambda: None),
            error=interruption,
        )

    assert caught.value is interruption
    acquired: list[bool] = []

    def probe_lifecycle_lock() -> None:
        locked = owner._lifecycle_lock.acquire(timeout=1)
        acquired.append(locked)
        if locked:
            owner._lifecycle_lock.release()

    probe = threading.Thread(target=probe_lifecycle_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]
    owner.close()
    assert owner.closed


def test_owned_path_build_lifecycle_settlement_does_not_promote_ambient_error(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    ambient = RuntimeError("ambient caller error")
    interruption = KeyboardInterrupt("lifecycle settlement")

    try:
        raise ambient
    except RuntimeError:
        with pytest.raises(KeyboardInterrupt) as caught:
            _interrupt_owned_path_on_line(
                captured_directory._run_context_with_cleanup_actions.__wrapped__,
                "_run_ordered_actions(failures)",
                lambda: owner._run_lifecycle(lambda: None),
                predicate=lambda values: any(
                    getattr(action, "label", None)
                    == "owned path build lifecycle lease release also failed"
                    for action in values["failures"].actions
                ),
                error=interruption,
            )

    assert caught.value is interruption
    assert caught.value is not ambient
    assert not owner.closed
    assert owner in captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    acquired: list[bool] = []

    def probe_lifecycle_lock() -> None:
        locked = owner._lifecycle_lock.acquire(timeout=1)
        acquired.append(locked)
        if locked:
            owner._lifecycle_lock.release()

    probe = threading.Thread(target=probe_lifecycle_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]
    owner.close()
    assert owner.closed


def test_owned_path_build_global_retry_normal_settle_interrupt_releases_lock() -> None:
    _retry_owned_path_build_cleanup()
    interruption = KeyboardInterrupt("global retry normal settlement")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_on_line(
            captured_directory._run_context_with_cleanup_actions.__wrapped__,
            "_run_ordered_actions(failures)",
            lambda: retry_retained_owned_path_build_cleanup(lambda _orphan: None),
            predicate=lambda values: any(
                getattr(action, "label", None)
                == "owned path build retry lease release also failed"
                for action in values["failures"].actions
            ),
            error=interruption,
        )

    assert caught.value is interruption
    acquired: list[bool] = []

    def probe_retry_lock() -> None:
        locked = captured_directory._RETAINED_OWNED_PATH_BUILD_RETRY_LOCK.acquire(
            timeout=1
        )
        acquired.append(locked)
        if locked:
            captured_directory._RETAINED_OWNED_PATH_BUILD_RETRY_LOCK.release()

    probe = threading.Thread(target=probe_retry_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]
    assert _retry_owned_path_build_cleanup() == ()


def test_owned_path_build_registry_normal_settle_interrupt_releases_lock() -> None:
    _retry_owned_path_build_cleanup()
    interruption = KeyboardInterrupt("registry normal settlement")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_on_line(
            captured_directory._run_context_with_cleanup_actions.__wrapped__,
            "_run_ordered_actions(failures)",
            lambda: captured_directory._owned_path_build_directory_retained(object()),
            error=interruption,
        )

    assert caught.value is interruption
    acquired: list[bool] = []

    def probe_registry_lock() -> None:
        locked = captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK.acquire(timeout=1)
        acquired.append(locked)
        if locked:
            captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK.release()

    probe = threading.Thread(target=probe_registry_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]


def test_owned_path_build_registry_error_handler_interrupt_preserves_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    natural_failure = RuntimeError("registry append failed")
    interruption = KeyboardInterrupt("registry outcome handler")

    class FailingRegistry(list):
        def append(self, _owner) -> None:
            raise natural_failure

    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_DIRECTORIES",
        FailingRegistry(),
    )

    with pytest.raises(RuntimeError, match="registry append failed") as caught:
        _interrupt_owned_path_on_line(
            captured_directory._capture_owned_path_build_lease_outcome,
            "outcome.error = error",
            lambda: captured_directory._register_owned_path_build_directory(object()),
            predicate=lambda values: values.get("error") is natural_failure,
            error=interruption,
        )

    assert caught.value is natural_failure
    assert caught.value.__cause__ is interruption or any(
        str(interruption) in note for note in getattr(caught.value, "__notes__", ())
    )
    acquired: list[bool] = []

    def probe_registry_lock() -> None:
        locked = captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK.acquire(timeout=1)
        acquired.append(locked)
        if locked:
            captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK.release()

    probe = threading.Thread(target=probe_registry_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]


@pytest.mark.parametrize("failure_kind", ["append", "iteration"])
def test_owned_path_build_registry_native_with_interrupt_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    _retry_owned_path_build_cleanup()
    lease_type = captured_directory._OwnedPathBuildRetryLease
    helper = captured_directory._run_with_owned_path_build_lease
    prime_lease = lease_type(
        threading.RLock(),
        acquire_blocking=False,
        reentrant_error="prime reentrant",
        concurrent_error="prime concurrent",
    )
    _prime_owned_path_opcode_tracing(
        helper,
        lambda: helper(
            prime_lease,
            lambda: None,
            release_label="prime lease release",
        ),
    )
    natural_failure = RuntimeError(f"registry {failure_kind} failed")
    interruption = KeyboardInterrupt(f"registry {failure_kind} with handler")

    class FailingRegistry(list):
        def append(self, owner) -> None:
            if failure_kind == "append":
                raise natural_failure
            super().append(owner)

        def __iter__(self):
            if failure_kind == "iteration":
                raise natural_failure
            return super().__iter__()

    def bypass_outcome(callback, _outcome) -> None:
        callback()

    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_DIRECTORIES",
        FailingRegistry(),
    )
    monkeypatch.setattr(
        captured_directory,
        "_capture_owned_path_build_lease_outcome",
        bypass_outcome,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_on_opcode(
            helper,
            "WITH_EXCEPT_START",
            lambda: captured_directory._register_owned_path_build_directory(object()),
            error=interruption,
        )

    assert caught.value is interruption
    assert caught.value.__context__ is natural_failure
    acquired: list[bool] = []

    def probe_registry_lock() -> None:
        locked = captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK.acquire(timeout=1)
        acquired.append(locked)
        if locked:
            captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK.release()

    probe = threading.Thread(target=probe_registry_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]


@pytest.mark.parametrize(
    "source_fragment",
    ["lease.close()", "raise  # preserve lease settlement failure"],
)
def test_owned_path_build_lifecycle_fallback_interrupt_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    lease_type = captured_directory._OwnedPathBuildRetryLease
    real_close = lease_type.close
    natural_failure = OSError(errno.EIO, "initial lease release failed")
    interruption = KeyboardInterrupt(f"fallback seam {source_fragment}")
    close_calls = 0

    def fail_first_close(lease) -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise natural_failure
        real_close(lease)

    monkeypatch.setattr(lease_type, "close", fail_first_close)

    expected_error = (
        natural_failure if source_fragment == "lease.close()" else interruption
    )
    with pytest.raises(type(expected_error)) as caught:
        _interrupt_owned_path_on_line(
            captured_directory._run_with_owned_path_build_lease,
            source_fragment,
            lambda: owner._run_lifecycle(lambda: None),
            predicate=lambda values: values.get("primary_error") is natural_failure,
            error=interruption,
        )

    assert caught.value is expected_error
    acquired: list[bool] = []

    def probe_lifecycle_lock() -> None:
        locked = owner._lifecycle_lock.acquire(timeout=1)
        acquired.append(locked)
        if locked:
            owner._lifecycle_lock.release()

    probe = threading.Thread(target=probe_lifecycle_lock)
    probe.start()
    probe.join(timeout=5)
    assert not probe.is_alive()
    assert acquired == [True]

    monkeypatch.setattr(lease_type, "close", real_close)
    owner.close()
    assert owner.closed


def test_owned_path_build_quiescent_partial_failure_preserves_prior_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    first = OwnedPathBuildDirectory.prepare(parent / "first")
    second = OwnedPathBuildDirectory.prepare(parent / "second")
    first.path.joinpath("payload.bin").write_bytes(b"first")
    second.path.joinpath("payload.bin").write_bytes(b"second")
    real_isolate = OwnedPathBuildDirectory._isolate_and_close
    failure = RuntimeError("second cleanup failed")
    failed = False

    def fail_second_once(
        owner: OwnedPathBuildDirectory,
        *,
        require_orphan: bool,
        _release_retention: bool = True,
    ):
        nonlocal failed
        if owner is second and not failed:
            failed = True
            raise failure
        return real_isolate(
            owner,
            require_orphan=require_orphan,
            _release_retention=_release_retention,
        )

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_isolate_and_close",
        fail_second_once,
    )
    accepted: list[atomic_directory.DirectoryOrphan] = []

    with pytest.raises(RuntimeError, match="second cleanup failed") as caught:
        retry_retained_owned_path_build_cleanup(accepted.append)

    assert caught.value is failure
    assert len(accepted) == 1
    assert first.closed
    assert not second.closed
    assert second in captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert (
        accepted[0].reopen(
            lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
        )
        == b"first"
    )

    remaining = _retry_owned_path_build_cleanup()

    assert len(remaining) == 1
    assert second.closed
    assert (
        remaining[0].reopen(
            lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
        )
        == b"second"
    )


def test_owned_path_build_quiescent_first_failure_retains_later_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    first = OwnedPathBuildDirectory.prepare(parent / "first")
    second = OwnedPathBuildDirectory.prepare(parent / "second")
    failure = RuntimeError("first cleanup failed")
    real_retry = OwnedPathBuildDirectory._retry_quiescent_cleanup

    def fail_first_once(
        owner: OwnedPathBuildDirectory,
        accept_orphan,
    ) -> None:
        if owner is first:
            raise failure
        real_retry(owner, accept_orphan)

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_retry_quiescent_cleanup",
        fail_first_once,
    )

    with pytest.raises(RuntimeError, match="first cleanup failed") as caught:
        retry_retained_owned_path_build_cleanup(lambda _orphan: None)

    assert caught.value is failure
    assert not first.closed
    assert not second.closed
    assert first in captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert second in captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_retry_quiescent_cleanup",
        real_retry,
    )
    remaining = _retry_owned_path_build_cleanup()

    assert len(remaining) == 2
    assert first.closed
    assert second.closed


def test_owned_path_build_group_retry_is_exact_and_stops_at_first_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    group_a = _OpaqueRetentionGroup()
    group_b = _OpaqueRetentionGroup()
    first_a = OwnedPathBuildDirectory.prepare(
        parent / "first-a",
        _retention_group=group_a,
    )
    owner_b = OwnedPathBuildDirectory.prepare(
        parent / "owner-b",
        _retention_group=group_b,
    )
    second_a = OwnedPathBuildDirectory.prepare(
        parent / "second-a",
        _retention_group=group_a,
    )
    legacy = OwnedPathBuildDirectory.prepare(parent / "legacy")
    owners_and_payloads = (
        (first_a, b"first-a"),
        (owner_b, b"owner-b"),
        (second_a, b"second-a"),
        (legacy, b"legacy"),
    )
    for owner, payload in owners_and_payloads:
        owner.path.joinpath("payload.bin").write_bytes(payload)

    none_callbacks: list[atomic_directory.DirectoryOrphan] = []
    with pytest.raises(TypeError, match="retention group must not be None"):
        captured_directory._retry_retained_owned_path_build_cleanup_for_group(
            None,
            none_callbacks.append,
        )
    assert not none_callbacks
    assert all(not owner.closed for owner, _payload in owners_and_payloads)

    real_retry = OwnedPathBuildDirectory._retry_quiescent_cleanup
    failure = RuntimeError("first routed cleanup failed")
    attempted: list[OwnedPathBuildDirectory] = []

    def fail_first_a(
        owner: OwnedPathBuildDirectory,
        accept_orphan,
    ) -> None:
        attempted.append(owner)
        if owner is first_a:
            raise failure
        real_retry(owner, accept_orphan)

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_retry_quiescent_cleanup",
        fail_first_a,
    )

    with pytest.raises(RuntimeError, match="first routed cleanup failed") as caught:
        captured_directory._retry_retained_owned_path_build_cleanup_for_group(
            group_a,
            lambda _orphan: None,
        )

    assert caught.value is failure
    assert attempted == [first_a]
    assert all(not owner.closed for owner, _payload in owners_and_payloads)

    monkeypatch.setattr(
        OwnedPathBuildDirectory,
        "_retry_quiescent_cleanup",
        real_retry,
    )
    accepted_a = _retry_owned_path_build_cleanup_for_group(group_a)

    assert first_a.closed
    assert second_a.closed
    assert not owner_b.closed
    assert not legacy.closed
    assert tuple(
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        for orphan in accepted_a
    ) == (b"first-a", b"second-a")
    assert owner_b.path.joinpath("payload.bin").read_bytes() == b"owner-b"
    assert legacy.path.joinpath("payload.bin").read_bytes() == b"legacy"

    accepted_b = _retry_owned_path_build_cleanup_for_group(group_b)

    assert owner_b.closed
    assert not legacy.closed
    assert len(accepted_b) == 1
    assert (
        accepted_b[0].reopen(
            lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
        )
        == b"owner-b"
    )

    unfiltered_grouped = OwnedPathBuildDirectory.prepare(
        parent / "unfiltered-grouped",
        _retention_group=group_b,
    )
    unfiltered_grouped.path.joinpath("payload.bin").write_bytes(b"unfiltered")
    accepted_unfiltered = _retry_owned_path_build_cleanup()

    assert legacy.closed
    assert unfiltered_grouped.closed
    assert tuple(
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        for orphan in accepted_unfiltered
    ) == (b"legacy", b"unfiltered")


def test_owned_path_build_quiescent_return_interrupt_follows_acceptance(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    accepted: list[atomic_directory.DirectoryOrphan] = []
    interruption = KeyboardInterrupt("quiescent retry return")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            retry_retained_owned_path_build_cleanup,
            lambda: retry_retained_owned_path_build_cleanup(accepted.append),
            predicate=lambda _frame, result: result is None,
            error=interruption,
        )

    assert caught.value is interruption
    assert len(accepted) == 1
    assert owner.closed
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert accepted[0].reopen(lambda reader: reader.inventory()) == ()


def test_owned_path_build_operation_activation_interrupt_clears_lease(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    interruption = KeyboardInterrupt("operation activation")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_after_attribute_store(
            OwnedPathBuildDirectory.path_operation.__wrapped__,
            "_operation_lease",
            lambda: owner.path_operation("writer").__enter__(),
            next_line_fragment='self._verify_active(f"before {label}")',
            error=interruption,
        )

    assert caught.value is interruption
    assert owner._operation_lease is None
    owner.close()
    assert owner.closed


def test_owned_path_build_operation_generator_return_interrupt_clears_lease(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    interruption = KeyboardInterrupt("operation generator return")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            OwnedPathBuildDirectory.path_operation.__wrapped__,
            lambda: owner.path_operation("writer").__enter__(),
            predicate=lambda frame, result: (
                frame.f_locals.get("self") is owner and result == owner.path
            ),
            error=interruption,
        )

    assert caught.value is interruption
    # CPython 3.10 can retain the suspended generator at this return event;
    # newer interpreters unwind it immediately.  The explicit quiescent
    # boundary must settle either state without requiring a live caller.
    _retry_owned_path_build_cleanup()
    assert owner._operation_lease is None
    assert owner.closed


def test_owned_path_build_operation_context_return_uses_quiescent_retry(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    operation = owner.path_operation("writer")
    interruption = KeyboardInterrupt("operation context return")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            contextlib._GeneratorContextManager.__enter__,
            operation.__enter__,
            predicate=lambda frame, result: (
                frame.f_locals.get("self") is operation and result == owner.path
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert owner._operation_lease is not None
    with pytest.raises(RuntimeError, match="reentrant"):
        owner.close()

    _retry_owned_path_build_cleanup()

    assert owner._operation_lease is None
    assert owner.closed
    operation.gen.close()


@pytest.mark.parametrize(
    "handoff",
    ("closed-authority-pointer", "released-pointer-registry"),
)
def test_owned_path_build_reconciles_authority_close_handoffs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handoff: str,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("writer"):
        owner.path.joinpath("payload.bin").write_bytes(b"bytes")
    authority_owner = owner._authority_owner
    interruption = KeyboardInterrupt(handoff)
    interrupted = False

    if handoff == "closed-authority-pointer":
        real_close = atomic_directory._PublicationAuthorityOwner.close

        def close_authority_then_interrupt(candidate, **kwargs) -> None:
            nonlocal interrupted
            if candidate is authority_owner and not interrupted:
                authority = candidate.authority
                assert authority is not None
                authority.close()
                assert authority._closed
                interrupted = True
                raise interruption
            real_close(candidate, **kwargs)

        monkeypatch.setattr(
            atomic_directory._PublicationAuthorityOwner,
            "close",
            close_authority_then_interrupt,
        )
    else:
        real_forget = atomic_directory._forget_publication_authority_owner

        def forget_then_interrupt(candidate) -> None:
            nonlocal interrupted
            if candidate is authority_owner and not interrupted:
                interrupted = True
                raise interruption
            real_forget(candidate)

        monkeypatch.setattr(
            atomic_directory,
            "_forget_publication_authority_owner",
            forget_then_interrupt,
        )

    with pytest.raises(KeyboardInterrupt) as caught:
        owner.isolate()

    assert caught.value is interruption
    assert caught.value.captured_directory_cleanup_owner is owner
    assert not owner.closed
    assert not atomic_directory._publication_authority_owner_released(authority_owner)

    orphan = owner.isolate()

    assert not owner.closed
    assert atomic_directory._publication_authority_owner_released(authority_owner)
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        == b"bytes"
    )
    owner.close()
    assert owner.closed


@pytest.mark.parametrize(
    "global_name",
    [
        "_RETAINED_OWNED_PATH_BUILD_LOCK",
        "_RETAINED_OWNED_PATH_BUILD_RETRY_LOCK",
        "_RETAINED_OWNED_PATH_BUILD_PID",
    ],
)
def test_owned_path_build_process_sync_store_interrupt_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
    global_name: str,
) -> None:
    _retry_owned_path_build_cleanup()
    original_pid = captured_directory._RETAINED_OWNED_PATH_BUILD_PID
    original_registry_lock = captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK
    original_retry_lock = captured_directory._RETAINED_OWNED_PATH_BUILD_RETRY_LOCK
    original_registry = captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert not original_registry
    inherited_owner = object()
    child_pid = original_pid + 10_000_000
    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_LOCK",
        original_registry_lock,
    )
    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_RETRY_LOCK",
        original_retry_lock,
    )
    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_DIRECTORIES",
        [inherited_owner],
    )
    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_PID",
        original_pid,
    )
    _prime_owned_path_opcode_tracing(
        captured_directory._synchronize_owned_path_build_process,
        captured_directory._synchronize_owned_path_build_process,
    )
    monkeypatch.setattr(captured_directory.os, "getpid", lambda: child_pid)
    interruption = KeyboardInterrupt(f"after {global_name}")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_after_global_store(
            captured_directory._synchronize_owned_path_build_process,
            global_name,
            captured_directory._synchronize_owned_path_build_process,
            error=interruption,
        )

    assert caught.value is interruption
    if global_name == "_RETAINED_OWNED_PATH_BUILD_PID":
        assert captured_directory._RETAINED_OWNED_PATH_BUILD_PID == child_pid
        assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    else:
        assert captured_directory._RETAINED_OWNED_PATH_BUILD_PID == original_pid

    captured_directory._synchronize_owned_path_build_process()

    assert captured_directory._RETAINED_OWNED_PATH_BUILD_PID == child_pid
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK.acquire(timeout=1)
    captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK.release()
    assert captured_directory._RETAINED_OWNED_PATH_BUILD_RETRY_LOCK.acquire(timeout=1)
    captured_directory._RETAINED_OWNED_PATH_BUILD_RETRY_LOCK.release()


def test_owned_path_build_process_sync_return_interrupt_is_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _retry_owned_path_build_cleanup()
    original_pid = captured_directory._RETAINED_OWNED_PATH_BUILD_PID
    original_registry_lock = captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK
    original_retry_lock = captured_directory._RETAINED_OWNED_PATH_BUILD_RETRY_LOCK
    original_registry = captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    assert not original_registry
    child_pid = original_pid + 10_000_000
    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_LOCK",
        original_registry_lock,
    )
    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_RETRY_LOCK",
        original_retry_lock,
    )
    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_DIRECTORIES",
        [object()],
    )
    monkeypatch.setattr(
        captured_directory,
        "_RETAINED_OWNED_PATH_BUILD_PID",
        original_pid,
    )
    monkeypatch.setattr(captured_directory.os, "getpid", lambda: child_pid)
    interruption = KeyboardInterrupt("process sync return")

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_owned_path_return(
            captured_directory._synchronize_owned_path_build_process,
            captured_directory._synchronize_owned_path_build_process,
            predicate=lambda _frame, result: result is None,
            error=interruption,
        )

    assert caught.value is interruption
    assert captured_directory._RETAINED_OWNED_PATH_BUILD_PID == child_pid
    assert not captured_directory._RETAINED_OWNED_PATH_BUILD_DIRECTORIES
    captured_directory._synchronize_owned_path_build_process()
    assert captured_directory._RETAINED_OWNED_PATH_BUILD_PID == child_pid


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_owned_path_build_process_sync_reentrant_subclass_rejection_preserves_base(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    child_parent = tmp_path / "child-private"
    child_parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    owner.path.joinpath("payload.bin").write_bytes(b"parent")
    _prime_owned_path_opcode_tracing(
        captured_directory._synchronize_owned_path_build_process,
        captured_directory._synchronize_owned_path_build_process,
    )
    child = os.fork()
    if child == 0:  # pragma: no branch - child reports exact status
        signal.signal(signal.SIGALRM, lambda *_args: os._exit(70))
        signal.alarm(5)
        try:

            class ChildOwnedPathBuildDirectory(OwnedPathBuildDirectory):
                pass

            def reject_subclass_then_prepare_base() -> None:
                try:
                    ChildOwnedPathBuildDirectory.prepare(
                        child_parent / "subclass-attempt"
                    )
                except TypeError as error:
                    if "does not support subclass construction" not in str(error):
                        os._exit(71)
                else:
                    os._exit(72)
                child_owner = OwnedPathBuildDirectory.prepare(
                    child_parent / "base-attempt"
                )
                child_owner.path.joinpath("payload.bin").write_bytes(b"child")

            _call_owned_path_before_global_load(
                captured_directory._synchronize_owned_path_build_process,
                "_RETAINED_OWNED_PATH_BUILD_DIRECTORIES",
                reject_subclass_then_prepare_base,
            )
            gc.collect()
            retained = captured_directory._snapshot_owned_path_build_directories()
            if len(retained) != 1 or type(retained[0]) is not OwnedPathBuildDirectory:
                os._exit(73)
            if tuple(child_parent.glob(".subclass-attempt.normalize-*")):
                os._exit(74)
            child_orphans = _retry_owned_path_build_cleanup()
            if len(child_orphans) != 1 or not retained[0].closed:
                os._exit(75)
            if (
                child_orphans[0].reopen(
                    lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
                )
                != b"child"
            ):
                os._exit(76)
        except BaseException:  # noqa: B036 - child reports exact failure
            os._exit(77)
        os._exit(0)

    _pid, status = os.waitpid(child, 0)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert owner.path.joinpath("payload.bin").read_bytes() == b"parent"
    assert not owner.closed
    owner.close()
    assert owner.closed


@pytest.mark.parametrize(
    "sync_seam",
    [
        "_RETAINED_OWNED_PATH_BUILD_LOCK",
        "_RETAINED_OWNED_PATH_BUILD_RETRY_LOCK",
        "_RETAINED_OWNED_PATH_BUILD_PID",
        "remove",
        "return",
    ],
)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_owned_path_build_process_sync_reentrant_prepare_retains_child_owner(
    tmp_path: Path,
    sync_seam: str,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    child_parent = tmp_path / "child-private"
    child_parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    owner.path.joinpath("payload.bin").write_bytes(b"parent")
    _prime_owned_path_opcode_tracing(
        captured_directory._synchronize_owned_path_build_process,
        captured_directory._synchronize_owned_path_build_process,
    )
    child = os.fork()
    if child == 0:  # pragma: no branch - child reports exact status
        signal.signal(signal.SIGALRM, lambda *_args: os._exit(80))
        signal.alarm(5)
        try:

            def prepare_child_owner() -> None:
                child_owner = OwnedPathBuildDirectory.prepare(child_parent / "attempt")
                child_owner.path.joinpath("payload.bin").write_bytes(b"child")

            if sync_seam == "return":
                _call_owned_path_on_return(
                    captured_directory._synchronize_owned_path_build_process,
                    prepare_child_owner,
                )
            elif sync_seam == "remove":
                _call_owned_path_after_named_load(
                    captured_directory._synchronize_owned_path_build_process,
                    "remove",
                    prepare_child_owner,
                )
            else:
                _call_owned_path_after_global_store(
                    captured_directory._synchronize_owned_path_build_process,
                    sync_seam,
                    prepare_child_owner,
                )
            gc.collect()
            retained = captured_directory._snapshot_owned_path_build_directories()
            if len(retained) != 1 or retained[0]._owner_pid != os.getpid():
                os._exit(81)
            child_orphans = _retry_owned_path_build_cleanup()
            if len(child_orphans) != 1 or not retained[0].closed:
                os._exit(82)
            if (
                child_orphans[0].reopen(
                    lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
                )
                != b"child"
            ):
                os._exit(83)
            if captured_directory._snapshot_owned_path_build_directories():
                os._exit(84)
        except BaseException:  # noqa: B036 - child reports exact failure
            os._exit(85)
        os._exit(0)

    _pid, status = os.waitpid(child, 0)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert owner.path.joinpath("payload.bin").read_bytes() == b"parent"
    assert not owner.closed
    owner.close()
    assert owner.closed


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_owned_path_build_retained_registry_resets_forked_lock_without_cleanup(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    child_parent = tmp_path / "child-private"
    child_parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("writer"):
        owner.path.joinpath("payload.bin").write_bytes(b"parent")
    locked = threading.Event()
    release = threading.Event()

    def hold_registry_lock() -> None:
        with captured_directory._RETAINED_OWNED_PATH_BUILD_RETRY_LOCK:
            with owner._lifecycle_lock:
                with captured_directory._RETAINED_OWNED_PATH_BUILD_LOCK:
                    locked.set()
                    release.wait(timeout=10)

    holder = threading.Thread(target=hold_registry_lock)
    holder.start()
    assert locked.wait(timeout=5)
    child = os.fork()
    if child == 0:  # pragma: no branch - child reports exact status
        signal.signal(signal.SIGALRM, lambda *_args: os._exit(90))
        signal.alarm(3)
        try:
            _retry_owned_path_build_cleanup()
            if not owner.path.joinpath("payload.bin").is_file():
                os._exit(91)
            try:
                owner.close()
            except RuntimeError as error:
                if "PID boundary" not in str(error):
                    os._exit(92)
            else:
                os._exit(93)
            if not owner.path.joinpath("payload.bin").is_file():
                os._exit(94)
            child_owner = OwnedPathBuildDirectory.prepare(child_parent / "attempt")
            child_owner.path.joinpath("payload.bin").write_bytes(b"child")
            child_orphans = _retry_owned_path_build_cleanup()
            if len(child_orphans) != 1 or not child_owner.closed:
                os._exit(96)
            if (
                child_orphans[0].reopen(
                    lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
                )
                != b"child"
            ):
                os._exit(97)
        except BaseException:  # noqa: B036 - child reports exact failure
            os._exit(95)
        os._exit(0)

    try:
        _pid, status = os.waitpid(child, 0)
    finally:
        release.set()
        holder.join(timeout=5)

    assert not holder.is_alive()
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert owner.path.joinpath("payload.bin").read_bytes() == b"parent"
    assert not owner.closed
    owner.close()
    assert owner.closed


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_owned_path_build_group_retry_drops_inherited_owner_without_callback(
    tmp_path: Path,
) -> None:
    _retry_owned_path_build_cleanup()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    child_parent = tmp_path / "child-private"
    child_parent.mkdir(mode=0o700)
    group_token = _OpaqueRetentionGroup()
    owner = OwnedPathBuildDirectory.prepare(
        parent / "attempt",
        _retention_group=group_token,
    )
    owner.path.joinpath("payload.bin").write_bytes(b"parent")
    child = os.fork()
    if child == 0:  # pragma: no branch - child reports exact status
        signal.signal(signal.SIGALRM, lambda *_args: os._exit(100))
        signal.alarm(5)
        try:
            inherited_callbacks: list[atomic_directory.DirectoryOrphan] = []
            captured_directory._retry_retained_owned_path_build_cleanup_for_group(
                group_token,
                inherited_callbacks.append,
            )
            if inherited_callbacks:
                os._exit(101)
            if captured_directory._snapshot_owned_path_build_directories():
                os._exit(102)
            if owner.path.joinpath("payload.bin").read_bytes() != b"parent":
                os._exit(103)
            try:
                owner.close()
            except RuntimeError as error:
                if "PID boundary" not in str(error):
                    os._exit(104)
            else:
                os._exit(105)
            child_owner = OwnedPathBuildDirectory.prepare(
                child_parent / "attempt",
                _retention_group=group_token,
            )
            child_owner.path.joinpath("payload.bin").write_bytes(b"child")
            child_orphans = _retry_owned_path_build_cleanup_for_group(group_token)
            if len(child_orphans) != 1 or not child_owner.closed:
                os._exit(106)
            if (
                child_orphans[0].reopen(
                    lambda reader: reader.read_bytes("payload.bin", max_bytes=16)
                )
                != b"child"
            ):
                os._exit(107)
        except BaseException:  # noqa: B036 - child reports exact failure
            os._exit(108)
        os._exit(0)

    _pid, status = os.waitpid(child, 0)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert owner.path.joinpath("payload.bin").read_bytes() == b"parent"
    assert not owner.closed
    owner.close()
    assert owner.closed


def test_owned_path_build_rejects_preexisting_ancestor_symlink(
    tmp_path: Path,
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    trusted = tmp_path / "trusted"
    trusted.mkdir(mode=0o700)
    (trusted / "alias").symlink_to(foreign, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        OwnedPathBuildDirectory.prepare(trusted / "alias" / "attempt")

    assert not tuple(foreign.glob(".attempt.normalize-*"))


def test_owned_path_build_create_parent_uses_private_missing_parent(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "new" / "nested" / "attempt"

    owner = OwnedPathBuildDirectory.prepare(destination, create_parent=True)
    try:
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
        assert not destination.exists()
    finally:
        owner.close()


def test_owned_path_build_rejects_ancestor_replacement_and_retries_cleanup(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("initial writer"):
        (owner.path / "owned.bin").write_bytes(b"owned")
    saved_parent = tmp_path / "saved-parent"
    parent.rename(saved_parent)
    parent.mkdir(mode=0o700)
    owner.path.mkdir(mode=0o700)
    valuable = owner.path / "valuable.bin"
    valuable.write_bytes(b"foreign")

    with pytest.raises(RuntimeError, match="changed during before raced writer"):
        with owner.path_operation("raced writer"):
            (owner.path / "should-not-exist").write_bytes(b"bad")
    with pytest.raises(RuntimeError) as caught:
        owner.close()

    assert caught.value.captured_directory_cleanup_owner is owner
    assert not owner.closed
    assert valuable.read_bytes() == b"foreign"
    assert not (owner.path / "should-not-exist").exists()

    foreign_parent = tmp_path / "foreign-parent"
    parent.rename(foreign_parent)
    saved_parent.rename(parent)
    owner.close()

    assert owner.closed
    assert (
        foreign_parent / owner.path.name / "valuable.bin"
    ).read_bytes() == b"foreign"


def test_owned_path_build_rejects_child_replacement_and_retries_cleanup(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("initial writer"):
        (owner.path / "owned.bin").write_bytes(b"owned")
    stolen = parent / "stolen-owned-root"
    owner.path.rename(stolen)
    owner.path.mkdir(mode=0o700)
    valuable = owner.path / "valuable.bin"
    valuable.write_bytes(b"foreign")

    with pytest.raises(RuntimeError, match="changed during before raced writer"):
        with owner.path_operation("raced writer"):
            (owner.path / "should-not-exist").write_bytes(b"bad")
    with pytest.raises(RuntimeError) as caught:
        owner.close()

    assert caught.value.captured_directory_cleanup_owner is owner
    assert not owner.closed
    assert valuable.read_bytes() == b"foreign"
    foreign = parent / "foreign-root"
    owner.path.rename(foreign)
    stolen.rename(owner.path)
    owner.close()

    assert owner.closed
    assert (foreign / "valuable.bin").read_bytes() == b"foreign"
    assert not (foreign / "should-not-exist").exists()


def test_owned_path_build_rejects_active_root_disappearance_until_restored(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("path writer"):
        owner.path.joinpath("payload.bin").write_bytes(b"owned")
    stolen = parent / "stolen-owned-root"
    owner.path.rename(stolen)

    with pytest.raises(RuntimeError, match="disappeared") as caught:
        owner.close()

    assert caught.value.captured_directory_cleanup_owner is owner
    assert not owner.closed
    assert stolen.joinpath("payload.bin").read_bytes() == b"owned"
    stolen.rename(owner.path)

    orphan = owner.isolate()
    assert not owner.closed
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        == b"owned"
    )
    owner.close()
    assert owner.closed


def test_owned_path_build_preserves_capture_cancellation_and_remains_owned(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("path writer"):
        for index in range(3):
            (owner.path / f"payload-{index}.bin").write_bytes(b"bytes")
    interruption = KeyboardInterrupt("capture cancelled")

    def cancel() -> None:
        raise interruption

    with pytest.raises(KeyboardInterrupt) as caught:
        owner.capture_ownership(check_cancelled=cancel)

    assert caught.value is interruption
    assert not owner.closed
    assert owner.path.is_dir()
    owner.close()
    assert owner.closed


def test_owned_path_build_retains_retry_after_isolation_cleanup_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("path writer"):
        (owner.path / "payload.bin").write_bytes(b"bytes")
    interruption = KeyboardInterrupt("cleanup interrupted")
    real_close = captured_directory.OwnedDirectoryStage._close_descriptors
    interrupted = False

    def interrupt_once(stage: OwnedDirectoryStage) -> None:
        nonlocal interrupted
        if stage is owner._stage and not interrupted:
            interrupted = True
            raise interruption
        real_close(stage)

    monkeypatch.setattr(
        captured_directory.OwnedDirectoryStage,
        "_close_descriptors",
        interrupt_once,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        owner.isolate()

    assert caught.value is interruption
    assert caught.value.captured_directory_cleanup_owner is owner
    assert not owner.closed
    assert not owner.path.exists()

    orphan = owner.isolate()
    assert not owner.closed
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        == b"bytes"
    )
    owner.close()
    assert owner.closed


def test_owned_path_build_reconciles_move_before_helper_result_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("path writer"):
        owner.path.joinpath("payload.bin").write_bytes(b"bytes")
    real_discard = captured_directory._discard_owned_directory_with_authority
    interruption = KeyboardInterrupt("after isolation helper result")
    interrupted = False

    def discard_then_interrupt(*args, **kwargs):
        nonlocal interrupted
        orphan = real_discard(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise interruption
        return orphan

    monkeypatch.setattr(
        captured_directory,
        "_discard_owned_directory_with_authority",
        discard_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        owner.isolate()

    assert caught.value is interruption
    assert caught.value.captured_directory_cleanup_owner is owner
    assert not owner.path.exists()
    retained = owner._isolation_owner
    assert retained is not None and retained.orphan is not None

    orphan = owner.isolate()
    assert orphan is retained.orphan
    assert not owner.closed
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        == b"bytes"
    )
    owner.close()
    assert owner.closed


@pytest.mark.parametrize("post_commit_error", (False, True))
def test_owned_path_build_rebinds_restored_source_before_isolation_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_commit_error: bool,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("path writer"):
        owner.path.joinpath("payload.bin").write_bytes(b"bytes")
    real_require = atomic_directory._OwnedDirectoryIsolationOwner._require_exact_tree
    failed_verification = False

    def fail_first_moved_verification(
        isolation_owner,
        path: Path,
        *,
        label: str,
        allow_root_rename: bool = False,
    ) -> None:
        nonlocal failed_verification
        if label == "moved destination" and not failed_verification:
            failed_verification = True
            raise RuntimeError("injected moved-tree verification failure")
        real_require(
            isolation_owner,
            path,
            label=label,
            allow_root_rename=allow_root_rename,
        )

    monkeypatch.setattr(
        atomic_directory._OwnedDirectoryIsolationOwner,
        "_require_exact_tree",
        fail_first_moved_verification,
    )
    restore_interrupted = False
    if post_commit_error:
        authority = owner._authority_owner.authority
        assert authority is not None
        real_rename = atomic_directory._PublicationAuthority.rename_noreplace

        def restore_then_error(candidate, source: str, destination: str):
            nonlocal restore_interrupted
            result = real_rename(candidate, source, destination)
            if (
                candidate is authority
                and destination == owner.path.name
                and source != owner.path.name
                and not restore_interrupted
            ):
                restore_interrupted = True
                raise OSError(errno.EIO, "injected post-restore failure")
            return result

        monkeypatch.setattr(
            atomic_directory._PublicationAuthority,
            "rename_noreplace",
            restore_then_error,
        )

    with pytest.raises(RuntimeError, match="could not be isolated") as caught:
        owner.isolate()

    assert caught.value.captured_directory_cleanup_owner is owner
    assert failed_verification
    assert restore_interrupted is post_commit_error
    assert owner.path.joinpath("payload.bin").read_bytes() == b"bytes"
    retained = owner._isolation_owner
    assert retained is not None
    assert retained.orphan is None
    assert retained._candidate is None

    orphan = owner.isolate()

    assert not owner.closed
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        == b"bytes"
    )
    owner.close()
    assert owner.closed


def test_owned_path_build_rechecks_parent_after_descriptor_close_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("path writer"):
        owner.path.joinpath("payload.bin").write_bytes(b"bytes")
    real_close = OwnedDirectoryStage._close_descriptors
    interruption = KeyboardInterrupt("after descriptor close")
    interrupted = False

    def close_then_interrupt(stage: OwnedDirectoryStage) -> None:
        nonlocal interrupted
        real_close(stage)
        if stage is owner._stage and not interrupted:
            interrupted = True
            raise interruption

    monkeypatch.setattr(
        OwnedDirectoryStage,
        "_close_descriptors",
        close_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        owner.isolate()

    retained = owner._isolation_owner
    assert retained is not None and retained.orphan is not None
    orphan = retained.orphan
    saved_parent = tmp_path / "saved-private"
    parent.rename(saved_parent)
    parent.mkdir(mode=0o700)

    with pytest.raises(RuntimeError) as caught:
        owner.isolate()

    assert caught.value.captured_directory_cleanup_owner is owner
    assert not owner.closed
    assert (
        saved_parent.joinpath(orphan.path.name, "payload.bin").read_bytes() == b"bytes"
    )
    replacement_parent = tmp_path / "replacement-private"
    parent.rename(replacement_parent)
    saved_parent.rename(parent)

    assert owner.isolate() is orphan
    assert not owner.closed
    owner.close()
    assert owner.closed


def test_owned_path_build_rechecks_orphan_child_before_authority_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    owner = OwnedPathBuildDirectory.prepare(parent / "attempt")
    with owner.path_operation("path writer"):
        owner.path.joinpath("payload.bin").write_bytes(b"bytes")
    real_close = OwnedDirectoryStage._close_descriptors
    interruption = KeyboardInterrupt("after descriptor close")
    interrupted = False

    def close_then_interrupt(stage: OwnedDirectoryStage) -> None:
        nonlocal interrupted
        real_close(stage)
        if stage is owner._stage and not interrupted:
            interrupted = True
            raise interruption

    monkeypatch.setattr(
        OwnedDirectoryStage,
        "_close_descriptors",
        close_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt):
        owner.isolate()

    retained = owner._isolation_owner
    assert retained is not None and retained.orphan is not None
    orphan = retained.orphan
    saved_orphan = parent / "saved-exact-orphan"
    orphan.path.rename(saved_orphan)
    orphan.path.mkdir(mode=0o700)
    orphan.path.joinpath("valuable.bin").write_bytes(b"foreign")

    with pytest.raises(RuntimeError) as caught:
        owner.isolate()

    assert caught.value.captured_directory_cleanup_owner is owner
    assert not owner.closed
    assert orphan.path.joinpath("valuable.bin").read_bytes() == b"foreign"
    foreign = parent / "foreign-orphan"
    orphan.path.rename(foreign)
    saved_orphan.rename(orphan.path)

    assert owner.isolate() is orphan
    assert not owner.closed
    assert foreign.joinpath("valuable.bin").read_bytes() == b"foreign"
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        == b"bytes"
    )
    owner.close()
    assert owner.closed


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


def test_workspace_plan_preserves_runtime_cancellation_during_budget_scan() -> None:
    cancellation = RuntimeError("exact workspace plan cancellation")
    calls = 0

    def check_cancelled() -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise cancellation

    with pytest.raises(RuntimeError) as raised:
        WorkspacePlan(
            subject_digest="d" * 64,
            directories=(WorkspaceDirectory("first"), WorkspaceDirectory("second")),
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancellation


def test_workspace_plan_polls_between_directory_and_file_records() -> None:
    cancellation = RuntimeError("stop before the first workspace file")

    def check_cancelled() -> None:
        raise cancellation

    with pytest.raises(RuntimeError) as raised:
        WorkspacePlan(
            subject_digest="d" * 64,
            directories=(WorkspaceDirectory("nested"),),
            files=(WorkspaceFile("nested/payload", max_bytes=1),),
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancellation


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    [
        (
            "subject_digest",
            type("DigestSubclass", (str,), {})("d" * 64),
            ValueError,
            "digest",
        ),
        (
            "root_mode",
            type("ModeSubclass", (int,), {})(0o700),
            TypeError,
            "mode",
        ),
    ],
)
def test_workspace_plan_rejects_nonexact_top_level_scalars_before_poll(
    field: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    cancellation = KeyboardInterrupt(f"pending nonexact {field} stop")
    kwargs = {
        "subject_digest": "d" * 64,
        "directories": (WorkspaceDirectory("first"), WorkspaceDirectory("second")),
        field: value,
    }

    def check_cancelled() -> None:
        raise cancellation

    with pytest.raises(error_type, match=message) as raised:
        WorkspacePlan(check_cancelled=check_cancelled, **kwargs)

    assert raised.value is not cancellation


@pytest.mark.parametrize("bad_index", [0, 1])
@pytest.mark.parametrize("entry_kind", ["directory", "file"])
def test_workspace_plan_current_field_error_precedes_pending_stop(
    entry_kind: str,
    bad_index: int,
) -> None:
    cancellation = SystemExit(f"pending {entry_kind} field stop")
    polls = 0
    entries: list[WorkspaceDirectory | WorkspaceFile]
    if entry_kind == "directory":
        entries = [WorkspaceDirectory("first"), WorkspaceDirectory("second")]
        object.__setattr__(entries[bad_index], "path", object())
        kwargs = {"directories": tuple(entries)}
        expected_error = TypeError
        message = "directory fields must use exact types"
    else:
        entries = [WorkspaceFile("first"), WorkspaceFile("second")]
        object.__setattr__(entries[bad_index], "max_bytes", -1)
        kwargs = {"files": tuple(entries)}
        expected_error = ValueError
        message = "size limit is out of bounds"

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls > bad_index:
            raise cancellation

    with pytest.raises(expected_error, match=message) as raised:
        WorkspacePlan(
            subject_digest="d" * 64,
            check_cancelled=check_cancelled,
            **kwargs,
        )

    assert raised.value is not cancellation
    assert polls == bad_index


@pytest.mark.parametrize("entry_kind", ["directory", "file"])
def test_workspace_plan_stop_precedes_poisoned_future_fields(entry_kind: str) -> None:
    cancellation = KeyboardInterrupt(f"exact future {entry_kind} stop")
    if entry_kind == "directory":
        first = WorkspaceDirectory("first")
        poisoned = WorkspaceDirectory("second")
        object.__setattr__(poisoned, "path", object())
        kwargs = {"directories": (first, poisoned)}
    else:
        first = WorkspaceFile("first")
        poisoned = WorkspaceFile("second")
        object.__setattr__(poisoned, "max_bytes", -1)
        kwargs = {"files": (first, poisoned)}

    def check_cancelled() -> None:
        raise cancellation

    with pytest.raises(KeyboardInterrupt) as raised:
        WorkspacePlan(
            subject_digest="d" * 64,
            check_cancelled=check_cancelled,
            **kwargs,
        )

    assert raised.value is cancellation


def test_workspace_plan_none_detaches_valid_frozen_items() -> None:
    source_directory = WorkspaceDirectory("nested")
    source_file = WorkspaceFile("nested/payload", max_bytes=8)
    plan = WorkspacePlan(
        subject_digest="d" * 64,
        directories=(source_directory,),
        files=(source_file,),
    )

    assert plan.directories[0] == source_directory
    assert plan.directories[0] is not source_directory
    assert plan.files[0] == source_file
    assert plan.files[0] is not source_file


@pytest.mark.parametrize(
    "case",
    ["directory-group", "file-current", "mutated-directory"],
)
def test_workspace_plan_ancestor_error_precedes_pending_stop(case: str) -> None:
    cancellation = SystemExit(f"pending {case} ancestor stop")
    if case == "directory-group":
        directories = (WorkspaceDirectory("missing/child"),)
        files = (WorkspaceFile("future"),)
    elif case == "file-current":
        directories = ()
        files = (WorkspaceFile("missing/child"), WorkspaceFile("future"))
    else:
        forged = WorkspaceDirectory("valid")
        object.__setattr__(forged, "path", PurePosixPath("missing/child"))
        directories = (forged,)
        files = (WorkspaceFile("future"),)
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise cancellation

    with pytest.raises(ValueError, match="missing directory ancestor") as raised:
        WorkspacePlan(
            subject_digest="d" * 64,
            directories=directories,
            files=files,
            check_cancelled=check_cancelled,
        )

    assert raised.value is not cancellation
    assert polls == 0


def test_workspace_plan_stop_precedes_future_directory_ancestor() -> None:
    cancellation = KeyboardInterrupt("exact future directory ancestor stop")

    def check_cancelled() -> None:
        raise cancellation

    with pytest.raises(KeyboardInterrupt) as raised:
        WorkspacePlan(
            subject_digest="d" * 64,
            directories=(
                WorkspaceDirectory("missing/child"),
                WorkspaceDirectory("missing"),
            ),
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancellation


@pytest.mark.parametrize("entry_kind", ["directories", "files"])
def test_workspace_plan_sort_stops_before_poisoned_next_item(
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    cancellation = SystemExit(f"exact {entry_kind} sort cancellation")
    real_merge = captured_directory.heapq.merge
    armed = False
    poison_consumed = False

    def poisoned_merge(*runs: object, key: object = None) -> Iterator[object]:
        nonlocal armed, poison_consumed
        merged = real_merge(*runs, key=key)  # type: ignore[arg-type]
        first = next(merged)
        armed = True
        yield first
        poison_consumed = True
        raise AssertionError("workspace plan sort consumed its poisoned next item")

    def check_cancelled() -> None:
        if armed:
            raise cancellation

    monkeypatch.setattr(captured_directory, "_WORKSPACE_SORT_RUN_ENTRIES", 1)
    monkeypatch.setattr(captured_directory.heapq, "merge", poisoned_merge)
    directories = (
        (WorkspaceDirectory("second"), WorkspaceDirectory("first"))
        if entry_kind == "directories"
        else ()
    )
    files = (
        (WorkspaceFile("second"), WorkspaceFile("first"))
        if entry_kind == "files"
        else ()
    )

    with pytest.raises(SystemExit) as raised:
        WorkspacePlan(
            subject_digest="d" * 64,
            directories=directories,
            files=files,
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancellation
    assert not poison_consumed


def test_workspace_plan_none_sort_keeps_canonical_output_without_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_merge(*_args: object, **_kwargs: object) -> Iterator[object]:
        raise AssertionError("None workspace plan sort used interruptible merge")
        yield  # pragma: no cover - retain the iterator call shape

    monkeypatch.setattr(captured_directory, "_WORKSPACE_SORT_RUN_ENTRIES", 1)
    monkeypatch.setattr(captured_directory.heapq, "merge", forbidden_merge)

    plan = WorkspacePlan(
        subject_digest="d" * 64,
        directories=(WorkspaceDirectory("second"), WorkspaceDirectory("first")),
        files=(WorkspaceFile("zeta"), WorkspaceFile("alpha")),
    )

    assert tuple(item.path.as_posix() for item in plan.directories) == (
        "first",
        "second",
    )
    assert tuple(item.path.as_posix() for item in plan.files) == (
        "alpha",
        "zeta",
    )


def test_workspace_plan_interruptible_sort_matches_canonical_none_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directories = (
        WorkspaceDirectory("zeta"),
        WorkspaceDirectory("alpha"),
        WorkspaceDirectory("middle"),
    )
    files = (
        WorkspaceFile("zeta/payload"),
        WorkspaceFile("alpha/payload"),
        WorkspaceFile("middle/payload"),
    )
    expected = WorkspacePlan(
        subject_digest="d" * 64,
        directories=directories,
        files=files,
    )
    calls = 0

    def check_cancelled() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(captured_directory, "_WORKSPACE_SORT_RUN_ENTRIES", 1)

    actual = WorkspacePlan(
        subject_digest="d" * 64,
        directories=directories,
        files=files,
        check_cancelled=check_cancelled,
    )

    assert calls > len(directories) + len(files)
    assert actual == expected
    assert actual.digest == expected.digest


def test_workspace_plan_snapshot_preserves_runtime_cancellation() -> None:
    plan = WorkspacePlan(
        subject_digest="d" * 64,
        directories=(WorkspaceDirectory("first"), WorkspaceDirectory("second")),
    )
    cancellation = RuntimeError("exact workspace plan snapshot cancellation")

    def check_cancelled() -> None:
        raise cancellation

    with pytest.raises(RuntimeError) as raised:
        captured_directory._snapshot_workspace_plan(
            plan,
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancellation


@pytest.mark.parametrize("bad_index", [0, 1])
@pytest.mark.parametrize("entry_kind", ["directory", "file"])
def test_workspace_plan_snapshot_current_semantics_precede_pending_stop(
    entry_kind: str,
    bad_index: int,
) -> None:
    if entry_kind == "directory":
        plan = WorkspacePlan(
            subject_digest="d" * 64,
            directories=(WorkspaceDirectory("first"), WorkspaceDirectory("second")),
        )
        object.__setattr__(
            plan.directories[bad_index],
            "path",
            PurePosixPath("../invalid"),
        )
        message = "normalized and bounded"
    else:
        plan = WorkspacePlan(
            subject_digest="d" * 64,
            files=(WorkspaceFile("first"), WorkspaceFile("second")),
        )
        object.__setattr__(plan.files[bad_index], "max_bytes", -1)
        message = "size limit is out of bounds"
    cancellation = SystemExit(f"pending snapshot {entry_kind} stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if polls > bad_index:
            raise cancellation

    with pytest.raises(ValueError, match=message) as raised:
        captured_directory._snapshot_workspace_plan(
            plan,
            check_cancelled=check_cancelled,
        )

    assert raised.value is not cancellation
    assert polls == bad_index


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("subject_digest", "x" * 64, "lowercase sha256"),
        ("digest", "x" * 64, "plan digest must be lowercase sha256"),
        ("root_mode", -1, "portable permission bits"),
    ],
)
def test_workspace_plan_snapshot_top_level_error_precedes_first_item_stop(
    field: str,
    value: object,
    message: str,
) -> None:
    plan = WorkspacePlan(
        subject_digest="d" * 64,
        files=(WorkspaceFile("first"), WorkspaceFile("second")),
    )
    object.__setattr__(plan, field, value)
    cancellation = SystemExit(f"pending snapshot {field} stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise cancellation

    with pytest.raises(ValueError, match=message) as raised:
        captured_directory._snapshot_workspace_plan(
            plan,
            check_cancelled=check_cancelled,
        )

    assert raised.value is not cancellation
    assert polls == 0


def test_workspace_plan_snapshot_none_rejects_malformed_source_digest() -> None:
    plan = WorkspacePlan(
        subject_digest="d" * 64,
        files=(WorkspaceFile("first"), WorkspaceFile("second")),
    )
    object.__setattr__(plan, "digest", "x" * 64)

    with pytest.raises(ValueError, match="plan digest must be lowercase sha256"):
        captured_directory._snapshot_workspace_plan(plan)


def test_workspace_plan_snapshot_none_rejects_well_formed_wrong_digest() -> None:
    plan = WorkspacePlan(
        subject_digest="d" * 64,
        files=(WorkspaceFile("first"), WorkspaceFile("second")),
    )
    wrong_digest = "0" * 64 if plan.digest != "0" * 64 else "1" * 64
    object.__setattr__(plan, "digest", wrong_digest)

    with pytest.raises(ValueError, match="plan digest is inconsistent"):
        captured_directory._snapshot_workspace_plan(plan)


def test_workspace_snapshot_stop_precedes_wrong_digest_and_future_poison() -> None:
    plan = WorkspacePlan(
        subject_digest="d" * 64,
        files=(WorkspaceFile("first"), WorkspaceFile("second")),
    )
    wrong_digest = "0" * 64 if plan.digest != "0" * 64 else "1" * 64
    object.__setattr__(plan, "digest", wrong_digest)
    object.__setattr__(plan.files[1], "max_bytes", -1)
    cancellation = KeyboardInterrupt("exact wrong-digest future stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise cancellation

    with pytest.raises(KeyboardInterrupt) as raised:
        captured_directory._snapshot_workspace_plan(
            plan,
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancellation
    assert polls == 1


@pytest.mark.parametrize("entry_kind", ["directory", "file"])
def test_workspace_plan_snapshot_ancestor_error_precedes_pending_stop(
    entry_kind: str,
) -> None:
    if entry_kind == "directory":
        plan = WorkspacePlan(
            subject_digest="d" * 64,
            directories=(WorkspaceDirectory("valid"),),
            files=(WorkspaceFile("future"),),
        )
        object.__setattr__(
            plan.directories[0],
            "path",
            PurePosixPath("missing/child"),
        )
    else:
        plan = WorkspacePlan(
            subject_digest="d" * 64,
            files=(WorkspaceFile("first"), WorkspaceFile("future")),
        )
        object.__setattr__(plan.files[0], "path", PurePosixPath("missing/child"))
    cancellation = SystemExit(f"pending snapshot {entry_kind} ancestor stop")
    polls = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        raise cancellation

    with pytest.raises(ValueError, match="missing directory ancestor") as raised:
        captured_directory._snapshot_workspace_plan(
            plan,
            check_cancelled=check_cancelled,
        )

    assert raised.value is not cancellation
    assert polls == 0


@pytest.mark.parametrize("entry_kind", ["directory", "file"])
def test_workspace_plan_snapshot_stop_precedes_poisoned_future_semantics(
    entry_kind: str,
) -> None:
    if entry_kind == "directory":
        plan = WorkspacePlan(
            subject_digest="d" * 64,
            directories=(WorkspaceDirectory("first"), WorkspaceDirectory("second")),
        )
        object.__setattr__(plan.directories[1], "path", PurePosixPath("../poison"))
    else:
        plan = WorkspacePlan(
            subject_digest="d" * 64,
            files=(WorkspaceFile("first"), WorkspaceFile("second")),
        )
        object.__setattr__(plan.files[1], "max_bytes", -1)
    cancellation = KeyboardInterrupt(f"exact future snapshot {entry_kind} stop")

    def check_cancelled() -> None:
        raise cancellation

    with pytest.raises(KeyboardInterrupt) as raised:
        captured_directory._snapshot_workspace_plan(
            plan,
            check_cancelled=check_cancelled,
        )

    assert raised.value is cancellation


def test_workspace_plan_snapshot_none_revalidates_and_detaches_items() -> None:
    plan = WorkspacePlan(
        subject_digest="d" * 64,
        directories=(WorkspaceDirectory("nested"),),
        files=(WorkspaceFile("nested/payload", max_bytes=8),),
    )

    detached = captured_directory._snapshot_workspace_plan(plan)

    assert detached == plan
    assert detached is not plan
    assert detached.directories[0] is not plan.directories[0]
    assert detached.files[0] is not plan.files[0]


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


def test_workspace_refresh_integrity_precedes_latched_cancellation(
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
        workspace.write_file("payload", (b"safe",))
        cancellation = KeyboardInterrupt("latched after forged capture")
        armed = False
        real_capture = atomic_directory._PublicationAuthority.capture_child

        def capture_then_forge(
            authority: object,
            name: str,
            **kwargs: object,
        ) -> _TreeOwnership:
            nonlocal armed
            kwargs.pop("check_cancelled", None)
            observed = real_capture(authority, name, **kwargs)  # type: ignore[arg-type]
            armed = True
            return replace(observed, inventory=())

        def check_cancelled() -> None:
            if armed:
                raise cancellation

        monkeypatch.setattr(
            atomic_directory._PublicationAuthority,
            "capture_child",
            capture_then_forge,
        )

        try:
            with pytest.raises(
                RuntimeError,
                match="inventory differs from its plan",
            ):
                workspace.seal(check_cancelled=check_cancelled)
            assert armed
            assert workspace.state == "closed"
        finally:
            workspace.close()


def test_workspace_refresh_stops_before_poisoned_next_inventory_item(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="8" * 64,
        files=(
            WorkspaceFile("first", max_bytes=1),
            WorkspaceFile("second", max_bytes=1),
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
        workspace.write_file("first", (b"1",))
        workspace.write_file("second", (b"2",))
        cancellation = KeyboardInterrupt("exact refresh inventory stop")
        armed = False
        poison_consumed = False

        class PoisonedKeys(dict):
            def __iter__(self) -> Iterator[str]:
                nonlocal armed, poison_consumed
                iterator = super().__iter__()
                first = next(iterator)
                armed = True
                yield first
                poison_consumed = True
                raise AssertionError(
                    "workspace refresh consumed its poisoned inventory item"
                )

        def check_cancelled() -> None:
            if armed:
                raise cancellation

        workspace._written_files = PoisonedKeys(workspace._written_files)
        try:
            with pytest.raises(KeyboardInterrupt) as raised:
                workspace.seal(check_cancelled=check_cancelled)

            assert raised.value is cancellation
            assert not poison_consumed
            assert workspace.state == "closed"
        finally:
            workspace.close()


@pytest.mark.parametrize(
    "projection_name",
    [
        "directory_ownership_entry_identities",
        "directory_ownership_file_records",
    ],
)
def test_workspace_refresh_stops_before_poisoned_next_projection_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    projection_name: str,
) -> None:
    plan = WorkspacePlan(
        subject_digest="8" * 64,
        files=(
            WorkspaceFile("first", max_bytes=1),
            WorkspaceFile("second", max_bytes=1),
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
        workspace.write_file("first", (b"1",))
        workspace.write_file("second", (b"2",))
        cancellation = SystemExit(f"exact refresh {projection_name} stop")
        armed = False
        poison_consumed = False
        real_projection = getattr(captured_directory, projection_name)

        class PoisonedProjection(tuple):
            def __iter__(self) -> Iterator[object]:
                nonlocal armed, poison_consumed
                iterator = super().__iter__()
                first = next(iterator)
                armed = True
                yield first
                poison_consumed = True
                raise AssertionError(
                    "workspace refresh consumed its poisoned projection item"
                )

        def poisoned_projection(ownership: object) -> PoisonedProjection:
            projected = real_projection(ownership)
            assert len(projected) >= 2
            return PoisonedProjection(projected)

        def check_cancelled() -> None:
            if armed:
                raise cancellation

        monkeypatch.setattr(
            captured_directory,
            projection_name,
            poisoned_projection,
        )
        try:
            with pytest.raises(SystemExit) as raised:
                workspace.seal(check_cancelled=check_cancelled)

            assert raised.value is cancellation
            assert not poison_consumed
            assert workspace.state == "closed"
        finally:
            workspace.close()


@pytest.mark.parametrize("validation_kind", ["directory", "file"])
def test_workspace_refresh_stops_before_poisoned_next_validation_item(
    tmp_path: Path,
    validation_kind: str,
) -> None:
    plan = WorkspacePlan(
        subject_digest="8" * 64,
        directories=(
            (
                WorkspaceDirectory("first"),
                WorkspaceDirectory("second"),
            )
            if validation_kind == "directory"
            else ()
        ),
        files=(
            (
                WorkspaceFile("first", max_bytes=1),
                WorkspaceFile("second", max_bytes=1),
            )
            if validation_kind == "file"
            else ()
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
        if validation_kind == "file":
            workspace.write_file("first", (b"1",))
            workspace.write_file("second", (b"2",))
        cancellation = SystemExit(f"exact refresh {validation_kind} validation stop")
        armed = False
        poison_consumed = False

        class PoisonedItems(dict):
            def items(self) -> Iterator[tuple[object, object]]:
                nonlocal armed, poison_consumed
                iterator = iter(super().items())
                first = next(iterator)
                armed = True
                yield first
                poison_consumed = True
                raise AssertionError(
                    "workspace refresh consumed its poisoned validation item"
                )

        def check_cancelled() -> None:
            if armed:
                raise cancellation

        if validation_kind == "directory":
            workspace._directory_identities = PoisonedItems(
                workspace._directory_identities
            )
        else:
            workspace._written_files = PoisonedItems(workspace._written_files)
        try:
            with pytest.raises(SystemExit) as raised:
                workspace.seal(check_cancelled=check_cancelled)

            assert raised.value is cancellation
            assert not poison_consumed
            assert workspace.state == "closed"
        finally:
            workspace.close()


def test_workspace_refresh_current_file_mismatch_precedes_pending_stop(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="8" * 64,
        files=(
            WorkspaceFile("first", max_bytes=1),
            WorkspaceFile("second", max_bytes=1),
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
        workspace.write_file("first", (b"1",))
        workspace.write_file("second", (b"2",))
        cancellation = KeyboardInterrupt("pending refresh mismatch stop")
        armed = False
        forged = dict(workspace._written_files)
        first_path = next(iter(forged))
        identity, mode, size, digest = forged[first_path]
        forged[first_path] = (
            (identity[0], identity[1] + 1, *identity[2:]),
            mode,
            size,
            digest,
        )

        class ArmCurrentItems(dict):
            def items(self) -> Iterator[tuple[object, object]]:
                nonlocal armed
                iterator = iter(super().items())
                first = next(iterator)
                armed = True
                yield first
                yield from iterator

        def check_cancelled() -> None:
            if armed:
                raise cancellation

        workspace._written_files = ArmCurrentItems(forged)
        try:
            with pytest.raises(RuntimeError, match="file changed") as raised:
                workspace.seal(check_cancelled=check_cancelled)

            assert raised.value is not cancellation
            assert armed
            assert workspace.state == "closed"
        finally:
            workspace.close()


def test_workspace_refresh_has_no_terminal_poll_before_caller_postcondition(
    tmp_path: Path,
) -> None:
    plan = WorkspacePlan(
        subject_digest="8" * 64,
        files=(WorkspaceFile("payload", max_bytes=1),),
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
        workspace.write_file("payload", (b"1",))
        cancellation = KeyboardInterrupt("forbidden terminal refresh stop")
        armed = False
        terminal_polls = 0
        caller_postcondition = False

        class ArmLastSpec(dict):
            def __iter__(self) -> Iterator[str]:
                nonlocal armed
                iterator = super().__iter__()
                for index, path in enumerate(iterator):
                    if index + 1 == len(self):
                        armed = True
                    yield path

        def check_cancelled() -> None:
            nonlocal terminal_polls
            if armed:
                terminal_polls += 1
                raise cancellation

        workspace._file_specs = ArmLastSpec(workspace._file_specs)

        def refresh_then_finish_caller() -> object:
            nonlocal caller_postcondition
            ownership = workspace._refresh_locked(
                require_complete=True,
                check_cancelled=check_cancelled,
            )
            caller_postcondition = True
            return ownership

        try:
            ownership = workspace._lock.run(refresh_then_finish_caller)

            assert ownership is not None
            assert caller_postcondition
            assert armed
            assert terminal_polls == 0
        finally:
            workspace.close()


def test_workspace_refresh_none_preserves_capture_call_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="8" * 64,
        files=(WorkspaceFile("payload", max_bytes=1),),
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
        workspace.write_file("payload", (b"1",))
        capture_kwargs: list[dict[str, object]] = []
        real_capture = atomic_directory._PublicationAuthority.capture_child

        def record_capture(
            authority: object,
            name: str,
            **kwargs: object,
        ) -> _TreeOwnership:
            capture_kwargs.append(dict(kwargs))
            return real_capture(authority, name, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            atomic_directory._PublicationAuthority,
            "capture_child",
            record_capture,
        )
        try:
            ownership = workspace._lock.run(
                lambda: workspace._refresh_locked(require_complete=True)
            )

            assert ownership is not None
            assert len(capture_kwargs) == 1
            assert "check_cancelled" not in capture_kwargs[0]
        finally:
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


def test_owned_workspace_seal_stops_before_future_directory_fsync(
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
        real_fsync = captured_directory.os.fsync
        cancellation = KeyboardInterrupt("exact directory fsync stop")
        observed: list[str] = []
        armed = False

        def poison_future_fsync(descriptor: int) -> None:
            nonlocal armed
            path = descriptor_paths.get(descriptor)
            if path is not None:
                if observed:
                    raise AssertionError("cancelled seal reached a future fsync")
                observed.append(path)
                armed = True
            real_fsync(descriptor)

        def check_cancelled() -> None:
            if armed:
                raise cancellation

        monkeypatch.setattr(captured_directory, "_WORKSPACE_SORT_RUN_ENTRIES", 1)
        monkeypatch.setattr(captured_directory.os, "fsync", poison_future_fsync)
        try:
            with pytest.raises(KeyboardInterrupt) as raised:
                workspace.seal(check_cancelled=check_cancelled)

            assert raised.value is cancellation
            assert observed == ["nested/deeper"]
            assert workspace.state == "closed"
        finally:
            workspace.close()


def test_owned_workspace_fsync_sort_stops_before_poisoned_future_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = WorkspacePlan(
        subject_digest="2" * 64,
        directories=(WorkspaceDirectory("first"), WorkspaceDirectory("second")),
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
        real_merge = captured_directory.heapq.merge
        cancellation = SystemExit("exact directory sort stop")
        armed = False
        poison_consumed = False

        def poisoned_merge(
            *runs: object,
            key: object = None,
            reverse: bool = False,
        ) -> Iterator[object]:
            nonlocal armed, poison_consumed
            merged = real_merge(
                *runs,
                key=key,  # type: ignore[arg-type]
                reverse=reverse,
            )
            first = next(merged)
            armed = True
            yield first
            poison_consumed = True
            raise AssertionError("directory sort consumed its poisoned future path")

        def check_cancelled() -> None:
            if armed:
                raise cancellation

        def forbidden_fsync(_descriptor: int) -> None:
            raise AssertionError("cancelled directory sort reached fsync")

        monkeypatch.setattr(captured_directory, "_WORKSPACE_SORT_RUN_ENTRIES", 1)
        monkeypatch.setattr(captured_directory.heapq, "merge", poisoned_merge)
        monkeypatch.setattr(captured_directory.os, "fsync", forbidden_fsync)
        try:
            with pytest.raises(SystemExit) as raised:
                workspace._lock.run(
                    lambda: workspace._fsync_directories_locked(
                        check_cancelled=check_cancelled
                    )
                )

            assert raised.value is cancellation
            assert not poison_consumed
        finally:
            workspace.close()


@pytest.mark.parametrize("mutate_final_binding", [False, True])
def test_owned_workspace_final_directory_fsync_reaches_seal_postflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate_final_binding: bool,
) -> None:
    plan = WorkspacePlan(subject_digest="2" * 64)
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
        workspace_root_descriptor = workspace._root_descriptor
        real_fsync = captured_directory.os.fsync
        real_child_metadata = atomic_directory._PublicationAuthority.child_metadata
        cancellation = KeyboardInterrupt("exact final directory fsync stop")
        armed = False
        corrupted_postflight = False

        def arm_after_final_fsync(descriptor: int) -> None:
            nonlocal armed
            real_fsync(descriptor)
            if descriptor == workspace_root_descriptor:
                armed = True

        def observe_postflight_metadata(
            authority: object,
            name: str,
            **kwargs: object,
        ) -> os.stat_result | None:
            nonlocal corrupted_postflight
            metadata = real_child_metadata(
                authority,  # type: ignore[arg-type]
                name,
                **kwargs,
            )
            if (
                mutate_final_binding
                and armed
                and not corrupted_postflight
                and metadata is not None
            ):
                values = list(metadata)
                values[stat.ST_INO] += 1
                corrupted_postflight = True
                return os.stat_result(values)
            return metadata

        def check_cancelled() -> None:
            if armed:
                raise cancellation

        monkeypatch.setattr(captured_directory.os, "fsync", arm_after_final_fsync)
        monkeypatch.setattr(
            atomic_directory._PublicationAuthority,
            "child_metadata",
            observe_postflight_metadata,
        )
        try:
            if mutate_final_binding:
                with pytest.raises(RuntimeError, match="root changed") as raised:
                    workspace.seal(check_cancelled=check_cancelled)
                assert raised.value is not cancellation
                assert corrupted_postflight
            else:
                with pytest.raises(KeyboardInterrupt) as raised:
                    workspace.seal(check_cancelled=check_cancelled)
                assert raised.value is cancellation
            assert armed
            assert workspace.state == "closed"
        finally:
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


def test_receipt_final_capture_cancellation_preserves_exact_stop_and_owner(
    tmp_path: Path,
) -> None:
    destination, _plan, receipt_owner = _publish_payload_generation(
        tmp_path / "receipt-final-capture-stop",
        payload=b"safe",
    )
    cancellation = KeyboardInterrupt("exact receipt postflight stop")
    armed = False
    calls = 0
    result = object()

    def check_cancelled() -> None:
        nonlocal calls
        calls += 1
        if armed:
            raise cancellation

    def consume(
        _receipt: PublishedWorkspaceReceipt,
        _reader: atomic_directory.PublicationDirectoryReader,
    ) -> object:
        nonlocal armed
        armed = True
        return result

    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            receipt_owner.consume(consume, check_cancelled=check_cancelled)

        assert raised.value is cancellation
        assert calls > 1
        assert receipt_owner.active
        assert destination.joinpath("payload").read_bytes() == b"safe"
        assert (
            receipt_owner.consume(
                lambda _receipt, reader: reader.read_bytes("payload", max_bytes=4)
            )
            == b"safe"
        )
    finally:
        receipt_owner.close()


def test_receipt_final_capture_mutation_precedes_exact_stop(tmp_path: Path) -> None:
    destination, _plan, receipt_owner = _publish_payload_generation(
        tmp_path / "receipt-final-capture-mutation",
        payload=b"safe",
    )
    cancellation = SystemExit("exact receipt postflight stop")
    armed = False

    def check_cancelled() -> None:
        if armed:
            raise cancellation

    def mutate(
        _receipt: PublishedWorkspaceReceipt,
        _reader: atomic_directory.PublicationDirectoryReader,
    ) -> None:
        nonlocal armed
        destination.joinpath("payload").write_bytes(b"evil")
        armed = True

    try:
        with pytest.raises(
            RuntimeError,
            match="publication callback tree changed",
        ) as raised:
            receipt_owner.consume(mutate, check_cancelled=check_cancelled)

        assert raised.value is not cancellation
        assert receipt_owner.active
    finally:
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


@pytest.mark.parametrize("source_state", ("wrong-owner", "closed-owner"))
def test_replacement_bind_requires_the_active_owner_of_the_exact_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_state: str,
) -> None:
    parent = tmp_path / "replacement-bind-owner"
    destination, plan, source_owner = _publish_payload_generation(
        parent,
        destination_name="published",
        stage_name=".first-source",
        payload=b"old",
    )
    _other_destination, _other_plan, other_owner = _publish_payload_generation(
        parent,
        destination_name="other",
        stage_name=".second-source",
        payload=b"old",
    )
    binding = source_owner.destination_binding
    selected_owner = other_owner
    if source_state == "closed-owner":
        source_owner.close()
        selected_owner = source_owner
    native_owner = _leased_native_replacement_owner(parent, destination)
    workspace = OwnedWorkspaceAuthority()
    provision_calls: list[object] = []

    def forbidden_provision(*_args: object, **_kwargs: object) -> None:
        provision_calls.append(object())
        raise AssertionError("inactive binding reached candidate provisioning")

    monkeypatch.setattr(
        workspace_owner,
        "provision_owner_replacement",
        forbidden_provision,
    )
    try:
        with pytest.raises(RuntimeError, match="active|closed"):
            workspace.bind_replacement_source(
                selected_owner,
                destination_binding=binding,
                native_owner=native_owner,
                stage_name=".replacement-stage",
                plan=plan,
            )

        assert provision_calls == []
        assert workspace.state == "empty"
        assert workspace_owner.owner_state(native_owner) == "destination-leased"
        assert not parent.joinpath(".replacement-stage").exists()
        assert destination.joinpath("payload").read_bytes() == b"old"
    finally:
        workspace.close()
        if not workspace_owner.owner_closed(native_owner):
            workspace_owner.abort_owner(native_owner)
        if not source_owner.closed:
            source_owner.close()
        other_owner.close()


@pytest.mark.parametrize("mismatch", ("parent", "incumbent"))
def test_replacement_bind_rejects_native_mismatch_before_candidate_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    parent = tmp_path / "replacement-bind-mismatch"
    destination, plan, source_owner = _publish_payload_generation(
        parent,
        destination_name="published",
        payload=b"old",
    )
    binding = source_owner.destination_binding
    native_owner = _leased_native_replacement_owner(parent, destination)
    workspace = OwnedWorkspaceAuthority()
    foreign_descriptor = -1
    provision_calls: list[object] = []

    if mismatch == "parent":
        foreign_parent = tmp_path / "foreign-parent"
        foreign_parent.mkdir()
        foreign_descriptor = os.open(
            foreign_parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        monkeypatch.setattr(
            workspace_owner,
            "borrow_owner_parent_descriptor",
            lambda _owner: foreign_descriptor,
        )
        expected_message = "parent differs"
    else:
        alternate = tmp_path / "alternate-incumbent"
        alternate.mkdir()
        alternate.joinpath("payload").write_bytes(b"old")
        alternate_ownership = capture_directory_ownership(alternate)
        monkeypatch.setattr(
            captured_directory,
            "_capture_posix_directory_descriptor",
            lambda *_args, **_kwargs: alternate_ownership,
        )
        expected_message = "incumbent differs"

    def forbidden_provision(*_args: object, **_kwargs: object) -> None:
        provision_calls.append(object())
        raise AssertionError("mismatched binding reached candidate provisioning")

    monkeypatch.setattr(
        workspace_owner,
        "provision_owner_replacement",
        forbidden_provision,
    )
    try:
        with pytest.raises(RuntimeError, match=expected_message):
            workspace.bind_replacement_source(
                source_owner,
                destination_binding=binding,
                native_owner=native_owner,
                stage_name=".replacement-stage",
                plan=plan,
            )

        assert provision_calls == []
        assert workspace.state == "closed"
        assert workspace_owner.owner_closed(native_owner)
        assert not parent.joinpath(".replacement-stage").exists()
        assert destination.joinpath("payload").read_bytes() == b"old"
        if foreign_descriptor >= 0:
            os.fstat(foreign_descriptor)
    finally:
        if workspace.state != "closed":
            workspace.close()
        if not workspace_owner.owner_closed(native_owner):
            workspace_owner.abort_owner(native_owner)
        if foreign_descriptor >= 0:
            os.close(foreign_descriptor)
        source_owner.close()


@pytest.mark.parametrize(
    "cancellation",
    (
        pytest.param(
            SystemExit("exact outer receipt preflight stop"),
            id="system-exit",
        ),
        pytest.param(
            TypeError("exact outer receipt preflight type stop"),
            id="type-error",
        ),
        pytest.param(
            ValueError("exact outer receipt preflight value stop"),
            id="value-error",
        ),
    ),
)
def test_replacement_bind_stops_in_real_outer_receipt_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancellation: BaseException,
) -> None:
    parent = tmp_path / "replacement-bind-outer-preflight-stop"
    destination, plan, source_owner = _publish_payload_generation(
        parent,
        destination_name="published",
        payload=b"old",
    )
    binding = source_owner.destination_binding
    native_owner = _leased_native_replacement_owner(parent, destination)
    workspace = OwnedWorkspaceAuthority()
    receipt_checks: list[object] = []
    poison_consumed = False
    real_receipt_capture = atomic_directory.PublicationDirectoryReader.capture_ownership
    real_scandir = atomic_directory.os.scandir

    def check_cancelled() -> None:
        raise cancellation

    def observe_real_receipt_capture(
        reader: atomic_directory.PublicationDirectoryReader,
        **kwargs: object,
    ) -> _TreeOwnership:
        receipt_checks.append(kwargs.get("check_cancelled"))
        return real_receipt_capture(reader, **kwargs)  # type: ignore[arg-type]

    def poison_future_record_scan(*_args: object, **_kwargs: object) -> object:
        nonlocal poison_consumed
        poison_consumed = True
        raise AssertionError("cancelled outer preflight consumed its poisoned record")

    monkeypatch.setattr(
        atomic_directory.PublicationDirectoryReader,
        "capture_ownership",
        observe_real_receipt_capture,
    )
    monkeypatch.setattr(
        atomic_directory.os,
        "scandir",
        poison_future_record_scan,
    )
    retry_owner: object | None = None
    retry_workspace: OwnedWorkspaceAuthority | None = None
    try:
        with pytest.raises(type(cancellation)) as raised:
            workspace.bind_replacement_source(
                source_owner,
                destination_binding=binding,
                native_owner=native_owner,
                stage_name=".replacement-stage",
                plan=plan,
                check_cancelled=check_cancelled,
            )

        assert raised.value is cancellation
        assert receipt_checks == [check_cancelled]
        assert not poison_consumed
        assert workspace.state == "empty"
        assert workspace_owner.owner_state(native_owner) == "destination-leased"
        assert source_owner.active
        assert destination.joinpath("payload").read_bytes() == b"old"
        assert not parent.joinpath(".replacement-stage").exists()

        workspace.close()
        workspace_owner.abort_owner(native_owner)
        assert workspace.state == "closed"
        assert workspace_owner.owner_closed(native_owner)

        monkeypatch.setattr(atomic_directory.os, "scandir", real_scandir)
        monkeypatch.setattr(
            atomic_directory.PublicationDirectoryReader,
            "capture_ownership",
            real_receipt_capture,
        )
        retry_owner = _leased_native_replacement_owner(parent, destination)
        retry_workspace = OwnedWorkspaceAuthority()
        retry_workspace.bind_replacement_source(
            source_owner,
            destination_binding=binding,
            native_owner=retry_owner,
            stage_name=".replacement-retry",
            plan=plan,
        )
        assert retry_workspace.state == "replacement-bound"
        assert source_owner.active
    finally:
        if retry_workspace is not None and retry_workspace.state != "closed":
            retry_workspace.close()
        if retry_owner is not None and not workspace_owner.owner_closed(retry_owner):
            workspace_owner.abort_owner(retry_owner)
        if workspace.state != "closed":
            workspace.close()
        if not workspace_owner.owner_closed(native_owner):
            workspace_owner.abort_owner(native_owner)
        source_owner.close()


def test_replacement_bind_threads_exact_cancellation_through_all_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "replacement-bind-cancellation"
    destination, plan, source_owner = _publish_payload_generation(
        parent,
        destination_name="published",
        payload=b"old",
    )
    binding = source_owner.destination_binding
    native_owner = _leased_native_replacement_owner(parent, destination)
    workspace = OwnedWorkspaceAuthority()
    cancellation = SystemExit("exact replacement binding stop")
    consume_checks: list[object] = []
    receipt_checks: list[object] = []
    native_checks: list[object] = []
    armed = False
    real_consume = PublishedWorkspaceReceiptOwner.consume
    real_receipt_capture = atomic_directory.PublicationDirectoryReader.capture_ownership
    real_native_capture = captured_directory._capture_posix_directory_descriptor

    def check_cancelled() -> None:
        if armed:
            raise cancellation

    def record_consume(
        owner: PublishedWorkspaceReceiptOwner,
        callback: object,
        **kwargs: object,
    ) -> object:
        consume_checks.append(kwargs.get("check_cancelled"))
        return real_consume(owner, callback, **kwargs)  # type: ignore[arg-type]

    def record_receipt_capture(
        reader: atomic_directory.PublicationDirectoryReader,
        **kwargs: object,
    ) -> _TreeOwnership:
        receipt_checks.append(kwargs.get("check_cancelled"))
        return real_receipt_capture(reader, **kwargs)  # type: ignore[arg-type]

    def cancel_native_capture(*args: object, **kwargs: object) -> _TreeOwnership:
        nonlocal armed
        native_checks.append(kwargs.get("check_cancelled"))
        armed = True
        return real_native_capture(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", record_consume)
    monkeypatch.setattr(
        atomic_directory.PublicationDirectoryReader,
        "capture_ownership",
        record_receipt_capture,
    )
    monkeypatch.setattr(
        captured_directory,
        "_capture_posix_directory_descriptor",
        cancel_native_capture,
    )
    try:
        with pytest.raises(SystemExit) as raised:
            workspace.bind_replacement_source(
                source_owner,
                destination_binding=binding,
                native_owner=native_owner,
                stage_name=".replacement-stage",
                plan=plan,
                check_cancelled=check_cancelled,
            )

        assert raised.value is cancellation
        assert consume_checks == [check_cancelled]
        assert check_cancelled in receipt_checks
        assert native_checks == [check_cancelled]
        assert workspace.state == "closed"
        assert workspace_owner.owner_closed(native_owner)
        assert source_owner.active
        assert destination.joinpath("payload").read_bytes() == b"old"
        assert not parent.joinpath(".replacement-stage").exists()
    finally:
        if workspace.state != "closed":
            workspace.close()
        if not workspace_owner.owner_closed(native_owner):
            workspace_owner.abort_owner(native_owner)
        source_owner.close()


def test_replacement_bind_receipt_mutation_precedes_latched_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "replacement-bind-receipt-mutation"
    destination, plan, source_owner = _publish_payload_generation(
        parent,
        destination_name="published",
        payload=b"old",
    )
    binding = source_owner.destination_binding
    native_owner = _leased_native_replacement_owner(parent, destination)
    workspace = OwnedWorkspaceAuthority()
    cancellation = KeyboardInterrupt("latched replacement receipt stop")
    armed = False
    real_native_capture = captured_directory._capture_posix_directory_descriptor

    def check_cancelled() -> None:
        if armed:
            raise cancellation

    def mutate_after_native_capture(
        *args: object,
        **kwargs: object,
    ) -> _TreeOwnership:
        nonlocal armed
        ownership = real_native_capture(*args, **kwargs)  # type: ignore[arg-type]
        destination.joinpath("payload").write_bytes(b"evil")
        armed = True
        return ownership

    monkeypatch.setattr(
        captured_directory,
        "_capture_posix_directory_descriptor",
        mutate_after_native_capture,
    )
    retry_owner: object | None = None
    try:
        with pytest.raises(
            RuntimeError,
            match="publication callback tree changed",
        ) as raised:
            workspace.bind_replacement_source(
                source_owner,
                destination_binding=binding,
                native_owner=native_owner,
                stage_name=".replacement-stage",
                plan=plan,
                check_cancelled=check_cancelled,
            )

        assert raised.value is not cancellation
        assert workspace.state == "closed"
        assert workspace_owner.owner_closed(native_owner)
        assert source_owner.active
        assert destination.joinpath("payload").read_bytes() == b"evil"
        assert not parent.joinpath(".replacement-stage").exists()

        retry_owner = _leased_native_replacement_owner(parent, destination)
        assert workspace_owner.owner_state(retry_owner) == "destination-leased"
    finally:
        if workspace.state != "closed":
            workspace.close()
        if not workspace_owner.owner_closed(native_owner):
            workspace_owner.abort_owner(native_owner)
        if retry_owner is not None and not workspace_owner.owner_closed(retry_owner):
            workspace_owner.abort_owner(retry_owner)
        source_owner.close()


def test_replacement_bind_authority_mutation_precedes_latched_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "replacement-bind-authority-mutation"
    destination, plan, source_owner = _publish_payload_generation(
        parent,
        destination_name="published",
        payload=b"old",
    )
    binding = source_owner.destination_binding
    native_owner = _leased_native_replacement_owner(parent, destination)
    workspace = OwnedWorkspaceAuthority()
    cancellation = SystemExit("latched replacement authority stop")
    moved_parent = tmp_path / "moved-replacement-authority"
    armed = False
    real_native_capture = captured_directory._capture_posix_directory_descriptor

    def check_cancelled() -> None:
        nonlocal armed
        if armed:
            armed = False
            parent.rename(moved_parent)
            parent.mkdir(mode=0o700)
            destination.mkdir(mode=0o700)
            raise cancellation

    def arm_after_native_capture(
        *args: object,
        **kwargs: object,
    ) -> _TreeOwnership:
        nonlocal armed
        ownership = real_native_capture(*args, **kwargs)  # type: ignore[arg-type]
        armed = True
        return ownership

    monkeypatch.setattr(
        captured_directory,
        "_capture_posix_directory_descriptor",
        arm_after_native_capture,
    )
    retry_owner: object | None = None
    try:
        with pytest.raises(
            RuntimeError,
            match="publication parent path changed",
        ) as raised:
            workspace.bind_replacement_source(
                source_owner,
                destination_binding=binding,
                native_owner=native_owner,
                stage_name=".replacement-stage",
                plan=plan,
                check_cancelled=check_cancelled,
            )

        assert raised.value is not cancellation
        assert workspace.state == "closed"
        assert workspace_owner.owner_closed(native_owner)
        assert source_owner.active
        assert moved_parent.joinpath("published/payload").read_bytes() == b"old"
        assert tuple(destination.iterdir()) == ()
        assert not parent.joinpath(".replacement-stage").exists()
        assert not moved_parent.joinpath(".replacement-stage").exists()

        retry_owner = _leased_native_replacement_owner(parent, destination)
        assert workspace_owner.owner_state(retry_owner) == "destination-leased"
    finally:
        if workspace.state != "closed":
            workspace.close()
        if not workspace_owner.owner_closed(native_owner):
            workspace_owner.abort_owner(native_owner)
        if retry_owner is not None and not workspace_owner.owner_closed(retry_owner):
            workspace_owner.abort_owner(retry_owner)
        source_owner.close()


def test_replacement_bind_preserves_none_legacy_scanner_call_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "replacement-bind-none-shape"
    destination, plan, source_owner = _publish_payload_generation(
        parent,
        destination_name="published",
        payload=b"old",
    )
    binding = source_owner.destination_binding
    native_owner = _leased_native_replacement_owner(parent, destination)
    workspace = OwnedWorkspaceAuthority()
    consume_kwargs: list[dict[str, object]] = []
    receipt_kwargs: list[dict[str, object]] = []
    native_kwargs: list[dict[str, object]] = []
    real_consume = PublishedWorkspaceReceiptOwner.consume
    real_receipt_capture = atomic_directory.PublicationDirectoryReader.capture_ownership
    real_native_capture = captured_directory._capture_posix_directory_descriptor

    def record_consume(
        owner: PublishedWorkspaceReceiptOwner,
        callback: object,
        **kwargs: object,
    ) -> object:
        consume_kwargs.append(dict(kwargs))
        return real_consume(owner, callback, **kwargs)  # type: ignore[arg-type]

    def record_receipt_capture(
        reader: atomic_directory.PublicationDirectoryReader,
        **kwargs: object,
    ) -> _TreeOwnership:
        receipt_kwargs.append(dict(kwargs))
        return real_receipt_capture(reader, **kwargs)  # type: ignore[arg-type]

    def record_native_capture(*args: object, **kwargs: object) -> _TreeOwnership:
        native_kwargs.append(dict(kwargs))
        return real_native_capture(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(PublishedWorkspaceReceiptOwner, "consume", record_consume)
    monkeypatch.setattr(
        atomic_directory.PublicationDirectoryReader,
        "capture_ownership",
        record_receipt_capture,
    )
    monkeypatch.setattr(
        captured_directory,
        "_capture_posix_directory_descriptor",
        record_native_capture,
    )
    try:
        workspace.bind_replacement_source(
            source_owner,
            destination_binding=binding,
            native_owner=native_owner,
            stage_name=".replacement-stage",
            plan=plan,
        )

        assert consume_kwargs == [{}]
        assert {"allow_empty_root": True} in receipt_kwargs
        assert len(native_kwargs) == 1
        assert "check_cancelled" not in native_kwargs[0]
    finally:
        if workspace.state != "closed":
            workspace.close()
        if not workspace_owner.owner_closed(native_owner):
            workspace_owner.abort_owner(native_owner)
        source_owner.close()


def test_real_v5_replacement_has_exact_order_and_candidate_only_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    with _bound_native_replacement(tmp_path) as prepared:
        workspace = prepared.workspace
        real_exchange = workspace._replacement_exchange
        assert real_exchange is not None

        def observed_exchange(
            source: bytes,
            destination: bytes,
            deadline_ns: int,
        ) -> object:
            events.append("exchange")
            return real_exchange(source, destination, deadline_ns)

        workspace._replacement_exchange = observed_exchange
        real_provision = workspace_owner.provision_owner_replacement

        def observed_provision(*args: object, **kwargs: object) -> None:
            events.append("provision")
            real_provision(*args, **kwargs)

        monkeypatch.setattr(
            workspace_owner,
            "provision_owner_replacement",
            observed_provision,
        )
        assert workspace.state == "replacement-bound"
        assert not prepared.stage.exists()
        workspace.provision_bound_replacement(deadline_ns=_replacement_deadline_ns())
        workspace.write_file("payload", (b"new",))
        workspace.seal()

        with pytest.raises(RuntimeError, match="replacement|dedicated"):
            workspace.publish_into(prepared.output_owner)
        assert prepared.output_owner.state == "empty"
        assert prepared.destination.joinpath("payload").read_bytes() == b"old"

        real_commit = workspace_owner.commit_owner_receipt

        def observed_commit(token: object) -> None:
            events.append("commit")
            real_commit(token)

        monkeypatch.setattr(
            workspace_owner,
            "commit_owner_receipt",
            observed_commit,
        )

        def forbidden_global(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("validator intercepted a frozen native callback")

        def validate_staged(
            reader: atomic_directory.PublicationDirectoryReader,
        ) -> None:
            assert reader.read_bytes("payload", max_bytes=3) == b"new"
            events.append("validate-staged")
            monkeypatch.setattr(
                workspace_owner,
                "_exchange_owner_replacement_exact",
                forbidden_global,
            )
            monkeypatch.setattr(
                workspace_owner,
                "commit_owner_receipt",
                forbidden_global,
            )
            monkeypatch.setattr(
                workspace_owner,
                "verify_owner_authority",
                forbidden_global,
            )
            monkeypatch.setattr(
                workspace_owner,
                "verify_owner_replacement_binding",
                forbidden_global,
            )
            monkeypatch.setattr(
                captured_directory,
                "_publish_staged_directory_with_authority",
                forbidden_global,
            )
            monkeypatch.setattr(
                atomic_directory,
                "DirectoryOrphan",
                forbidden_global,
            )
            monkeypatch.setattr(
                atomic_directory,
                "_DirectoryOrphanLocator",
                forbidden_global,
            )
            monkeypatch.setattr(
                atomic_directory._NativeReplacementPublication,
                "capture_incumbent",
                forbidden_global,
            )

        def validate_published(
            reader: atomic_directory.PublicationDirectoryReader,
        ) -> None:
            assert reader.read_bytes("payload", max_bytes=3) == b"new"
            assert prepared.stage.joinpath("payload").read_bytes() == b"old"
            events.append("validate-published")

        workspace.publish_replacement_into(
            prepared.output_owner,
            deadline_ns=_replacement_deadline_ns(),
            validate_staged_directory=validate_staged,
            validate_published_destination=validate_published,
        )

        assert events == [
            "provision",
            "validate-staged",
            "exchange",
            "validate-published",
            "commit",
        ]
        assert prepared.output_owner.active
        assert (
            workspace_owner.owner_state(prepared.native_owner)
            == "replacement-receipted"
        )
        orphan = prepared.output_owner.receipt.orphan
        assert orphan is not None
        assert orphan.locator.backend_tag == "linux-renameat2"
        assert (
            orphan.reopen(lambda reader: reader.read_bytes("payload", max_bytes=3))
            == b"old"
        )
        with pytest.raises(RuntimeError):
            prepared.source_owner.consume(lambda _receipt, _reader: None)

        parked = prepared.parent / ".parked-incumbent"
        orphan.path.rename(parked)
        assert (
            prepared.output_owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "payload",
                    max_bytes=3,
                )
            )
            == b"new"
        )

        parked_candidate = prepared.parent / ".parked-candidate"
        prepared.destination.rename(parked_candidate)
        prepared.destination.mkdir()
        prepared.destination.joinpath("payload").write_bytes(b"new")
        with pytest.raises(RuntimeError):
            prepared.output_owner.consume(lambda _receipt, _reader: None)

        prepared.output_owner.close()
        assert workspace_owner.owner_closed(prepared.native_owner)
        assert parked.joinpath("payload").read_bytes() == b"old"
        parked.rename(orphan.path)
        assert (
            orphan.reopen(lambda reader: reader.read_bytes("payload", max_bytes=3))
            == b"old"
        )
        parked = prepared.parent / ".original-displaced-incumbent"
        orphan.path.rename(parked)
        orphan.path.mkdir()
        orphan.path.joinpath("payload").write_bytes(b"old")
        with pytest.raises(RuntimeError):
            orphan.reopen(lambda _reader: None)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_bound_replacement_child_cleanup_cannot_affect_parent_lease_or_mapping(
    tmp_path: Path,
) -> None:
    with _bound_native_replacement(tmp_path) as prepared:
        workspace = prepared.workspace
        workspace.provision_bound_replacement(deadline_ns=_replacement_deadline_ns())
        descriptors = {
            workspace._replacement_parent_descriptor,
            workspace._replacement_incumbent_descriptor,
            workspace._root_descriptor,
            *workspace._directory_descriptors.values(),
        }
        descriptors.discard(-1)
        child = os.fork()
        if child == 0:  # pragma: no branch - child reports exact status
            signal.signal(signal.SIGALRM, lambda *_args: os._exit(80))
            signal.alarm(3)
            try:
                workspace.close()
            except RuntimeError as error:
                if "PID boundary" not in str(error):
                    os._exit(81)
            except BaseException:  # noqa: B036 - child reports exact failure
                os._exit(82)
            else:
                os._exit(83)
            for descriptor in descriptors:
                try:
                    os.fstat(descriptor)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        os._exit(84)
                else:
                    os._exit(85)
            os._exit(0)

        _pid, status = os.waitpid(child, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
        for descriptor in descriptors:
            os.fstat(descriptor)
        assert workspace_owner.owner_state(prepared.native_owner) == (
            "replacement-adopted"
        )
        assert prepared.destination.joinpath("payload").read_bytes() == b"old"
        assert prepared.stage.is_dir()

        workspace.write_file("payload", (b"new",))
        workspace.seal()
        workspace.publish_replacement_into(
            prepared.output_owner,
            deadline_ns=_replacement_deadline_ns(),
        )
        assert (
            prepared.output_owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "payload",
                    max_bytes=3,
                )
            )
            == b"new"
        )


@pytest.mark.parametrize("install_point", ("before-store", "after-store"))
def test_replacement_baseexception_settles_on_the_receipt_slot_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    install_point: str,
) -> None:
    with _bound_native_replacement(tmp_path) as prepared:
        workspace = prepared.workspace
        workspace.provision_bound_replacement(deadline_ns=_replacement_deadline_ns())
        workspace.write_file("payload", (b"new",))
        workspace.seal()
        real_install = PublishedWorkspaceReceiptOwner._install
        real_abort = workspace_owner.abort_owner
        real_commit = workspace_owner.commit_owner_receipt
        abort_calls: list[object] = []
        commit_calls: list[object] = []
        interruption = KeyboardInterrupt(install_point)

        def observed_abort(owner: object) -> None:
            abort_calls.append(owner)
            real_abort(owner)

        def observed_commit(token: object) -> None:
            commit_calls.append(token)
            real_commit(token)

        def interrupted_install(self, reservation, receipt) -> None:
            if install_point == "after-store":
                real_install(self, reservation, receipt)
            raise interruption

        monkeypatch.setattr(workspace_owner, "abort_owner", observed_abort)
        monkeypatch.setattr(
            workspace_owner,
            "commit_owner_receipt",
            observed_commit,
        )
        monkeypatch.setattr(
            PublishedWorkspaceReceiptOwner,
            "_install",
            interrupted_install,
        )

        with pytest.raises(KeyboardInterrupt) as caught:
            workspace.publish_replacement_into(
                prepared.output_owner,
                deadline_ns=_replacement_deadline_ns(),
            )
        assert caught.value is interruption

        if install_point == "before-store":
            assert abort_calls == [prepared.native_owner]
            assert commit_calls == []
            assert prepared.output_owner.state == "cleanup"
            with pytest.raises(RuntimeError, match="expected active"):
                _ = prepared.output_owner.receipt
            assert workspace_owner.owner_closed(prepared.native_owner)
            assert prepared.destination.joinpath("payload").read_bytes() == b"old"
            prepared.output_owner.close()
            assert prepared.output_owner.closed
            assert workspace.state == "closed"
        else:
            assert abort_calls == []
            assert len(commit_calls) == 1
            assert prepared.output_owner.active
            assert (
                workspace_owner.owner_state(prepared.native_owner)
                == "replacement-receipted"
            )
            assert prepared.destination.joinpath("payload").read_bytes() == b"new"


def test_replacement_commit_failure_retries_same_token_without_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _bound_native_replacement(tmp_path) as prepared:
        workspace = prepared.workspace
        workspace.provision_bound_replacement(deadline_ns=_replacement_deadline_ns())
        workspace.write_file("payload", (b"new",))
        workspace.seal()
        real_commit = workspace_owner.commit_owner_receipt
        real_abort = workspace_owner.abort_owner
        commit_calls: list[object] = []
        abort_calls: list[object] = []
        first_error = OSError(errno.EIO, "injected receipt commit failure")

        def retrying_commit(token: object) -> None:
            commit_calls.append(token)
            if len(commit_calls) == 1:
                raise first_error
            real_commit(token)

        def observed_abort(owner: object) -> None:
            abort_calls.append(owner)
            real_abort(owner)

        monkeypatch.setattr(
            workspace_owner,
            "commit_owner_receipt",
            retrying_commit,
        )
        monkeypatch.setattr(workspace_owner, "abort_owner", observed_abort)

        with pytest.raises(OSError, match="receipt commit failure") as caught:
            workspace.publish_replacement_into(
                prepared.output_owner,
                deadline_ns=_replacement_deadline_ns(),
            )

        assert caught.value is first_error
        assert len(commit_calls) == 2
        assert commit_calls[0] is commit_calls[1]
        assert abort_calls == []
        assert prepared.output_owner.active
        assert (
            workspace_owner.owner_state(prepared.native_owner)
            == "replacement-receipted"
        )
        assert (
            prepared.output_owner.consume(
                lambda _receipt, reader: reader.read_bytes(
                    "payload",
                    max_bytes=3,
                )
            )
            == b"new"
        )
