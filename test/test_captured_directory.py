# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import codenib._atomic_directory as atomic_directory
import codenib._captured_directory as captured_directory
import codenib._windows_fs_authority as windows_authority
from codenib._atomic_directory import (
    capture_directory_ownership,
    directory_ownership_file_records,
)
from codenib._captured_directory import CapturedDirectoryReader, OwnedDirectoryStage


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
