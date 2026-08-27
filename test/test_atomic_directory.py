# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dis
import errno
import hashlib
import inspect
import os
import stat
import sys
import threading
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import codenib._atomic_directory as atomic_module
import codenib._windows_fs_authority as windows_authority_module
from codenib._atomic_directory import (
    DirectoryOrphan,
    capture_directory_ownership,
    directory_ownership_inventory,
    publication_parent_identity,
    publish_staged_directory,
)


def _write_tree(root: Path, name: str, value: str) -> None:
    root.mkdir()
    (root / name).write_text(value, encoding="utf-8")


def _private_reclaimer_parent(
    tmp_path: Path,
    *,
    name: str = "attempt-pool",
) -> tuple[Path, tuple[int, ...]]:
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        pytest.skip("quiescent directory reclamation is POSIX-only")
    parent = tmp_path / name
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        identity = publication_parent_identity(descriptor)
    finally:
        os.close(descriptor)
    return parent, identity


def _assert_quiescent_reclaimer_publicly_retryable(
    reclaimer: atomic_module.QuiescentDirectoryReclaimer,
    *,
    first_result: bool = False,
) -> None:
    """Prove the public lease is free in this and another thread."""

    results: list[bool] = []
    failures: list[BaseException] = []

    def retry_in_worker() -> None:
        try:
            results.append(reclaimer.retry())
        except BaseException as error:  # noqa: B036 - report thread failure
            failures.append(error)

    worker = threading.Thread(target=retry_in_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert failures == []
    assert results == [first_result]
    assert reclaimer.retry() is False


def _raise_from_quiescent_reclaimer_context(
    reclaimer: atomic_module.QuiescentDirectoryReclaimer,
    error: BaseException,
) -> None:
    with reclaimer:
        raise error


def _exception_chain_cleanup_owners(
    error: BaseException,
) -> tuple[object, ...]:
    """Collect bounded owner tuples reachable through built-in error links."""

    pending = [error]
    seen: set[int] = set()
    owners: list[object] = []
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        try:
            retained = BaseException.__getattribute__(
                current,
                "publication_cleanup_owners",
            )
        except BaseException:  # noqa: B036 - hostile metadata stays on a carrier
            retained = ()
        if type(retained) is tuple and len(retained) <= 64:
            for owner in retained:
                if not any(candidate is owner for candidate in owners):
                    owners.append(owner)
        for attribute in ("__cause__", "__context__"):
            try:
                linked = vars(BaseException)[attribute].__get__(
                    current,
                    type(current),
                )
            except BaseException:  # noqa: B036 - test only inspects inert links
                continue
            if isinstance(linked, BaseException):
                pending.append(linked)
    return tuple(owners)


def _exception_chain_contains(
    error: BaseException,
    target: BaseException,
) -> bool:
    """Return whether built-in cause/context links retain one exact error."""

    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < 32:
        current = pending.pop()
        if current is target:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        for attribute in ("__cause__", "__context__"):
            linked = vars(BaseException)[attribute].__get__(current, type(current))
            if isinstance(linked, BaseException):
                pending.append(linked)
    return False


def _exception_link_graph_is_acyclic(error: BaseException) -> bool:
    """Independently reject a cycle in bounded raw cause/context links."""

    pending: list[tuple[BaseException, bool]] = [(error, False)]
    visiting: set[int] = set()
    complete: set[int] = set()
    while pending:
        current, leaving = pending.pop()
        identity = id(current)
        if leaving:
            visiting.discard(identity)
            complete.add(identity)
            continue
        if identity in visiting:
            return False
        if identity in complete:
            continue
        if len(visiting) + len(complete) >= 128:
            return False
        visiting.add(identity)
        pending.append((current, True))
        for attribute in ("__context__", "__cause__"):
            linked = vars(BaseException)[attribute].__get__(current, type(current))
            if isinstance(linked, BaseException):
                pending.append((linked, False))
    return True


def _bounded_exception_recovery_graph(error: BaseException) -> tuple[object, ...]:
    """Walk only raw exception links and small exact built-in carrier args."""

    pending: list[object] = [error]
    seen: set[int] = set()
    recovered: list[object] = []
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        assert len(seen) < 256
        seen.add(identity)
        recovered.append(current)
        if isinstance(current, BaseException):
            for attribute in ("__cause__", "__context__"):
                linked = vars(BaseException)[attribute].__get__(current, type(current))
                if isinstance(linked, BaseException):
                    pending.append(linked)
            if type(current) is RuntimeError:
                args = BaseException.__getattribute__(current, "args")
                assert type(args) is tuple and len(args) <= 64
                pending.append(args)
        elif type(current) is tuple:
            assert len(current) <= 64
            pending.extend(current)
    return tuple(recovered)


class _HostileCleanupOwnerMetadata:
    """Refuse even BaseException-level access to cleanup owner metadata."""

    def __get__(self, _instance: object, _owner: object) -> object:
        raise RuntimeError("hostile cleanup owner metadata read")

    def __set__(self, _instance: object, _value: object) -> None:
        raise RuntimeError("hostile cleanup owner metadata write")


class _HostileCleanupPrimary(ValueError):
    publication_cleanup_owners = _HostileCleanupOwnerMetadata()


class _HostileCleanupSecondary(OSError):
    publication_cleanup_owners = _HostileCleanupOwnerMetadata()


class _HostileTracebackPrimary(ValueError):
    @property
    def __traceback__(self) -> object:
        raise AssertionError("subclass traceback getter was dispatched")

    @__traceback__.setter
    def __traceback__(self, _value: object) -> None:
        raise AssertionError("subclass traceback setter was dispatched")


def _adopt_fake_native_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str] | None = None,
) -> SimpleNamespace:
    """Adopt the real dual-root facade around test-owned POSIX descriptors."""

    parent = tmp_path / "authority"
    parent.mkdir()
    stage = parent / ".candidate"
    destination = parent / "published"
    _write_tree(stage, "payload.bin", "new")
    _write_tree(destination, "payload.bin", "old")
    parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    candidate_descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
    incumbent_descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    native_owner = object()
    native_state = {"value": "replacement-adopted"}
    receipt_token = object()
    exchange_calls: list[tuple[bytes, bytes, int]] = []

    def require_owner(candidate: object) -> object:
        if candidate is not native_owner:
            raise TypeError("wrong native owner")
        return candidate

    def verify_owner(candidate: object) -> None:
        if candidate is not native_owner:
            raise TypeError("wrong native owner")

    def exchange(source: bytes, target: bytes, deadline_ns: int) -> object:
        exchange_calls.append((source, target, deadline_ns))
        if events is not None:
            events.append("exchange")
        source_name = os.fsdecode(source)
        target_name = os.fsdecode(target)
        swap_name = ".test-only-exchange-swap"
        os.rename(
            source_name,
            swap_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.rename(
            target_name,
            source_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.rename(
            swap_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        native_state["value"] = "replacement-exchanged-unreceipted"
        return receipt_token

    native_module = atomic_module._native_workspace_owner
    monkeypatch.setattr(native_module, "require_exact_owner", require_owner)
    monkeypatch.setattr(native_module, "verify_owner_authority", verify_owner)

    def forbidden_late_borrow(_candidate: object) -> int:
        raise AssertionError("replacement adoption borrowed a descriptor late")

    monkeypatch.setattr(
        native_module,
        "borrow_owner_parent_descriptor",
        forbidden_late_borrow,
    )
    monkeypatch.setattr(
        native_module,
        "borrow_owner_root_descriptor",
        forbidden_late_borrow,
    )
    monkeypatch.setattr(
        native_module,
        "owner_state",
        lambda candidate: (
            native_state["value"] if candidate is native_owner else "wrong-native-owner"
        ),
    )
    authority_owner = atomic_module._PublicationAuthorityOwner()
    try:
        authority, replacement = (
            atomic_module._adopt_native_posix_replacement_authority(
                parent,
                native_owner=native_owner,
                parent_descriptor=parent_descriptor,
                candidate_descriptor=candidate_descriptor,
                incumbent_descriptor=incumbent_descriptor,
                expected_parent_identity=publication_parent_identity(parent_descriptor),
                expected_incumbent_identity=(
                    atomic_module._directory_inode_identity(
                        os.fstat(incumbent_descriptor)
                    )
                ),
                destination_name=destination.name,
                replacement_slot=stage.name,
                exchange_callback=exchange,
                authority_owner=authority_owner,
            )
        )
    except BaseException:
        authority_owner.close()
        os.close(incumbent_descriptor)
        os.close(candidate_descriptor)
        os.close(parent_descriptor)
        raise
    return SimpleNamespace(
        authority=authority,
        authority_owner=authority_owner,
        candidate_descriptor=candidate_descriptor,
        destination=destination,
        exchange_calls=exchange_calls,
        incumbent_descriptor=incumbent_descriptor,
        native_owner=native_owner,
        native_state=native_state,
        parent=parent,
        parent_descriptor=parent_descriptor,
        receipt_token=receipt_token,
        replacement=replacement,
        stage=stage,
    )


def _close_fake_native_replacement(prepared: SimpleNamespace) -> None:
    prepared.authority_owner.close()
    os.close(prepared.incumbent_descriptor)
    os.close(prepared.candidate_descriptor)
    os.close(prepared.parent_descriptor)


def _exception_notes(error: BaseException) -> tuple[str, ...]:
    return (
        *tuple(getattr(error, "__notes__", ())),
        *tuple(getattr(error, "_codenib_cleanup_notes", ())),
    )


class _FakeWindowsApi:
    """In-memory HANDLE/FILE_ID model; it intentionally has no path reads."""

    def __init__(self) -> None:
        self.nodes: dict[int, dict[str, object]] = {}
        self.handles: dict[int, int] = {}
        self.offsets: dict[int, int] = {}
        self.next_file_id = 10
        self.next_handle = 100
        self.open_by_id_calls: list[tuple[int, int, int, bool]] = []
        self.rename_calls: list[tuple[int, int, str]] = []
        self.volume_root_id = self.add_directory()
        self.root_id = self.add_directory(self.volume_root_id, "authority")

    def add_directory(self, parent: int | None = None, name: str = "") -> int:
        file_id = self.next_file_id
        self.next_file_id += 1
        self.nodes[file_id] = {
            "directory": True,
            "children": {},
            "data": b"",
            "version": 1,
        }
        if parent is not None:
            children = self.nodes[parent]["children"]
            assert isinstance(children, dict)
            children[name] = file_id
        return file_id

    def add_file(self, parent: int, name: str, data: bytes) -> int:
        file_id = self.next_file_id
        self.next_file_id += 1
        self.nodes[file_id] = {
            "directory": False,
            "children": {},
            "data": data,
            "version": 1,
        }
        children = self.nodes[parent]["children"]
        assert isinstance(children, dict)
        children[name] = file_id
        return file_id

    def _new_handle(self, file_id: int) -> int:
        handle = self.next_handle
        self.next_handle += 1
        self.handles[handle] = file_id
        self.offsets[handle] = 0
        return handle

    def create_directory_handle(self, path: Path) -> int:
        normalized = str(path).replace("/", "\\").rstrip("\\").casefold()
        file_id = self.volume_root_id if normalized == "c:" else self.root_id
        return self._new_handle(file_id)

    def open_relative(
        self,
        parent_handle: int,
        name: str,
        *,
        desired_access: int,
        is_directory: bool,
        allow_reparse: bool,
    ) -> int:
        del desired_access, allow_reparse
        parent = self.nodes[self.handles[parent_handle]]
        children = parent["children"]
        assert isinstance(children, dict)
        matches = [
            file_id
            for child_name, file_id in children.items()
            if child_name.casefold() == name.casefold()
        ]
        if len(matches) != 1:
            raise FileNotFoundError(name)
        file_id = matches[0]
        assert self.nodes[file_id]["directory"] is is_directory
        return self._new_handle(file_id)

    def duplicate_handle(self, handle: int) -> int:
        return self._new_handle(self.handles[handle])

    def close(self, handle: int) -> None:
        self.handles.pop(handle, None)
        self.offsets.pop(handle, None)

    def metadata(self, handle: int) -> object:
        node = self.nodes[self.handles[handle]]
        directory = bool(node["directory"])
        data = node["data"]
        assert isinstance(data, bytes)
        version = int(node["version"])
        attributes = atomic_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY if directory else 0
        return atomic_module._WindowsHandleMetadata(
            st_dev=7,
            st_ino=self.handles[handle],
            st_mode=atomic_module._windows_mode_from_attributes(attributes),
            st_size=0 if directory else len(data),
            st_mtime_ns=version,
            st_ctime_ns=version,
            st_nlink=int(node.get("nlink", 1)),
            st_file_attributes=attributes,
            file_id_128=self.handles[handle].to_bytes(16, "little"),
        )

    def iter_directory(self, handle: int):
        node = self.nodes[self.handles[handle]]
        children = node["children"]
        assert isinstance(children, dict)
        for name, file_id in children.items():
            yield atomic_module._WindowsDirectoryEntry(
                name=name,
                file_id=file_id,
                attributes=(
                    atomic_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY
                    if self.nodes[file_id]["directory"]
                    else 0
                ),
                file_id_128=file_id.to_bytes(16, "little"),
            )

    def enumerate_directory(
        self,
        handle: int,
    ) -> tuple[object, ...]:
        return tuple(self.iter_directory(handle))

    def open_by_id(
        self,
        volume_hint: int,
        file_id: int,
        *,
        desired_access: int,
        is_directory: bool,
    ) -> int:
        assert self.nodes[file_id]["directory"] is is_directory
        self.open_by_id_calls.append(
            (volume_hint, file_id, desired_access, is_directory)
        )
        return self._new_handle(file_id)

    def read(self, handle: int, size: int) -> bytes:
        node = self.nodes[self.handles[handle]]
        data = node["data"]
        assert isinstance(data, bytes)
        offset = self.offsets[handle]
        block = data[offset : offset + size]
        self.offsets[handle] += len(block)
        return block

    def rename_noreplace(
        self,
        source_handle: int,
        parent_handle: int,
        destination: str,
    ) -> None:
        parent = self.nodes[self.handles[parent_handle]]
        children = parent["children"]
        assert isinstance(children, dict)
        if any(name.casefold() == destination.casefold() for name in children):
            raise FileExistsError(destination)
        source_id = self.handles[source_handle]
        source_names = [
            name for name, file_id in children.items() if file_id == source_id
        ]
        if len(source_names) != 1:
            raise FileNotFoundError(source_id)
        source_name = source_names[0]
        children[destination] = children.pop(source_name)
        parent["version"] = int(parent["version"]) + 1
        self.rename_calls.append((source_handle, parent_handle, destination))


def _install_fake_windows_api(
    monkeypatch: pytest.MonkeyPatch,
    api: _FakeWindowsApi,
) -> None:
    """Install the fake under POSIX while preserving Windows path spelling."""

    real_open = windows_authority_module.open_lexical_directory_authority

    def open_lexical(path: Path, **kwargs: object) -> object:
        raw = os.fspath(path).replace("\\", "/")
        drive_offset = raw.casefold().rfind("c:/")
        if drive_offset >= 0:
            path = Path(raw[drive_offset:])
        return real_open(path, **kwargs)

    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)
    monkeypatch.setattr(atomic_module, "_windows_require_publication_api", lambda: None)
    monkeypatch.setattr(
        atomic_module._windows_fs,
        "open_lexical_directory_authority",
        open_lexical,
    )


def test_publish_staged_directory_replaces_existing_tree(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    nested = destination / "nested"
    nested.mkdir()
    (nested / "one.txt").write_text("one", encoding="utf-8")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    orphan = publish_staged_directory(stage, destination)

    assert isinstance(orphan, DirectoryOrphan)
    assert orphan.verified_at_isolation
    assert orphan.path.name.startswith(".published.previous-")
    assert (orphan.path / "old.txt").read_text(encoding="utf-8") == "old"
    assert not stage.exists()
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_first_publication_does_not_create_an_empty_orphan(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    orphan = publish_staged_directory(stage, destination)

    assert orphan is None
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_rename_noreplace_at_moves_without_overwriting(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("source", encoding="utf-8")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        atomic_module._rename_noreplace_at("source", "moved", descriptor, descriptor)
        assert not source.exists()
        assert (tmp_path / "moved").read_text(encoding="utf-8") == "source"

        source.write_text("new source", encoding="utf-8")
        with pytest.raises(FileExistsError):
            atomic_module._rename_noreplace_at(
                "source", "moved", descriptor, descriptor
            )
    finally:
        os.close(descriptor)

    assert source.read_text(encoding="utf-8") == "new source"
    assert (tmp_path / "moved").read_text(encoding="utf-8") == "source"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "a\x00b"])
def test_rename_noreplace_at_rejects_non_child_names(
    tmp_path: Path,
    name: str,
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="one bounded file name"):
            atomic_module._rename_noreplace_at(name, "target", descriptor, descriptor)
    finally:
        os.close(descriptor)


def test_publish_staged_directory_restores_previous_tree_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_rename = atomic_module._rename_noreplace_at

    def fail_final(src: str, dst: str, src_dir_fd: int, dst_dir_fd: int) -> None:
        if src == stage.name and dst == destination.name:
            raise OSError("injected final rename failure")
        real_rename(src, dst, src_dir_fd, dst_dir_fd)

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", fail_final)

    with pytest.raises(OSError, match="injected final rename failure"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_publish_staged_directory_restores_missing_target_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_rename = atomic_module._rename_noreplace_at

    def fail_final(src: str, dst: str, src_dir_fd: int, dst_dir_fd: int) -> None:
        if src == stage.name and dst == destination.name:
            raise OSError("injected final rename failure")
        real_rename(src, dst, src_dir_fd, dst_dir_fd)

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", fail_final)

    with pytest.raises(OSError, match="injected final rename failure"):
        publish_staged_directory(stage, destination)

    assert not destination.exists()
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_publish_staged_directory_rejects_invalid_relationships(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(ValueError, match="must differ"):
        publish_staged_directory(stage, stage)

    destination_file = tmp_path / "published"
    destination_file.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        publish_staged_directory(stage, destination_file)


def test_publish_staged_directory_does_not_follow_destination_symlink(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    victim = tmp_path / "victim"
    _write_tree(victim, "keep.txt", "keep")
    destination = tmp_path / "published"
    destination.symlink_to(victim, target_is_directory=True)
    with pytest.raises(ValueError, match="link"):
        publish_staged_directory(stage, destination)

    assert destination.is_symlink()
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_publish_staged_directory_detects_destination_swap_at_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    stolen = tmp_path / "stolen-old"
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def replace_before_claim(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        if src == destination.name and ".published.previous-" in dst and not injected:
            injected = True
            destination.rename(stolen)
            _write_tree(destination, "foreign.txt", "preserve")
        real_rename(src, dst, src_dir_fd, dst_dir_fd)

    monkeypatch.setattr(
        atomic_module,
        "_rename_noreplace_at",
        replace_before_claim,
    )

    with pytest.raises(RuntimeError, match="changed at the publication boundary"):
        publish_staged_directory(stage, destination)

    assert (destination / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_raced_destination_before_final_rename_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def insert_foreign(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        if src == stage.name and dst == destination.name and not injected:
            injected = True
            _write_tree(destination, "foreign.txt", "preserve")
        real_rename(src, dst, src_dir_fd, dst_dir_fd)

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", insert_foreign)

    with pytest.raises(RuntimeError, match="previous output remains isolated"):
        publish_staged_directory(stage, destination)

    assert (destination / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    previous = list(tmp_path.glob(".published.previous-*"))
    assert len(previous) == 1
    assert (previous[0] / "old.txt").read_text(encoding="utf-8") == "old"


def test_publish_staged_directory_rejects_real_stage_swap_before_destination_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    stolen = tmp_path / "stolen-stage"
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def replace_stage(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        if src == stage.name and dst == destination.name and not injected:
            injected = True
            stage.rename(stolen)
            _write_tree(stage, "foreign.txt", "preserve")
        real_rename(src, dst, src_dir_fd, dst_dir_fd)

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", replace_stage)

    with pytest.raises(RuntimeError, match="suspect output was quarantined"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stolen / "new.txt").read_text(encoding="utf-8") == "new"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "foreign.txt").read_text(encoding="utf-8") == ("preserve")


def test_expected_stage_ownership_requires_the_complete_token(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    expected = capture_directory_ownership(stage)
    (stage / "new.txt").write_text("NEW", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed before publication"):
        publish_staged_directory(
            stage,
            destination,
            expected_stage_root_ownership=expected,
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "NEW"


def test_publish_uses_borrowed_parent_descriptor_without_closing_it(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    expected_parent = publication_parent_identity(descriptor)
    try:
        orphan = publish_staged_directory(
            stage,
            destination,
            parent_descriptor=descriptor,
            expected_parent_identity=expected_parent,
        )
        assert publication_parent_identity(descriptor) == expected_parent
    finally:
        os.close(descriptor)

    assert orphan is not None
    assert orphan.parent_identity == expected_parent
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_public_publish_wrapper_does_not_request_strict_commit_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    def forbidden_fsync(_descriptor: int) -> None:
        raise AssertionError("compatibility publication must not use strict fsync")

    monkeypatch.setattr(atomic_module.os, "fsync", forbidden_fsync)

    orphan = publish_staged_directory(stage, destination)

    assert orphan is not None
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_authority_publish_helper_rejects_noncallable_commit_before_mutation(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        with pytest.raises(TypeError, match="commit callback must be callable"):
            atomic_module._publish_staged_directory_with_authority(
                authority,
                stage,
                destination,
                commit_callback=object(),  # type: ignore[arg-type]
            )
        assert not authority._closed
    finally:
        authority.close()

    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".published.previous-*"))


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict commit durability currently requires Linux directory fsync",
)
def test_authority_publish_helper_borrows_authority_and_commits_exact_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    expected_previous = capture_directory_ownership(destination)
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    expected_stage = capture_directory_ownership(stage)
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    events: list[str] = []
    observed: list[tuple[object, object, DirectoryOrphan | None]] = []
    real_verify = authority._verify_callback
    real_fsync = atomic_module.os.fsync

    def verify_parent_binding() -> None:
        real_verify()
        events.append("verified")

    def fsync_parent(descriptor: int) -> None:
        assert descriptor == authority.resource
        real_fsync(descriptor)
        events.append("synced")

    def commit(
        sealed: object,
        published: object,
        previous: DirectoryOrphan | None,
        publication_token: object | None,
    ) -> None:
        assert publication_token is None
        assert events == ["verified", "synced"]
        events.append("committed")
        observed.append((sealed, published, previous))

    authority._verify_callback = verify_parent_binding
    monkeypatch.setattr(atomic_module.os, "fsync", fsync_parent)
    try:
        orphan = atomic_module._publish_staged_directory_with_authority(
            authority,
            stage,
            destination,
            expected_stage_root_ownership=expected_stage,
            commit_callback=commit,
        )
        assert not authority._closed
        authority.verify_path_binding()
    finally:
        authority.close()

    assert authority._closed
    assert events == ["verified", "synced", "committed", "verified"]
    assert len(observed) == 1
    assert observed[0][0] == expected_stage
    published = observed[0][1]
    assert isinstance(published, type(expected_stage))
    assert (
        replace(
            published,
            root_version_identity=expected_stage.root_version_identity,
        )
        == expected_stage
    )
    assert observed[0][2] is orphan
    assert orphan is not None
    assert orphan.ownership_digest == expected_previous.digest
    assert (orphan.path / "old.txt").read_text(encoding="utf-8") == "old"
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert published == capture_directory_ownership(destination)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict commit durability currently requires Linux directory fsync",
)
def test_authority_publish_does_not_poll_cancellation_after_rename(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    expected_stage = capture_directory_ownership(stage)
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    cancellation = KeyboardInterrupt("latched at directory rename")
    renamed = False
    polls = 0
    commits = 0
    real_rename = authority._rename_callback

    def rename(source: str, target: str) -> object | None:
        nonlocal renamed
        token = real_rename(source, target)
        renamed = True
        return token

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if renamed:
            raise cancellation

    def validate_published(reader: atomic_module.PublicationDirectoryReader) -> None:
        assert renamed
        assert reader.read_bytes("new.txt", max_bytes=3) == b"new"

    def commit(
        _staged: object,
        _published: object,
        _previous: DirectoryOrphan | None,
        _publication_token: object | None,
    ) -> None:
        nonlocal commits
        commits += 1

    authority._rename_callback = rename
    try:
        orphan = atomic_module._publish_staged_directory_with_authority(
            authority,
            stage,
            destination,
            expected_stage_root_ownership=expected_stage,
            expected_destination_ownership=None,
            validate_published_destination=validate_published,
            commit_callback=commit,
            check_cancelled=check_cancelled,
        )
        assert orphan is None
        assert polls > 0
        assert renamed
        assert commits == 1
        assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
        with pytest.raises(KeyboardInterrupt) as caught:
            check_cancelled()
        assert caught.value is cancellation
    finally:
        authority.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict commit durability currently requires Linux directory fsync",
)
def test_authority_publish_helper_does_not_commit_before_validation_finishes(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    commits: list[object] = []

    def fail_validation(_reader: object) -> None:
        raise RuntimeError("injected pre-commit validation failure")

    try:
        with pytest.raises(RuntimeError, match="suspect output was quarantined"):
            atomic_module._publish_staged_directory_with_authority(
                authority,
                stage,
                destination,
                validate_published_destination=fail_validation,
                commit_callback=lambda sealed, _published, _previous, _token: (
                    commits.append(sealed)
                ),
            )
        assert not authority._closed
    finally:
        authority.close()

    assert commits == []
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "new.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict commit durability currently requires Linux directory fsync",
)
def test_authority_publish_helper_rechecks_exact_tree_after_orphan_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    commits: list[object] = []
    real_orphan_metadata = atomic_module._orphan_metadata

    def mutate_after_orphan_receipt(*args: object, **kwargs: object) -> DirectoryOrphan:
        orphan = real_orphan_metadata(*args, **kwargs)
        (destination / "late.txt").write_text("late", encoding="utf-8")
        return orphan

    monkeypatch.setattr(
        atomic_module,
        "_orphan_metadata",
        mutate_after_orphan_receipt,
    )
    try:
        with pytest.raises(RuntimeError, match="published staged directory changed"):
            atomic_module._publish_staged_directory_with_authority(
                authority,
                stage,
                destination,
                commit_callback=lambda sealed, _published, _previous, _token: (
                    commits.append(sealed)
                ),
            )
        assert not authority._closed
    finally:
        authority.close()

    assert commits == []
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert (destination / "late.txt").read_text(encoding="utf-8") == "late"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict commit durability currently requires Linux directory fsync",
)
def test_authority_publish_helper_fsync_failure_prevents_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    commits: list[object] = []
    error = OSError(errno.EIO, "injected parent fsync failure")

    def fail_parent_fsync(descriptor: int) -> None:
        assert descriptor == authority.resource
        raise error

    monkeypatch.setattr(atomic_module.os, "fsync", fail_parent_fsync)
    try:
        with pytest.raises(OSError) as caught:
            atomic_module._publish_staged_directory_with_authority(
                authority,
                stage,
                destination,
                commit_callback=lambda sealed, _published, _previous, _token: (
                    commits.append(sealed)
                ),
            )
        assert caught.value is error
        assert not authority._closed
    finally:
        authority.close()

    assert commits == []
    assert not stage.exists()
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    previous = list(tmp_path.glob(".published.previous-*"))
    assert len(previous) == 1
    assert (previous[0] / "old.txt").read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize("backend_tag", ["windows-file-id", "darwin-renameatx-np"])
def test_authority_publish_helper_rejects_unsupported_durable_commit(
    tmp_path: Path,
    backend_tag: str,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    authority.backend_tag = backend_tag
    commits: list[object] = []
    try:
        with pytest.raises(RuntimeError, match="supported Linux"):
            atomic_module._publish_staged_directory_with_authority(
                authority,
                stage,
                destination,
                commit_callback=lambda sealed, _published, _previous, _token: (
                    commits.append(sealed)
                ),
            )
        assert not authority._closed
    finally:
        authority.close()

    assert commits == []
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".published.previous-*"))


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="strict commit durability currently requires Linux directory fsync",
)
def test_authority_publish_helper_never_rolls_back_commit_interruptions(
    tmp_path: Path,
    error_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    expected_stage = capture_directory_ownership(stage)
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    observed: list[tuple[object, object, DirectoryOrphan | None]] = []
    synced = False
    real_fsync = atomic_module.os.fsync

    def fsync_parent(descriptor: int) -> None:
        nonlocal synced
        assert descriptor == authority.resource
        real_fsync(descriptor)
        synced = True

    def interrupt_commit(
        sealed: object,
        published: object,
        previous: DirectoryOrphan | None,
        publication_token: object | None,
    ) -> None:
        assert publication_token is None
        assert synced
        observed.append((sealed, published, previous))
        raise error_type("injected commit interruption")

    monkeypatch.setattr(atomic_module.os, "fsync", fsync_parent)
    try:
        with pytest.raises(error_type, match="injected commit interruption"):
            atomic_module._publish_staged_directory_with_authority(
                authority,
                stage,
                destination,
                commit_callback=interrupt_commit,
            )
        assert not authority._closed
    finally:
        authority.close()

    assert len(observed) == 1
    assert observed[0][0] == expected_stage
    published = observed[0][1]
    assert isinstance(published, type(expected_stage))
    assert (
        replace(
            published,
            root_version_identity=expected_stage.root_version_identity,
        )
        == expected_stage
    )
    previous = observed[0][2]
    assert previous is not None
    assert (previous.path / "old.txt").read_text(encoding="utf-8") == "old"
    assert not stage.exists()
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_authority_publish_helper_rejects_paths_outside_authority_parent(
    tmp_path: Path,
) -> None:
    foreign_parent = tmp_path / "foreign"
    foreign_parent.mkdir()
    destination = foreign_parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = foreign_parent / "stage"
    _write_tree(stage, "new.txt", "new")
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        with pytest.raises(ValueError, match="match the authority parent"):
            atomic_module._publish_staged_directory_with_authority(
                authority,
                stage,
                destination,
            )
        assert not authority._closed
    finally:
        authority.close()

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_rejects_wrong_parent_authority_before_mutation(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="does not match authority"):
            publish_staged_directory(
                stage,
                destination,
                parent_descriptor=descriptor,
                expected_parent_identity=(0,),
            )
    finally:
        os.close(descriptor)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publication_paths_never_call_path_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    def forbidden_resolve(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("publication authority must not resolve lexical paths")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)

    publish_staged_directory(stage, destination)

    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_publication_rejects_preexisting_parent_symlink(
    tmp_path: Path,
) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    destination = foreign / "published"
    _write_tree(destination, "valuable.txt", "preserve")
    stage = foreign / "stage"
    _write_tree(stage, "new.txt", "new")
    alias = tmp_path / "alias"
    alias.symlink_to(foreign, target_is_directory=True)

    with pytest.raises(ValueError, match="real directory"):
        publish_staged_directory(alias / "stage", alias / "published")

    assert (destination / "valuable.txt").read_text(encoding="utf-8") == "preserve"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publication_rejects_reversible_intermediate_parent_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted = tmp_path / "trusted"
    intermediate = trusted / "intermediate"
    parent = intermediate / "parent"
    parent.mkdir(parents=True)
    destination = parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = parent / "stage"
    _write_tree(stage, "new.txt", "new")

    foreign = tmp_path / "foreign-intermediate"
    foreign_parent = foreign / "parent"
    foreign_parent.mkdir(parents=True)
    _write_tree(foreign_parent / "published", "valuable.txt", "preserve")
    saved = tmp_path / "saved-intermediate"
    real_open = atomic_module.os.open
    swapped = False

    def reversible_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "intermediate" and dir_fd is not None and not swapped:
            swapped = True
            intermediate.rename(saved)
            foreign.rename(intermediate)
            try:
                return real_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                intermediate.rename(foreign)
                saved.rename(intermediate)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(atomic_module.os, "open", reversible_open)

    with pytest.raises(RuntimeError, match="changed while it was opened"):
        publish_staged_directory(stage, destination)

    assert swapped
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert (foreign_parent / "published" / "valuable.txt").read_text(
        encoding="utf-8"
    ) == "preserve"


def test_parent_path_replacement_cannot_redirect_pinned_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "active-parent"
    parent.mkdir()
    destination = parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = parent / "stage"
    _write_tree(stage, "new.txt", "new")
    moved_parent = tmp_path / "moved-parent"
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    expected_parent = publication_parent_identity(descriptor)
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def replace_parent(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        if src == stage.name and dst == destination.name and not injected:
            injected = True
            parent.rename(moved_parent)
            parent.mkdir()
            foreign = parent / destination.name
            _write_tree(foreign, "foreign.txt", "preserve")
        real_rename(src, dst, src_dir_fd, dst_dir_fd)

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", replace_parent)
    try:
        with pytest.raises(RuntimeError, match="parent path changed"):
            publish_staged_directory(
                stage,
                destination,
                parent_descriptor=descriptor,
                expected_parent_identity=expected_parent,
            )
    finally:
        os.close(descriptor)

    assert (parent / "published" / "foreign.txt").read_text(
        encoding="utf-8"
    ) == "preserve"
    assert (moved_parent / "published" / "new.txt").read_text(encoding="utf-8") == "new"


def test_discard_owned_directory_isolates_instead_of_deleting(tmp_path: Path) -> None:
    root = tmp_path / "owned"
    _write_tree(root, "payload.txt", "payload")
    ownership = capture_directory_ownership(root)

    orphan = atomic_module.discard_owned_directory(root, ownership)

    assert isinstance(orphan, DirectoryOrphan)
    assert orphan.verified_at_isolation
    assert not root.exists()
    assert (orphan.path / "payload.txt").read_text(encoding="utf-8") == "payload"


def test_discard_restores_foreign_replacement_claimed_at_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "owned"
    _write_tree(root, "payload.txt", "payload")
    ownership = capture_directory_ownership(root)
    stolen = tmp_path / "stolen-owned"
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def replace_before_claim(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        if src == root.name and ".owned.discarded-" in dst and not injected:
            injected = True
            root.rename(stolen)
            _write_tree(root, "foreign.txt", "preserve")
        real_rename(src, dst, src_dir_fd, dst_dir_fd)

    monkeypatch.setattr(
        atomic_module,
        "_rename_noreplace_at",
        replace_before_claim,
    )

    orphan = atomic_module.discard_owned_directory(root, ownership)

    assert orphan is None
    assert (root / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert (stolen / "payload.txt").read_text(encoding="utf-8") == "payload"


def test_publish_and_discard_never_recursively_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("online cleanup must not delete entries")

    monkeypatch.setattr(atomic_module.os, "unlink", forbidden)
    monkeypatch.setattr(atomic_module.os, "rmdir", forbidden)
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    previous = publish_staged_directory(stage, destination)
    assert previous is not None
    published = capture_directory_ownership(destination)
    discarded = atomic_module.discard_owned_directory(destination, published)

    assert discarded is not None
    assert (previous.path / "old.txt").read_text(encoding="utf-8") == "old"
    assert (discarded.path / "new.txt").read_text(encoding="utf-8") == "new"


def test_quiescent_reclaimer_reclaims_exact_child_and_reports_absence(
    tmp_path: Path,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    nested = attempt / "nested"
    nested.mkdir(parents=True)
    (attempt / "root.txt").write_text("root", encoding="utf-8")
    (nested / "payload.txt").write_text("payload", encoding="utf-8")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )

    assert reclaimer.reclaim_child(attempt.name) is True
    assert reclaimer.reclaim_child(attempt.name) is False
    assert reclaimer.retry() is False
    assert not attempt.exists()

    reclaimer.close()
    assert reclaimer.closed
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_snapshots_sorted_descriptor_child_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    names = ("zeta", ".alpha", "middle", "café")
    for name in names:
        (parent / name).mkdir()
    real_scandir = atomic_module.os.scandir
    scanned: list[object] = []

    def capture_scandir(descriptor: object) -> object:
        scanned.append(descriptor)
        return real_scandir(descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(atomic_module.os, "scandir", capture_scandir)
        with atomic_module.QuiescentDirectoryReclaimer(
            parent,
            expected_parent_identity=parent_identity,
        ) as reclaimer:
            assert reclaimer.snapshot_child_names() == tuple(
                sorted(names, key=os.fsencode)
            )
            assert reclaimer.retry() is False

    assert len(scanned) == 1
    assert type(scanned[0]) is int
    assert {path.name for path in parent.iterdir()} == set(names)


def test_quiescent_reclaimer_child_snapshot_stops_at_incremental_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)

    class PoisonEntry:
        @property
        def name(self) -> str:
            raise AssertionError("snapshot inspected the limit-plus-one entry")

    entries = (
        SimpleNamespace(name="one"),
        SimpleNamespace(name="two"),
        PoisonEntry(),
    )

    class BoundedScandir:
        def __init__(self) -> None:
            self.index = 0
            self.closed = False

        def __iter__(self) -> BoundedScandir:
            return self

        def __next__(self) -> object:
            if self.index >= len(entries):
                raise AssertionError("snapshot read beyond limit plus one")
            entry = entries[self.index]
            self.index += 1
            return entry

        def close(self) -> None:
            self.closed = True

        def __enter__(self) -> BoundedScandir:
            return self

        def __exit__(self, *_exc: object) -> None:
            self.close()

    iterator = BoundedScandir()
    scanned: list[object] = []

    def bounded_scandir(descriptor: object) -> BoundedScandir:
        scanned.append(descriptor)
        return iterator

    monkeypatch.setattr(atomic_module, "_MAX_QUIESCENT_DIRECTORY_CHILDREN", 2)
    monkeypatch.setattr(atomic_module.os, "scandir", bounded_scandir)

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(RuntimeError, match="snapshot exceeds its limit"):
            reclaimer.snapshot_child_names()
        assert reclaimer.retry() is False

    assert len(scanned) == 1
    assert type(scanned[0]) is int
    assert iterator.index == 3
    assert iterator.closed


def test_quiescent_reclaimer_child_snapshot_closes_iterator_on_scan_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    real_scandir = atomic_module.os.scandir
    failure = OSError(errno.EIO, "injected snapshot scan failure")

    class FailingScandir:
        def __init__(self, descriptor: int) -> None:
            self._entries = real_scandir(descriptor)
            self.closed = False

        def __iter__(self) -> FailingScandir:
            return self

        def __next__(self) -> object:
            raise failure

        def close(self) -> None:
            self._entries.close()
            self.closed = True

        def __enter__(self) -> FailingScandir:
            return self

        def __exit__(self, *_exc: object) -> None:
            self.close()

    iterators: list[FailingScandir] = []

    def failing_scandir(descriptor: int) -> FailingScandir:
        assert type(descriptor) is int
        iterator = FailingScandir(descriptor)
        iterators.append(iterator)
        return iterator

    before_fds = (
        len(os.listdir("/proc/self/fd")) if sys.platform.startswith("linux") else 0
    )
    monkeypatch.setattr(atomic_module.os, "scandir", failing_scandir)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )

    with pytest.raises(OSError) as caught:
        reclaimer.snapshot_child_names()

    assert caught.value is failure
    assert len(iterators) == 1
    assert iterators[0].closed
    assert reclaimer.retry() is False
    reclaimer.close()
    if sys.platform.startswith("linux"):
        assert len(os.listdir("/proc/self/fd")) <= before_fds


def test_quiescent_reclaimer_child_snapshot_retries_interrupted_iterator_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    (parent / "attempt").mkdir()
    real_scandir = atomic_module.os.scandir
    interruption = KeyboardInterrupt("injected snapshot iterator close interruption")

    class InterruptingScandir:
        def __init__(self, descriptor: int) -> None:
            self._entries = real_scandir(descriptor)
            self.close_calls = 0
            self.closed = False

        def __iter__(self) -> InterruptingScandir:
            return self

        def __next__(self) -> object:
            return next(self._entries)

        def close(self) -> None:
            self.close_calls += 1
            self._entries.close()
            if self.close_calls == 1:
                raise interruption
            self.closed = True

        def __enter__(self) -> InterruptingScandir:
            return self

        def __exit__(self, *_exc: object) -> None:
            self.close()

    iterators: list[InterruptingScandir] = []

    def interrupting_scandir(descriptor: int) -> InterruptingScandir:
        assert type(descriptor) is int
        iterator = InterruptingScandir(descriptor)
        iterators.append(iterator)
        return iterator

    before_fds = (
        len(os.listdir("/proc/self/fd")) if sys.platform.startswith("linux") else 0
    )
    monkeypatch.setattr(atomic_module.os, "scandir", interrupting_scandir)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        reclaimer.snapshot_child_names()

    assert caught.value is interruption
    assert len(iterators) == 1
    assert iterators[0].close_calls >= 2
    assert iterators[0].closed
    assert reclaimer.retry() is False
    reclaimer.close()
    if sys.platform.startswith("linux"):
        assert len(os.listdir("/proc/self/fd")) <= before_fds


def test_quiescent_reclaimer_retains_iterator_after_persistent_close_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    (parent / "attempt").mkdir()
    before_fds = (
        len(os.listdir("/proc/self/fd")) if sys.platform.startswith("linux") else 0
    )
    real_scope = atomic_module._run_quiescent_directory_resource_scope
    real_close = atomic_module._ScandirIteratorCleanupOwner.close
    iterator_owners: list[atomic_module._ScandirIteratorCleanupOwner] = []
    close_failures: list[KeyboardInterrupt] = []
    fail_close = True

    def capture_scope(
        resources: atomic_module._QuiescentDirectoryResourceOwner,
        callback: object,
        *,
        label: str,
    ) -> object:
        if label == "quiescent directory snapshot iterator cleanup also failed":
            assert isinstance(
                resources,
                atomic_module._ScandirIteratorCleanupOwner,
            )
            iterator_owners.append(resources)
        return real_scope(resources, callback, label=label)

    def interrupt_before_close(
        owner: atomic_module._ScandirIteratorCleanupOwner,
    ) -> None:
        if iterator_owners and owner is iterator_owners[0] and fail_close:
            failure = KeyboardInterrupt(
                f"persistent snapshot iterator close {len(close_failures)}"
            )
            close_failures.append(failure)
            raise failure
        real_close(owner)

    monkeypatch.setattr(
        atomic_module,
        "_run_quiescent_directory_resource_scope",
        capture_scope,
    )
    monkeypatch.setattr(
        atomic_module._ScandirIteratorCleanupOwner,
        "close",
        interrupt_before_close,
    )
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        reclaimer.snapshot_child_names()

    assert len(iterator_owners) == 1
    iterator_owner = iterator_owners[0]
    assert iterator_owner._iterator is not None
    assert not iterator_owner.closed
    assert len(close_failures) > atomic_module._MAX_ORDERED_ACTION_CANCELLATION_RETRIES
    top_owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert type(top_owners) is tuple
    assert sum(owner is iterator_owner for owner in top_owners) == 1

    fail_close = False
    for owner in top_owners:
        if not owner.closed:
            owner.close()

    assert iterator_owner.closed
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()
    if sys.platform.startswith("linux"):
        assert len(os.listdir("/proc/self/fd")) <= before_fds


def test_quiescent_reclaimer_child_snapshot_rejects_recursive_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    (parent / "attempt").mkdir()
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_scandir = atomic_module.os.scandir
    nested_errors: list[BaseException] = []

    def reenter(descriptor: int) -> object:
        try:
            reclaimer.snapshot_child_names()
        except BaseException as error:  # noqa: B036 - inspect nested rejection
            nested_errors.append(error)
        return real_scandir(descriptor)

    monkeypatch.setattr(atomic_module.os, "scandir", reenter)

    assert reclaimer.snapshot_child_names() == ("attempt",)
    assert len(nested_errors) == 1
    assert isinstance(nested_errors[0], RuntimeError)
    assert "lifecycle transition is already active" in str(nested_errors[0])
    reclaimer.close()


def test_quiescent_reclaimer_child_snapshot_serializes_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    (parent / "attempt").mkdir()
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    entered = threading.Event()
    release = threading.Event()
    results: list[tuple[str, ...]] = []
    failures: list[BaseException] = []
    real_scandir = atomic_module.os.scandir

    def blocked_scandir(descriptor: int) -> object:
        entered.set()
        assert release.wait(timeout=5)
        return real_scandir(descriptor)

    def snapshot_in_worker() -> None:
        try:
            results.append(reclaimer.snapshot_child_names())
        except BaseException as error:  # noqa: B036 - report thread failure
            failures.append(error)

    monkeypatch.setattr(atomic_module.os, "scandir", blocked_scandir)
    worker = threading.Thread(target=snapshot_in_worker)
    worker.start()
    assert entered.wait(timeout=5)
    with pytest.raises(RuntimeError, match="lifecycle transition is already active"):
        reclaimer.snapshot_child_names()
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert results == [("attempt",)]
    reclaimer.close()


def test_quiescent_reclaimer_reclaims_quarantined_child_without_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    quarantined = parent / ".attempt-a.discarded-0123456789abcdef"
    _write_tree(quarantined, "payload.txt", "payload")
    rename_calls = 0

    def forbidden_rename(*_args: object, **_kwargs: object) -> None:
        nonlocal rename_calls
        rename_calls += 1
        raise AssertionError("an already quarantined child must not be renamed")

    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "rename_noreplace",
        forbidden_rename,
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        assert reclaimer.reclaim_quarantined_child(quarantined.name) is True
        assert reclaimer.reclaim_quarantined_child(quarantined.name) is False

    assert rename_calls == 0
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_retries_quarantined_child_without_name_nesting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    quarantined = parent / ".attempt-a.discarded-0123456789abcdef"
    _write_tree(quarantined, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_unlink = atomic_module.os.unlink
    interrupted = False

    def interrupt_before_unlink(path: object, *, dir_fd: int | None = None) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("quarantined unlink interruption")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(atomic_module.os, "unlink", interrupt_before_unlink)

    with pytest.raises(KeyboardInterrupt, match="quarantined unlink") as caught:
        reclaimer.reclaim_quarantined_child(quarantined.name)
    assert reclaimer in caught.value.publication_cleanup_owners
    assert [path.name for path in parent.iterdir()] == [quarantined.name]
    assert not list(parent.glob(f".{quarantined.name}.discarded-*"))

    assert reclaimer.retry() is True
    reclaimer.close()
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_bounds_parent_before_quarantined_child_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    quarantined = parent / ".attempt-a.discarded-0123456789abcdef"
    _write_tree(quarantined, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )

    class ParentPathTooLong(RuntimeError):
        pass

    def reject_parent_path(_value: object) -> bytes:
        raise ParentPathTooLong("parent receipt path is too long")

    def forbidden_removal(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("parent receipt validation must precede deletion")

    monkeypatch.setattr(
        atomic_module,
        "_bounded_directory_orphan_parent_path_bytes",
        reject_parent_path,
    )
    monkeypatch.setattr(atomic_module.os, "unlink", forbidden_removal)
    monkeypatch.setattr(atomic_module.os, "rmdir", forbidden_removal)

    with pytest.raises(ParentPathTooLong):
        reclaimer.reclaim_quarantined_child(quarantined.name)

    assert (quarantined / "payload.txt").read_text(encoding="utf-8") == "payload"
    assert reclaimer.retry() is False
    reclaimer.close()


def test_quiescent_reclaimer_rejects_non_directory_quarantined_child(
    tmp_path: Path,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    ordinary_file = parent / ".attempt.discarded-file"
    ordinary_file.write_text("preserve", encoding="utf-8")

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(ValueError, match="not a directory or is a link"):
            reclaimer.reclaim_quarantined_child(ordinary_file.name)
        assert reclaimer.reclaim_quarantined_child("missing") is False

    assert ordinary_file.read_text(encoding="utf-8") == "preserve"


def test_quiescent_reclaimer_reclaims_authenticated_orphan_idempotently(
    tmp_path: Path,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        assert reclaimer.reclaim_orphan(orphan) is True
        assert reclaimer.reclaim_orphan(orphan) is True

    assert not orphan.path.exists()
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_rejects_tampered_absent_receipt_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    tampered = replace(
        orphan,
        locator=replace(
            orphan.locator,
            child_name="definitely-absent",
        ),
    )
    child_lookups = 0
    sync_calls = 0
    real_child_metadata = atomic_module.QuiescentDirectoryReclaimer._child_metadata
    real_sync = atomic_module._PublicationAuthority.sync_parent

    def count_child_metadata(authority: object, path: Path) -> object | None:
        nonlocal child_lookups
        child_lookups += 1
        return real_child_metadata(authority, path)

    def count_sync(authority: object) -> None:
        nonlocal sync_calls
        sync_calls += 1
        real_sync(authority)

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(count_child_metadata),
    )
    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "sync_parent",
        count_sync,
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(RuntimeError, match="receipt binding changed"):
            reclaimer.reclaim_orphan(tampered)

        assert child_lookups == 0
        assert sync_calls == 0
        assert (orphan.path / "payload.txt").read_text(encoding="utf-8") == "payload"
        assert reclaimer.reclaim_orphan(orphan) is True

    assert child_lookups > 0
    assert sync_calls > 0
    assert not orphan.path.exists()


def test_quiescent_reclaimer_receipt_binds_complete_ownership_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    first = parent / "first"
    second = parent / "second"
    _write_tree(first, "payload.txt", "identical")
    _write_tree(second, "payload.txt", "identical")
    first_orphan = atomic_module.discard_owned_directory(
        first,
        capture_directory_ownership(first),
    )
    second_orphan = atomic_module.discard_owned_directory(
        second,
        capture_directory_ownership(second),
    )
    assert first_orphan is not None
    assert second_orphan is not None
    first_token = first_orphan.locator.ownership
    second_token = second_orphan.locator.ownership
    assert first_token.digest == second_token.digest
    assert first_token.entries == second_token.entries
    assert first_token.byte_count == second_token.byte_count
    assert first_token.root_version_identity != second_token.root_version_identity
    tampered = replace(
        first_orphan,
        locator=replace(
            first_orphan.locator,
            ownership=second_token,
        ),
    )
    child_lookups = 0
    real_child_metadata = atomic_module.QuiescentDirectoryReclaimer._child_metadata

    def count_child_metadata(authority: object, path: Path) -> object | None:
        nonlocal child_lookups
        child_lookups += 1
        return real_child_metadata(authority, path)

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(count_child_metadata),
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(RuntimeError, match="receipt binding changed"):
            reclaimer.reclaim_orphan(tampered)

        assert child_lookups == 0
        assert (first_orphan.path / "payload.txt").read_text(
            encoding="utf-8"
        ) == "identical"
        assert (second_orphan.path / "payload.txt").read_text(
            encoding="utf-8"
        ) == "identical"
        assert reclaimer.reclaim_orphan(first_orphan) is True
        assert reclaimer.reclaim_orphan(second_orphan) is True

    assert child_lookups > 0
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_rejects_unbounded_receipt_identity_before_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    ownership = orphan.locator.ownership
    root_version = list(ownership.root_version_identity)
    root_version[5] = 1 << 100_000
    tampered = replace(
        orphan,
        locator=replace(
            orphan.locator,
            ownership=replace(
                ownership,
                root_version_identity=tuple(root_version),
            ),
        ),
    )

    def forbidden_digest(**_kwargs: object) -> str:
        raise AssertionError("unbounded identity must not reach receipt hashing")

    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unbounded identity must not reach child lookup")

    monkeypatch.setattr(
        atomic_module,
        "_directory_orphan_receipt_digest",
        forbidden_digest,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(forbidden_lookup),
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(RuntimeError, match="invalid identity"):
            reclaimer.reclaim_orphan(tampered)

    assert (orphan.path / "payload.txt").read_text(encoding="utf-8") == "payload"


@pytest.mark.parametrize(
    "identity_field",
    ("root_identity", "root_version_identity", "entry_identity"),
)
def test_quiescent_reclaimer_bounds_identity_arity_before_element_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_field: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    ownership = orphan.locator.ownership
    marker = int("123456789012345678901234567890123456789")
    oversized = (marker,) * 10_000
    if identity_field == "entry_identity":
        entry = ownership.entry_identities[0]
        tampered_ownership = replace(
            ownership,
            entry_identities=((entry[0], entry[1], oversized),),
        )
    else:
        tampered_ownership = replace(
            ownership,
            **{identity_field: oversized},
        )
    tampered = replace(
        orphan,
        locator=replace(orphan.locator, ownership=tampered_ownership),
    )
    marker_type_calls = 0
    real_type = type

    def bounded_type(value: object) -> type[object]:
        nonlocal marker_type_calls
        if value is marker:
            marker_type_calls += 1
            if marker_type_calls > 256:
                raise AssertionError("identity arity must precede element traversal")
        return real_type(value)

    def forbidden_digest(**_kwargs: object) -> str:
        raise AssertionError("invalid identity must not reach receipt hashing")

    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid identity must not reach child lookup")

    monkeypatch.setattr(atomic_module, "type", bounded_type, raising=False)
    monkeypatch.setattr(
        atomic_module,
        "_directory_orphan_receipt_digest",
        forbidden_digest,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(forbidden_lookup),
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(TypeError, match="not exact"):
            reclaimer.reclaim_orphan(tampered)

    assert marker_type_calls == 0
    assert (orphan.path / "payload.txt").read_text(encoding="utf-8") == "payload"


@pytest.mark.parametrize(
    "path_field",
    ("inventory", "entry_identity", "file_record", "child_name"),
)
def test_quiescent_reclaimer_bounds_receipt_paths_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_field: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    ownership = orphan.locator.ownership
    oversized = "x" * (atomic_module._MAX_OWNERSHIP_PATH_BYTES + 1)
    tampered_ownership = ownership
    tampered_locator = orphan.locator
    if path_field == "inventory":
        inventory = ownership.inventory[0]
        tampered_ownership = replace(
            ownership,
            inventory=((oversized, inventory[1]),),
        )
    elif path_field == "entry_identity":
        entry = ownership.entry_identities[0]
        tampered_ownership = replace(
            ownership,
            entry_identities=((oversized, entry[1], entry[2]),),
        )
    elif path_field == "file_record":
        record = ownership.file_records[0]
        tampered_ownership = replace(
            ownership,
            file_records=(replace(record, path=oversized),),
        )
    else:
        oversized = "x" * (atomic_module._MAX_OWNERSHIP_COMPONENT_BYTES + 1)
        tampered_locator = replace(tampered_locator, child_name=oversized)
    if tampered_ownership is not ownership:
        tampered_locator = replace(
            tampered_locator,
            ownership=tampered_ownership,
        )
    tampered = replace(orphan, locator=tampered_locator)
    real_pure_path = atomic_module.PurePosixPath
    real_fsencode = atomic_module.os.fsencode

    def bounded_pure_path(value: object) -> PurePosixPath:
        if value is oversized:
            raise AssertionError("oversized receipt path must not be parsed")
        return real_pure_path(value)

    def bounded_fsencode(value: object) -> bytes:
        if value is oversized:
            raise AssertionError("oversized receipt path must not be encoded")
        return real_fsencode(value)

    def forbidden_digest(**_kwargs: object) -> str:
        raise AssertionError("invalid receipt path must not reach hashing")

    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid receipt path must not reach child lookup")

    monkeypatch.setattr(atomic_module, "PurePosixPath", bounded_pure_path)
    monkeypatch.setattr(atomic_module.os, "fsencode", bounded_fsencode)
    monkeypatch.setattr(
        atomic_module,
        "_directory_orphan_receipt_digest",
        forbidden_digest,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(forbidden_lookup),
    )

    expected_error = ValueError if path_field == "child_name" else RuntimeError
    expected_message = (
        "bounded file name" if path_field == "child_name" else "invalid path"
    )
    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(expected_error, match=expected_message):
            reclaimer.reclaim_orphan(tampered)

    assert (orphan.path / "payload.txt").read_text(encoding="utf-8") == "payload"


def test_quiescent_reclaimer_bounds_inventory_kind_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    ownership = orphan.locator.ownership
    inventory = ownership.inventory[0]
    oversized_kind = "directory" * 10_000
    tampered = replace(
        orphan,
        locator=replace(
            orphan.locator,
            ownership=replace(
                ownership,
                inventory=((inventory[0], oversized_kind),),
            ),
        ),
    )

    def forbidden_digest(**_kwargs: object) -> str:
        raise AssertionError("invalid entry kind must not reach hashing")

    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid entry kind must not reach child lookup")

    monkeypatch.setattr(
        atomic_module,
        "_directory_orphan_receipt_digest",
        forbidden_digest,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(forbidden_lookup),
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(RuntimeError, match="invalid entry kind"):
            reclaimer.reclaim_orphan(tampered)

    assert (orphan.path / "payload.txt").read_text(encoding="utf-8") == "payload"


@pytest.mark.parametrize("accounting_field", ("metadata_bytes", "byte_count"))
def test_quiescent_reclaimer_rejects_incremental_accounting_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accounting_field: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "a.txt", "a")
    (owned / "b.txt").write_text("bb", encoding="utf-8")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    ownership = orphan.locator.ownership
    assert tuple(path for path, _kind in ownership.inventory) == (
        "a.txt",
        "b.txt",
    )
    assert tuple(record.path for record in ownership.file_records) == (
        "a.txt",
        "b.txt",
    )
    tampered = replace(
        orphan,
        locator=replace(
            orphan.locator,
            ownership=replace(
                ownership,
                **{accounting_field: 0},
            ),
        ),
    )

    if accounting_field == "metadata_bytes":
        real_path_bytes = atomic_module._ownership_token_path_bytes

        def reject_second_path(value: object) -> bytes:
            if value == "b.txt":
                raise AssertionError(
                    "metadata overflow must stop before the next inventory item"
                )
            return real_path_bytes(value)

        monkeypatch.setattr(
            atomic_module,
            "_ownership_token_path_bytes",
            reject_second_path,
        )
    else:
        second_record = ownership.file_records[1]
        sha256_slot = vars(atomic_module.TreeFileRecord)["sha256"]

        class RejectSecondRecord:
            def __get__(self, instance: object, owner: type[object]) -> object:
                if instance is None:
                    return sha256_slot
                if instance is second_record:
                    raise AssertionError(
                        "byte overflow must stop before the next file record"
                    )
                return sha256_slot.__get__(instance, owner)

            def __set__(self, instance: object, value: object) -> None:
                sha256_slot.__set__(instance, value)

        monkeypatch.setattr(
            atomic_module.TreeFileRecord,
            "sha256",
            RejectSecondRecord(),
        )

    def forbidden_digest(**_kwargs: object) -> str:
        raise AssertionError("invalid accounting must not reach receipt hashing")

    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid accounting must not reach child lookup")

    monkeypatch.setattr(
        atomic_module,
        "_directory_orphan_receipt_digest",
        forbidden_digest,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(forbidden_lookup),
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(RuntimeError, match="accounting is inconsistent"):
            reclaimer.reclaim_orphan(tampered)

    assert (orphan.path / "a.txt").read_text(encoding="utf-8") == "a"
    assert (orphan.path / "b.txt").read_text(encoding="utf-8") == "bb"


def test_quiescent_reclaimer_uses_bounded_parent_bytes_from_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    oversized = b"x" * (atomic_module._MAX_ORPHAN_PARENT_PATH_BYTES + 1)
    tampered = replace(
        orphan,
        locator=replace(orphan.locator, parent_path_bytes=oversized),
    )
    real_fsdecode = atomic_module.os.fsdecode

    def bounded_fsdecode(value: object) -> str:
        if value is oversized:
            raise AssertionError("oversized parent bytes must not be decoded")
        return real_fsdecode(value)

    def forbidden_digest(**_kwargs: object) -> str:
        raise AssertionError("invalid parent bytes must not reach hashing")

    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid parent bytes must not reach child lookup")

    monkeypatch.setattr(atomic_module.os, "fsdecode", bounded_fsdecode)
    monkeypatch.setattr(
        atomic_module,
        "_directory_orphan_receipt_digest",
        forbidden_digest,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(forbidden_lookup),
    )

    with pytest.raises(RuntimeError, match="parent path is invalid"):
        _ = tampered.path
    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(RuntimeError, match="parent path is invalid"):
            reclaimer.reclaim_orphan(tampered)

    assert (orphan.path / "payload.txt").read_text(encoding="utf-8") == "payload"


@pytest.mark.parametrize("operation", ("publish", "discard", "retained-owner"))
def test_orphan_parent_bound_precedes_every_isolation_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    stage = parent / "stage"
    destination = parent / "destination"
    owned = parent / "owned"
    _write_tree(stage, "payload.txt", "new")
    _write_tree(destination, "payload.txt", "old")
    _write_tree(owned, "payload.txt", "owned")
    ownership = capture_directory_ownership(owned)
    authority_owner: atomic_module._PublicationAuthorityOwner | None = None
    authority: atomic_module._PublicationAuthority | None = None
    if operation == "retained-owner":
        authority_owner = atomic_module._PublicationAuthorityOwner()
        authority = atomic_module._open_publication_authority(
            parent,
            parent_resource=None,
            expected_parent_identity=None,
            authority_owner=authority_owner,
        )

    class ParentPathTooLong(RuntimeError):
        pass

    def reject_parent_path(_value: object) -> bytes:
        raise ParentPathTooLong("parent receipt path is too long")

    def forbidden_rename(
        _authority: object,
        _source: str,
        _destination: str,
    ) -> object:
        raise AssertionError("receipt parent validation must precede every rename")

    monkeypatch.setattr(
        atomic_module,
        "_bounded_directory_orphan_parent_path_bytes",
        reject_parent_path,
    )
    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "rename_noreplace",
        forbidden_rename,
    )

    try:
        if operation == "publish":
            with pytest.raises(ParentPathTooLong):
                publish_staged_directory(stage, destination)
        elif operation == "discard":
            assert atomic_module.discard_owned_directory(owned, ownership) is None
        else:
            assert authority is not None
            with pytest.raises(ParentPathTooLong):
                atomic_module._OwnedDirectoryIsolationOwner(
                    authority,
                    owned,
                    ownership,
                )
    finally:
        if authority_owner is not None:
            authority_owner.close()

    assert (stage / "payload.txt").read_text(encoding="utf-8") == "new"
    assert (destination / "payload.txt").read_text(encoding="utf-8") == "old"
    assert (owned / "payload.txt").read_text(encoding="utf-8") == "owned"
    assert {path.name for path in parent.iterdir()} == {
        "stage",
        "destination",
        "owned",
    }


def test_quiescent_reclaimer_rejects_public_unverified_restored_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    ownership = capture_directory_ownership(owned)
    real_require = atomic_module._require_tree_ownership_at
    real_rename = atomic_module._PublicationAuthority.rename_noreplace
    restored = False

    def reject_moved_validation(
        authority: object,
        name: str,
        *,
        path: Path,
        expected: object,
        label: str,
        allow_root_rename: bool = False,
    ) -> None:
        if label == "moved destination":
            raise RuntimeError("injected moved validation failure")
        real_require(
            authority,
            name,
            path=path,
            expected=expected,
            label=label,
            allow_root_rename=allow_root_rename,
        )

    def restore_then_lose_response(
        authority: object,
        source: str,
        destination: str,
    ) -> object | None:
        nonlocal restored
        result = real_rename(authority, source, destination)
        if destination == owned.name and source != owned.name and not restored:
            restored = True
            raise OSError(errno.EIO, "restore response lost")
        return result

    monkeypatch.setattr(
        atomic_module,
        "_require_tree_ownership_at",
        reject_moved_validation,
    )
    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "rename_noreplace",
        restore_then_lose_response,
    )

    orphan = atomic_module.discard_owned_directory(owned, ownership)

    assert orphan is not None
    assert orphan.verified_at_isolation is False
    assert restored
    assert not orphan.path.exists()
    assert (owned / "payload.txt").read_text(encoding="utf-8") == "payload"
    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        upgraded = replace(orphan, verified_at_isolation=True)
        with pytest.raises(RuntimeError, match="receipt binding changed"):
            reclaimer.reclaim_orphan(upgraded)
        with pytest.raises(RuntimeError, match="not verified at isolation"):
            reclaimer.reclaim_orphan(orphan)

    assert (owned / "payload.txt").read_text(encoding="utf-8") == "payload"


def test_quiescent_reclaimer_exact_gate_rejects_hostile_ownership_before_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    hostile_dispatch: list[str] = []

    class HostileOwnership:
        @property
        def __class__(self) -> object:
            hostile_dispatch.append("__class__")
            raise AssertionError("exact ownership gate executed hostile __class__")

    tampered = replace(
        orphan,
        locator=replace(
            orphan.locator,
            ownership=HostileOwnership(),
        ),
    )

    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid receipt must not reach child lookup")

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(forbidden_lookup),
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(TypeError, match="ownership token fields are not exact"):
            reclaimer.reclaim_orphan(tampered)

    assert hostile_dispatch == []
    assert (orphan.path / "payload.txt").read_text(encoding="utf-8") == "payload"


def test_quiescent_reclaimer_quarantines_before_descriptor_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_unlink = atomic_module.os.unlink
    observed_quarantine: Path | None = None

    def checked_unlink(path: object, *, dir_fd: int | None = None) -> None:
        nonlocal observed_quarantine
        assert path == "payload.txt"
        assert dir_fd is not None
        assert not attempt.exists()
        quarantines = list(parent.glob(".attempt-a.discarded-*"))
        assert len(quarantines) == 1
        observed_quarantine = quarantines[0]
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(atomic_module.os, "unlink", checked_unlink)

    with reclaimer:
        assert reclaimer.reclaim_child(attempt.name) is True

    assert observed_quarantine is not None
    assert not observed_quarantine.exists()


def test_quiescent_reclaimer_rejects_parent_guards_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    parent.chmod(0o755)

    with pytest.raises(PermissionError, match="private owner-only"):
        atomic_module.QuiescentDirectoryReclaimer(
            parent,
            expected_parent_identity=parent_identity,
        )

    parent.chmod(0o700)
    wrong_identity = (
        parent_identity[0],
        parent_identity[1] + 1,
        *parent_identity[2:],
    )
    with pytest.raises(RuntimeError, match="identity"):
        atomic_module.QuiescentDirectoryReclaimer(
            parent,
            expected_parent_identity=wrong_identity,
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(atomic_module.os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(PermissionError, match="private owner-only"):
            atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )

    def forbidden_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unsupported reclamation must not open authority")

    monkeypatch.setattr(atomic_module, "_open_publication_authority", forbidden_open)
    monkeypatch.setattr(atomic_module.sys, "platform", "freebsd14")
    with pytest.raises(RuntimeError, match="reclamation is unsupported"):
        atomic_module.QuiescentDirectoryReclaimer(
            parent,
            expected_parent_identity=parent_identity,
        )

    assert (attempt / "payload.txt").read_text(encoding="utf-8") == "payload"


def test_quiescent_reclaimer_requires_directory_fd_listing_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    opened = False

    def forbidden_open(*_args: object, **_kwargs: object) -> object:
        nonlocal opened
        opened = True
        raise AssertionError("unsupported reclaimer must not open parent authority")

    monkeypatch.setattr(
        atomic_module.os,
        "supports_fd",
        atomic_module.os.supports_fd - {atomic_module.os.listdir},
    )
    monkeypatch.setattr(
        atomic_module,
        "_open_publication_authority",
        forbidden_open,
    )

    with pytest.raises(RuntimeError, match="directory-fd listing support"):
        atomic_module.QuiescentDirectoryReclaimer(
            parent,
            expected_parent_identity=parent_identity,
        )

    assert not opened


def test_quiescent_reclaimer_is_sealed_and_pid_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)

    class DerivedReclaimer(atomic_module.QuiescentDirectoryReclaimer):
        pass

    with pytest.raises(TypeError, match="cannot be subclassed"):
        DerivedReclaimer(parent, expected_parent_identity=parent_identity)

    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    acquire_calls = 0
    real_acquire = atomic_module._QuiescentDirectoryLifecycleAttempt._acquire

    def counted_acquire(attempt: object) -> None:
        nonlocal acquire_calls
        acquire_calls += 1
        real_acquire(attempt)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleAttempt,
        "_acquire",
        counted_acquire,
    )
    owner_pid = os.getpid()
    original_getpid = atomic_module.os.getpid
    atomic_module.os.getpid = lambda: owner_pid + 1
    try:
        with pytest.raises(RuntimeError, match="PID boundary"):
            reclaimer.reclaim_child("missing-attempt")
        with pytest.raises(RuntimeError, match="PID boundary"):
            reclaimer.retry()
        with pytest.raises(RuntimeError, match="PID boundary"):
            reclaimer.close()
    finally:
        atomic_module.os.getpid = original_getpid
    assert acquire_calls == 0
    assert reclaimer.reclaim_child("missing-attempt") is False
    assert acquire_calls == 1
    reclaimer.close()
    assert reclaimer.closed


def test_quiescent_reclaimer_rejects_hard_links_before_quarantine(
    tmp_path: Path,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "one.txt", "payload")
    os.link(attempt / "one.txt", attempt / "two.txt")

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(RuntimeError, match="hard-linked file"):
            reclaimer.reclaim_child(attempt.name)

    assert attempt.is_dir()
    assert {path.name for path in attempt.iterdir()} == {"one.txt", "two.txt"}
    assert not list(parent.glob(".attempt-a.discarded-*"))


def test_quiescent_reclaimer_rejects_mount_boundary_before_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    real_is_mount = atomic_module._path_is_mount_point
    monkeypatch.setattr(
        atomic_module,
        "_path_is_mount_point",
        lambda path, **kwargs: path == attempt or real_is_mount(path, **kwargs),
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(PermissionError, match="owned local directory"):
            reclaimer.reclaim_child(attempt.name)

    assert (attempt / "payload.txt").read_text(encoding="utf-8") == "payload"
    assert not list(parent.glob(".attempt-a.discarded-*"))


def test_quiescent_reclaimer_rejects_cross_device_child_before_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    metadata = attempt.lstat()
    foreign_device = SimpleNamespace(
        st_dev=metadata.st_dev + 1,
        st_file_attributes=getattr(metadata, "st_file_attributes", 0),
        st_ino=metadata.st_ino,
        st_mode=metadata.st_mode,
        st_uid=metadata.st_uid,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(lambda _authority, _path: foreign_device),
    )

    with pytest.raises(PermissionError, match="owned local directory"):
        reclaimer.reclaim_child(attempt.name)

    reclaimer.close()
    assert (attempt / "payload.txt").read_text(encoding="utf-8") == "payload"
    assert not list(parent.glob(".attempt-a.discarded-*"))


def test_quiescent_reclaimer_enforces_capture_budget_before_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    attempt.mkdir()
    (attempt / "one.txt").write_text("one", encoding="utf-8")
    (attempt / "two.txt").write_text("two", encoding="utf-8")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    monkeypatch.setattr(atomic_module, "_MAX_OWNERSHIP_ENTRIES", 1)

    with pytest.raises(RuntimeError, match="entry limit"):
        reclaimer.reclaim_child(attempt.name)

    reclaimer.close()
    assert attempt.is_dir()
    assert {path.name for path in attempt.iterdir()} == {"one.txt", "two.txt"}
    assert not list(parent.glob(".attempt-a.discarded-*"))


def test_quiescent_reclaimer_authenticates_orphan_backend_before_absence(
    tmp_path: Path,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    parked = parent / ".parked-owned"
    orphan.path.rename(parked)
    wrong_backend = replace(
        orphan,
        locator=replace(orphan.locator, backend_tag="foreign-backend"),
    )

    with atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    ) as reclaimer:
        with pytest.raises(ValueError, match="another parent authority"):
            reclaimer.reclaim_orphan(wrong_backend)
        assert reclaimer.reclaim_orphan(orphan) is True
        parked.rename(orphan.path)
        assert reclaimer.reclaim_orphan(orphan) is True

    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_rejects_tampered_or_stale_orphan(
    tmp_path: Path,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )

    tampered = replace(orphan, byte_count=orphan.byte_count + 1)
    with pytest.raises(RuntimeError, match="metadata changed"):
        reclaimer.reclaim_orphan(tampered)

    parked = parent / ".parked-owned"
    orphan.path.rename(parked)
    _write_tree(orphan.path, "foreign.txt", "preserve")
    with pytest.raises(RuntimeError, match="quarantine changed") as caught:
        reclaimer.reclaim_orphan(orphan)
    assert reclaimer in caught.value.publication_cleanup_owners
    assert (orphan.path / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert (parked / "payload.txt").read_text(encoding="utf-8") == "payload"

    (orphan.path / "foreign.txt").unlink()
    orphan.path.rmdir()
    parked.rename(orphan.path)
    assert reclaimer.retry() is True
    reclaimer.close()
    assert reclaimer.closed


def test_quiescent_reclaimer_retries_committed_quarantine_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    real_rename = atomic_module._rename_noreplace_at
    interrupted = False

    def interrupt_after_quarantine(
        source: str,
        destination: str,
        source_parent: int,
        destination_parent: int,
    ) -> None:
        nonlocal interrupted
        real_rename(source, destination, source_parent, destination_parent)
        if source == attempt.name and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("post-quarantine interruption")

    monkeypatch.setattr(
        atomic_module,
        "_rename_noreplace_at",
        interrupt_after_quarantine,
    )
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )

    with pytest.raises(KeyboardInterrupt, match="post-quarantine") as caught:
        reclaimer.reclaim_child(attempt.name)
    assert reclaimer in caught.value.publication_cleanup_owners
    assert not attempt.exists()
    assert len(list(parent.glob(".attempt-a.discarded-*"))) == 1

    assert reclaimer.retry() is True
    reclaimer.close()
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_reconciles_post_unlink_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_unlink = atomic_module.os.unlink
    interrupted = False

    def interrupt_after_unlink(path: object, *, dir_fd: int | None = None) -> None:
        nonlocal interrupted
        real_unlink(path, dir_fd=dir_fd)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("post-unlink interruption")

    monkeypatch.setattr(atomic_module.os, "unlink", interrupt_after_unlink)

    with pytest.raises(KeyboardInterrupt, match="post-unlink") as caught:
        reclaimer.reclaim_child(attempt.name)
    assert reclaimer in caught.value.publication_cleanup_owners
    assert reclaimer.retry() is True
    reclaimer.close()
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_reconciles_post_root_removal_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    attempt.mkdir()
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_rmdir = atomic_module.os.rmdir
    interrupted = False

    def interrupt_after_rmdir(path: object, *, dir_fd: int | None = None) -> None:
        nonlocal interrupted
        real_rmdir(path, dir_fd=dir_fd)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("post-rmdir interruption")

    monkeypatch.setattr(atomic_module.os, "rmdir", interrupt_after_rmdir)

    with pytest.raises(KeyboardInterrupt, match="post-rmdir") as caught:
        reclaimer.reclaim_child(attempt.name)
    assert reclaimer in caught.value.publication_cleanup_owners
    assert reclaimer.retry() is True
    reclaimer.close()
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_fresh_receipt_syncs_absent_removed_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    attempt.mkdir()
    original = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_rmdir = atomic_module.os.rmdir
    interrupted_removal = False

    def interrupt_after_rmdir(path: object, *, dir_fd: int | None = None) -> None:
        nonlocal interrupted_removal
        real_rmdir(path, dir_fd=dir_fd)
        if not interrupted_removal:
            interrupted_removal = True
            raise KeyboardInterrupt("post-root-rmdir interruption")

    monkeypatch.setattr(atomic_module.os, "rmdir", interrupt_after_rmdir)
    with pytest.raises(KeyboardInterrupt, match="post-root-rmdir"):
        original.reclaim_child(attempt.name)
    active = original._active
    assert active is not None
    assert active.root_state == "removed"
    orphan = active.orphan
    assert orphan is not None and orphan.verified_at_isolation
    assert not orphan.path.exists()

    monkeypatch.setattr(atomic_module.os, "rmdir", real_rmdir)
    fresh = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    fresh_authority = fresh._authority_owner.authority
    real_sync = atomic_module._PublicationAuthority.sync_parent
    sync_calls = 0
    interrupted_sync = False

    def interrupt_after_fresh_sync(authority: object) -> None:
        nonlocal sync_calls, interrupted_sync
        real_sync(authority)
        if authority is fresh_authority:
            sync_calls += 1
            if not interrupted_sync:
                interrupted_sync = True
                raise KeyboardInterrupt("post-absent-sync interruption")

    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "sync_parent",
        interrupt_after_fresh_sync,
    )

    with pytest.raises(KeyboardInterrupt, match="post-absent-sync"):
        fresh.reclaim_orphan(orphan)
    assert sync_calls == 1
    assert fresh.reclaim_orphan(orphan) is True
    assert sync_calls == 2
    assert fresh.reclaim_child(orphan.path.name) is False
    assert sync_calls == 2

    fresh.close()
    assert original.retry() is True
    original.close()
    assert list(parent.iterdir()) == []


def test_quiescent_reclaimer_retries_completed_parent_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    attempt.mkdir()
    real_sync = atomic_module._PublicationAuthority.sync_parent
    interrupted = False

    def interrupt_after_sync(authority: object) -> None:
        nonlocal interrupted
        real_sync(authority)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("post-sync interruption")

    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "sync_parent",
        interrupt_after_sync,
    )
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )

    with pytest.raises(KeyboardInterrupt, match="post-sync") as caught:
        reclaimer.reclaim_child(attempt.name)
    assert reclaimer in caught.value.publication_cleanup_owners
    assert reclaimer.retry() is True
    reclaimer.close()
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("surface", ["unlink", "rmdir", "sync"])
def test_quiescent_reclaimer_rejects_recursive_retry_during_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    if surface == "unlink":
        _write_tree(attempt, "payload.txt", "payload")
    elif surface == "rmdir":
        (attempt / "nested").mkdir(parents=True)
    else:
        attempt.mkdir()
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    recursive_calls: list[str] = []

    def reject_recursive_retry() -> None:
        if recursive_calls:
            return
        recursive_calls.append(surface)
        with pytest.raises(RuntimeError, match="lifecycle transition is already"):
            reclaimer.retry()

    if surface == "unlink":
        real_unlink = atomic_module.os.unlink

        def checked_unlink(path: object, *, dir_fd: int | None = None) -> None:
            reject_recursive_retry()
            real_unlink(path, dir_fd=dir_fd)

        monkeypatch.setattr(atomic_module.os, "unlink", checked_unlink)
    elif surface == "rmdir":
        real_rmdir = atomic_module.os.rmdir

        def checked_rmdir(path: object, *, dir_fd: int | None = None) -> None:
            reject_recursive_retry()
            real_rmdir(path, dir_fd=dir_fd)

        monkeypatch.setattr(atomic_module.os, "rmdir", checked_rmdir)
    else:
        real_sync = atomic_module._PublicationAuthority.sync_parent

        def checked_sync(authority: object) -> None:
            reject_recursive_retry()
            real_sync(authority)

        monkeypatch.setattr(
            atomic_module._PublicationAuthority,
            "sync_parent",
            checked_sync,
        )

    assert reclaimer.reclaim_child(attempt.name) is True
    assert recursive_calls == [surface]
    assert reclaimer._active is None
    reclaimer.close()
    assert reclaimer.closed


def test_quiescent_reclaimer_rejects_recursive_close_without_dropping_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_unlink = atomic_module.os.unlink
    active_during_rejection: list[object] = []

    def checked_unlink(path: object, *, dir_fd: int | None = None) -> None:
        if not active_during_rejection:
            active = reclaimer._active
            assert active is not None
            with pytest.raises(RuntimeError, match="lifecycle transition is already"):
                reclaimer.close()
            assert reclaimer._active is active
            active_during_rejection.append(active)
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(atomic_module.os, "unlink", checked_unlink)

    assert reclaimer.reclaim_child(attempt.name) is True
    assert len(active_during_rejection) == 1
    reclaimer.close()
    assert reclaimer.closed


def test_quiescent_reclaimer_defers_public_recovery_until_error_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    first = parent / "attempt-a"
    second = parent / "attempt-b"
    _write_tree(first, "payload.txt", "first")
    _write_tree(second, "payload.txt", "second")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    real_unlink = atomic_module.os.unlink
    fail_release = True
    fail_remove = True
    observed_permissions: list[bool] = []
    nested_rejections: list[str] = []
    nested_results: list[tuple[str, bool]] = []
    nested_exit_attempted = False

    class CleanupOwners:
        def __get__(self, _instance: object, _owner: object) -> tuple[()]:
            return ()

        def __set__(self, _instance: object, _value: object) -> None:
            nonlocal fail_release, fail_remove, nested_exit_attempted
            if not nested_exit_attempted:
                nested_exit_attempted = True
                try:
                    reclaimer.__exit__(None, None, None)
                except RuntimeError as error:
                    assert "lifecycle transition is already active" in str(error)
                    nested_rejections.append("exit")
                else:
                    nested_results.append(("exit", True))
                fail_release = False
                fail_remove = False
                try:
                    nested_results.append(("retry-a", reclaimer.retry()))
                except RuntimeError as error:
                    assert "lifecycle transition is already active" in str(error)
                    nested_rejections.append("retry")
                    fail_release = True
                    fail_remove = True
                else:
                    nested_results.append(
                        ("reclaim-b", reclaimer.reclaim_child(second.name))
                    )
            current = reclaimer._lifecycle_lease.current_transition()
            allowed = (
                type(current) is atomic_module._QuiescentDirectoryLifecycleAttempt
                and not current._outer_call._guard.gi_running
            )
            observed_permissions.append(allowed)
            raise RuntimeError("refuse cleanup-owner metadata")

    class RemovalFailure(OSError):
        publication_cleanup_owners = CleanupOwners()

    primary = RemovalFailure(errno.EIO, "outer removal failed")

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise OSError(errno.EIO, "lifecycle release failed")
        real_release(lease)

    def fail_payload_removal(path: object, *, dir_fd: int | None = None) -> None:
        if path == "payload.txt" and fail_remove:
            raise primary
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )
    monkeypatch.setattr(atomic_module.os, "unlink", fail_payload_removal)

    with pytest.raises(RemovalFailure) as caught:
        reclaimer.reclaim_child(first.name)

    assert caught.value is primary
    assert observed_permissions
    assert not any(observed_permissions)
    assert nested_rejections == ["exit", "retry"]
    assert nested_results == []
    assert (second / "payload.txt").read_text(encoding="utf-8") == "second"

    fail_release = False
    fail_remove = False
    assert reclaimer.retry() is True
    assert reclaimer.reclaim_child(second.name) is True
    reclaimer.close()
    assert reclaimer.closed


@pytest.mark.parametrize(
    "tamper",
    ["owner-close", "lease-clear", "forged-guard"],
)
def test_quiescent_reclaimer_published_attempt_cannot_reenter_owning_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    first = parent / "attempt-a"
    second = parent / "attempt-b"
    _write_tree(first, "payload.txt", "first")
    _write_tree(second, "payload.txt", "second")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    real_unlink = atomic_module.os.unlink
    fail_release = True
    fail_remove = True
    published_attempt: object | None = None
    tamper_results: list[str] = []
    internal_guard_results: list[str] = []
    nested_results: list[tuple[str, object]] = []

    class CleanupOwners:
        def __get__(self, _instance: object, _owner: object) -> tuple[()]:
            return ()

        def __set__(self, _instance: object, value: object) -> None:
            nonlocal fail_release, fail_remove, published_attempt
            attempts = (
                tuple(
                    owner
                    for owner in value
                    if type(owner) is atomic_module._QuiescentDirectoryLifecycleAttempt
                )
                if type(value) is tuple
                else ()
            )
            if attempts and published_attempt is None:
                attempt = attempts[0]
                published_attempt = attempt
                fail_release = False
                fail_remove = False
                if tamper == "lease-clear":
                    attempt._lease.release()
                    attempt._lease.clear_transition(attempt)
                    tamper_results.append("cleared")
                else:
                    if tamper == "forged-guard":
                        forged_guard = (
                            atomic_module._quiescent_directory_execution_guard(
                                lambda: None
                            )
                        )
                        forged_guard.close()
                        try:
                            attempt.execution_guard = forged_guard
                        except AttributeError:
                            tamper_results.append("immutable")
                        else:
                            tamper_results.append("forged")
                    try:
                        attempt.close()
                    except RuntimeError as error:
                        tamper_results.append(str(error))
                    else:
                        tamper_results.append("closed")

                try:
                    attempt._close_borrowed(object())
                except RuntimeError as error:
                    internal_guard_results.append(str(error))
                else:
                    internal_guard_results.append("closed")

                for label, callback in (
                    ("retry", reclaimer.retry),
                    (
                        "reclaim-b",
                        lambda: reclaimer.reclaim_child(second.name),
                    ),
                ):
                    try:
                        nested_results.append((label, callback()))
                    except RuntimeError as error:
                        nested_results.append((label, str(error)))
            raise RuntimeError("refuse cleanup-owner metadata")

    class RemovalFailure(OSError):
        publication_cleanup_owners = CleanupOwners()

    primary = RemovalFailure(errno.EIO, "outer removal failed")

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise OSError(errno.EIO, "lifecycle release failed")
        real_release(lease)

    def fail_payload_removal(path: object, *, dir_fd: int | None = None) -> None:
        if path == "payload.txt" and fail_remove:
            raise primary
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )
    monkeypatch.setattr(atomic_module.os, "unlink", fail_payload_removal)

    with pytest.raises(RemovalFailure) as caught:
        reclaimer.reclaim_child(first.name)

    assert caught.value is primary
    assert type(published_attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert tamper_results == (
        ["cleared"]
        if tamper == "lease-clear"
        else (
            [
                "immutable",
                (
                    "quiescent directory reclaimer lifecycle transition "
                    "is already active"
                ),
            ]
            if tamper == "forged-guard"
            else [
                (
                    "quiescent directory reclaimer lifecycle transition "
                    "is already active"
                )
            ]
        )
    )
    assert internal_guard_results == [
        "quiescent directory lifecycle settlement token is not active"
    ]
    assert nested_results == [
        (
            "retry",
            "quiescent directory reclaimer lifecycle transition is already active",
        ),
        (
            "reclaim-b",
            "quiescent directory reclaimer lifecycle transition is already active",
        ),
    ]
    assert reclaimer._active is not None
    assert (second / "payload.txt").read_text(encoding="utf-8") == "second"

    with pytest.raises(
        RuntimeError,
        match="lifecycle settlement token is not active",
    ):
        published_attempt._close_borrowed(published_attempt._outer_call._token)

    assert reclaimer.retry() is True
    assert reclaimer.reclaim_child(second.name) is True
    reclaimer.close()
    assert reclaimer.closed


def test_quiescent_exit_defers_public_recovery_until_error_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True
    observed_permissions: list[bool] = []
    nested_closes: list[bool] = []

    class CleanupOwners:
        def __get__(self, _instance: object, _owner: object) -> tuple[()]:
            return ()

        def __set__(self, _instance: object, _value: object) -> None:
            nonlocal fail_release
            current = reclaimer._lifecycle_lease.current_transition()
            allowed = (
                type(current) is atomic_module._QuiescentDirectoryLifecycleAttempt
                and not current._outer_call._guard.gi_running
            )
            observed_permissions.append(allowed)
            if allowed and not nested_closes:
                fail_release = False
                reclaimer.close()
                nested_closes.append(True)
            raise RuntimeError("refuse cleanup-owner metadata")

    class ReleaseFailure(OSError):
        publication_cleanup_owners = CleanupOwners()

    primary = ReleaseFailure(errno.EIO, "exit lifecycle release failed")

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise primary
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(ReleaseFailure) as caught:
        reclaimer.__exit__(None, None, None)

    assert caught.value is primary
    assert observed_permissions
    assert not any(observed_permissions)
    assert nested_closes == []

    fail_release = False
    reclaimer.close()
    assert reclaimer.closed


def test_quiescent_reclaimer_serializes_two_threads_before_operation_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    other = parent / "attempt-b"
    _write_tree(attempt, "payload.txt", "payload")
    _write_tree(other, "foreign.txt", "preserve")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    entered = threading.Event()
    release = threading.Event()
    real_child_metadata = atomic_module.QuiescentDirectoryReclaimer._child_metadata
    results: list[bool] = []
    failures: list[BaseException] = []

    def blocked_child_metadata(authority: object, path: Path) -> object | None:
        if path == attempt and not entered.is_set():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("pre-install test did not release worker")
        return real_child_metadata(authority, path)

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_child_metadata",
        staticmethod(blocked_child_metadata),
    )

    def reclaim_in_worker() -> None:
        try:
            results.append(reclaimer.reclaim_child(attempt.name))
        except BaseException as error:  # noqa: B036 - report thread failure
            failures.append(error)

    worker = threading.Thread(target=reclaim_in_worker)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        assert reclaimer._active is None
        with pytest.raises(RuntimeError, match="lifecycle transition is already"):
            reclaimer.reclaim_child(other.name)
        assert reclaimer._active is None
        assert (other / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert results == [True]
    assert reclaimer.reclaim_child(other.name) is True
    reclaimer.close()


def test_quiescent_reclaimer_atomically_replaces_inactive_execution_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    assert reclaimer.retry() is False
    stale_guard = reclaimer._lifecycle_lease._outer_call._guard
    assert stale_guard is not None
    assert not stale_guard.gi_running

    admission = threading.Barrier(2)
    callback_entered = threading.Event()
    callback_release = threading.Event()
    rejected = threading.Event()
    admission_counter_lock = threading.Lock()
    admission_calls = 0
    real_acquire = atomic_module._QuiescentDirectoryLifecycleLease.acquire_nonblocking
    real_resume = atomic_module.QuiescentDirectoryReclaimer._resume
    results: list[bool] = []
    failures: list[BaseException] = []

    def synchronized_acquire(lease: object) -> bool:
        nonlocal admission_calls
        if lease is reclaimer._lifecycle_lease:
            with admission_counter_lock:
                admission_calls += 1
                synchronize = admission_calls <= 2
            if synchronize:
                admission.wait(timeout=5)
        return real_acquire(lease)

    def blocked_resume(
        candidate: atomic_module.QuiescentDirectoryReclaimer,
    ) -> bool:
        if candidate is reclaimer and not callback_entered.is_set():
            callback_entered.set()
            if not callback_release.wait(timeout=5):
                raise AssertionError("execution-guard test did not release callback")
        return real_resume(candidate)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "acquire_nonblocking",
        synchronized_acquire,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_resume",
        blocked_resume,
    )

    def retry_in_worker() -> None:
        try:
            results.append(reclaimer.retry())
        except BaseException as error:  # noqa: B036 - report thread failure
            failures.append(error)
            rejected.set()

    workers = [threading.Thread(target=retry_in_worker) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert callback_entered.wait(timeout=5)
        assert rejected.wait(timeout=5)
        current = reclaimer._lifecycle_lease.current_transition()
        guard = reclaimer._lifecycle_lease._outer_call._guard
        assert type(current) is atomic_module._QuiescentDirectoryLifecycleAttempt
        assert guard is not stale_guard
        assert guard is current._outer_call._guard
        assert guard.gi_running
    finally:
        callback_release.set()
        for worker in workers:
            worker.join(timeout=5)

    assert not any(worker.is_alive() for worker in workers)
    assert results == [False]
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "lifecycle transition is already active" in str(failures[0])
    assert not reclaimer._lifecycle_lease.transition_active()
    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "acquire_nonblocking",
        real_acquire,
    )
    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_resume",
        real_resume,
    )
    assert reclaimer.retry() is False
    reclaimer.close()


def test_quiescent_reclaimer_reserves_created_outer_call_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    lease = reclaimer._lifecycle_lease
    real_reserve = atomic_module._QuiescentDirectoryLifecycleLease.reserve_outer_call
    installed_calls: list[object] = []
    nested_failures: list[BaseException] = []
    worker_failures: list[BaseException] = []

    def reserve_then_reenter(owned_lease: object, outer_call: object) -> None:
        real_reserve(owned_lease, outer_call)
        if owned_lease is not lease or installed_calls:
            return
        installed_calls.append(outer_call)
        assert lease._outer_call is outer_call
        assert outer_call._guard.gi_frame is not None
        assert not outer_call._guard.gi_running

        try:
            reclaimer.retry()
        except BaseException as error:  # noqa: B036 - exact fail-fast result
            nested_failures.append(error)

        def retry_in_worker() -> None:
            try:
                reclaimer.retry()
            except BaseException as error:  # noqa: B036 - exact fail-fast result
                worker_failures.append(error)

        worker = threading.Thread(target=retry_in_worker)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert lease._outer_call is outer_call

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "reserve_outer_call",
        reserve_then_reenter,
    )

    assert reclaimer.retry() is False
    assert len(installed_calls) == 1
    assert len(nested_failures) == 1
    assert len(worker_failures) == 1
    for failure in (*nested_failures, *worker_failures):
        assert isinstance(failure, RuntimeError)
        assert "lifecycle transition is already active" in str(failure)
    assert lease._outer_call is installed_calls[0]
    assert lease._outer_call.closed
    assert not lease.transition_active()
    reclaimer.close()


@pytest.mark.parametrize(
    "borrowed_depth",
    [False, True],
    ids=["fresh-created", "borrowed-active-operation"],
)
def test_quiescent_reclaimer_handed_reservation_cannot_expose_outer_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    borrowed_depth: bool,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    first = parent / "attempt-a"
    second = parent / "attempt-b"
    _write_tree(first, "payload.txt", "first")
    _write_tree(second, "payload.txt", "second")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    lease = reclaimer._lifecycle_lease
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    real_unlink = atomic_module.os.unlink
    fail_release = borrowed_depth
    fail_remove = borrowed_depth
    setup_removal = OSError(errno.EIO, "active operation removal failed")

    def fail_active_release(owned_lease: object) -> None:
        if owned_lease is lease and fail_release:
            raise OSError(errno.EIO, "active operation release failed")
        real_release(owned_lease)

    def fail_active_removal(
        path: object,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == "payload.txt" and fail_remove:
            raise setup_removal
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_active_release,
    )
    monkeypatch.setattr(atomic_module.os, "unlink", fail_active_removal)
    if borrowed_depth:
        with pytest.raises(OSError) as setup_caught:
            reclaimer.reclaim_child(first.name)
        assert setup_caught.value is setup_removal
        assert reclaimer._active is not None
        assert lease.transition_active()
        assert lease.owned_by_current_thread()
        assert lease._outer_call is not None
        assert lease._outer_call.closed
        fail_release = False
        fail_remove = False

    public_surfaces: list[tuple[str, ...]] = []
    owner_closed_results: list[bool] = []
    nested_results: list[tuple[str, str]] = []
    attacked = False

    class CleanupOwners:
        def __get__(self, _instance: object, _owner: object) -> tuple[()]:
            return ()

        def __set__(self, _instance: object, value: object) -> None:
            nonlocal attacked
            reservations = (
                tuple(
                    owner
                    for owner in value
                    if type(owner)
                    is atomic_module._QuiescentDirectoryOuterCallReservation
                )
                if type(value) is tuple
                else ()
            )
            if reservations and not attacked:
                attacked = True
                reservation = reservations[0]
                public_surfaces.append(
                    tuple(name for name in dir(reservation) if not name.startswith("_"))
                )
                # Callback-controlled metadata may use only the documented
                # cleanup-owner protocol. It must not receive a public path to
                # the still-CREATED outer call behind this reservation.
                reservation.close()
                owner_closed_results.append(reservation.closed)
                for label, callback in (
                    ("retry", reclaimer.retry),
                    ("reclaim", lambda: reclaimer.reclaim_child(second.name)),
                    ("exit", lambda: reclaimer.__exit__(None, None, None)),
                ):
                    try:
                        result = callback()
                    except RuntimeError as error:
                        nested_results.append((label, str(error)))
                    else:
                        nested_results.append((label, f"returned {result!r}"))
            raise RuntimeError("refuse reservation owner metadata")

    class ReservationFailure(RuntimeError):
        publication_cleanup_owners = CleanupOwners()

    primary = ReservationFailure("outer-call reservation failed")
    real_reserve = atomic_module._QuiescentDirectoryLifecycleLease.reserve_outer_call
    injected = False

    def fail_after_reservation(
        owned_lease: object,
        outer_call: object,
    ) -> None:
        nonlocal injected
        real_reserve(owned_lease, outer_call)
        if owned_lease is lease and not injected:
            injected = True
            raise primary

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "reserve_outer_call",
        fail_after_reservation,
    )

    with pytest.raises(ReservationFailure) as caught:
        reclaimer.retry()

    assert caught.value is primary
    assert injected
    assert attacked
    assert public_surfaces == [("close", "closed")]
    assert owner_closed_results == [True]
    assert nested_results == [
        (
            "retry",
            "quiescent directory reclaimer lifecycle transition is already active",
        ),
        (
            "reclaim",
            "quiescent directory reclaimer lifecycle transition is already active",
        ),
        (
            "exit",
            "quiescent directory reclaimer lifecycle transition is already active",
        ),
    ]
    assert (second / "payload.txt").read_text(encoding="utf-8") == "second"
    assert lease._outer_call is not None
    assert lease._outer_call.closed

    if borrowed_depth:
        assert lease.transition_active()
        assert lease.owned_by_current_thread()
        assert reclaimer.retry() is True
        assert not first.exists()
    else:
        assert not lease.transition_active()
        assert not lease.owned_by_current_thread()
        assert reclaimer.retry() is False
    assert reclaimer.reclaim_child(second.name) is True
    reclaimer.close()
    assert reclaimer.closed


@pytest.mark.parametrize(
    "pending_attempt",
    [False, True],
    ids=["no-prior-depth", "borrowed-attempt-depth"],
)
def test_quiescent_reclaimer_outer_admission_opcode_seams_recover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_attempt: bool,
) -> None:
    seam_specs = (
        (
            atomic_module._QuiescentDirectoryLifecycleLease.reserve_outer_call,
            "self._outer_call = outer_call",
        ),
        (
            atomic_module._QuiescentDirectoryOuterCallReservation.close,
            (
                "if self._complete or self._borrowed_existing_depth:"
                if pending_attempt
                else "self._lease.release_admission()"
            ),
        ),
        (
            atomic_module._run_quiescent_directory_outer_call,
            "return next(guard)",
        ),
        (
            atomic_module._QuiescentDirectoryLifecycleLease.require_outer_call,
            "self._require_process()",
        ),
    )

    for seam_index, (function, source_fragment) in enumerate(seam_specs):
        source_offsets = _source_statement_opcode_offsets(function, source_fragment)
        with monkeypatch.context() as observation_patch:
            observed_parent, observed_identity = _private_reclaimer_parent(
                tmp_path,
                name=(
                    f"outer-admission-observe-{int(pending_attempt)}-" f"{seam_index}"
                ),
            )
            observed_reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                observed_parent,
                expected_parent_identity=observed_identity,
            )
            observed_lease = observed_reclaimer._lifecycle_lease
            observed_real_release = (
                atomic_module._QuiescentDirectoryLifecycleLease.release
            )
            observed_fail_release = [pending_attempt]

            def fail_observed_release(
                owned_lease: object,
                *,
                target_lease: object = observed_lease,
                failure_switch: list[bool] = observed_fail_release,
                release: object = observed_real_release,
            ) -> None:
                if owned_lease is target_lease and failure_switch[0]:
                    raise OSError(errno.EIO, "observed pending lifecycle release")
                release(owned_lease)

            observation_patch.setattr(
                atomic_module._QuiescentDirectoryLifecycleLease,
                "release",
                fail_observed_release,
            )
            if pending_attempt:
                with pytest.raises(
                    OSError,
                    match="observed pending lifecycle release",
                ):
                    observed_reclaimer.retry()
                observed_fail_release[0] = False

            def selected_observed_frame(
                values: dict[str, object],
                *,
                selected_function: object = function,
                target_lease: object = observed_lease,
            ) -> bool:
                if selected_function is (
                    atomic_module._run_quiescent_directory_outer_call
                ):
                    return values.get("lease") is target_lease
                candidate = values.get("self")
                if selected_function is (
                    atomic_module._QuiescentDirectoryOuterCallReservation.close
                ):
                    return getattr(candidate, "_lease", None) is target_lease
                return candidate is target_lease

            observed_offsets = _observed_opcode_offsets(
                function,
                observed_reclaimer.retry,
                predicate=selected_observed_frame,
            )
            observed_reclaimer.close()
        offsets = tuple(
            offset for offset in source_offsets if offset in observed_offsets
        )
        assert offsets
        for opcode_index, opcode_offset in enumerate(offsets):
            with monkeypatch.context() as patch:
                parent, parent_identity = _private_reclaimer_parent(
                    tmp_path,
                    name=(
                        f"outer-admission-{int(pending_attempt)}-"
                        f"{seam_index}-{opcode_index}"
                    ),
                )
                reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                    parent,
                    expected_parent_identity=parent_identity,
                )
                lease = reclaimer._lifecycle_lease
                real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
                fail_release = [pending_attempt]

                def fail_pending_release(
                    owned_lease: object,
                    *,
                    target_lease: object = lease,
                    failure_switch: list[bool] = fail_release,
                    release: object = real_release,
                ) -> None:
                    if owned_lease is target_lease and failure_switch[0]:
                        raise OSError(errno.EIO, "pending lifecycle release")
                    release(owned_lease)

                patch.setattr(
                    atomic_module._QuiescentDirectoryLifecycleLease,
                    "release",
                    fail_pending_release,
                )
                if pending_attempt:
                    with pytest.raises(OSError, match="pending lifecycle release"):
                        reclaimer.retry()
                    prior_attempt = lease.current_transition()
                    assert type(prior_attempt) is (
                        atomic_module._QuiescentDirectoryLifecycleAttempt
                    )
                    assert lease.owned_by_current_thread()
                    assert lease._outer_call is prior_attempt._outer_call
                    assert lease._outer_call.closed
                    fail_release[0] = False

                interruption = KeyboardInterrupt(
                    f"outer admission {source_fragment} {opcode_offset}"
                )

                def selected_frame(
                    values: dict[str, object],
                    *,
                    selected_function: object = function,
                    target_lease: object = lease,
                ) -> bool:
                    if selected_function is (
                        atomic_module._run_quiescent_directory_outer_call
                    ):
                        return values.get("lease") is target_lease
                    candidate = values.get("self")
                    if selected_function is (
                        atomic_module._QuiescentDirectoryOuterCallReservation.close
                    ):
                        return getattr(candidate, "_lease", None) is target_lease
                    return candidate is target_lease

                with pytest.raises(KeyboardInterrupt) as caught:
                    _call_with_interrupt_at_opcode(
                        function,
                        opcode_offset,
                        reclaimer.retry,
                        predicate=selected_frame,
                        error=interruption,
                    )

                assert caught.value is interruption
                try:
                    owners = BaseException.__getattribute__(
                        caught.value,
                        "publication_cleanup_owners",
                    )
                except AttributeError:
                    owners = ()
                assert type(owners) is tuple
                for owner in owners:
                    owner.close()

                # The interrupted call may leave the exact prior attempt for
                # same-origin public recovery, but never requires link walking.
                assert reclaimer.retry() is False
                assert not lease.owned_by_current_thread()
                assert not lease.transition_active()
                assert lease._outer_call is not None
                assert lease._outer_call.closed
                _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
                reclaimer.close()


def test_quiescent_reclaimer_raw_only_attempt_owner_reenters_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    other = parent / "attempt-b"
    _write_tree(other, "payload.txt", "preserve")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    lease = reclaimer._lifecycle_lease
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_raw_release(owned_lease: object) -> None:
        if owned_lease is lease and fail_release:
            raise OSError(errno.EIO, "raw-only lifecycle release")
        real_release(owned_lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_raw_release,
    )
    interruption = KeyboardInterrupt("raw-only lifecycle acquisition")

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_return(
            atomic_module._QuiescentDirectoryLifecycleLease.acquire_nonblocking,
            reclaimer.retry,
            call_occurrence=2,
            predicate=lambda frame, result: (
                frame.f_locals.get("self") is lease and result is True
            ),
            error=interruption,
        )

    assert caught.value is interruption
    owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert len(owners) == 1
    attempt = owners[0]
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert tuple(name for name in dir(attempt) if not name.startswith("_")) == (
        "close",
        "closed",
    )
    assert lease.current_transition() is None
    assert lease.owned_by_current_thread()
    assert lease._outer_call is attempt._outer_call
    assert lease._outer_call.closed

    with pytest.raises(RuntimeError, match="settlement token is not active"):
        attempt._close_borrowed(object())
    with pytest.raises(RuntimeError, match="settlement token is not active"):
        attempt._close_borrowed(attempt._outer_call._token)

    cross_thread_failures: list[BaseException] = []

    def close_in_worker() -> None:
        try:
            attempt.close()
        except BaseException as error:  # noqa: B036 - exact origin rejection
            cross_thread_failures.append(error)

    worker = threading.Thread(target=close_in_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(cross_thread_failures) == 1
    assert isinstance(cross_thread_failures[0], RuntimeError)
    assert "lifecycle transition is already active" in str(cross_thread_failures[0])
    assert (other / "payload.txt").read_text(encoding="utf-8") == "preserve"

    fail_release = False
    attempt.close()
    assert attempt.closed
    assert not lease.owned_by_current_thread()
    assert lease.current_transition() is None
    assert reclaimer.retry() is False
    assert reclaimer.reclaim_child(other.name) is True
    reclaimer.close()


def test_quiescent_reclaimer_suspended_outer_call_is_directly_recoverable(
    tmp_path: Path,
) -> None:
    function = atomic_module._QuiescentDirectoryOuterCall.close
    source_offsets = _source_statement_opcode_offsets(function, "self._guard.close()")
    observed_parent, observed_identity = _private_reclaimer_parent(
        tmp_path,
        name="outer-close-observe",
    )
    observed_reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        observed_parent,
        expected_parent_identity=observed_identity,
    )
    observed_lease = observed_reclaimer._lifecycle_lease
    observed_offsets = _observed_opcode_offsets(
        function,
        observed_reclaimer.retry,
        predicate=lambda values: values.get("self") is observed_lease._outer_call,
    )
    observed_reclaimer.close()
    offsets = tuple(offset for offset in source_offsets if offset in observed_offsets)
    assert offsets

    for index, opcode_offset in enumerate(offsets):
        parent, parent_identity = _private_reclaimer_parent(
            tmp_path,
            name=f"outer-close-{index}",
        )
        reclaimer = atomic_module.QuiescentDirectoryReclaimer(
            parent,
            expected_parent_identity=parent_identity,
        )
        lease = reclaimer._lifecycle_lease
        interruption = KeyboardInterrupt(f"outer close {opcode_offset}")

        with pytest.raises(KeyboardInterrupt) as caught:
            _call_with_interrupt_at_opcode(
                function,
                opcode_offset,
                reclaimer.retry,
                predicate=lambda values, target_lease=lease: (
                    values.get("self") is target_lease._outer_call
                ),
                error=interruption,
            )

        assert caught.value is interruption
        outer_call = lease._outer_call
        assert type(outer_call) is atomic_module._QuiescentDirectoryOuterCall
        assert tuple(name for name in dir(outer_call) if not name.startswith("_")) == (
            "close",
            "closed",
        )
        if outer_call._guard.gi_frame is not None:
            assert not outer_call._guard.gi_running
            owners = BaseException.__getattribute__(
                caught.value,
                "publication_cleanup_owners",
            )
            assert owners == (outer_call,)
            for owner in owners:
                owner.close()
        assert outer_call.closed
        assert reclaimer.retry() is False
        _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
        reclaimer.close()


def test_quiescent_reclaimer_next_result_close_handoff_has_direct_owner(
    tmp_path: Path,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    post_handoff = parent / "post-handoff"
    _write_tree(post_handoff, "payload.txt", "post-handoff")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    lease = reclaimer._lifecycle_lease
    interruption = KeyboardInterrupt("outer next result handoff")

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._run_quiescent_directory_outer_call,
            "outer_call.close()",
            reclaimer.retry,
            predicate=lambda values: (
                values.get("lease") is lease
                and values["outer_call"]._guard.gi_frame is not None
                and not values["outer_call"]._guard.gi_running
            ),
            error=interruption,
        )

    assert caught.value is interruption
    outer_call = lease._outer_call
    assert type(outer_call) is atomic_module._QuiescentDirectoryOuterCall
    assert outer_call._guard.gi_frame is not None
    assert tuple(name for name in dir(outer_call) if not name.startswith("_")) == (
        "close",
        "closed",
    )
    # A suspended owner is handed out only after its lifecycle callback and
    # operation state have fully settled. Closing it can therefore admit the
    # next public call without exposing an in-flight transition.
    assert lease.current_transition() is None
    assert not lease.owned_by_current_thread()
    assert reclaimer._active is None
    owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert owners == (outer_call,)
    for owner in owners:
        owner.close()
    assert outer_call.closed
    assert reclaimer.retry() is False
    assert reclaimer.reclaim_child(post_handoff.name) is True
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


@pytest.mark.parametrize("prior_kind", ["explicit", "implicit"])
@pytest.mark.parametrize("metadata_shape", ["empty-read", "refuse-read"])
def test_quiescent_reclaimer_hostile_final_close_uses_builtin_owner_carrier(
    tmp_path: Path,
    prior_kind: str,
    metadata_shape: str,
) -> None:
    before_fds = (
        len(os.listdir("/proc/self/fd")) if sys.platform.startswith("linux") else 0
    )
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    post_handoff = parent / "post-handoff"
    _write_tree(post_handoff, "payload.txt", "post-handoff")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    lease = reclaimer._lifecycle_lease
    transfer_attempts: list[object] = []
    metadata_errors: list[RuntimeError] = []
    link_dispatches: list[str] = []

    class CleanupOwners:
        def __get__(self, _instance: object, _owner: object) -> tuple[()]:
            if metadata_shape == "refuse-read":
                metadata_error = RuntimeError("refuse final-close owner read")
                metadata_errors.append(metadata_error)
                raise metadata_error
            return ()

        def __set__(self, _instance: object, value: object) -> None:
            transfer_attempts.append(value)
            metadata_error = RuntimeError("refuse final-close owner metadata")
            metadata_errors.append(metadata_error)
            raise metadata_error

    class FinalCloseFailure(KeyboardInterrupt):
        publication_cleanup_owners = CleanupOwners()

        @property
        def __cause__(self) -> BaseException | None:
            link_dispatches.append("cause-get")
            raise RuntimeError("hostile cause getter")

        @__cause__.setter
        def __cause__(self, _value: BaseException | None) -> None:
            link_dispatches.append("cause-set")
            raise RuntimeError("hostile cause setter")

        @property
        def __context__(self) -> BaseException | None:
            link_dispatches.append("context-get")
            raise RuntimeError("hostile context getter")

        @__context__.setter
        def __context__(self, _value: BaseException | None) -> None:
            link_dispatches.append("context-set")
            raise RuntimeError("hostile context setter")

    prior = LookupError("final-close prior cause")
    primary = FinalCloseFailure("hostile final-close interruption")
    if prior_kind == "explicit":
        vars(BaseException)["__cause__"].__set__(primary, prior)
    else:
        vars(BaseException)["__context__"].__set__(primary, prior)

    with pytest.raises(FinalCloseFailure) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._run_quiescent_directory_outer_call,
            "outer_call.close()",
            reclaimer.retry,
            predicate=lambda values: (
                values.get("lease") is lease
                and values["outer_call"]._guard.gi_frame is not None
                and not values["outer_call"]._guard.gi_running
            ),
            error=primary,
        )

    assert caught.value is primary
    if metadata_shape == "empty-read":
        assert transfer_attempts
    else:
        assert transfer_attempts == []
    assert metadata_errors
    outer_call = lease._outer_call
    assert type(outer_call) is atomic_module._QuiescentDirectoryOuterCall
    assert outer_call._guard.gi_frame is not None
    assert not outer_call._guard.gi_running
    assert lease.current_transition() is None
    assert not lease.owned_by_current_thread()
    assert reclaimer._active is None
    assert _exception_chain_contains(primary, prior)

    carrier = vars(BaseException)["__cause__"].__get__(primary, type(primary))
    assert type(carrier) is RuntimeError
    owners = BaseException.__getattribute__(
        carrier,
        "publication_cleanup_owners",
    )
    assert owners == (outer_call,)
    assert carrier.args[1] is owners
    assert tuple(name for name in dir(outer_call) if not name.startswith("_")) == (
        "close",
        "closed",
    )
    assert _exception_link_graph_is_acyclic(primary)
    recovery_graph = _bounded_exception_recovery_graph(primary)
    assert any(candidate is prior for candidate in recovery_graph)
    assert any(candidate is outer_call for candidate in recovery_graph)
    inner_carriers = tuple(
        candidate
        for candidate in recovery_graph
        if type(candidate) is RuntimeError
        and candidate is not carrier
        and any(
            owner is outer_call
            for owner in getattr(candidate, "publication_cleanup_owners", ())
        )
    )
    assert inner_carriers
    retained_metadata = tuple(
        metadata_error
        for metadata_error in metadata_errors
        if any(candidate is metadata_error for candidate in recovery_graph)
    )
    assert retained_metadata
    inert_wrappers = tuple(
        candidate
        for candidate in recovery_graph
        if type(candidate) is RuntimeError
        and any(
            argument is metadata_error
            for argument in candidate.args
            for metadata_error in retained_metadata
        )
    )
    assert inert_wrappers
    assert all(wrapper.__cause__ is None for wrapper in inert_wrappers)
    assert all(wrapper.__context__ is None for wrapper in inert_wrappers)
    assert link_dispatches == []

    for callback in (
        reclaimer.retry,
        lambda: reclaimer.reclaim_child(post_handoff.name),
        reclaimer.close,
    ):
        with pytest.raises(RuntimeError, match="lifecycle transition is already"):
            callback()
    assert (post_handoff / "payload.txt").read_text(encoding="utf-8") == (
        "post-handoff"
    )

    for owner in owners:
        owner.close()
    assert outer_call.closed
    assert reclaimer.retry() is False
    assert reclaimer.reclaim_child(post_handoff.name) is True
    reclaimer.close()
    assert reclaimer.closed
    if sys.platform.startswith("linux"):
        assert len(os.listdir("/proc/self/fd")) <= before_fds


def test_quiescent_reclaimer_metadata_diagnostic_interruption_stays_acyclic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = atomic_module._QuiescentCleanupHandoffBoundary.__exit__
    offsets = _source_statement_opcode_offsets(
        function,
        "post_transfer_cause = _publication_exception_cause(active_error)",
    )
    real_outer_close = atomic_module._QuiescentDirectoryOuterCall.close

    for index, opcode_offset in enumerate(offsets):
        with monkeypatch.context() as patch:
            before_fds = (
                len(os.listdir("/proc/self/fd"))
                if sys.platform.startswith("linux")
                else 0
            )
            parent, parent_identity = _private_reclaimer_parent(
                tmp_path,
                name=f"metadata-diagnostic-{index}",
            )
            pending = parent / "pending"
            _write_tree(pending, "payload.txt", "pending")
            reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )
            lease = reclaimer._lifecycle_lease

            class CleanupOwners:
                def __get__(self, _instance: object, _owner: object) -> object:
                    raise RuntimeError("refuse diagnostic owner read")

                def __set__(self, _instance: object, _value: object) -> None:
                    raise RuntimeError("refuse diagnostic owner write")

            class FinalCloseFailure(KeyboardInterrupt):
                publication_cleanup_owners = CleanupOwners()

            prior = LookupError("metadata diagnostic prior")
            primary = FinalCloseFailure("metadata diagnostic primary")
            primary.__context__ = prior
            interruption = KeyboardInterrupt(
                f"metadata diagnostic interruption {opcode_offset}"
            )
            fail_outer_close = True

            def fail_suspended_outer_close(
                outer_call: atomic_module._QuiescentDirectoryOuterCall,
            ) -> None:
                if (
                    fail_outer_close  # noqa: B023
                    and outer_call is lease._outer_call  # noqa: B023
                    and outer_call._guard.gi_frame is not None
                    and not outer_call._guard.gi_running
                ):
                    raise primary  # noqa: B023
                real_outer_close(outer_call)

            patch.setattr(
                atomic_module._QuiescentDirectoryOuterCall,
                "close",
                fail_suspended_outer_close,
            )

            with pytest.raises(KeyboardInterrupt) as caught:
                _call_with_interrupt_at_opcode(
                    function,
                    opcode_offset,
                    reclaimer.retry,
                    predicate=lambda values: values.get("active_error")
                    is primary,  # noqa: B023
                    error=interruption,
                )

            assert caught.value is interruption
            assert _exception_link_graph_is_acyclic(interruption)
            assert _exception_link_graph_is_acyclic(primary)
            recovery_graph = _bounded_exception_recovery_graph(interruption)
            assert any(candidate is primary for candidate in recovery_graph)
            assert any(candidate is prior for candidate in recovery_graph)
            outer_call = lease._outer_call
            assert type(outer_call) is atomic_module._QuiescentDirectoryOuterCall
            owners = BaseException.__getattribute__(
                interruption,
                "publication_cleanup_owners",
            )
            assert owners == (outer_call,)
            for callback in (
                reclaimer.retry,
                lambda current=reclaimer, child_name=pending.name: (
                    current.reclaim_child(child_name)
                ),
                reclaimer.close,
            ):
                with pytest.raises(
                    RuntimeError,
                    match="lifecycle transition is already",
                ):
                    callback()
            assert (pending / "payload.txt").read_text(encoding="utf-8") == "pending"

            fail_outer_close = False
            outer_call.close()
            assert reclaimer.retry() is False
            assert reclaimer.reclaim_child(pending.name) is True
            reclaimer.close()
            assert reclaimer.closed
            if sys.platform.startswith("linux"):
                assert len(os.listdir("/proc/self/fd")) <= before_fds


def test_quiescent_reclaimer_serializes_two_threads_during_active_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    other = parent / "attempt-b"
    _write_tree(attempt, "payload.txt", "payload")
    _write_tree(other, "foreign.txt", "preserve")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    entered = threading.Event()
    release = threading.Event()
    real_unlink = atomic_module.os.unlink
    results: list[bool] = []
    failures: list[BaseException] = []

    def blocked_unlink(path: object, *, dir_fd: int | None = None) -> None:
        if path == "payload.txt" and not entered.is_set():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("active-removal test did not release worker")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(atomic_module.os, "unlink", blocked_unlink)

    def reclaim_in_worker() -> None:
        try:
            results.append(reclaimer.reclaim_child(attempt.name))
        except BaseException as error:  # noqa: B036 - report thread failure
            failures.append(error)

    worker = threading.Thread(target=reclaim_in_worker)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        active = reclaimer._active
        assert active is not None
        with pytest.raises(RuntimeError, match="lifecycle transition is already"):
            reclaimer.reclaim_child(other.name)
        with pytest.raises(RuntimeError, match="lifecycle transition is already"):
            reclaimer.close()
        assert reclaimer._active is active
        assert (other / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert failures == []
    assert results == [True]
    assert reclaimer.reclaim_child(other.name) is True
    reclaimer.close()


def test_quiescent_reclaimer_releases_lease_after_acquire_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    interruption = KeyboardInterrupt("lifecycle acquire result interruption")
    real_acquire = atomic_module._QuiescentDirectoryLifecycleLease.acquire_nonblocking
    interrupted = False

    def interrupt_after_acquire(lease: object) -> bool:
        nonlocal interrupted
        acquired = real_acquire(lease)
        if lease is reclaimer._lifecycle_lease and acquired and not interrupted:
            interrupted = True
            raise interruption
        return acquired

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "acquire_nonblocking",
        interrupt_after_acquire,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        reclaimer.reclaim_child(attempt.name)

    assert caught.value is interruption
    assert reclaimer._active is None
    assert (attempt / "payload.txt").read_text(encoding="utf-8") == "payload"
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    assert reclaimer.reclaim_child(attempt.name) is True
    reclaimer.close()


def test_quiescent_reclaimer_reconciles_lease_release_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    interruption = KeyboardInterrupt("post-lifecycle-release interruption")
    interrupted = False

    def interrupt_after_release(lease: object) -> None:
        nonlocal interrupted
        real_release(lease)
        if lease is reclaimer._lifecycle_lease and not interrupted:
            interrupted = True
            raise interruption

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        interrupt_after_release,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        reclaimer.reclaim_child(attempt.name)

    assert caught.value is interruption
    assert not attempt.exists()
    assert reclaimer._active is None
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


@pytest.mark.parametrize("caller", ["recursive", "thread"], ids=["recursive", "thread"])
def test_quiescent_reclaimer_retains_logical_lease_through_raw_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    other = parent / "attempt-b"
    _write_tree(attempt, "payload.txt", "payload")
    _write_tree(other, "foreign.txt", "preserve")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    nested_results: list[bool] = []
    nested_failures: list[BaseException] = []
    injected = False

    def try_nested_reclaim() -> None:
        try:
            nested_results.append(reclaimer.reclaim_child(other.name))
        except BaseException as error:  # noqa: B036 - inspect exact rejection
            nested_failures.append(error)

    def release_then_reenter(lease: object) -> None:
        nonlocal injected
        real_release(lease)
        if lease is reclaimer._lifecycle_lease and not injected:
            injected = True
            if caller == "recursive":
                try_nested_reclaim()
            else:
                worker = threading.Thread(target=try_nested_reclaim)
                worker.start()
                worker.join(timeout=5)
                assert not worker.is_alive()

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        release_then_reenter,
    )

    assert reclaimer.reclaim_child(attempt.name) is True
    assert injected
    assert nested_results == []
    assert len(nested_failures) == 1
    assert isinstance(nested_failures[0], RuntimeError)
    assert "lifecycle transition is already active" in str(nested_failures[0])
    assert (other / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert not reclaimer._lifecycle_lease.transition_active()

    assert reclaimer.reclaim_child(other.name) is True
    reclaimer.close()


def test_quiescent_reclaimer_closed_waits_for_logical_lease_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    observations: list[bool] = []

    def observe_raw_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and not observations:
            observations.append(reclaimer.closed)
            real_release(lease)
            observations.append(reclaimer.closed)
            return
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        observe_raw_release,
    )

    reclaimer.close()

    assert observations == [False, False]
    assert reclaimer.closed
    # Idempotent close runs under a fresh logical transition; its internal
    # closed probe is deliberately false until that new transition settles.
    reclaimer.close()
    assert reclaimer.closed


@pytest.mark.parametrize(
    ("function_name", "source_fragment", "on_return"),
    [
        ("attempt", "self._lease.clear_transition(self)", False),
        ("lease", "self._current_transition = None", False),
        ("lease", "self._current_transition = None", True),
    ],
    ids=["attempt-before-clear", "clear-before-store", "clear-return"],
)
def test_quiescent_reclaimer_recovers_interrupted_logical_lease_clear(
    tmp_path: Path,
    function_name: str,
    source_fragment: str,
    on_return: bool,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    interruption = KeyboardInterrupt(f"logical lease clear {source_fragment}")
    function = (
        atomic_module._QuiescentDirectoryLifecycleAttempt._close_unprotected
        if function_name == "attempt"
        else atomic_module._QuiescentDirectoryLifecycleLease.clear_transition
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        if on_return:
            _call_with_interrupt_on_return(
                function,
                lambda: reclaimer.reclaim_child("missing-attempt"),
                predicate=lambda frame, _result: (
                    frame.f_locals.get("attempt") is not None
                    and frame.f_locals.get("self") is reclaimer._lifecycle_lease
                ),
                error=interruption,
            )
        else:
            _call_with_interrupt_on_source_line(
                function,
                source_fragment,
                lambda: reclaimer.reclaim_child("missing-attempt"),
                predicate=lambda values: (
                    values.get("attempt") is not None
                    if function_name == "lease"
                    else values.get("self") is not None
                    and values["self"]._lease is reclaimer._lifecycle_lease
                ),
                error=interruption,
            )

    assert caught.value is interruption
    assert not reclaimer._lifecycle_lease.transition_active()
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


@pytest.mark.parametrize(
    ("source_fragment", "occurrence"),
    [
        ("locally_unwinding = context_error is not None", 1),
        ("locally_unwinding = context_error is not None", 2),
        ("_run_ordered_actions(failures)", 1),
        ("_run_ordered_actions(failures)", 2),
        ("_prune_publication_cleanup_owners(failures.primary_error)", 1),
        ("_prune_publication_cleanup_owners(failures.primary_error)", 2),
    ],
    ids=[
        "inner-entry",
        "outer-entry",
        "inner-call",
        "outer-call",
        "inner-return",
        "outer-return",
    ],
)
@pytest.mark.parametrize(
    ("cleanup_label", "tree_kind"),
    [
        ("quiescent directory traversal cleanup also failed", "traversal"),
        ("quiescent directory child cleanup also failed", "child"),
    ],
    ids=["traversal-owner", "child-owner"],
)
def test_quiescent_reclaimer_auto_closes_local_descriptor_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
    occurrence: int,
    cleanup_label: str,
    tree_kind: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    nested = attempt / "nested"
    nested.mkdir(parents=True)
    if tree_kind == "traversal":
        (nested / "payload.txt").write_text("payload", encoding="utf-8")
    before_fds = (
        len(os.listdir("/proc/self/fd")) if sys.platform.startswith("linux") else 0
    )
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_scope = atomic_module._run_quiescent_directory_resource_scope
    real_close = atomic_module._PosixResourceOwner.close
    local_owners: list[atomic_module._PosixResourceOwner] = []
    opened_descriptors: list[int] = []

    def capture_scope(
        resources: atomic_module._PosixResourceOwner,
        callback: object,
        *,
        label: str,
    ) -> object:
        if label == cleanup_label and not local_owners:
            local_owners.append(resources)
        return real_scope(resources, callback, label=label)

    def capture_close(resources: atomic_module._PosixResourceOwner) -> None:
        if local_owners and resources is local_owners[0] and not opened_descriptors:
            opened_descriptors.extend(
                record.descriptor
                for record in resources._records
                if record.descriptor >= 0
            )
        real_close(resources)

    def selected_cleanup(values: dict[str, object]) -> bool:
        actions = values.get("cleanup_actions")
        return bool(
            type(actions) is tuple
            and len(actions) == 1
            and isinstance(actions[0], atomic_module._OrderedAction)
            and actions[0].label == cleanup_label
        )

    monkeypatch.setattr(
        atomic_module,
        "_run_quiescent_directory_resource_scope",
        capture_scope,
    )
    monkeypatch.setattr(atomic_module._PosixResourceOwner, "close", capture_close)
    interruption = KeyboardInterrupt(
        f"{tree_kind} resource cleanup {source_fragment} {occurrence}"
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._run_context_with_cleanup_actions.__wrapped__,
            source_fragment,
            lambda: reclaimer.reclaim_child(attempt.name),
            predicate=selected_cleanup,
            occurrence=occurrence,
            error=interruption,
        )

    assert caught.value is interruption
    assert len(local_owners) == 1
    assert opened_descriptors
    assert local_owners[0].closed
    for descriptor in opened_descriptors:
        _assert_descriptor_closed(descriptor)
    assert reclaimer in caught.value.publication_cleanup_owners

    _assert_quiescent_reclaimer_publicly_retryable(reclaimer, first_result=True)
    reclaimer.close()
    if sys.platform.startswith("linux"):
        assert len(os.listdir("/proc/self/fd")) <= before_fds


@pytest.mark.parametrize(
    "source_fragment",
    [
        "locally_unwinding = context_error is not None",
        "_run_ordered_actions(failures)",
        "_prune_publication_cleanup_owners(failures.primary_error)",
    ],
    ids=["finalizer-entry", "finalizer-call", "finalizer-return"],
)
@pytest.mark.parametrize(
    ("cleanup_label", "tree_kind"),
    [
        ("quiescent directory traversal cleanup also failed", "traversal"),
        ("quiescent directory child cleanup also failed", "child"),
    ],
    ids=["traversal-owner", "child-owner"],
)
def test_quiescent_reclaimer_preserves_entry_error_while_closing_local_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
    cleanup_label: str,
    tree_kind: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    nested = attempt / "nested"
    nested.mkdir(parents=True)
    if tree_kind == "traversal":
        (nested / "payload.txt").write_text("payload", encoding="utf-8")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_scope = atomic_module._run_quiescent_directory_resource_scope
    real_unlink = atomic_module.os.unlink
    real_empty = atomic_module._require_quiescent_directory_empty
    natural_failure = OSError(errno.EIO, f"natural {tree_kind} entry failure")
    interruption = KeyboardInterrupt(f"{tree_kind} cleanup finalizer interruption")
    local_owners: list[atomic_module._PosixResourceOwner] = []
    fail_operation = True

    def capture_scope(
        resources: atomic_module._PosixResourceOwner,
        callback: object,
        *,
        label: str,
    ) -> object:
        if label == cleanup_label and not local_owners:
            local_owners.append(resources)
        return real_scope(resources, callback, label=label)

    def fail_unlink(path: object, *, dir_fd: int | None = None) -> None:
        if fail_operation and path == "payload.txt":
            raise natural_failure
        real_unlink(path, dir_fd=dir_fd)

    def fail_child_empty(descriptor: int, *, label: str) -> None:
        if fail_operation and "untracked entry" in label:
            raise natural_failure
        real_empty(descriptor, label=label)

    def selected_cleanup(values: dict[str, object]) -> bool:
        actions = values.get("cleanup_actions")
        return bool(
            type(actions) is tuple
            and len(actions) == 1
            and isinstance(actions[0], atomic_module._OrderedAction)
            and actions[0].label == cleanup_label
        )

    monkeypatch.setattr(
        atomic_module,
        "_run_quiescent_directory_resource_scope",
        capture_scope,
    )
    if tree_kind == "traversal":
        monkeypatch.setattr(atomic_module.os, "unlink", fail_unlink)
    else:
        monkeypatch.setattr(
            atomic_module,
            "_require_quiescent_directory_empty",
            fail_child_empty,
        )

    with pytest.raises(OSError) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._run_context_with_cleanup_actions.__wrapped__,
            source_fragment,
            lambda: reclaimer.reclaim_child(attempt.name),
            predicate=selected_cleanup,
            error=interruption,
        )

    assert caught.value is natural_failure
    diagnostic_notes = (
        *getattr(caught.value, "__notes__", ()),
        *getattr(caught.value, "_codenib_cleanup_notes", ()),
    )
    assert caught.value.__cause__ is interruption or any(
        str(interruption) in note for note in diagnostic_notes
    )
    assert _exception_link_graph_is_acyclic(caught.value)
    assert len(local_owners) == 1
    assert local_owners[0].closed
    assert reclaimer in caught.value.publication_cleanup_owners

    fail_operation = False
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer, first_result=True)
    reclaimer.close()


@pytest.mark.parametrize(
    ("cleanup_label", "tree_kind"),
    [
        ("quiescent directory traversal cleanup also failed", "traversal"),
        ("quiescent directory child cleanup also failed", "child"),
    ],
    ids=["traversal-owner", "child-owner"],
)
def test_quiescent_reclaimer_transfers_persistently_unclosed_local_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_label: str,
    tree_kind: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    nested = attempt / "nested"
    nested.mkdir(parents=True)
    if tree_kind == "traversal":
        (nested / "payload.txt").write_text("payload", encoding="utf-8")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_scope = atomic_module._run_quiescent_directory_resource_scope
    real_close = atomic_module._PosixResourceOwner.close
    close_error = OSError(errno.EIO, f"persistent {tree_kind} descriptor close")
    local_owners: list[atomic_module._PosixResourceOwner] = []
    opened_descriptors: list[int] = []
    fail_close = True

    def capture_scope(
        resources: atomic_module._PosixResourceOwner,
        callback: object,
        *,
        label: str,
    ) -> object:
        if label == cleanup_label and not local_owners:
            local_owners.append(resources)
        return real_scope(resources, callback, label=label)

    def fail_local_close(resources: atomic_module._PosixResourceOwner) -> None:
        if local_owners and resources is local_owners[0] and fail_close:
            if not opened_descriptors:
                opened_descriptors.extend(
                    record.descriptor
                    for record in resources._records
                    if record.descriptor >= 0
                )
            raise close_error
        real_close(resources)

    monkeypatch.setattr(
        atomic_module,
        "_run_quiescent_directory_resource_scope",
        capture_scope,
    )
    monkeypatch.setattr(atomic_module._PosixResourceOwner, "close", fail_local_close)

    with pytest.raises(OSError) as caught:
        reclaimer.reclaim_child(attempt.name)

    assert caught.value is close_error
    assert len(local_owners) == 1
    owner = local_owners[0]
    assert opened_descriptors
    assert not owner.closed
    owners = caught.value.publication_cleanup_owners
    assert sum(candidate is owner for candidate in owners) == 1
    assert sum(candidate is reclaimer for candidate in owners) == 1

    fail_close = False
    owner.close()
    assert owner.closed
    for descriptor in opened_descriptors:
        _assert_descriptor_closed(descriptor)
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer, first_result=True)
    reclaimer.close()


@pytest.mark.parametrize(
    "source_fragment",
    ["merged = inherited if not existing else existing", "BaseException.__setattr__("],
    ids=["merge-build", "tuple-store"],
)
def test_quiescent_reclaimer_durably_transfers_local_owner_to_entry_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    nested = attempt / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("payload", encoding="utf-8")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_scope = atomic_module._run_quiescent_directory_resource_scope
    real_close = atomic_module._PosixResourceOwner.close
    real_unlink = atomic_module.os.unlink
    natural_failure = ValueError("natural traversal entry failure")
    cleanup_failure = OSError(errno.EIO, "persistent traversal owner close")
    interruption = KeyboardInterrupt(f"local owner {source_fragment}")
    local_owners: list[atomic_module._PosixResourceOwner] = []
    opened_descriptors: list[int] = []
    fail_close = True
    fail_operation = True

    def capture_scope(
        resources: atomic_module._PosixResourceOwner,
        callback: object,
        *,
        label: str,
    ) -> object:
        if (
            label == "quiescent directory traversal cleanup also failed"
            and not local_owners
        ):
            local_owners.append(resources)
        return real_scope(resources, callback, label=label)

    def fail_unlink(path: object, *, dir_fd: int | None = None) -> None:
        if fail_operation:
            raise natural_failure
        real_unlink(path, dir_fd=dir_fd)

    def fail_local_close(resources: atomic_module._PosixResourceOwner) -> None:
        if local_owners and resources is local_owners[0] and fail_close:
            if not opened_descriptors:
                opened_descriptors.extend(
                    record.descriptor
                    for record in resources._records
                    if record.descriptor >= 0
                )
            raise cleanup_failure
        real_close(resources)

    monkeypatch.setattr(
        atomic_module,
        "_run_quiescent_directory_resource_scope",
        capture_scope,
    )
    monkeypatch.setattr(atomic_module.os, "unlink", fail_unlink)
    monkeypatch.setattr(atomic_module._PosixResourceOwner, "close", fail_local_close)

    with pytest.raises(ValueError) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._merge_publication_cleanup_owner_tuple,
            source_fragment,
            lambda: reclaimer.reclaim_child(attempt.name),
            predicate=lambda values: values.get("failure") is natural_failure,
            error=interruption,
        )

    assert caught.value is natural_failure
    assert len(local_owners) == 1
    owner = local_owners[0]
    assert not owner.closed
    owners = caught.value.publication_cleanup_owners
    assert sum(candidate is owner for candidate in owners) == 1
    assert sum(candidate is reclaimer for candidate in owners) == 1

    fail_close = False
    fail_operation = False
    for cleanup_owner in owners:
        cleanup_owner.close()
    assert owner.closed
    for descriptor in opened_descriptors:
        _assert_descriptor_closed(descriptor)
    assert reclaimer.closed


@pytest.mark.parametrize(
    "source_fragment",
    ["merged = inherited if not existing else existing", "BaseException.__setattr__("],
    ids=["merge-build", "tuple-store"],
)
def test_quiescent_reclaimer_durably_transfers_lifecycle_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    natural_failure = ValueError("natural lifecycle callback failure")
    cleanup_failure = OSError(errno.EIO, "persistent lifecycle release failure")
    interruption = KeyboardInterrupt(f"lifecycle owner {source_fragment}")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_reclaim(_reclaimer: object, _child_name: str) -> bool:
        raise natural_failure

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_reclaim_child",
        fail_reclaim,
    )
    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(ValueError) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._merge_publication_cleanup_owner_tuple,
            source_fragment,
            lambda: reclaimer.reclaim_child("attempt-a"),
            predicate=lambda values: values.get("failure") is natural_failure,
            error=interruption,
        )

    assert caught.value is natural_failure
    owners = caught.value.publication_cleanup_owners
    attempts = tuple(
        owner
        for owner in owners
        if isinstance(owner, atomic_module._QuiescentDirectoryLifecycleAttempt)
    )
    assert len(attempts) == 1
    assert not attempts[0].closed
    assert reclaimer._lifecycle_lease.transition_active()

    fail_release = False
    for cleanup_owner in owners:
        cleanup_owner.close()
    assert attempts[0].closed
    assert not reclaimer._lifecycle_lease.transition_active()
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


@pytest.mark.parametrize(
    "source_fragment",
    ["merged = inherited if not existing else existing", "BaseException.__setattr__("],
    ids=["merge-build", "tuple-store"],
)
def test_quiescent_reclaimer_exit_durably_transfers_lifecycle_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    body_failure = ValueError("reclaimer context body failure")
    cleanup_failure = OSError(errno.EIO, "persistent context release failure")
    interruption = KeyboardInterrupt(f"context owner {source_fragment}")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(ValueError) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._merge_publication_cleanup_owner_tuple,
            source_fragment,
            lambda: _raise_from_quiescent_reclaimer_context(
                reclaimer,
                body_failure,
            ),
            predicate=lambda values: values.get("failure") is body_failure,
            error=interruption,
        )

    assert caught.value is body_failure
    owners = caught.value.publication_cleanup_owners
    attempts = tuple(
        owner
        for owner in owners
        if isinstance(owner, atomic_module._QuiescentDirectoryLifecycleAttempt)
    )
    assert len(attempts) == 1
    assert sum(owner is reclaimer for owner in owners) == 1
    assert not reclaimer.closed

    fail_release = False
    for cleanup_owner in owners:
        cleanup_owner.close()
    assert attempts[0].closed
    assert reclaimer.closed


def test_quiescent_reclaimer_lifecycle_uses_carrier_for_hostile_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    prior_cause = LookupError("callback prior cause")
    natural_failure = _HostileCleanupPrimary("hostile lifecycle callback failure")
    natural_failure.__cause__ = prior_cause
    cleanup_failure = OSError(errno.EIO, "persistent lifecycle release failure")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_reclaim(_reclaimer: object, _child_name: str) -> bool:
        raise natural_failure

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_reclaim_child",
        fail_reclaim,
    )
    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(_HostileCleanupPrimary) as caught:
        reclaimer.reclaim_child("attempt-a")

    assert caught.value is natural_failure
    carrier = caught.value.__cause__
    assert type(carrier) is RuntimeError
    owners = carrier.publication_cleanup_owners
    assert carrier.args[1] is owners
    assert carrier.args[2] is cleanup_failure
    assert carrier.args[3] is not natural_failure
    diagnostic_wrapper = carrier.__cause__
    assert type(diagnostic_wrapper) is RuntimeError
    assert diagnostic_wrapper.__cause__ is None
    assert diagnostic_wrapper.__context__ is None
    assert _exception_link_graph_is_acyclic(natural_failure)
    recovery_graph = _bounded_exception_recovery_graph(natural_failure)
    assert any(candidate is cleanup_failure for candidate in recovery_graph)
    assert any(candidate is prior_cause for candidate in recovery_graph)
    assert _exception_chain_contains(carrier, prior_cause)
    assert len(owners) == 1
    attempt = owners[0]
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert not attempt.closed

    fail_release = False
    attempt.close()
    assert attempt.closed
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


@pytest.mark.parametrize(
    "hostile_error",
    [False, True],
    ids=["hostile-primary", "hostile-secondary"],
)
def test_quiescent_reclaimer_exit_uses_carrier_for_hostile_owner_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hostile_error: bool,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    prior_cause = LookupError("body prior cause")
    body_failure: BaseException = ValueError("context body failure")
    cleanup_failure: BaseException = _HostileCleanupSecondary(
        errno.EIO,
        "persistent context release failure",
    )
    if not hostile_error:
        body_failure = _HostileCleanupPrimary("hostile context body failure")
        cleanup_failure = OSError(errno.EIO, "persistent context release failure")
    body_failure.__cause__ = prior_cause
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(type(body_failure)) as caught:
        _raise_from_quiescent_reclaimer_context(reclaimer, body_failure)

    assert caught.value is body_failure
    carrier = caught.value.__cause__
    assert type(carrier) is RuntimeError
    owners = carrier.publication_cleanup_owners
    assert carrier.args[1] is owners
    assert carrier.args[2] is cleanup_failure
    assert carrier.args[3] is prior_cause
    diagnostic_wrapper = carrier.__cause__
    assert type(diagnostic_wrapper) is RuntimeError
    assert diagnostic_wrapper.__cause__ is None
    assert diagnostic_wrapper.__context__ is None
    assert any(argument is cleanup_failure for argument in diagnostic_wrapper.args)
    assert carrier.__context__ is prior_cause
    assert _exception_link_graph_is_acyclic(body_failure)
    recovery_graph = _bounded_exception_recovery_graph(body_failure)
    assert any(candidate is cleanup_failure for candidate in recovery_graph)
    assert any(candidate is prior_cause for candidate in recovery_graph)
    assert len(owners) == 2
    attempt, retained_reclaimer = owners
    assert any(candidate is attempt for candidate in recovery_graph)
    assert any(candidate is reclaimer for candidate in recovery_graph)
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert retained_reclaimer is reclaimer
    assert not attempt.closed
    assert not reclaimer.closed

    fail_release = False
    attempt.close()
    assert attempt.closed
    close_failures: list[BaseException] = []

    def close_in_worker() -> None:
        try:
            reclaimer.close()
        except BaseException as error:  # noqa: B036 - report thread failure
            close_failures.append(error)

    worker = threading.Thread(target=close_in_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert close_failures == []
    assert reclaimer.closed


@pytest.mark.parametrize(
    "hostile_primary",
    [False, True],
    ids=["transferred-primary", "carrier-primary"],
)
def test_quiescent_reclaimer_resource_tail_interrupt_owns_public_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hostile_primary: bool,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt_path = parent / "attempt-a"
    nested = attempt_path / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("payload", encoding="utf-8")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_scope = atomic_module._run_quiescent_directory_resource_scope
    real_close = atomic_module._PosixResourceOwner.close
    real_unlink = atomic_module.os.unlink
    natural_failure: ValueError = ValueError("natural resource tail failure")
    source_fragment = "raise operation_error"
    if hostile_primary:
        natural_failure = _HostileCleanupPrimary("hostile resource tail failure")
        source_fragment = "raise operation_error from delivery_carrier"
    cleanup_failure = OSError(errno.EIO, "persistent resource tail cleanup")
    interruption = KeyboardInterrupt("resource post-inherit tail")
    local_owners: list[atomic_module._PosixResourceOwner] = []
    opened_descriptors: list[int] = []
    fail_close = True
    fail_operation = True

    def capture_scope(
        resources: atomic_module._PosixResourceOwner,
        callback: object,
        *,
        label: str,
    ) -> object:
        if (
            label == "quiescent directory traversal cleanup also failed"
            and not local_owners
        ):
            local_owners.append(resources)
        return real_scope(resources, callback, label=label)

    def fail_unlink(path: object, *, dir_fd: int | None = None) -> None:
        if fail_operation:
            raise natural_failure
        real_unlink(path, dir_fd=dir_fd)

    def fail_local_close(resources: atomic_module._PosixResourceOwner) -> None:
        if local_owners and resources is local_owners[0] and fail_close:
            if not opened_descriptors:
                opened_descriptors.extend(
                    record.descriptor
                    for record in resources._records
                    if record.descriptor >= 0
                )
            raise cleanup_failure
        real_close(resources)

    monkeypatch.setattr(
        atomic_module,
        "_run_quiescent_directory_resource_scope",
        capture_scope,
    )
    monkeypatch.setattr(atomic_module.os, "unlink", fail_unlink)
    monkeypatch.setattr(atomic_module._PosixResourceOwner, "close", fail_local_close)

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._run_quiescent_directory_resource_scope_unprotected,
            source_fragment,
            lambda: reclaimer.reclaim_child(attempt_path.name),
            predicate=lambda values: values.get("operation_error") is natural_failure,
            error=interruption,
        )

    assert caught.value is interruption
    owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert _exception_chain_contains(caught.value, natural_failure)
    assert len(local_owners) == 1
    local_owner = local_owners[0]
    assert sum(owner is local_owner for owner in owners) == 1
    assert sum(owner is reclaimer for owner in owners) == 1

    fail_close = False
    fail_operation = False
    for owner in owners:
        owner.close()
    assert local_owner.closed
    for descriptor in opened_descriptors:
        _assert_descriptor_closed(descriptor)
    assert reclaimer.closed
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize(
    "hostile_primary",
    [False, True],
    ids=["transferred-primary", "carrier-primary"],
)
def test_quiescent_reclaimer_lifecycle_tail_interrupt_owns_public_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hostile_primary: bool,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    natural_failure: ValueError = ValueError("natural lifecycle tail failure")
    source_fragment = "raise operation_error"
    if hostile_primary:
        natural_failure = _HostileCleanupPrimary("hostile lifecycle tail failure")
        source_fragment = "raise operation_error from delivery_carrier"
    cleanup_failure = OSError(errno.EIO, "persistent lifecycle tail cleanup")
    interruption = KeyboardInterrupt("lifecycle post-inherit tail")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_reclaim(_reclaimer: object, _child_name: str) -> bool:
        raise natural_failure

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_reclaim_child",
        fail_reclaim,
    )
    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected,
            source_fragment,
            lambda: reclaimer.reclaim_child("attempt-a"),
            predicate=lambda values: values.get("operation_error") is natural_failure,
            error=interruption,
        )

    assert caught.value is interruption
    owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert _exception_chain_contains(caught.value, natural_failure)
    assert len(owners) == 1
    attempt = owners[0]
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert not attempt.closed

    fail_release = False
    for owner in owners:
        owner.close()
    assert attempt.closed
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


def test_quiescent_reclaimer_exit_tail_interrupt_owns_public_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    body_failure = ValueError("context body tail failure")
    cleanup_failure = OSError(errno.EIO, "persistent context tail cleanup")
    interruption = KeyboardInterrupt("context post-inherit tail")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._adjudicate_quiescent_reclaimer_exit_error,
            "if not mandatory_transferred or not inherited_transferred:",
            lambda: _raise_from_quiescent_reclaimer_context(
                reclaimer,
                body_failure,
            ),
            predicate=lambda values: values.get("exc") is body_failure,
            error=interruption,
        )

    assert caught.value is interruption
    owners = caught.value.publication_cleanup_owners
    assert len(owners) == 2
    attempt, retained_reclaimer = owners
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert retained_reclaimer is reclaimer
    assert not attempt.closed
    assert not reclaimer.closed

    fail_release = False
    attempt.close()
    assert attempt.closed
    close_failures: list[BaseException] = []

    def close_in_worker() -> None:
        try:
            retained_reclaimer.close()
        except BaseException as error:  # noqa: B036 - report thread failure
            close_failures.append(error)

    worker = threading.Thread(target=close_in_worker)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert close_failures == []
    assert reclaimer.closed


def test_quiescent_resource_cleanup_tail_interrupt_has_top_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = atomic_module._PosixResourceOwner()
    descriptor = resources.open(os.devnull, os.O_RDONLY)
    cleanup_failure = OSError(errno.EIO, "persistent resource-only cleanup")
    interruption = KeyboardInterrupt("resource cleanup-only final delivery")
    real_close = atomic_module._PosixResourceOwner.close
    fail_close = True

    def fail_resources(owner: atomic_module._PosixResourceOwner) -> None:
        if owner is resources and fail_close:
            raise cleanup_failure
        real_close(owner)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "close",
        fail_resources,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._run_quiescent_directory_resource_scope_unprotected,
            "raise settlement_error",
            lambda: atomic_module._run_quiescent_directory_resource_scope(
                resources,
                lambda: None,
                label="resource cleanup-only final delivery",
            ),
            predicate=lambda values: values.get("settlement_error") is cleanup_failure,
            error=interruption,
        )

    assert caught.value is interruption
    owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert owners == (resources,)

    fail_close = False
    for owner in owners:
        owner.close()
    _assert_descriptor_closed(descriptor)


def test_quiescent_lifecycle_cleanup_tail_interrupt_has_top_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    cleanup_failure = OSError(errno.EIO, "persistent lifecycle-only cleanup")
    interruption = KeyboardInterrupt("lifecycle cleanup-only final delivery")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected,
            "raise settlement_error",
            lambda: reclaimer._run_lifecycle_transition(lambda: False),
            predicate=lambda values: values.get("settlement_error") is cleanup_failure,
            error=interruption,
        )

    assert caught.value is interruption
    owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert len(owners) == 1
    attempt = owners[0]
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt

    fail_release = False
    for owner in owners:
        owner.close()
    assert attempt.closed
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


def test_quiescent_reclaimer_exit_delivery_interrupt_has_top_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    body_failure = _HostileCleanupPrimary("hostile context body final delivery")
    cleanup_failure = OSError(errno.EIO, "persistent context final delivery")
    interruption = KeyboardInterrupt("context final delivery")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module.QuiescentDirectoryReclaimer._exit_unprotected,
            "raise exc from delivery_carrier",
            lambda: _raise_from_quiescent_reclaimer_context(
                reclaimer,
                body_failure,
            ),
            predicate=lambda values: values.get("exc") is body_failure,
            error=interruption,
        )

    assert caught.value is interruption
    owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert len(owners) == 2
    attempt, retained_reclaimer = owners
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert retained_reclaimer is reclaimer

    fail_release = False
    for owner in owners:
        owner.close()
    assert reclaimer.closed


def test_quiescent_reclaimer_exit_implicit_delivery_interrupt_has_top_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    body_failure = ValueError("context body implicit final delivery")
    cleanup_failure = OSError(errno.EIO, "persistent implicit final delivery")
    interruption = KeyboardInterrupt("context implicit final delivery")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module.QuiescentDirectoryReclaimer._exit_unprotected,
            "if delivery_carrier is not None:",
            lambda: _raise_from_quiescent_reclaimer_context(
                reclaimer,
                body_failure,
            ),
            predicate=lambda values: values.get("exc") is body_failure,
            error=interruption,
        )

    assert caught.value is interruption
    owners = BaseException.__getattribute__(
        caught.value,
        "publication_cleanup_owners",
    )
    assert len(owners) == 2
    attempt, retained_reclaimer = owners
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert retained_reclaimer is reclaimer

    fail_release = False
    for owner in owners:
        owner.close()
    assert reclaimer.closed


def test_quiescent_reclaimer_exit_detects_same_ambient_error_rethrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    ambient = _HostileTracebackPrimary("same ambient exit failure")

    def raise_same(_reclaimer: object) -> None:
        raise ambient

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_close",
        raise_same,
    )

    with pytest.raises(_HostileTracebackPrimary) as caught:
        try:
            raise ambient
        except _HostileTracebackPrimary:
            reclaimer.__exit__(None, None, None)

    assert caught.value is ambient
    assert BaseException.__getattribute__(
        ambient,
        "publication_cleanup_owners",
    ) == (reclaimer,)

    monkeypatch.undo()
    reclaimer.close()
    assert reclaimer.closed


def test_quiescent_resource_boundary_detects_same_ambient_error_rethrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = atomic_module._PosixResourceOwner()
    descriptor = resources.open(os.devnull, os.O_RDONLY)
    ambient = _HostileTracebackPrimary("same ambient resource failure")
    cleanup_failure = OSError(errno.EIO, "same ambient resource cleanup")
    real_close = atomic_module._PosixResourceOwner.close
    fail_close = True

    def fail_resources(owner: atomic_module._PosixResourceOwner) -> None:
        if owner is resources and fail_close:
            raise cleanup_failure
        real_close(owner)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "close",
        fail_resources,
    )

    with pytest.raises(_HostileTracebackPrimary) as caught:
        try:
            raise ambient
        except _HostileTracebackPrimary:
            _call_with_interrupt_on_source_line(
                atomic_module._run_quiescent_directory_resource_scope_unprotected,
                "raise settlement_error",
                lambda: atomic_module._run_quiescent_directory_resource_scope(
                    resources,
                    lambda: None,
                    label="same ambient resource cleanup",
                ),
                predicate=lambda values: values.get("settlement_error")
                is cleanup_failure,
                error=ambient,
            )

    assert caught.value is ambient
    owners = BaseException.__getattribute__(
        ambient,
        "publication_cleanup_owners",
    )
    assert owners == (resources,)
    fail_close = False
    for owner in owners:
        owner.close()
    _assert_descriptor_closed(descriptor)


def test_quiescent_lifecycle_boundary_detects_same_ambient_error_rethrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    ambient = _HostileTracebackPrimary("same ambient lifecycle failure")
    primary = ValueError("same ambient lifecycle primary")
    cleanup_failure = OSError(errno.EIO, "same ambient lifecycle cleanup")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(_HostileTracebackPrimary) as caught:
        try:
            raise ambient
        except _HostileTracebackPrimary:
            _call_with_interrupt_on_source_line(
                atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected,
                "raise operation_error",
                lambda: reclaimer._run_lifecycle_transition(
                    lambda: (_ for _ in ()).throw(primary)
                ),
                predicate=lambda values: values.get("operation_error") is primary,
                error=ambient,
            )

    assert caught.value is ambient
    owners = BaseException.__getattribute__(
        ambient,
        "publication_cleanup_owners",
    )
    assert len(owners) == 1
    attempt = owners[0]
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert _exception_chain_contains(ambient, primary)
    fail_release = False
    for owner in owners:
        owner.close()
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


def test_quiescent_resource_handler_detects_same_ambient_error_rethrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = atomic_module._run_quiescent_directory_resource_scope_unprotected
    offsets = _source_statement_opcode_offsets(function, "try:", occurrence=4)

    for opcode_offset in offsets:
        with monkeypatch.context() as patch:
            resources = atomic_module._PosixResourceOwner()
            descriptor = resources.open(os.devnull, os.O_RDONLY)
            ambient = _HostileTracebackPrimary("same ambient resource handler")
            primary = ValueError("same ambient resource handler primary")
            cleanup_failure = OSError(errno.EIO, "same ambient resource handler")
            real_close = atomic_module._PosixResourceOwner.close
            fail_close = True

            def fail_resources(owner: atomic_module._PosixResourceOwner) -> None:
                if owner is resources and fail_close:  # noqa: B023
                    raise cleanup_failure  # noqa: B023
                real_close(owner)  # noqa: B023

            patch.setattr(
                atomic_module._PosixResourceOwner,
                "close",
                fail_resources,
            )

            with pytest.raises(_HostileTracebackPrimary) as caught:
                try:
                    raise ambient
                except _HostileTracebackPrimary:
                    _call_with_interrupt_at_opcode(
                        function,
                        opcode_offset,
                        lambda: atomic_module._run_quiescent_directory_resource_scope(  # noqa: B023
                            resources,  # noqa: B023
                            lambda: (_ for _ in ()).throw(primary),  # noqa: B023
                            label="same ambient resource handler",
                        ),
                        predicate=lambda values: values.get("settlement_error")
                        is cleanup_failure,  # noqa: B023
                        error=ambient,
                    )

            assert caught.value is ambient
            owners = BaseException.__getattribute__(
                ambient,
                "publication_cleanup_owners",
            )
            assert owners == (resources,)
            assert _exception_chain_contains(ambient, primary)
            fail_close = False
            for owner in owners:
                owner.close()
            _assert_descriptor_closed(descriptor)


def test_quiescent_lifecycle_handler_detects_same_ambient_error_rethrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = (
        atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected
    )
    offsets = _source_statement_opcode_offsets(function, "try:", occurrence=4)

    for index, opcode_offset in enumerate(offsets):
        with monkeypatch.context() as patch:
            parent, parent_identity = _private_reclaimer_parent(
                tmp_path,
                name=f"same-ambient-lifecycle-handler-{index}",
            )
            reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )
            ambient = _HostileTracebackPrimary("same ambient lifecycle handler")
            primary = ValueError("same ambient lifecycle handler primary")
            cleanup_failure = OSError(errno.EIO, "same ambient lifecycle handler")
            real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
            fail_release = True

            def fail_lease_release(lease: object) -> None:
                if lease is reclaimer._lifecycle_lease and fail_release:  # noqa: B023
                    raise cleanup_failure  # noqa: B023
                real_release(lease)  # noqa: B023

            patch.setattr(
                atomic_module._QuiescentDirectoryLifecycleLease,
                "release",
                fail_lease_release,
            )

            with pytest.raises(_HostileTracebackPrimary) as caught:
                try:
                    raise ambient
                except _HostileTracebackPrimary:
                    _call_with_interrupt_at_opcode(
                        function,
                        opcode_offset,
                        lambda: reclaimer._run_lifecycle_transition(  # noqa: B023
                            lambda: (_ for _ in ()).throw(primary)  # noqa: B023
                        ),
                        predicate=lambda values: values.get("settlement_error")
                        is cleanup_failure,  # noqa: B023
                        error=ambient,
                    )

            assert caught.value is ambient
            owners = BaseException.__getattribute__(
                ambient,
                "publication_cleanup_owners",
            )
            assert len(owners) == 1
            attempt = owners[0]
            assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
            assert _exception_chain_contains(ambient, primary)
            fail_release = False
            for owner in owners:
                owner.close()
            _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
            reclaimer.close()


def test_cleanup_guard_filters_unchanged_hostile_ambient_error() -> None:
    ambient = _HostileTracebackPrimary("unchanged ambient cleanup guard")
    cleanup_calls: list[str] = []

    try:
        raise ambient
    except _HostileTracebackPrimary:
        with atomic_module._run_context_with_cleanup_actions(
            (("unexpected cleanup", lambda: cleanup_calls.append("cleanup")),),
            cleanup_on_success=False,
        ):
            pass

    assert cleanup_calls == []
    with pytest.raises(AttributeError):
        BaseException.__getattribute__(ambient, "publication_cleanup_owners")


def test_quiescent_reclaimer_inner_exit_guard_final_opcodes_have_top_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = atomic_module._QuiescentReclaimerExitHandoffBoundary.__exit__
    offsets = _source_statement_opcode_offsets(
        function,
        "return False",
        occurrence=2,
    )
    return_offsets = {
        instruction.offset
        for instruction in dis.get_instructions(function)
        if instruction.opname == "RETURN_VALUE"
    }
    delivery_events: tuple[int | None, ...] = tuple(
        offset for offset in offsets if offset not in return_offsets
    ) + (None,)

    for index, opcode_offset in enumerate(delivery_events):
        with monkeypatch.context() as patch:
            parent, parent_identity = _private_reclaimer_parent(
                tmp_path,
                name=f"exit-guard-final-{index}",
            )
            reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )
            cleanup_failure = OSError(errno.EIO, "persistent exit guard cleanup")
            interruption = KeyboardInterrupt(f"exit guard final {opcode_offset}")
            real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
            fail_release = True

            def fail_lease_release(lease: object) -> None:
                if lease is reclaimer._lifecycle_lease and fail_release:  # noqa: B023
                    raise cleanup_failure  # noqa: B023
                real_release(lease)  # noqa: B023

            patch.setattr(
                atomic_module._QuiescentDirectoryLifecycleLease,
                "release",
                fail_lease_release,
            )

            def callback() -> None:
                reclaimer.__exit__(None, None, None)  # noqa: B023

            with pytest.raises(KeyboardInterrupt) as caught:
                if opcode_offset is None:
                    _call_with_interrupt_on_return(
                        function,
                        callback,
                        call_occurrence=1,
                        warmup=lambda: function(  # noqa: B023
                            atomic_module._QuiescentReclaimerExitHandoffBoundary(
                                reclaimer,  # noqa: B023
                                None,
                            ),
                            None,
                            None,
                            None,
                        ),
                        predicate=lambda frame, result: (
                            result is False
                            and frame.f_locals.get("active_error")
                            is cleanup_failure  # noqa: B023
                            and frame.f_locals.get("self")._reclaimer
                            is reclaimer  # noqa: B023
                        ),
                        error=interruption,
                    )
                else:
                    _call_with_interrupt_at_opcode(
                        function,
                        opcode_offset,
                        callback,
                        call_occurrence=1,
                        warmup=lambda: function(  # noqa: B023
                            atomic_module._QuiescentReclaimerExitHandoffBoundary(
                                reclaimer,  # noqa: B023
                                None,
                            ),
                            None,
                            None,
                            None,
                        ),
                        predicate=lambda values: (
                            values.get("active_error") is cleanup_failure  # noqa: B023
                            and values.get("self")._reclaimer is reclaimer  # noqa: B023
                        ),
                        error=interruption,
                    )

            assert caught.value is interruption
            owners = BaseException.__getattribute__(
                caught.value,
                "publication_cleanup_owners",
            )
            assert _exception_chain_contains(caught.value, cleanup_failure)
            assert len(owners) == 2
            attempt, retained_reclaimer = owners
            assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
            assert retained_reclaimer is reclaimer
            fail_release = False
            for owner in owners:
                owner.close()
            assert reclaimer.closed


def test_quiescent_resource_owning_boundary_opcodes_have_top_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = atomic_module._QuiescentCleanupHandoffBoundary.__exit__
    baseline_resources = atomic_module._PosixResourceOwner()
    baseline_descriptor = baseline_resources.open(os.devnull, os.O_RDONLY)
    baseline_primary = ValueError("resource boundary baseline primary")
    baseline_cleanup = OSError(errno.EIO, "resource boundary baseline cleanup")
    real_close = atomic_module._PosixResourceOwner.close
    baseline_fails = True

    def fail_baseline(owner: atomic_module._PosixResourceOwner) -> None:
        if owner is baseline_resources and baseline_fails:
            raise baseline_cleanup
        real_close(owner)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "close",
        fail_baseline,
    )

    def observe_boundary() -> None:
        with pytest.raises(ValueError) as caught:
            atomic_module._run_quiescent_directory_resource_scope(
                baseline_resources,
                lambda: (_ for _ in ()).throw(baseline_primary),
                label="resource boundary baseline cleanup",
            )
        assert caught.value is baseline_primary

    offsets = _observed_opcode_offsets(
        function,
        observe_boundary,
        call_occurrence=1,
        warmup=lambda: function(
            atomic_module._QuiescentCleanupHandoffBoundary(
                atomic_module._QuiescentDirectoryLifecycleOutcome(),
                (),
                label="resource boundary trace warmup",
            ),
            None,
            None,
            None,
        ),
    )
    baseline_fails = False
    baseline_resources.close()
    _assert_descriptor_closed(baseline_descriptor)

    for opcode_offset in offsets:
        with monkeypatch.context() as patch:
            resources = atomic_module._PosixResourceOwner()
            descriptor = resources.open(os.devnull, os.O_RDONLY)
            primary = ValueError("resource boundary opcode primary")
            cleanup_failure = OSError(errno.EIO, "resource boundary opcode cleanup")
            interruption = KeyboardInterrupt(f"resource boundary {opcode_offset}")
            fail_close = True

            def fail_resources(owner: atomic_module._PosixResourceOwner) -> None:
                if owner is resources and fail_close:  # noqa: B023
                    raise cleanup_failure  # noqa: B023
                real_close(owner)

            patch.setattr(
                atomic_module._PosixResourceOwner,
                "close",
                fail_resources,
            )

            with pytest.raises(KeyboardInterrupt) as caught:
                _call_with_interrupt_at_opcode(
                    function,
                    opcode_offset,
                    lambda: atomic_module._run_quiescent_directory_resource_scope(  # noqa: B023
                        resources,  # noqa: B023
                        lambda: (_ for _ in ()).throw(primary),  # noqa: B023
                        label="resource boundary opcode cleanup",
                    ),
                    call_occurrence=1,
                    warmup=lambda: function(
                        atomic_module._QuiescentCleanupHandoffBoundary(
                            atomic_module._QuiescentDirectoryLifecycleOutcome(),
                            (),
                            label="resource boundary trace warmup",
                        ),
                        None,
                        None,
                        None,
                    ),
                    error=interruption,
                )

            assert caught.value is interruption
            owners = BaseException.__getattribute__(
                caught.value,
                "publication_cleanup_owners",
            )
            assert owners == (resources,)
            assert _exception_chain_contains(caught.value, primary)
            fail_close = False
            for owner in owners:
                owner.close()
            _assert_descriptor_closed(descriptor)


def test_quiescent_reclaimer_exit_guard_opcodes_have_top_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = atomic_module._QuiescentReclaimerExitHandoffBoundary.__exit__
    baseline_parent, baseline_identity = _private_reclaimer_parent(
        tmp_path,
        name="exit-boundary-baseline",
    )
    baseline_reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        baseline_parent,
        expected_parent_identity=baseline_identity,
    )
    baseline_cleanup = OSError(errno.EIO, "exit boundary baseline cleanup")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    baseline_fails = True

    def fail_baseline(lease: object) -> None:
        if lease is baseline_reclaimer._lifecycle_lease and baseline_fails:
            raise baseline_cleanup
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_baseline,
    )

    def observe_boundary() -> None:
        with pytest.raises(OSError) as caught:
            baseline_reclaimer.__exit__(None, None, None)
        assert caught.value is baseline_cleanup

    offsets = _observed_opcode_offsets(
        function,
        observe_boundary,
        call_occurrence=1,
        warmup=lambda: function(
            atomic_module._QuiescentReclaimerExitHandoffBoundary(
                baseline_reclaimer,
                None,
            ),
            None,
            None,
            None,
        ),
        predicate=lambda values: values.get("self")._reclaimer is baseline_reclaimer,
    )
    baseline_fails = False
    for owner in BaseException.__getattribute__(
        baseline_cleanup,
        "publication_cleanup_owners",
    ):
        owner.close()
    assert baseline_reclaimer.closed

    for index, opcode_offset in enumerate(offsets):
        with monkeypatch.context() as patch:
            parent, parent_identity = _private_reclaimer_parent(
                tmp_path,
                name=f"exit-boundary-{index}",
            )
            reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )
            cleanup_failure = OSError(errno.EIO, "exit boundary opcode cleanup")
            interruption = KeyboardInterrupt(f"exit boundary {opcode_offset}")
            fail_release = True

            def fail_lease_release(lease: object) -> None:
                if lease is reclaimer._lifecycle_lease and fail_release:  # noqa: B023
                    raise cleanup_failure  # noqa: B023
                real_release(lease)

            patch.setattr(
                atomic_module._QuiescentDirectoryLifecycleLease,
                "release",
                fail_lease_release,
            )

            with pytest.raises(KeyboardInterrupt) as caught:
                _call_with_interrupt_at_opcode(
                    function,
                    opcode_offset,
                    lambda: reclaimer.__exit__(None, None, None),  # noqa: B023
                    call_occurrence=1,
                    warmup=lambda: function(  # noqa: B023
                        atomic_module._QuiescentReclaimerExitHandoffBoundary(
                            reclaimer,  # noqa: B023
                            None,
                        ),
                        None,
                        None,
                        None,
                    ),
                    predicate=lambda values: values.get("self")._reclaimer
                    is reclaimer,  # noqa: B023
                    error=interruption,
                )

            assert caught.value is interruption
            owners = BaseException.__getattribute__(
                caught.value,
                "publication_cleanup_owners",
            )
            assert len(owners) == 2
            attempt, retained_reclaimer = owners
            assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
            assert retained_reclaimer is reclaimer
            fail_release = False
            for owner in owners:
                owner.close()
            assert reclaimer.closed


def test_quiescent_lifecycle_outer_delivery_boundary_is_publicly_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(
        tmp_path,
        name="lifecycle-outer-delivery",
    )
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    primary = ValueError("lifecycle outer delivery primary")
    cleanup_failure = OSError(errno.EIO, "lifecycle outer delivery cleanup")
    interruption = KeyboardInterrupt("lifecycle outer delivery")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )
    function = atomic_module._QuiescentCleanupHandoffBoundary.__exit__

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_return(
            function,
            lambda: reclaimer._run_lifecycle_transition(
                lambda: (_ for _ in ()).throw(primary)
            ),
            call_occurrence=2,
            warmup=lambda: function(
                atomic_module._QuiescentCleanupHandoffBoundary(
                    atomic_module._QuiescentDirectoryLifecycleOutcome(),
                    (),
                    label="lifecycle outer delivery trace warmup",
                ),
                None,
                None,
                None,
            ),
            predicate=lambda frame, result: (
                result is False and frame.f_locals.get("active_error") is primary
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert atomic_module._publication_exception_context(interruption) is primary
    owners = BaseException.__getattribute__(
        primary,
        "publication_cleanup_owners",
    )
    assert len(owners) == 1
    attempt = owners[0]
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert not attempt._outer_call._guard.gi_running

    # Recovery uses only the public object.  The caller never walks or closes
    # owners from the irreducible outer delivery exception chain.
    fail_release = False
    assert reclaimer.retry() is False
    assert attempt.closed
    reclaimer.close()


def test_quiescent_exit_outer_delivery_boundary_is_publicly_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(
        tmp_path,
        name="exit-outer-delivery",
    )
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    cleanup_failure = OSError(errno.EIO, "exit outer delivery cleanup")
    interruption = KeyboardInterrupt("exit outer delivery")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_failure
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )
    function = atomic_module._QuiescentReclaimerExitHandoffBoundary.__exit__

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_return(
            function,
            lambda: reclaimer.__exit__(None, None, None),
            call_occurrence=2,
            warmup=lambda: function(
                atomic_module._QuiescentReclaimerExitHandoffBoundary(
                    reclaimer,
                    None,
                ),
                None,
                None,
                None,
            ),
            predicate=lambda frame, result: (
                result is False
                and frame.f_locals.get("active_error") is cleanup_failure
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert atomic_module._publication_exception_context(interruption) is cleanup_failure
    owners = BaseException.__getattribute__(
        cleanup_failure,
        "publication_cleanup_owners",
    )
    assert len(owners) == 2
    attempt, retained_reclaimer = owners
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
    assert retained_reclaimer is reclaimer
    assert not attempt._outer_call._guard.gi_running

    fail_release = False
    reclaimer.close()
    assert attempt.closed
    assert reclaimer.closed


def _assert_quiescent_resource_handler_entry_opcode(
    function: object,
    opcode_offset: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        resources = atomic_module._PosixResourceOwner()
        descriptor = resources.open(os.devnull, os.O_RDONLY)
        primary = ValueError("resource handler-entry primary")
        cleanup_failure = OSError(errno.EIO, "resource handler-entry cleanup")
        interruption = KeyboardInterrupt(f"resource handler {opcode_offset}")
        real_close = atomic_module._PosixResourceOwner.close
        fail_close = True

        def fail_resources(owner: atomic_module._PosixResourceOwner) -> None:
            if owner is resources and fail_close:
                raise cleanup_failure
            real_close(owner)

        patch.setattr(
            atomic_module._PosixResourceOwner,
            "close",
            fail_resources,
        )

        with pytest.raises(KeyboardInterrupt) as caught:
            _call_with_interrupt_at_opcode(
                function,
                opcode_offset,
                lambda: atomic_module._run_quiescent_directory_resource_scope(
                    resources,
                    lambda: (_ for _ in ()).throw(primary),
                    label="resource handler-entry cleanup",
                ),
                predicate=lambda values: values.get("settlement_error")
                is cleanup_failure,
                error=interruption,
            )

        assert caught.value is interruption
        owners = BaseException.__getattribute__(
            caught.value,
            "publication_cleanup_owners",
        )
        assert _exception_chain_contains(caught.value, primary)
        assert owners == (resources,)
        fail_close = False
        for owner in owners:
            owner.close()
        _assert_descriptor_closed(descriptor)


def test_quiescent_resource_handler_entry_opcodes_have_top_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = atomic_module._run_quiescent_directory_resource_scope_unprotected
    offsets = _source_statement_opcode_offsets(function, "try:", occurrence=4)
    failures: list[BaseException] = []

    # Opcode tracing can interrupt before CPython restores the active handler's
    # exception state.  Keep that synthetic interpreter state on a disposable
    # thread while preserving the exact delivered exception and owner checks.
    def verify_handler_entries() -> None:
        try:
            for opcode_offset in offsets:
                _assert_quiescent_resource_handler_entry_opcode(
                    function,
                    opcode_offset,
                    monkeypatch,
                )
        except BaseException as error:  # noqa: B036 - report thread failure
            failures.append(error)

    worker = threading.Thread(target=verify_handler_entries)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert failures == []


def test_quiescent_lifecycle_handler_entry_opcodes_have_top_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = (
        atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected
    )
    offsets = _source_statement_opcode_offsets(function, "try:", occurrence=4)

    for index, opcode_offset in enumerate(offsets):
        with monkeypatch.context() as patch:
            parent, parent_identity = _private_reclaimer_parent(
                tmp_path,
                name=f"lifecycle-handler-{index}",
            )
            reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )
            primary = ValueError("lifecycle handler-entry primary")
            cleanup_failure = OSError(errno.EIO, "lifecycle handler-entry cleanup")
            interruption = KeyboardInterrupt(f"lifecycle handler {opcode_offset}")
            real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
            fail_release = True

            def fail_lease_release(lease: object) -> None:
                if lease is reclaimer._lifecycle_lease and fail_release:  # noqa: B023
                    raise cleanup_failure  # noqa: B023
                real_release(lease)  # noqa: B023

            patch.setattr(
                atomic_module._QuiescentDirectoryLifecycleLease,
                "release",
                fail_lease_release,
            )

            with pytest.raises(KeyboardInterrupt) as caught:
                _call_with_interrupt_at_opcode(
                    function,
                    opcode_offset,
                    lambda: reclaimer._run_lifecycle_transition(  # noqa: B023
                        lambda: (_ for _ in ()).throw(primary)  # noqa: B023
                    ),
                    predicate=lambda values: values.get("settlement_error")
                    is cleanup_failure,  # noqa: B023
                    error=interruption,
                )

            assert caught.value is interruption
            owners = BaseException.__getattribute__(
                caught.value,
                "publication_cleanup_owners",
            )
            assert _exception_chain_contains(caught.value, primary)
            assert len(owners) == 1
            attempt = owners[0]
            assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
            fail_release = False
            for owner in owners:
                owner.close()
            _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
            reclaimer.close()


@pytest.mark.parametrize(
    ("source_fragment", "operation_fails"),
    [
        ("operation_error = outcome.error", True),
        (
            "operation_error = _quiescent_directory_lifecycle_operation_context(",
            False,
        ),
        (
            "prior_link, prior_was_explicit = _publication_exception_prior_link(",
            True,
        ),
    ],
    ids=["outcome", "context", "prior-cause"],
)
def test_quiescent_reclaimer_resource_pretransfer_opcodes_own_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
    operation_fails: bool,
) -> None:
    real_scope = atomic_module._run_quiescent_directory_resource_scope
    function = atomic_module._run_quiescent_directory_resource_scope_unprotected
    offsets = _source_statement_opcode_offsets(function, source_fragment)

    def exercise(index: int, opcode_offset: int) -> None:
        with monkeypatch.context() as patch:
            parent, parent_identity = _private_reclaimer_parent(
                tmp_path,
                name=f"resource-{source_fragment[:7]}-{index}",
            )
            attempt_path = parent / "attempt-a"
            nested = attempt_path / "nested"
            nested.mkdir(parents=True)
            (nested / "payload.txt").write_text("payload", encoding="utf-8")
            reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )
            real_close = atomic_module._PosixResourceOwner.close
            real_unlink = atomic_module.os.unlink
            natural_failure = ValueError("natural resource opcode failure")
            cleanup_failure = OSError(errno.EIO, "resource opcode cleanup")
            interruption = KeyboardInterrupt(f"resource opcode {opcode_offset}")
            local_owners: list[atomic_module._PosixResourceOwner] = []
            fail_close = True
            fail_operation = operation_fails

            def capture_scope(
                resources: atomic_module._PosixResourceOwner,
                callback: object,
                *,
                label: str,
            ) -> object:
                if (
                    label == "quiescent directory traversal cleanup also failed"
                    and not local_owners
                ):
                    local_owners.append(resources)
                return real_scope(resources, callback, label=label)

            def fail_unlink(path: object, *, dir_fd: int | None = None) -> None:
                if fail_operation:
                    raise natural_failure
                real_unlink(path, dir_fd=dir_fd)

            def fail_local_close(
                resources: atomic_module._PosixResourceOwner,
            ) -> None:
                if local_owners and resources is local_owners[0] and fail_close:
                    raise cleanup_failure
                real_close(resources)

            patch.setattr(
                atomic_module,
                "_run_quiescent_directory_resource_scope",
                capture_scope,
            )
            patch.setattr(atomic_module.os, "unlink", fail_unlink)
            patch.setattr(
                atomic_module._PosixResourceOwner,
                "close",
                fail_local_close,
            )

            with pytest.raises(KeyboardInterrupt) as caught:
                _call_with_interrupt_at_opcode(
                    function,
                    opcode_offset,
                    lambda: reclaimer.reclaim_child(attempt_path.name),
                    predicate=lambda values: (
                        values.get("label")
                        == "quiescent directory traversal cleanup also failed"
                        and (
                            source_fragment != "prior_link, prior_was_explicit = "
                            "_publication_exception_prior_link("
                            or values.get("operation_error") is natural_failure
                        )
                    ),
                    error=interruption,
                )

            assert caught.value is interruption
            owners = BaseException.__getattribute__(
                caught.value,
                "publication_cleanup_owners",
            )
            assert len(local_owners) == 1
            local_owner = local_owners[0]
            assert sum(owner is local_owner for owner in owners) == 1
            assert sum(owner is reclaimer for owner in owners) == 1

            fail_close = False
            fail_operation = False
            for owner in owners:
                owner.close()
            assert local_owner.closed
            assert reclaimer.closed
            assert list(parent.iterdir()) == []

    for index, opcode_offset in enumerate(offsets):
        exercise(index, opcode_offset)


@pytest.mark.parametrize(
    ("source_fragment", "operation_fails"),
    [
        ("operation_error = outcome.error", True),
        (
            "operation_error = _quiescent_directory_lifecycle_operation_context(",
            False,
        ),
        (
            "prior_link, prior_was_explicit = _publication_exception_prior_link(",
            True,
        ),
    ],
    ids=["outcome", "context", "prior-cause"],
)
def test_quiescent_reclaimer_lifecycle_pretransfer_opcodes_own_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
    operation_fails: bool,
) -> None:
    function = (
        atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected
    )
    offsets = _source_statement_opcode_offsets(function, source_fragment)

    def exercise(index: int, opcode_offset: int) -> None:
        with monkeypatch.context() as patch:
            parent, parent_identity = _private_reclaimer_parent(
                tmp_path,
                name=f"lifecycle-{source_fragment[:7]}-{index}",
            )
            reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )
            natural_failure = ValueError("natural lifecycle opcode failure")
            cleanup_failure = OSError(errno.EIO, "lifecycle opcode cleanup")
            interruption = KeyboardInterrupt(f"lifecycle opcode {opcode_offset}")
            real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
            fail_release = True

            def reclaim(_reclaimer: object, _child_name: str) -> bool:
                if operation_fails:
                    raise natural_failure
                return False

            def fail_lease_release(lease: object) -> None:
                if lease is reclaimer._lifecycle_lease and fail_release:
                    raise cleanup_failure
                real_release(lease)

            patch.setattr(
                atomic_module.QuiescentDirectoryReclaimer,
                "_reclaim_child",
                reclaim,
            )
            patch.setattr(
                atomic_module._QuiescentDirectoryLifecycleLease,
                "release",
                fail_lease_release,
            )

            with pytest.raises(KeyboardInterrupt) as caught:
                _call_with_interrupt_at_opcode(
                    function,
                    opcode_offset,
                    lambda: reclaimer.reclaim_child("attempt-a"),
                    predicate=lambda values: (
                        values.get("attempt") is not None
                        and values["attempt"]._lease is reclaimer._lifecycle_lease
                        and (
                            source_fragment != "prior_link, prior_was_explicit = "
                            "_publication_exception_prior_link("
                            or values.get("operation_error") is natural_failure
                        )
                    ),
                    error=interruption,
                )

            assert caught.value is interruption
            owners = BaseException.__getattribute__(
                caught.value,
                "publication_cleanup_owners",
            )
            assert len(owners) == 1
            attempt = owners[0]
            assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
            assert not attempt.closed

            fail_release = False
            attempt.close()
            assert attempt.closed
            _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
            reclaimer.close()

    for index, opcode_offset in enumerate(offsets):
        exercise(index, opcode_offset)


def test_quiescent_reclaimer_exit_pretransfer_opcodes_own_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = atomic_module.QuiescentDirectoryReclaimer._exit_unprotected
    source_fragment = "current = self._lifecycle_lease.current_transition()"
    offsets = _source_statement_opcode_offsets(function, source_fragment)

    def exercise(index: int, opcode_offset: int) -> None:
        with monkeypatch.context() as patch:
            parent, parent_identity = _private_reclaimer_parent(
                tmp_path,
                name=f"exit-current-{index}",
            )
            reclaimer = atomic_module.QuiescentDirectoryReclaimer(
                parent,
                expected_parent_identity=parent_identity,
            )
            body_failure = ValueError("context body opcode failure")
            cleanup_failure = OSError(errno.EIO, "context opcode cleanup")
            interruption = KeyboardInterrupt(f"exit opcode {opcode_offset}")
            real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
            fail_release = True

            def fail_lease_release(lease: object) -> None:
                if lease is reclaimer._lifecycle_lease and fail_release:
                    raise cleanup_failure
                real_release(lease)

            patch.setattr(
                atomic_module._QuiescentDirectoryLifecycleLease,
                "release",
                fail_lease_release,
            )

            with pytest.raises(KeyboardInterrupt) as caught:
                _call_with_interrupt_at_opcode(
                    function,
                    opcode_offset,
                    lambda: _raise_from_quiescent_reclaimer_context(
                        reclaimer,
                        body_failure,
                    ),
                    predicate=lambda values: values.get("exc") is body_failure,
                    error=interruption,
                )

            assert caught.value is interruption
            owners = BaseException.__getattribute__(
                caught.value,
                "publication_cleanup_owners",
            )
            assert len(owners) == 2
            attempt, retained_reclaimer = owners
            assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt
            assert retained_reclaimer is reclaimer
            assert not attempt.closed
            assert not reclaimer.closed

            fail_release = False
            attempt.close()
            retained_reclaimer.close()
            assert reclaimer.closed

    for index, opcode_offset in enumerate(offsets):
        exercise(index, opcode_offset)


@pytest.mark.parametrize("link_kind", ["cause", "context"])
def test_quiescent_resource_settlement_preserves_prior_error_link(
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    resources = atomic_module._PosixResourceOwner()
    descriptor = resources.open(os.devnull, os.O_RDONLY)
    primary = ValueError(f"resource primary with prior {link_kind}")
    prior = LookupError(f"resource prior {link_kind}")
    cleanup_error = OSError(errno.EIO, "resource persistent close")
    real_close = atomic_module._PosixResourceOwner.close
    fail_close = True

    def fail_resources(owner: atomic_module._PosixResourceOwner) -> None:
        if owner is resources and fail_close:
            raise cleanup_error
        real_close(owner)

    def fail_callback() -> None:
        if link_kind == "cause":
            primary.__cause__ = prior
            raise primary
        try:
            raise prior
        except LookupError:
            raise primary

    def forbidden_handoff(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("natural settlement entered interruption recovery")

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "close",
        fail_resources,
    )
    monkeypatch.setattr(
        atomic_module,
        "_raise_quiescent_cleanup_handoff_boundary",
        forbidden_handoff,
    )

    with pytest.raises(ValueError) as caught:
        atomic_module._run_quiescent_directory_resource_scope(
            resources,
            fail_callback,
            label="resource prior-link cleanup also failed",
        )

    assert caught.value is primary
    if link_kind == "cause":
        assert primary.__cause__ is prior
    else:
        carrier = primary.__cause__
        assert type(carrier) is RuntimeError
        assert carrier.args[2] is cleanup_error
        assert carrier.args[3] is prior
        assert carrier.__context__ is prior
    owners = BaseException.__getattribute__(
        primary,
        "publication_cleanup_owners",
    )
    assert sum(owner is resources for owner in owners) == 1

    fail_close = False
    resources.close()
    _assert_descriptor_closed(descriptor)


@pytest.mark.parametrize("link_kind", ["cause", "context"])
def test_quiescent_lifecycle_settlement_preserves_prior_error_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    primary = ValueError(f"lifecycle primary with prior {link_kind}")
    prior = LookupError(f"lifecycle prior {link_kind}")
    cleanup_error = OSError(errno.EIO, "lifecycle persistent release")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_callback() -> None:
        if link_kind == "cause":
            primary.__cause__ = prior
            raise primary
        try:
            raise prior
        except LookupError:
            raise primary

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_error
        real_release(lease)

    def forbidden_handoff(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("natural settlement entered interruption recovery")

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )
    monkeypatch.setattr(
        atomic_module,
        "_raise_quiescent_cleanup_handoff_boundary",
        forbidden_handoff,
    )

    with pytest.raises(ValueError) as caught:
        reclaimer._run_lifecycle_transition(fail_callback)

    assert caught.value is primary
    if link_kind == "cause":
        assert primary.__cause__ is prior
    else:
        carrier = primary.__cause__
        assert type(carrier) is RuntimeError
        assert carrier.args[2] is cleanup_error
        assert carrier.args[3] is prior
        assert carrier.__context__ is prior
    owners = BaseException.__getattribute__(
        primary,
        "publication_cleanup_owners",
    )
    attempts = tuple(
        owner
        for owner in owners
        if type(owner) is atomic_module._QuiescentDirectoryLifecycleAttempt
    )
    assert len(attempts) == 1

    fail_release = False
    attempts[0].close()
    assert attempts[0].closed
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


def test_quiescent_resource_cleanup_only_skips_handoff_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = atomic_module._PosixResourceOwner()
    descriptor = resources.open(os.devnull, os.O_RDONLY)
    cleanup_error = OSError(errno.EIO, "resource cleanup-only failure")
    real_close = atomic_module._PosixResourceOwner.close
    fail_close = True

    def fail_resources(owner: atomic_module._PosixResourceOwner) -> None:
        if owner is resources and fail_close:
            raise cleanup_error
        real_close(owner)

    def forbidden_handoff(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup-only settlement entered handoff recovery")

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "close",
        fail_resources,
    )
    monkeypatch.setattr(
        atomic_module,
        "_raise_quiescent_cleanup_handoff_boundary",
        forbidden_handoff,
    )

    with pytest.raises(OSError) as caught:
        atomic_module._run_quiescent_directory_resource_scope(
            resources,
            lambda: None,
            label="resource cleanup-only failure",
        )

    assert caught.value is cleanup_error
    owners = BaseException.__getattribute__(
        cleanup_error,
        "publication_cleanup_owners",
    )
    assert owners == (resources,)
    fail_close = False
    resources.close()
    _assert_descriptor_closed(descriptor)


def test_quiescent_lifecycle_cleanup_only_skips_handoff_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    cleanup_error = OSError(errno.EIO, "lifecycle cleanup-only failure")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_error
        real_release(lease)

    def forbidden_handoff(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cleanup-only settlement entered handoff recovery")

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )
    monkeypatch.setattr(
        atomic_module,
        "_raise_quiescent_cleanup_handoff_boundary",
        forbidden_handoff,
    )

    with pytest.raises(OSError) as caught:
        reclaimer._run_lifecycle_transition(lambda: False)

    assert caught.value is cleanup_error
    owners = BaseException.__getattribute__(
        cleanup_error,
        "publication_cleanup_owners",
    )
    attempts = tuple(
        owner
        for owner in owners
        if type(owner) is atomic_module._QuiescentDirectoryLifecycleAttempt
    )
    assert len(attempts) == 1
    fail_release = False
    attempts[0].close()
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


def test_publication_cleanup_owner_readers_reject_oversized_metadata() -> None:
    probes: list[int] = []

    class ForeignOwner:
        @property
        def closed(self) -> bool:
            probes.append(1)
            raise AssertionError("oversized owner metadata was probed")

        def close(self) -> None:
            raise AssertionError("oversized owner metadata was closed")

    class LocalOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    foreign = tuple(
        ForeignOwner()
        for _index in range(atomic_module._MAX_PUBLICATION_CLEANUP_OWNERS + 1)
    )
    source = OSError(errno.EIO, "oversized cleanup metadata")
    destination = ValueError("bounded cleanup destination")
    BaseException.__setattr__(
        source,
        "publication_cleanup_owners",
        foreign,
    )

    assert not atomic_module._inherit_publication_cleanup_owners(
        destination,
        source,
    )
    assert not atomic_module._publication_cleanup_owners_transferred(
        destination,
        foreign,
    )
    atomic_module._prune_publication_cleanup_owners(source)
    assert probes == []
    assert (
        BaseException.__getattribute__(
            source,
            "publication_cleanup_owners",
        )
        is foreign
    )

    local = LocalOwner()
    atomic_module._attach_publication_cleanup_owner(source, local)
    assert probes == []
    assert BaseException.__getattribute__(
        source,
        "publication_cleanup_owners",
    ) == (local,)


def test_quiescent_handoff_carrier_durably_retains_exact_operation_error() -> None:
    class LocalOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = LocalOwner()
    prior = LookupError("operation prior cause")
    primary = ValueError("exact interrupted operation primary")
    primary.__cause__ = prior
    settlement = OSError(errno.EIO, "interrupted operation settlement")
    interruption = KeyboardInterrupt("operation handoff replacement")

    with pytest.raises(KeyboardInterrupt) as caught:
        atomic_module._raise_quiescent_cleanup_handoff_boundary(
            interruption,
            settlement,
            (owner,),
            primary,
            label="operation handoff carrier",
        )

    assert caught.value is interruption
    assert BaseException.__getattribute__(
        interruption,
        "publication_cleanup_owners",
    ) == (owner,)
    carrier = interruption.__cause__
    assert type(carrier) is RuntimeError
    assert carrier.args[2] is settlement
    assert carrier.args[3] is primary
    assert carrier.__cause__ is settlement
    assert carrier.__context__ is primary
    assert primary.__cause__ is prior


def test_quiescent_handoff_carrier_breaks_self_secondary_cycle() -> None:
    class LocalOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = LocalOwner()
    prior = LookupError("self-secondary prior")
    primary = _HostileCleanupPrimary("self-secondary handoff")
    primary.__context__ = prior

    with pytest.raises(_HostileCleanupPrimary) as caught:
        atomic_module._raise_quiescent_cleanup_handoff_boundary(
            primary,
            primary,
            (owner,),
            None,
            label="self-secondary handoff carrier",
        )

    assert caught.value is primary
    assert _exception_link_graph_is_acyclic(primary)
    carrier = BaseException.__getattribute__(primary, "__cause__")
    assert type(carrier) is RuntimeError
    owners = BaseException.__getattribute__(
        carrier,
        "publication_cleanup_owners",
    )
    assert owners == (owner,)
    diagnostic_wrapper = BaseException.__getattribute__(carrier, "__cause__")
    assert type(diagnostic_wrapper) is RuntimeError
    assert BaseException.__getattribute__(diagnostic_wrapper, "__cause__") is None
    assert BaseException.__getattribute__(diagnostic_wrapper, "__context__") is None
    recovery_graph = _bounded_exception_recovery_graph(primary)
    assert any(candidate is primary for candidate in recovery_graph)
    assert any(candidate is prior for candidate in recovery_graph)
    assert any(candidate is owner for candidate in recovery_graph)
    owner.close()
    assert owner.closed


def test_quiescent_lifecycle_oversized_primary_uses_bounded_carrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    probes: list[int] = []

    class ForeignOwner:
        @property
        def closed(self) -> bool:
            probes.append(1)
            raise AssertionError("oversized callback owner was probed")

        def close(self) -> None:
            raise AssertionError("oversized callback owner was closed")

    primary = ValueError("callback with oversized cleanup metadata")
    BaseException.__setattr__(
        primary,
        "publication_cleanup_owners",
        tuple(
            ForeignOwner()
            for _index in range(atomic_module._MAX_PUBLICATION_CLEANUP_OWNERS + 1)
        ),
    )
    cleanup_error = OSError(errno.EIO, "persistent lifecycle release")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True

    def fail_lease_release(lease: object) -> None:
        if lease is reclaimer._lifecycle_lease and fail_release:
            raise cleanup_error
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        fail_lease_release,
    )

    with pytest.raises(ValueError) as caught:
        reclaimer._run_lifecycle_transition(lambda: (_ for _ in ()).throw(primary))

    assert caught.value is primary
    assert probes == []
    carrier = primary.__cause__
    assert type(carrier) is RuntimeError
    owners = BaseException.__getattribute__(
        carrier,
        "publication_cleanup_owners",
    )
    assert carrier.args[1] is owners
    assert carrier.args[2] is cleanup_error
    assert len(owners) == 1
    attempt = owners[0]
    assert type(attempt) is atomic_module._QuiescentDirectoryLifecycleAttempt

    fail_release = False
    attempt.close()
    assert attempt.closed
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


def test_quiescent_reclaimer_final_delivery_retains_settled_operation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt = parent / "attempt-a"
    _write_tree(attempt, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_unlink = atomic_module.os.unlink
    natural_failure = OSError(errno.EIO, "natural removal failure at final delivery")
    interruption = KeyboardInterrupt("settled lifecycle final delivery")

    def fail_unlink(_path: object, *, dir_fd: int | None = None) -> None:
        raise natural_failure

    monkeypatch.setattr(atomic_module.os, "unlink", fail_unlink)

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected,
            "if outcome.error is not None:",
            lambda: reclaimer.reclaim_child(attempt.name),
            predicate=lambda values: values.get("outcome") is not None
            and values["outcome"].error is natural_failure,
            error=interruption,
        )

    assert caught.value is interruption
    transition_frames = []
    traceback = caught.value.__traceback__
    transition_code = (
        atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected.__code__
    )
    while traceback is not None:
        if traceback.tb_frame.f_code is transition_code:
            transition_frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert transition_frames
    assert any(
        frame.f_locals["outcome"].error is natural_failure
        for frame in transition_frames
    )
    assert reclaimer in natural_failure.publication_cleanup_owners
    assert not reclaimer._lifecycle_lease.transition_active()

    monkeypatch.setattr(atomic_module.os, "unlink", real_unlink)
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer, first_result=True)
    reclaimer.close()


def test_quiescent_reclaimer_exact_receipt_recovers_lost_return(
    tmp_path: Path,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    owned = parent / "owned"
    _write_tree(owned, "payload.txt", "payload")
    orphan = atomic_module.discard_owned_directory(
        owned,
        capture_directory_ownership(owned),
    )
    assert orphan is not None
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    interruption = KeyboardInterrupt("reclaim public return interruption")

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_return(
            atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition,
            lambda: reclaimer.reclaim_orphan(orphan),
            predicate=lambda _frame, result: result is True,
            error=interruption,
        )

    assert caught.value is interruption
    assert not orphan.path.exists()
    assert reclaimer._active is None
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    assert reclaimer.reclaim_orphan(orphan) is True
    assert reclaimer.reclaim_child(orphan.path.name) is False
    reclaimer.close()


@pytest.mark.parametrize(
    ("source_fragment", "occurrence"),
    [
        ("locally_unwinding = context_error is not None", 2),
        ("locally_unwinding = context_error is not None", 3),
        ("_run_ordered_actions(failures)", 2),
        ("_run_ordered_actions(failures)", 3),
        ("_prune_publication_cleanup_owners(failures.primary_error)", 2),
        ("_prune_publication_cleanup_owners(failures.primary_error)", 3),
    ],
    ids=[
        "inner-entry",
        "outer-entry",
        "inner-call",
        "outer-call",
        "inner-return",
        "outer-return",
    ],
)
def test_quiescent_reclaimer_auto_releases_interrupted_finalizer(
    tmp_path: Path,
    source_fragment: str,
    occurrence: int,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    interruption = KeyboardInterrupt(
        f"lifecycle finalizer interruption {source_fragment} {occurrence}"
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._run_context_with_cleanup_actions.__wrapped__,
            source_fragment,
            lambda: reclaimer.reclaim_child("missing-attempt"),
            occurrence=occurrence,
            error=interruption,
        )

    assert caught.value is interruption
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    assert reclaimer.reclaim_child("missing-attempt") is False
    reclaimer.close()


@pytest.mark.parametrize(
    ("seam", "occurrence"),
    [("handler", 1), ("finalizer", 2), ("finalizer", 3)],
    ids=["handler-store", "inner-finalizer", "outer-finalizer"],
)
def test_quiescent_reclaimer_preserves_callback_error_across_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
    occurrence: int,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    natural_failure = ValueError("natural reclaimer callback failure")
    interruption = KeyboardInterrupt(f"callback {seam} interruption")

    def fail_reclaim(_reclaimer: object, _child_name: str) -> bool:
        raise natural_failure

    monkeypatch.setattr(
        atomic_module.QuiescentDirectoryReclaimer,
        "_reclaim_child",
        fail_reclaim,
    )
    function = (
        atomic_module._capture_quiescent_directory_lifecycle_outcome
        if seam == "handler"
        else atomic_module._run_context_with_cleanup_actions.__wrapped__
    )
    source_fragment = (
        "outcome.error = error"
        if seam == "handler"
        else "_run_ordered_actions(failures)"
    )
    predicate = (
        (lambda values: values.get("error") is natural_failure)
        if seam == "handler"
        else None
    )

    with pytest.raises(ValueError) as caught:
        _call_with_interrupt_on_source_line(
            function,
            source_fragment,
            lambda: reclaimer.reclaim_child("attempt-a"),
            predicate=predicate,
            occurrence=occurrence,
            error=interruption,
        )

    assert caught.value is natural_failure
    diagnostic_notes = (
        *getattr(caught.value, "__notes__", ()),
        *getattr(caught.value, "_codenib_cleanup_notes", ()),
    )
    assert caught.value.__cause__ is interruption or any(
        str(interruption) in note for note in diagnostic_notes
    )
    assert _exception_link_graph_is_acyclic(caught.value)
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


@pytest.mark.parametrize(
    "source_fragment",
    ["settle_attempt()", "raise  # preserve lifecycle settlement failure"],
)
def test_quiescent_reclaimer_outer_plan_covers_fallback_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_fragment: str,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    attempt_type = atomic_module._QuiescentDirectoryLifecycleAttempt
    real_close = attempt_type._close_borrowed
    natural_failure = OSError(errno.EIO, "initial lifecycle release failed")
    interruption = KeyboardInterrupt(f"fallback seam {source_fragment}")
    close_calls = 0

    def fail_first_close(attempt: object, execution_guard: object) -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise natural_failure
        real_close(attempt, execution_guard)

    monkeypatch.setattr(attempt_type, "_close_borrowed", fail_first_close)
    expected_error = (
        natural_failure if source_fragment == "settle_attempt()" else interruption
    )

    with pytest.raises(type(expected_error)) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module.QuiescentDirectoryReclaimer._run_lifecycle_transition_unprotected,
            source_fragment,
            lambda: reclaimer.reclaim_child("missing-attempt"),
            predicate=lambda values: values.get("primary_error") is natural_failure,
            error=interruption,
        )

    assert caught.value is expected_error
    _assert_quiescent_reclaimer_publicly_retryable(reclaimer)
    reclaimer.close()


def test_quiescent_reclaimer_exit_transfers_exhausted_lease_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    attempt_path = parent / "attempt-a"
    _write_tree(attempt_path, "payload.txt", "payload")
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    removal_failure = OSError(errno.EIO, "active removal failed")
    real_unlink = atomic_module.os.unlink

    def fail_unlink(_path: object, *, dir_fd: int | None = None) -> None:
        raise removal_failure

    monkeypatch.setattr(atomic_module.os, "unlink", fail_unlink)
    with pytest.raises(OSError) as initial:
        reclaimer.reclaim_child(attempt_path.name)
    assert initial.value is removal_failure
    assert reclaimer._active is not None

    body_primary = ValueError("context body primary")
    interruption = KeyboardInterrupt("persistent pre-release interruption")
    real_release = atomic_module._QuiescentDirectoryLifecycleLease.release
    fail_release = True
    release_calls = 0

    def interrupt_before_release(lease: object) -> None:
        nonlocal release_calls
        if lease is reclaimer._lifecycle_lease and fail_release:
            release_calls += 1
            raise interruption
        real_release(lease)

    monkeypatch.setattr(
        atomic_module._QuiescentDirectoryLifecycleLease,
        "release",
        interrupt_before_release,
    )

    with pytest.raises(ValueError) as caught:
        with reclaimer:
            raise body_primary

    assert caught.value is body_primary
    assert release_calls > atomic_module._MAX_ORDERED_ACTION_CANCELLATION_RETRIES
    owners = BaseException.__getattribute__(
        body_primary,
        "publication_cleanup_owners",
    )
    lease_owners = tuple(
        owner
        for owner in owners
        if isinstance(owner, atomic_module._QuiescentDirectoryLifecycleAttempt)
    )
    assert len(lease_owners) == 1
    assert sum(owner is reclaimer for owner in owners) == 1
    assert not lease_owners[0].closed

    fail_release = False
    lease_owners[0].close()
    assert lease_owners[0].closed
    monkeypatch.setattr(atomic_module.os, "unlink", real_unlink)
    assert reclaimer.retry() is True
    reclaimer.close()
    assert reclaimer.closed


def test_quiescent_reclaimer_retries_interrupted_close_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, parent_identity = _private_reclaimer_parent(tmp_path)
    reclaimer = atomic_module.QuiescentDirectoryReclaimer(
        parent,
        expected_parent_identity=parent_identity,
    )
    real_close = atomic_module._PublicationAuthorityOwner.close
    interruption = KeyboardInterrupt("post-authority-close interruption")
    interrupted = False

    def interrupt_after_close(owner: object, **kwargs: object) -> None:
        nonlocal interrupted
        real_close(owner, **kwargs)
        if owner is reclaimer._authority_owner and not interrupted:
            interrupted = True
            raise interruption

    monkeypatch.setattr(
        atomic_module._PublicationAuthorityOwner,
        "close",
        interrupt_after_close,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        reclaimer.close()

    assert caught.value is interruption
    assert not reclaimer.closed
    reclaimer.close()
    assert reclaimer.closed


def test_publish_restores_old_tree_when_rename_completes_then_interrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def interrupt_after_claim(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        real_rename(src, dst, src_dir_fd, dst_dir_fd)
        if src == destination.name and ".published.previous-" in dst and not injected:
            injected = True
            raise KeyboardInterrupt("post-claim interruption")

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", interrupt_after_claim)

    with pytest.raises(KeyboardInterrupt, match="post-claim interruption"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_final_rename_that_completes_before_interrupt_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def interrupt_after_publish(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        real_rename(src, dst, src_dir_fd, dst_dir_fd)
        if src == stage.name and dst == destination.name and not injected:
            injected = True
            raise KeyboardInterrupt("post-publish interruption")

    monkeypatch.setattr(
        atomic_module,
        "_rename_noreplace_at",
        interrupt_after_publish,
    )

    with pytest.raises(KeyboardInterrupt, match="post-publish interruption"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "new.txt").read_text(encoding="utf-8") == "new"


def test_published_callback_cannot_bypass_exact_tree_token(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    def mutate(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        assert reader.read_bytes("new.txt", max_bytes=16) == b"new"
        (destination / "late.txt").write_text("late", encoding="utf-8")

    with pytest.raises(RuntimeError, match="suspect output was quarantined"):
        publish_staged_directory(
            stage,
            destination,
            validate_published_destination=mutate,
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "late.txt").read_text(encoding="utf-8") == "late"


def test_publish_staged_directory_never_restores_swapped_callback_backup(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    stolen = tmp_path / "stolen-old"
    calls = 0

    def swap_on_second_validation(reader: object) -> None:
        nonlocal calls
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        assert reader.read_bytes("old.txt", max_bytes=16) == b"old"
        calls += 1
        if calls == 2:
            backup = next(tmp_path.glob(".published.previous-*"))
            backup.rename(stolen)
            _write_tree(backup, "foreign.txt", "preserve")
            raise RuntimeError("injected previous validation failure")

    with pytest.raises(
        RuntimeError,
        match="previous output identity lost",
    ):
        publish_staged_directory(
            stage,
            destination,
            validate_moved_destination=swap_on_second_validation,
        )

    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    previous = list(tmp_path.glob(".published.previous-*"))
    assert len(previous) == 1
    assert (previous[0] / "foreign.txt").read_text(encoding="utf-8") == "preserve"


def test_publication_fails_closed_on_unsupported_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    monkeypatch.setattr(atomic_module.sys, "platform", "freebsd14")

    with pytest.raises(RuntimeError, match="unsupported on this host"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_orphan_name_collision_budget_fails_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    monkeypatch.setattr(atomic_module.secrets, "token_hex", lambda _size: "a" * 32)
    collision = tmp_path / f".published.previous-{'a' * 32}"
    collision.write_text("foreign", encoding="utf-8")

    with pytest.raises(RuntimeError, match="claim directory under a bounded orphan"):
        publish_staged_directory(stage, destination)

    assert collision.read_text(encoding="utf-8") == "foreign"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_preserves_late_destination_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_claim = atomic_module._claim_child_as_orphan
    injected = False

    def mutate_then_claim(*args: object, **kwargs: object) -> Path:
        nonlocal injected
        if not injected:
            injected = True
            (destination / "late.txt").write_text("late", encoding="utf-8")
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(atomic_module, "_claim_child_as_orphan", mutate_then_claim)

    with pytest.raises(RuntimeError, match="changed during directory publication"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (destination / "late.txt").read_text(encoding="utf-8") == "late"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_preserves_nested_late_destination_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    nested = destination / "nested"
    nested.mkdir(parents=True)
    (nested / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_claim = atomic_module._claim_child_as_orphan
    injected = False

    def mutate_then_claim(*args: object, **kwargs: object) -> Path:
        nonlocal injected
        if not injected:
            injected = True
            (nested / "late.txt").write_text("late", encoding="utf-8")
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(atomic_module, "_claim_child_as_orphan", mutate_then_claim)

    with pytest.raises(RuntimeError, match="changed during directory publication"):
        publish_staged_directory(stage, destination)

    assert (destination / "nested" / "old.txt").read_text(encoding="utf-8") == "old"
    assert (destination / "nested" / "late.txt").read_text(encoding="utf-8") == "late"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_rejects_nested_stage_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    nested = stage / "nested"
    nested.mkdir(parents=True)
    (nested / "new.txt").write_text("new", encoding="utf-8")
    real_claim = atomic_module._claim_child_as_orphan
    injected = False

    def mutate_then_claim(*args: object, **kwargs: object) -> Path:
        nonlocal injected
        if not injected:
            injected = True
            (nested / "late.txt").write_text("late", encoding="utf-8")
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(atomic_module, "_claim_child_as_orphan", mutate_then_claim)

    with pytest.raises(RuntimeError, match="staged directory changed"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "nested" / "new.txt").read_text(encoding="utf-8") == "new"
    assert (stage / "nested" / "late.txt").read_text(encoding="utf-8") == "late"


def test_publish_staged_directory_rejects_same_size_destination_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    previous = destination / "old.txt"
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_claim = atomic_module._claim_child_as_orphan
    injected = False

    def rewrite_then_claim(*args: object, **kwargs: object) -> Path:
        nonlocal injected
        if not injected:
            injected = True
            previous.write_text("NEW", encoding="utf-8")
        return real_claim(*args, **kwargs)

    monkeypatch.setattr(atomic_module, "_claim_child_as_orphan", rewrite_then_claim)

    with pytest.raises(RuntimeError, match="changed during directory publication"):
        publish_staged_directory(stage, destination)

    assert previous.read_text(encoding="utf-8") == "NEW"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_preserves_raced_missing_target_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def race_missing_target(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        if src == stage.name and dst == destination.name and not injected:
            injected = True
            _write_tree(destination, "late.txt", "late")
        real_rename(src, dst, src_dir_fd, dst_dir_fd)

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", race_missing_target)

    with pytest.raises(FileExistsError):
        publish_staged_directory(
            stage,
            destination,
            expected_destination_identity=None,
        )

    assert (destination / "late.txt").read_text(encoding="utf-8") == "late"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_restores_old_tree_on_scan_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_require = atomic_module._require_tree_ownership_at

    def interrupt_moved_tree(
        authority: object,
        name: str,
        *,
        path: Path,
        expected: object,
        label: str,
        allow_root_rename: bool = False,
    ) -> None:
        if label == "moved destination":
            raise KeyboardInterrupt("injected ownership interruption")
        real_require(
            authority,
            name,
            path=path,
            expected=expected,
            label=label,
            allow_root_rename=allow_root_rename,
        )

    monkeypatch.setattr(
        atomic_module,
        "_require_tree_ownership_at",
        interrupt_moved_tree,
    )

    with pytest.raises(KeyboardInterrupt, match="ownership interruption"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_publish_restores_old_tree_when_moved_lstat_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_directory_or_missing = atomic_module._directory_or_missing_at

    def interrupt_moved_lstat(
        parent_descriptor: int,
        name: str,
        *,
        path: Path,
        label: str,
    ) -> os.stat_result | None:
        if label == "moved destination":
            raise KeyboardInterrupt("injected moved lstat interruption")
        return real_directory_or_missing(
            parent_descriptor,
            name,
            path=path,
            label=label,
        )

    monkeypatch.setattr(
        atomic_module,
        "_directory_or_missing_at",
        interrupt_moved_lstat,
    )

    with pytest.raises(KeyboardInterrupt, match="moved lstat interruption"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_restores_old_tree_when_final_stage_lstat_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_require = atomic_module._require_tree_ownership_at
    stage_checks = 0

    def interrupt_final_stage_check(
        authority: object,
        name: str,
        *,
        path: Path,
        expected: object,
        label: str,
        allow_root_rename: bool = False,
    ) -> None:
        nonlocal stage_checks
        if label == "staged directory":
            stage_checks += 1
            if stage_checks == 2:
                raise KeyboardInterrupt("injected final stage interruption")
        real_require(
            authority,
            name,
            path=path,
            expected=expected,
            label=label,
            allow_root_rename=allow_root_rename,
        )

    monkeypatch.setattr(
        atomic_module,
        "_require_tree_ownership_at",
        interrupt_final_stage_check,
    )

    with pytest.raises(KeyboardInterrupt, match="final stage interruption"):
        publish_staged_directory(stage, destination)

    assert stage_checks == 2
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_first_publication_fails_closed_without_safe_ownership_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(atomic_module, "_SAFE_OWNERSHIP_DIRECTORY_FDS", False)

    with pytest.raises(RuntimeError, match="no-follow directory-fd support"):
        publish_staged_directory(stage, destination)

    assert stage.is_dir()
    assert not destination.exists()


def test_existing_publication_fails_closed_without_safe_ownership_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    monkeypatch.setattr(atomic_module, "_SAFE_OWNERSHIP_DIRECTORY_FDS", False)

    with pytest.raises(RuntimeError, match="no-follow directory-fd support"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_quarantines_failed_published_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def mutate_after_publish(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        real_rename(src, dst, src_dir_fd, dst_dir_fd)
        if src == stage.name and dst == destination.name and not injected:
            injected = True
            (destination / "late.txt").write_text("late", encoding="utf-8")

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", mutate_after_publish)

    with pytest.raises(RuntimeError, match="suspect output was quarantined"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "late.txt").read_text(encoding="utf-8") == "late"


def test_published_boundary_quarantines_new_before_lost_backup_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    stolen = tmp_path / "stolen-old"
    real_rename = atomic_module._rename_noreplace_at
    injected = False

    def lose_backup_after_publish(
        src: str,
        dst: str,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal injected
        real_rename(src, dst, src_dir_fd, dst_dir_fd)
        if src == stage.name and dst == destination.name and not injected:
            injected = True
            backup = next(tmp_path.glob(".published.previous-*"))
            backup.rename(stolen)
            _write_tree(backup, "foreign.txt", "preserve")
            (destination / "late.txt").write_text("late", encoding="utf-8")

    monkeypatch.setattr(
        atomic_module,
        "_rename_noreplace_at",
        lose_backup_after_publish,
    )

    with pytest.raises(RuntimeError, match="previous output remains isolated"):
        publish_staged_directory(stage, destination)

    assert not destination.exists()
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    backups = list(tmp_path.glob(".published.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "late.txt").read_text(encoding="utf-8") == "late"


def test_published_callback_quarantines_new_before_lost_backup_check(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    stolen = tmp_path / "stolen-old"

    def lose_backup_during_validation(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        assert reader.read_bytes("new.txt", max_bytes=16) == b"new"
        backup = next(tmp_path.glob(".published.previous-*"))
        backup.rename(stolen)
        _write_tree(backup, "foreign.txt", "preserve")
        raise RuntimeError("injected published validation failure")

    with pytest.raises(RuntimeError, match="previous output remains isolated"):
        publish_staged_directory(
            stage,
            destination,
            validate_published_destination=lose_backup_during_validation,
        )

    assert not destination.exists()
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    backups = list(tmp_path.glob(".published.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_fails_closed_without_safe_cleanup_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("orphan-only publication must not delete")

    monkeypatch.setattr(atomic_module.os, "unlink", forbidden)
    monkeypatch.setattr(atomic_module.os, "rmdir", forbidden)

    orphan = publish_staged_directory(stage, destination)

    assert orphan is not None
    assert (orphan.path / "old.txt").read_text(encoding="utf-8") == "old"
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_removes_empty_sentinel_without_cleanup_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("first publication must not create/delete a sentinel")

    monkeypatch.setattr(atomic_module.os, "unlink", forbidden)
    monkeypatch.setattr(atomic_module.os, "rmdir", forbidden)

    orphan = publish_staged_directory(stage, destination)

    assert orphan is None
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_expected_stage_root_preserves_substituted_cleanup_path(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    expected = capture_directory_ownership(stage)
    stolen = tmp_path / "stolen-stage"
    stage.rename(stolen)
    _write_tree(stage, "foreign.txt", "preserve")

    with pytest.raises(RuntimeError, match="root changed before publication"):
        publish_staged_directory(
            stage,
            destination,
            expected_stage_root_ownership=expected,
        )
    atomic_module.discard_owned_directory(stage, expected)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stolen / "new.txt").read_text(encoding="utf-8") == "new"
    assert (stage / "foreign.txt").read_text(encoding="utf-8") == "preserve"


def test_publish_staged_directory_does_not_reacquire_swapped_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    stolen = tmp_path / "stolen-old"
    real_require = atomic_module._require_tree_ownership_at
    injected = False

    def swap_before_previous_check(
        authority: object,
        name: str,
        *,
        path: Path,
        expected: object,
        label: str,
        allow_root_rename: bool = False,
    ) -> None:
        nonlocal injected
        if label == "previous destination" and not injected:
            injected = True
            path.rename(stolen)
            _write_tree(path, "foreign.txt", "preserve")
        real_require(
            authority,
            name,
            path=path,
            expected=expected,
            label=label,
            allow_root_rename=allow_root_rename,
        )

    monkeypatch.setattr(
        atomic_module,
        "_require_tree_ownership_at",
        swap_before_previous_check,
    )

    with pytest.raises(RuntimeError, match="previous output identity lost"):
        publish_staged_directory(stage, destination)

    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    backups = list(tmp_path.glob(".published.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "foreign.txt").read_text(encoding="utf-8") == "preserve"


def test_publish_staged_directory_does_not_rollback_after_cleanup_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "one.txt", "one")
    (destination / "two.txt").write_text("two", encoding="utf-8")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    unlink_calls = 0

    def count_forbidden(*_args: object, **_kwargs: object) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        raise AssertionError("destructive cleanup is unreachable")

    monkeypatch.setattr(atomic_module.os, "unlink", count_forbidden)

    orphan = publish_staged_directory(stage, destination)

    assert unlink_calls == 0
    assert orphan is not None
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert (orphan.path / "one.txt").read_text(encoding="utf-8") == "one"
    assert (orphan.path / "two.txt").read_text(encoding="utf-8") == "two"


@pytest.mark.parametrize(
    ("constant", "limit", "files", "error"),
    [
        ("_MAX_OWNERSHIP_ENTRIES", 1, {"a": "1", "b": "2"}, "entry limit"),
        ("_MAX_OWNERSHIP_BYTES", 2, {"a": "123"}, "byte limit"),
        ("_MAX_OWNERSHIP_METADATA_BYTES", 1, {"a": "1"}, "metadata"),
        ("_MAX_OWNERSHIP_COMPONENT_BYTES", 1, {"aa": "1"}, "component"),
    ],
)
def test_publish_staged_directory_bounds_builtin_ownership_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    files: dict[str, str],
    error: str,
) -> None:
    short_names = constant == "_MAX_OWNERSHIP_COMPONENT_BYTES"
    destination = tmp_path / ("d" if short_names else "published")
    stage = tmp_path / ("s" if short_names else "stage")
    stage.mkdir()
    for name, content in files.items():
        (stage / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(atomic_module, constant, limit)

    with pytest.raises(RuntimeError, match=error):
        publish_staged_directory(stage, destination)

    assert not destination.exists()


def test_directory_ownership_is_independent_of_entry_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for name in ("a", "b", "c"):
        (first / name).write_text(name, encoding="utf-8")
    for name in ("c", "b", "a"):
        (second / name).write_text(name, encoding="utf-8")

    first_token = capture_directory_ownership(first)
    second_token = capture_directory_ownership(second)
    assert (
        first_token.digest,
        first_token.entries,
        first_token.byte_count,
        first_token.metadata_bytes,
    ) == (
        second_token.digest,
        second_token.entries,
        second_token.byte_count,
        second_token.metadata_bytes,
    )


def test_directory_ownership_inventory_comes_from_the_bounded_scan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "one.txt").write_text("one", encoding="utf-8")
    (nested / "two.txt").write_text("two", encoding="utf-8")

    ownership = capture_directory_ownership(root)

    assert directory_ownership_inventory(ownership) == (
        ("nested", "directory"),
        ("nested/two.txt", "file"),
        ("one.txt", "file"),
    )


@pytest.mark.parametrize(
    ("constant", "limit", "error"),
    [
        ("_MAX_SAFE_REMOVAL_DEPTH", 1, "depth limit"),
        ("_MAX_OWNERSHIP_PATH_BYTES", 3, "path exceeds"),
    ],
)
def test_directory_ownership_bounds_derived_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int,
    error: str,
) -> None:
    root = tmp_path / "root"
    nested = root / "aa" / "bb"
    nested.mkdir(parents=True)
    (nested / "value").write_text("value", encoding="utf-8")
    monkeypatch.setattr(atomic_module, constant, limit)

    with pytest.raises(RuntimeError, match=error):
        capture_directory_ownership(root)


def test_directory_ownership_reserves_parent_entries_before_child_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "one").write_text("one", encoding="utf-8")
    (child / "two").write_text("two", encoding="utf-8")
    monkeypatch.setattr(atomic_module, "_MAX_OWNERSHIP_ENTRIES", 2)

    with pytest.raises(RuntimeError, match="entry limit"):
        capture_directory_ownership(root)


def test_directory_ownership_binds_required_root_marker(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_text("payload", encoding="utf-8")

    with pytest.raises(RuntimeError, match="required marker"):
        capture_directory_ownership(
            root,
            required_root_file="manifest.json",
            allow_empty_root=True,
        )

    (root / "manifest.json").mkdir()
    with pytest.raises(RuntimeError, match="marker is not a regular file"):
        capture_directory_ownership(
            root,
            required_root_file="manifest.json",
            allow_empty_root=True,
        )


@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, ValueError, KeyboardInterrupt, BaseException],
)
def test_ownership_sort_stops_before_poisoned_future_run(
    error_type: type[BaseException],
) -> None:
    poisoned = False

    class PoisonedRuns:
        def __len__(self) -> int:
            return atomic_module._OWNERSHIP_SORT_RUN_ENTRIES + 1

        def __getitem__(self, item: object) -> object:
            nonlocal poisoned
            assert isinstance(item, slice)
            if item.start == 0:
                return list(range(atomic_module._OWNERSHIP_SORT_RUN_ENTRIES, 0, -1))
            poisoned = True
            raise AssertionError("ownership sort consumed its poisoned future run")

    stop = error_type("injected ownership sort cancellation")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(BaseException) as caught:
        atomic_module._interruptible_sorted_ownership_items(
            PoisonedRuns(),  # type: ignore[arg-type]
            key=None,
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poisoned


def test_ownership_sort_none_path_preserves_builtin_sort_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_merge(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("None ownership sort used the interruptible merge")

    monkeypatch.setattr(atomic_module.heapq, "merge", forbidden_merge)

    assert atomic_module._interruptible_sorted_ownership_items(
        [3, 1, 2],
        key=None,
        check_cancelled=None,
    ) == (1, 2, 3)


def test_required_ownership_marker_final_mismatch_precedes_armed_stop() -> None:
    armed = False
    cancellation_calls = 0
    stop = KeyboardInterrupt("armed after the final marker candidate")

    def matches(value: str) -> bool:
        nonlocal armed
        if value == "final":
            armed = True
        return False

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        if armed:
            raise stop

    assert not atomic_module._contains_required_ownership_marker(
        ("first", "final"),
        matches=matches,
        check_cancelled=check_cancelled,
    )
    assert armed
    assert cancellation_calls == 1


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX ownership scan")
def test_posix_ownership_callback_routes_all_bounded_sorts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "b").write_bytes(b"b")
    (root / "a").write_bytes(b"a")
    expected = capture_directory_ownership(root)
    real_sort = atomic_module._interruptible_sorted_ownership_items
    sort_calls = 0

    def check_cancelled() -> None:
        return None

    def observe_sort(
        entries: object,
        *,
        key: object,
        check_cancelled: object,
    ) -> object:
        nonlocal sort_calls
        assert check_cancelled is not None
        sort_calls += 1
        return real_sort(
            entries,  # type: ignore[arg-type]
            key=key,  # type: ignore[arg-type]
            check_cancelled=check_cancelled,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        atomic_module,
        "_interruptible_sorted_ownership_items",
        observe_sort,
    )

    observed = capture_directory_ownership(
        root,
        check_cancelled=check_cancelled,
    )

    assert observed == expected
    assert sort_calls == 4


def test_directory_ownership_detects_same_size_rewrite_across_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    payload = root / "payload"
    payload.write_bytes(b"original")
    real_read = atomic_module.os.read
    mutated = False

    def rewrite_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        result = real_read(descriptor, size)
        if result and not mutated:
            mutated = True
            payload.write_bytes(b"modified")
        return result

    expected = capture_directory_ownership(root)
    monkeypatch.setattr(atomic_module.os, "read", rewrite_after_read)

    try:
        raced = capture_directory_ownership(root)
    except RuntimeError as exc:
        assert "file changed" in str(exc)
    else:
        observed = capture_directory_ownership(root)
        assert raced != observed
        assert expected != observed


def test_expected_destination_ownership_rejects_identical_root_swap(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    expected = capture_directory_ownership(destination)
    stolen = tmp_path / "stolen"
    destination.rename(stolen)
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    with pytest.raises(RuntimeError, match="changed before directory publication"):
        publish_staged_directory(
            stage,
            destination,
            expected_destination_ownership=expected,
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"


def test_publish_staged_directory_rejects_mounted_tree_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    mounted = destination / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "external.txt").write_text("preserve", encoding="utf-8")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    real_mount_check = atomic_module._path_is_mount_point

    def fake_mount_check(path: Path, **kwargs: object) -> bool:
        return Path(path).name == "mounted" or real_mount_check(path, **kwargs)

    monkeypatch.setattr(atomic_module, "_path_is_mount_point", fake_mount_check)

    with pytest.raises(RuntimeError, match="ownership scan refuses mounted"):
        publish_staged_directory(stage, destination)

    assert (mounted / "external.txt").read_text(encoding="utf-8") == "preserve"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_quarantine_replace_failure_preserves_original_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()

    def fail_rename(
        _src: str,
        _dst: str,
        _src_dir_fd: int,
        _dst_dir_fd: int,
    ) -> None:
        raise OSError("injected quarantine rename failure")

    monkeypatch.setattr(atomic_module, "_rename_noreplace_at", fail_rename)

    with pytest.raises(OSError, match="injected quarantine rename failure"):
        atomic_module._quarantine_destination(destination)

    assert destination.is_dir()


def test_directory_check_rejects_windows_reparse_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o755,
        st_file_attributes=0x400,
    )
    monkeypatch.setattr(Path, "lstat", lambda _path: metadata)

    with pytest.raises(ValueError, match="link"):
        atomic_module._directory_or_missing(tmp_path / "junction", label="target")


def test_darwin_rename_uses_exclusive_renameatx_np(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, bytes, int, bytes, int]] = []

    class FakeRename:
        argtypes: object = None
        restype: object = None

        def __call__(
            self,
            source_fd: int,
            source: bytes,
            destination_fd: int,
            destination: bytes,
            flags: int,
        ) -> int:
            calls.append((source_fd, source, destination_fd, destination, flags))
            return 0

    fake_rename = FakeRename()
    fake_libc = SimpleNamespace(renameatx_np=fake_rename)
    monkeypatch.setattr(atomic_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        atomic_module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: fake_libc,
    )

    atomic_module._rename_noreplace_at("source", "target", 11, 12)

    assert calls == [(11, b"source", 12, b"target", 0x4)]


def test_darwin_exclusive_rename_preserves_existing_destination_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExistingRename:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_args: object) -> int:
            atomic_module.ctypes.set_errno(errno.EEXIST)
            return -1

    monkeypatch.setattr(atomic_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        atomic_module.ctypes,
        "CDLL",
        lambda *_args, **_kwargs: SimpleNamespace(renameatx_np=ExistingRename()),
    )

    with pytest.raises(FileExistsError):
        atomic_module._rename_noreplace_at("source", "target", 11, 12)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires real macOS syscalls")
def test_darwin_real_exclusive_rename_does_not_replace(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.txt").write_text("source", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "destination.txt").write_text("destination", encoding="utf-8")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            atomic_module._rename_noreplace_at(
                source.name,
                destination.name,
                descriptor,
                descriptor,
            )
    finally:
        os.close(descriptor)

    assert (source / "source.txt").read_text(encoding="utf-8") == "source"
    assert (destination / "destination.txt").read_text(
        encoding="utf-8"
    ) == "destination"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires real macOS syscalls")
def test_darwin_real_publication_reader_and_orphan_locator(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    observed: list[bytes] = []

    def validate(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        observed.append(reader.read_bytes("new.txt", max_bytes=16))

    orphan = publish_staged_directory(
        stage,
        destination,
        validate_staged_directory=validate,
        validate_published_destination=validate,
    )

    assert orphan is not None
    assert observed == [b"new", b"new"]
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("old.txt", max_bytes=16))
        == b"old"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="requires real macOS syscalls")
def test_darwin_real_authority_closes_fds_after_reader_failure(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    _write_tree(stage, "payload.txt", "payload")
    before = len(os.listdir("/dev/fd"))

    for _attempt in range(16):
        authority = atomic_module._open_publication_authority(
            tmp_path,
            parent_resource=None,
            expected_parent_identity=None,
        )
        try:
            ownership = authority.capture_child(
                stage.name,
                path=stage,
                label="stage",
            )

            def fail(reader: object) -> None:
                assert isinstance(reader, atomic_module.PublicationDirectoryReader)
                reader.read_bytes("missing.txt", max_bytes=16)

            with pytest.raises(ValueError, match="absent from captured ownership"):
                authority.read_child(
                    stage.name,
                    path=stage,
                    label="stage",
                    expected_ownership=ownership,
                    callback=fail,
                )
        finally:
            authority.close()

    assert len(os.listdir("/dev/fd")) <= before


def test_publication_authority_has_no_proc_path_dependency() -> None:
    atomic_source = inspect.getsource(atomic_module)
    windows_source = inspect.getsource(windows_authority_module)
    assert "/proc/self/fd" not in atomic_source
    assert "/proc/self/fd" not in windows_source
    assert "renameatx_np" in atomic_source
    assert "SetFileInformationByHandle" in windows_source


def test_windows_handle_primitives_are_owned_by_neutral_module() -> None:
    atomic_source = inspect.getsource(atomic_module)
    windows_source = inspect.getsource(windows_authority_module)
    assert atomic_module._WindowsKernelApi is windows_authority_module.WindowsKernelApi
    assert (
        atomic_module._WindowsHandleMetadata
        is windows_authority_module.WindowsHandleMetadata
    )
    assert (
        atomic_module._WindowsDirectoryEntry
        is windows_authority_module.WindowsDirectoryEntry
    )
    assert "class _WindowsKernelApi" not in atomic_source
    assert "OpenFileById" not in atomic_source
    assert "SetFileInformationByHandle" not in atomic_source
    assert "OpenFileById" in windows_source
    assert "SetFileInformationByHandle" in windows_source
    assert "_atomic_directory" not in windows_source


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux fd accounting",
)
def test_linux_authority_closes_fds_after_preopen_authentication_failure(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    nested = stage / "nested"
    _write_tree(nested, "payload.txt", "payload")
    before = len(os.listdir("/proc/self/fd"))
    authority = atomic_module._open_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        ownership = authority.capture_child(
            stage.name,
            path=stage,
            label="stage",
        )
        original = nested / "payload.txt"
        held = nested / ".payload.txt.held"
        original.rename(held)
        original.write_bytes(b"foreign")

        def read_replaced_file(reader: object) -> None:
            assert isinstance(reader, atomic_module.PublicationDirectoryReader)
            reader.read_bytes("nested/payload.txt", max_bytes=16)

        with pytest.raises(RuntimeError, match="differs from captured ownership"):
            authority.read_child(
                stage.name,
                path=stage,
                label="stage",
                expected_ownership=ownership,
                callback=read_replaced_file,
            )
    finally:
        authority.close()

    assert len(os.listdir("/proc/self/fd")) <= before


def test_windows_ownership_and_authenticated_reader_traverse_file_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    nested = api.add_directory(api.root_id, "nested")
    nested_file = api.add_file(nested, "one.txt", b"one")
    root_file = api.add_file(api.root_id, "two.txt", b"two")
    root_handle = api.create_directory_handle(Path("C:/authority"))
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            root_handle,
            Path("C:/authority"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
        reader = atomic_module.PublicationDirectoryReader(
            Path("C:/authority"),
            ownership.root_identity,
            lambda required_root_file, allow_empty_root, entry_policy: (
                atomic_module._capture_windows_directory_handle(
                    api,
                    root_handle,
                    Path("C:/authority"),
                    required_root_file=required_root_file,
                    allow_empty_root=allow_empty_root,
                    entry_policy=entry_policy,
                )
            ),
            lambda relative, max_bytes, expected: (
                atomic_module._open_windows_authenticated_file(
                    api,
                    root_handle,
                    Path("C:/authority"),
                    relative,
                    max_bytes=max_bytes,
                    expected=expected,
                )
            ),
            ownership,
        )
        try:
            snapshot = reader.authenticated_snapshot(
                "nested/one.txt",
                max_bytes=16,
            )
        finally:
            reader._deactivate()
    finally:
        api.close(root_handle)

    assert ownership.inventory == (
        ("nested", "directory"),
        ("nested/one.txt", "file"),
        ("two.txt", "file"),
    )
    assert snapshot.read_bytes() == b"one"
    assert snapshot.record.sha256 == hashlib.sha256(b"one").hexdigest()
    opened_ids = {
        file_id for _volume, file_id, _access, _directory in api.open_by_id_calls
    }
    assert {nested, nested_file, root_file} <= opened_ids


def test_windows_ownership_rejects_zero_directory_entry_file_id() -> None:
    api = _FakeWindowsApi()
    api.add_file(api.root_id, "payload.txt", b"payload")
    root_handle = api.create_directory_handle(Path("C:/authority"))
    iter_directory = api.iter_directory

    def iterate_with_zero_id(handle: int):
        yield from (
            atomic_module._WindowsDirectoryEntry(
                name=entry.name,
                file_id=0,
                attributes=entry.attributes,
            )
            for entry in iter_directory(handle)
        )

    api.iter_directory = iterate_with_zero_id  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="reliable FILE_ID"):
            atomic_module._capture_windows_directory_handle(
                api,
                root_handle,
                Path("C:/authority"),
                required_root_file=None,
                allow_empty_root=False,
                entry_policy=None,
            )
    finally:
        api.close(root_handle)


def test_windows_ownership_uses_streaming_directory_enumeration() -> None:
    api = _FakeWindowsApi()
    api.add_file(api.root_id, "one.txt", b"one")
    api.add_file(api.root_id, "two.txt", b"two")
    root_handle = api.create_directory_handle(Path("C:/authority"))

    def forbid_materialized_enumeration(_handle: int) -> tuple[object, ...]:
        raise AssertionError("ownership scan must not materialize enumeration first")

    api.enumerate_directory = forbid_materialized_enumeration  # type: ignore[method-assign]
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            root_handle,
            Path("C:/authority"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
    finally:
        api.close(root_handle)

    assert ownership.inventory == (("one.txt", "file"), ("two.txt", "file"))


def test_windows_ownership_callback_routes_all_bounded_sorts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    api.add_file(api.root_id, "two.txt", b"two")
    api.add_file(api.root_id, "one.txt", b"one")
    root_handle = api.create_directory_handle(Path("C:/authority"))
    real_sort = atomic_module._interruptible_sorted_ownership_items
    sort_calls = 0

    def check_cancelled() -> None:
        return None

    def observe_sort(
        entries: object,
        *,
        key: object,
        check_cancelled: object,
    ) -> object:
        nonlocal sort_calls
        assert check_cancelled is not None
        sort_calls += 1
        return real_sort(
            entries,  # type: ignore[arg-type]
            key=key,  # type: ignore[arg-type]
            check_cancelled=check_cancelled,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        atomic_module,
        "_interruptible_sorted_ownership_items",
        observe_sort,
    )
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            root_handle,
            Path("C:/authority"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
            check_cancelled=check_cancelled,
        )
    finally:
        api.close(root_handle)

    assert ownership.inventory == (("one.txt", "file"), ("two.txt", "file"))
    assert sort_calls == 4


def test_windows_ownership_stops_stream_when_entry_budget_is_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    for name in ("one.txt", "two.txt", "unreachable.txt"):
        api.add_file(api.root_id, name, name.encode())
    root_handle = api.create_directory_handle(Path("C:/authority"))
    real_iter = api.iter_directory
    yielded: list[str] = []

    def bounded_iter(handle: int):
        for entry in real_iter(handle):
            if len(yielded) == 2:
                raise AssertionError("ownership scan consumed beyond its entry budget")
            yielded.append(entry.name)
            yield entry

    api.iter_directory = bounded_iter  # type: ignore[method-assign]
    monkeypatch.setattr(atomic_module, "_MAX_OWNERSHIP_ENTRIES", 1)
    try:
        with pytest.raises(RuntimeError, match="entry limit"):
            atomic_module._capture_windows_directory_handle(
                api,
                root_handle,
                Path("C:/authority"),
                required_root_file=None,
                allow_empty_root=False,
                entry_policy=None,
            )
    finally:
        api.close(root_handle)

    assert yielded == ["one.txt", "two.txt"]


def test_windows_publication_rejects_ancestor_swap_during_lexical_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    volume_children = api.nodes[api.volume_root_id]["children"]
    assert isinstance(volume_children, dict)
    volume_children.pop("authority")
    trusted = api.add_directory(api.volume_root_id, "trusted")
    trusted_children = api.nodes[trusted]["children"]
    assert isinstance(trusted_children, dict)
    trusted_children["authority"] = api.root_id
    foreign = api.add_directory()
    api.add_directory(foreign, "authority")
    real_iter = api.iter_directory
    opened_paths: list[str] = []
    real_create = api.create_directory_handle
    swapped = False

    def create_anchor_only(path: Path) -> int:
        opened_paths.append(str(path))
        normalized = str(path).replace("/", "\\").rstrip("\\").casefold()
        if normalized != "c:":
            raise AssertionError("publication must not reopen the full lexical path")
        return real_create(path)

    def swap_before_binding_check(handle: int):
        nonlocal swapped
        if api.handles[handle] == api.volume_root_id and not swapped:
            swapped = True
            volume_children["trusted"] = foreign
        yield from real_iter(handle)

    api.create_directory_handle = create_anchor_only  # type: ignore[method-assign]
    api.iter_directory = swap_before_binding_check  # type: ignore[method-assign]
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)

    with pytest.raises(RuntimeError, match="changed while opening"):
        atomic_module._open_windows_publication_authority(
            Path("C:/trusted/authority"),
            parent_resource=None,
            expected_parent_identity=None,
        )

    assert swapped
    assert opened_paths == ["C:\\"]
    assert api.handles == {}


def test_windows_publication_retains_lexical_ancestor_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    volume_children = api.nodes[api.volume_root_id]["children"]
    assert isinstance(volume_children, dict)
    volume_children.pop("authority")
    trusted = api.add_directory(api.volume_root_id, "trusted")
    trusted_children = api.nodes[trusted]["children"]
    assert isinstance(trusted_children, dict)
    trusted_children["authority"] = api.root_id
    foreign = api.add_directory()
    api.add_directory(foreign, "authority")
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)

    authority = atomic_module._open_windows_publication_authority(
        Path("C:/trusted/authority"),
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        volume_children["trusted"] = foreign
        with pytest.raises(RuntimeError, match="binding changed"):
            authority.verify_path_binding()
    finally:
        authority.close()

    assert api.handles == {}


def test_windows_publication_accepts_lexical_component_case_variation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)

    authority = atomic_module._open_windows_publication_authority(
        Path("C:/AUTHORITY"),
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        authority.verify_path_binding()
    finally:
        authority.close()

    assert api.handles == {}


def test_windows_authority_closes_handles_after_reader_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    stage = api.add_directory(api.root_id, "stage")
    api.add_file(stage, "payload.txt", b"payload")
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)

    authority = atomic_module._open_windows_publication_authority(
        Path("C:/authority"),
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        ownership = authority.capture_child(
            "stage",
            path=Path("C:/authority/stage"),
            label="stage",
        )

        def fail(reader: object) -> None:
            assert isinstance(reader, atomic_module.PublicationDirectoryReader)
            reader.read_bytes("missing.txt", max_bytes=16)

        with pytest.raises(ValueError, match="absent from captured ownership"):
            authority.read_child(
                "stage",
                path=Path("C:/authority/stage"),
                label="stage",
                expected_ownership=ownership,
                callback=fail,
            )
        wrong_root = replace(
            ownership,
            root_identity=(
                ownership.root_identity[0],
                ownership.root_identity[1] + 1,
                *ownership.root_identity[2:],
            ),
        )
        with pytest.raises(RuntimeError, match="differs from captured ownership"):
            authority.read_child(
                "stage",
                path=Path("C:/authority/stage"),
                label="stage",
                expected_ownership=wrong_root,
                callback=lambda _reader: None,
            )
    finally:
        authority.close()

    assert api.handles == {}


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_handle_rename_does_not_replace(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_tree(source, "source.txt", "source")
    destination = tmp_path / "destination"
    _write_tree(destination, "destination.txt", "destination")
    authority = atomic_module._open_windows_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        with pytest.raises(FileExistsError):
            authority.rename_noreplace(source.name, destination.name)
    finally:
        authority.close()

    assert (source / "source.txt").read_text(encoding="utf-8") == "source"
    assert (destination / "destination.txt").read_text(
        encoding="utf-8"
    ) == "destination"


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_publication_blocks_root_rename_and_reopens_orphan(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")
    held = tmp_path / "held"
    observed: list[bytes] = []

    def validate_stage(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        with pytest.raises(OSError):
            stage.rename(held)
        observed.append(reader.read_bytes("new.txt", max_bytes=16))

    orphan = publish_staged_directory(
        stage,
        destination,
        validate_staged_directory=validate_stage,
        validate_published_destination=lambda reader: observed.append(
            reader.read_bytes("new.txt", max_bytes=16)
        ),
    )

    assert orphan is not None
    assert observed == [b"new", b"new"]
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("old.txt", max_bytes=16))
        == b"old"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_mode_contract_is_readonly_and_execute_neutral(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    writable = stage / "writable.bin"
    writable.write_bytes(b"writable")
    readonly = stage / "readonly.bin"
    readonly.write_bytes(b"readonly")
    readonly.chmod(stat.S_IREAD)
    executable = stage / "tool.exe"
    executable.write_bytes(b"tool")
    executable.chmod(0o755)
    try:
        ownership = capture_directory_ownership(stage)
        modes = {
            record.path: record.mode
            for record in atomic_module.directory_ownership_file_records(ownership)
        }
        assert modes == {
            "readonly.bin": 0o444,
            "tool.exe": 0o666,
            "writable.bin": 0o666,
        }

        def require_posix_modes(
            path: str,
            kind: str,
            mode: int,
            _size: int,
        ) -> None:
            if kind == "file" and mode not in {0o644, 0o755}:
                raise ValueError(f"unrepresentable portable mode: {path}")

        with pytest.raises(ValueError, match="unrepresentable portable mode"):
            capture_directory_ownership(stage, entry_policy=require_posix_modes)
    finally:
        readonly.chmod(stat.S_IWRITE)


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_publishable_stage_rejects_hard_link(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "payload.txt", "payload")
    alias = tmp_path / "payload-alias.txt"
    os.link(stage / "payload.txt", alias)

    with pytest.raises(RuntimeError, match="external hard link"):
        publish_staged_directory(stage, destination)

    assert alias.read_text(encoding="utf-8") == "payload"
    assert (stage / "payload.txt").read_text(encoding="utf-8") == "payload"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_authority_closes_handles_after_reader_failure(
    tmp_path: Path,
) -> None:
    import ctypes.wintypes as wintypes

    kernel32 = atomic_module.ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (
        wintypes.HANDLE,
        atomic_module.ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            atomic_module.ctypes.byref(count),
        ):
            raise atomic_module.ctypes.WinError(atomic_module.ctypes.get_last_error())
        return int(count.value)

    stage = tmp_path / "stage"
    _write_tree(stage, "payload.txt", "payload")
    before = handle_count()
    for _attempt in range(16):
        authority = atomic_module._open_windows_publication_authority(
            tmp_path,
            parent_resource=None,
            expected_parent_identity=None,
        )
        try:
            ownership = authority.capture_child(
                stage.name,
                path=stage,
                label="stage",
            )

            def fail(reader: object) -> None:
                assert isinstance(reader, atomic_module.PublicationDirectoryReader)
                reader.read_bytes("missing.txt", max_bytes=16)

            with pytest.raises(ValueError, match="absent from captured ownership"):
                authority.read_child(
                    stage.name,
                    path=stage,
                    label="stage",
                    expected_ownership=ownership,
                    callback=fail,
                )
        finally:
            authority.close()

    assert handle_count() <= before


def test_windows_authority_renames_source_handle_without_replace_or_share_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    stage = api.add_directory(api.root_id, "stage")
    api.add_file(stage, "payload.txt", b"payload")
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)

    authority = atomic_module._open_windows_publication_authority(
        Path("C:/authority"),
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        ownership = authority.capture_child(
            "stage",
            path=Path("C:/authority/stage"),
            label="stage",
        )
        authority.rename_noreplace("stage", "published")
        authority.verify_path_binding()
    finally:
        authority.close()

    assert ownership.inventory == (("payload.txt", "file"),)
    assert api.rename_calls
    source_opens = [
        call
        for call in api.open_by_id_calls
        if call[1] == stage and call[2] & atomic_module._WINDOWS_DELETE
    ]
    assert source_opens
    implementation = inspect.getsource(atomic_module._WindowsKernelApi)
    assert "ReplaceIfExists = False" in implementation
    assert "FILE_SHARE_DELETE" not in implementation


def test_windows_stage_gate_rejects_external_file_id_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    stage = api.add_directory(api.root_id, "stage")
    payload = api.add_file(stage, "payload.txt", b"payload")
    api.nodes[payload]["nlink"] = 2
    root_handle = api._new_handle(stage)
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            root_handle,
            Path("C:/authority/stage"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
    finally:
        api.close(root_handle)

    with pytest.raises(RuntimeError, match="external hard link"):
        atomic_module.require_publishable_directory_ownership(ownership)
    assert api.nodes[payload]["data"] == b"payload"


def test_windows_modes_follow_readonly_attributes_without_posix_fabrication() -> None:
    writable_file = atomic_module._windows_mode_from_attributes(0)
    readonly_file = atomic_module._windows_mode_from_attributes(
        atomic_module._WINDOWS_FILE_ATTRIBUTE_READONLY
    )
    writable_directory = atomic_module._windows_mode_from_attributes(
        atomic_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY
    )
    readonly_directory = atomic_module._windows_mode_from_attributes(
        atomic_module._WINDOWS_FILE_ATTRIBUTE_DIRECTORY
        | atomic_module._WINDOWS_FILE_ATTRIBUTE_READONLY
    )

    assert stat.S_IMODE(writable_file) == 0o666
    assert stat.S_IMODE(readonly_file) == 0o444
    assert stat.S_IMODE(writable_directory) == 0o777
    assert stat.S_IMODE(readonly_directory) == 0o555


@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("payload:stream", "canonical Windows"),
        ("CON", "canonical Windows"),
        ("trailing.", "canonical Windows"),
        ("trailing ", "canonical Windows"),
    ],
)
def test_windows_authority_rejects_aliased_child_names(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    error: str,
) -> None:
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    with pytest.raises(ValueError, match=error):
        atomic_module._simple_child_name(name, label="child")


def test_publishable_stage_rejects_external_posix_hard_link(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    stage = tmp_path / "stage"
    _write_tree(stage, "payload.txt", "payload")
    alias = tmp_path / "payload-alias.txt"
    os.link(stage / "payload.txt", alias)

    with pytest.raises(RuntimeError, match="external hard link"):
        publish_staged_directory(stage, destination)

    assert alias.read_text(encoding="utf-8") == "payload"
    assert (stage / "payload.txt").read_text(encoding="utf-8") == "payload"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"


def test_legacy_destination_hard_link_can_be_observed_and_isolated(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    _write_tree(destination, "old.txt", "old")
    alias = tmp_path / "old-alias.txt"
    os.link(destination / "old.txt", alias)
    stage = tmp_path / "stage"
    _write_tree(stage, "new.txt", "new")

    orphan = publish_staged_directory(stage, destination)

    assert orphan is not None
    assert alias.read_text(encoding="utf-8") == "old"
    assert (orphan.path / "old.txt").read_text(encoding="utf-8") == "old"
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_directory_orphan_reopens_through_parent_authority(tmp_path: Path) -> None:
    parent = tmp_path / "authority"
    parent.mkdir()
    destination = parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = parent / "stage"
    _write_tree(stage, "new.txt", "new")

    orphan = publish_staged_directory(stage, destination)

    assert orphan is not None
    rebound = orphan.rebind()
    assert rebound.digest == orphan.ownership_digest
    saved_reader: list[object] = []

    def read_old(reader: object) -> bytes:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        saved_reader.append(reader)
        return reader.read_bytes("old.txt", max_bytes=16)

    assert orphan.reopen(read_old) == b"old"
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_reader[0].capture_ownership()


def test_directory_orphan_callback_primary_survives_post_content_drift(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "authority"
    parent.mkdir()
    destination = parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = parent / "stage"
    _write_tree(stage, "new.txt", "new")
    orphan = publish_staged_directory(stage, destination)
    assert orphan is not None
    primary = ValueError("callback failed")
    saved_reader: list[atomic_module.PublicationDirectoryReader] = []

    def mutate_then_fail(reader: atomic_module.PublicationDirectoryReader) -> None:
        saved_reader.append(reader)
        (orphan.path / "old.txt").write_text("mutated", encoding="utf-8")
        raise primary

    with pytest.raises(ValueError) as caught:
        orphan.reopen(mutate_then_fail)

    assert caught.value is primary
    notes = _exception_notes(primary)
    assert any(
        "directory orphan post-callback ownership validation also failed" in note
        and ("changed" in note or "differs" in note)
        for note in notes
    )
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_reader[0].capture_ownership()


def _posix_cleanup_test_reader(
    root: Path,
) -> tuple[atomic_module.PublicationDirectoryReader, int]:
    ownership = capture_directory_ownership(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    root_descriptor = os.open(root, flags)
    reader = atomic_module.PublicationDirectoryReader(
        root,
        ownership.root_identity,
        lambda _required, _allow_empty, _policy: ownership,
        lambda relative, max_bytes, expected: (
            atomic_module._open_posix_authenticated_file(
                root_descriptor,
                root,
                relative,
                max_bytes=max_bytes,
                expected=expected,
            )
        ),
        ownership,
    )
    return reader, root_descriptor


def _windows_cleanup_test_reader() -> tuple[
    _FakeWindowsApi,
    atomic_module.PublicationDirectoryReader,
    int,
]:
    api = _FakeWindowsApi()
    nested = api.add_directory(api.root_id, "nested")
    api.add_file(nested, "payload.txt", b"payload")
    root_path = Path("C:/authority")
    root_handle = api.create_directory_handle(root_path)
    ownership = atomic_module._capture_windows_directory_handle(
        api,
        root_handle,
        root_path,
        required_root_file=None,
        allow_empty_root=False,
        entry_policy=None,
    )
    reader = atomic_module.PublicationDirectoryReader(
        root_path,
        ownership.root_identity,
        lambda _required, _allow_empty, _policy: ownership,
        lambda relative, max_bytes, expected: (
            atomic_module._open_windows_authenticated_file(
                api,
                root_handle,
                root_path,
                relative,
                max_bytes=max_bytes,
                expected=expected,
            )
        ),
        ownership,
    )
    return api, reader, root_handle


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX directory descriptors",
)
@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_posix_authenticated_file_body_primary_survives_cleanup_faults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
) -> None:
    root = tmp_path / "publication"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_bytes(b"payload")
    reader, root_descriptor = _posix_cleanup_test_reader(root)
    real_close = os.close
    primary = primary_type("body-primary")
    close_errors: list[OSError] = []

    def fail_finalize(_authenticated: object) -> None:
        raise RuntimeError("finalize-secondary")

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        error = OSError(f"close-secondary-{len(close_errors)}")
        close_errors.append(error)
        raise error

    try:
        with monkeypatch.context() as faults:
            faults.setattr(
                atomic_module.PublicationAuthenticatedFile,
                "_finalize",
                fail_finalize,
            )
            faults.setattr(atomic_module.os, "close", close_then_fail)
            with pytest.raises(primary_type, match="body-primary") as caught:
                with reader.open_authenticated_file(
                    "nested/payload.txt",
                    max_bytes=16,
                ):
                    raise primary

        assert caught.value is primary
        notes = _exception_notes(primary)
        assert any(
            "file finalization also failed" in note and "finalize-secondary" in note
            for note in notes
        )
        assert any("file descriptor cleanup also failed" in note for note in notes)
        assert any("directory descriptor cleanup also failed" in note for note in notes)
        assert len(close_errors) == 3
        assert reader._authentication_failed is True
        with pytest.raises(RuntimeError, match="suppressed authentication failure"):
            reader._require_valid()
    finally:
        reader._deactivate()
        real_close(root_descriptor)


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_windows_authenticated_file_body_primary_survives_cleanup_faults(
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
) -> None:
    api, reader, root_handle = _windows_cleanup_test_reader()
    real_close = api.close
    primary = primary_type("body-primary")
    close_errors: list[OSError] = []

    def fail_finalize(_authenticated: object) -> None:
        raise RuntimeError("finalize-secondary")

    def close_then_fail(handle: int) -> None:
        real_close(handle)
        error = OSError(f"close-secondary-{len(close_errors)}")
        close_errors.append(error)
        raise error

    try:
        with monkeypatch.context() as faults:
            faults.setattr(
                atomic_module.PublicationAuthenticatedFile,
                "_finalize",
                fail_finalize,
            )
            faults.setattr(api, "close", close_then_fail)
            with pytest.raises(primary_type, match="body-primary") as caught:
                with reader.open_authenticated_file(
                    "nested/payload.txt",
                    max_bytes=16,
                ):
                    raise primary

        assert caught.value is primary
        notes = _exception_notes(primary)
        assert any(
            "file finalization also failed" in note and "finalize-secondary" in note
            for note in notes
        )
        assert any("file HANDLE cleanup also failed" in note for note in notes)
        assert any("directory HANDLE cleanup also failed" in note for note in notes)
        assert len(close_errors) == 2
        assert reader._authentication_failed is True
        with pytest.raises(RuntimeError, match="suppressed authentication failure"):
            reader._require_valid()
    finally:
        reader._deactivate()
        real_close(root_handle)
    assert api.handles == {}


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX directory descriptors",
)
@pytest.mark.parametrize("failure_phase", ["finalize", "close"])
def test_posix_authenticated_file_cleanup_primary_is_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    root = tmp_path / "publication"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_bytes(b"payload")
    reader, root_descriptor = _posix_cleanup_test_reader(root)
    real_close = os.close
    cleanup_primary = SystemExit(f"{failure_phase}-primary")
    close_calls = 0

    def fail_finalize(_authenticated: object) -> None:
        raise cleanup_primary

    def close_with_first_fault(descriptor: int) -> None:
        nonlocal close_calls
        real_close(descriptor)
        close_calls += 1
        if close_calls == 1:
            raise cleanup_primary

    try:
        with monkeypatch.context() as faults:
            if failure_phase == "finalize":
                faults.setattr(
                    atomic_module.PublicationAuthenticatedFile,
                    "_finalize",
                    fail_finalize,
                )
            else:
                faults.setattr(atomic_module.os, "close", close_with_first_fault)
            with pytest.raises(SystemExit, match=f"{failure_phase}-primary") as caught:
                with reader.open_authenticated_file(
                    "nested/payload.txt",
                    max_bytes=16,
                ):
                    pass

        assert caught.value is cleanup_primary
        if failure_phase == "close":
            assert close_calls == 3
        assert reader._authentication_failed is True
        with pytest.raises(RuntimeError, match="suppressed authentication failure"):
            reader._require_valid()
    finally:
        reader._deactivate()
        real_close(root_descriptor)


@pytest.mark.parametrize("failure_phase", ["finalize", "close"])
def test_windows_authenticated_file_cleanup_primary_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    api, reader, root_handle = _windows_cleanup_test_reader()
    real_close = api.close
    cleanup_primary = SystemExit(f"{failure_phase}-primary")
    close_calls = 0

    def fail_finalize(_authenticated: object) -> None:
        raise cleanup_primary

    def close_with_first_fault(handle: int) -> None:
        nonlocal close_calls
        real_close(handle)
        close_calls += 1
        if close_calls == 1:
            raise cleanup_primary

    try:
        with monkeypatch.context() as faults:
            if failure_phase == "finalize":
                faults.setattr(
                    atomic_module.PublicationAuthenticatedFile,
                    "_finalize",
                    fail_finalize,
                )
            else:
                faults.setattr(api, "close", close_with_first_fault)
            with pytest.raises(SystemExit, match=f"{failure_phase}-primary") as caught:
                with reader.open_authenticated_file(
                    "nested/payload.txt",
                    max_bytes=16,
                ):
                    pass

        assert caught.value is cleanup_primary
        if failure_phase == "close":
            assert close_calls == 2
        assert reader._authentication_failed is True
        with pytest.raises(RuntimeError, match="suppressed authentication failure"):
            reader._require_valid()
    finally:
        reader._deactivate()
        real_close(root_handle)
    assert api.handles == {}


def test_publication_reader_streams_and_authenticates_on_context_exit(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    payload = (b"0123456789abcdef" * 131_072) + b"tail"
    stage.mkdir()
    (stage / "documents.json").write_bytes(payload)
    records: list[object] = []
    prefixes: list[bytes] = []

    def validate(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        with reader.open_authenticated_file(
            "documents.json",
            max_bytes=4 << 20,
        ) as authenticated:
            prefixes.append(authenticated.read(17))
            # Stop early deliberately. Context exit must drain and authenticate
            # the same handle without retaining the complete payload.
        records.append(authenticated.record)

    orphan = publish_staged_directory(
        stage,
        destination,
        validate_staged_directory=validate,
        validate_published_destination=validate,
    )

    assert orphan is None
    assert prefixes == [payload[:17], payload[:17]]
    assert all(
        record.sha256 == hashlib.sha256(payload).hexdigest() for record in records
    )


def test_directory_orphan_rebind_rejects_replaced_parent(tmp_path: Path) -> None:
    parent = tmp_path / "authority"
    parent.mkdir()
    destination = parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = parent / "stage"
    _write_tree(stage, "new.txt", "new")
    orphan = publish_staged_directory(stage, destination)
    assert orphan is not None
    moved_parent = tmp_path / "moved-authority"
    parent.rename(moved_parent)
    parent.mkdir()

    with pytest.raises(RuntimeError, match="does not match authority"):
        orphan.rebind()

    assert (moved_parent / orphan.path.name / "old.txt").read_text(
        encoding="utf-8"
    ) == "old"


@pytest.mark.parametrize("window", ["stage", "moved", "published"])
def test_reader_callbacks_survive_reversible_parent_swap(
    tmp_path: Path,
    window: str,
) -> None:
    parent = tmp_path / "authority"
    parent.mkdir()
    destination = parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = parent / "stage"
    _write_tree(stage, "new.txt", "new")
    moved_parent = tmp_path / f"moved-parent-{window}"
    foreign_parent = tmp_path / f"foreign-parent-{window}"
    injected = False
    observed: list[bytes] = []

    def validate(reader: object) -> None:
        nonlocal injected
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        expected_name = "old.txt" if window == "moved" else "new.txt"
        if not injected:
            injected = True
            if window == "stage":
                active_name = stage.name
            elif window == "moved":
                active_name = next(parent.glob(".published.previous-*")).name
            else:
                active_name = destination.name
            parent.rename(moved_parent)
            parent.mkdir()
            _write_tree(parent / active_name, "foreign.txt", "foreign")
            observed.append(reader.read_bytes(expected_name, max_bytes=16))
            parent.rename(foreign_parent)
            moved_parent.rename(parent)
        else:
            observed.append(reader.read_bytes(expected_name, max_bytes=16))

    callbacks = {
        "stage": {"validate_staged_directory": validate},
        "moved": {"validate_moved_destination": validate},
        "published": {"validate_published_destination": validate},
    }
    orphan = publish_staged_directory(
        stage,
        destination,
        **callbacks[window],
    )

    assert orphan is not None
    assert observed and set(observed) == ({b"old"} if window == "moved" else {b"new"})
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    foreign_children = list(foreign_parent.iterdir())
    assert len(foreign_children) == 1
    assert (foreign_children[0] / "foreign.txt").read_text(
        encoding="utf-8"
    ) == "foreign"


@pytest.mark.parametrize("window", ["stage", "moved", "published"])
def test_reader_callbacks_survive_root_swap_when_ctime_advances(
    tmp_path: Path,
    window: str,
    filesystem_ctime_tick,
) -> None:
    parent = tmp_path / "authority"
    parent.mkdir()
    destination = parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = parent / "stage"
    _write_tree(stage, "new.txt", "new")
    held = parent / f"held-{window}"
    foreign = parent / f"foreign-{window}"
    injected = False
    observed: list[bytes] = []

    def validate(reader: object) -> None:
        nonlocal injected
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        expected_name = "old.txt" if window == "moved" else "new.txt"
        if not injected:
            injected = True
            if window == "stage":
                active = stage
            elif window == "moved":
                active = next(parent.glob(".published.previous-*"))
            else:
                active = destination
            captured_ctime_ns = active.stat().st_ctime_ns
            if sys.platform != "win32":
                # Exercise the versioned endpoint check after a real clock tick;
                # the reader itself remains bound to the original open inode.
                filesystem_ctime_tick(captured_ctime_ns)
            active.rename(held)
            if sys.platform != "win32":
                assert held.stat().st_ctime_ns != captured_ctime_ns
            _write_tree(active, "foreign.txt", "foreign")
            observed.append(reader.read_bytes(expected_name, max_bytes=16))
            active.rename(foreign)
            held.rename(active)
        else:
            observed.append(reader.read_bytes(expected_name, max_bytes=16))

    callbacks = {
        "stage": {"validate_staged_directory": validate},
        "moved": {"validate_moved_destination": validate},
        "published": {"validate_published_destination": validate},
    }
    if window == "stage":
        with pytest.raises(RuntimeError, match="staged directory changed"):
            publish_staged_directory(stage, destination, **callbacks[window])
        assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    else:
        orphan = publish_staged_directory(
            stage,
            destination,
            **callbacks[window],
        )
        assert orphan is not None
        assert (destination / "new.txt").read_text(encoding="utf-8") == "new"

    assert observed and set(observed) == ({b"old"} if window == "moved" else {b"new"})
    assert (foreign / "foreign.txt").read_text(encoding="utf-8") == "foreign"


@pytest.mark.parametrize("window", ["stage", "moved", "published"])
def test_reader_rejects_suppressed_file_swap_authentication_failure(
    tmp_path: Path,
    window: str,
) -> None:
    parent = tmp_path / "authority"
    parent.mkdir()
    destination = parent / "published"
    _write_tree(destination, "old.txt", "old")
    stage = parent / "stage"
    _write_tree(stage, "new.txt", "new")
    evil_store = tmp_path / f"evil-{window}.txt"
    injected = False
    suppressed: list[bool] = []

    def validate(reader: object) -> None:
        nonlocal injected
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        if injected:
            return
        injected = True
        expected_name = "old.txt" if window == "moved" else "new.txt"
        if window == "stage":
            active = stage
        elif window == "moved":
            active = next(parent.glob(".published.previous-*"))
        else:
            active = destination
        expected_file = active / expected_name
        held_original = active / f".{expected_name}.held"
        expected_file.rename(held_original)
        expected_file.write_bytes(b"foreign bytes")
        try:
            try:
                reader.read_bytes(expected_name, max_bytes=64)
            except RuntimeError:
                suppressed.append(True)
        finally:
            expected_file.rename(evil_store)
            held_original.rename(expected_file)

    callbacks = {
        "stage": {"validate_staged_directory": validate},
        "moved": {"validate_moved_destination": validate},
        "published": {"validate_published_destination": validate},
    }
    with pytest.raises(RuntimeError) as raised:
        publish_staged_directory(stage, destination, **callbacks[window])

    messages: list[str] = []
    pending: list[BaseException | None] = [raised.value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        messages.append(str(current))
        pending.extend([current.__cause__, current.__context__])
    assert any("suppressed authentication failure" in message for message in messages)
    assert suppressed == [True]
    assert evil_store.read_bytes() == b"foreign bytes"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    if window in {"stage", "moved"}:
        assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    else:
        quarantines = list(parent.glob(".published.quarantine-*"))
        assert len(quarantines) == 1
        assert (quarantines[0] / "new.txt").read_text(encoding="utf-8") == "new"


def test_publication_reader_builds_expected_lookup_maps_once(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    stage.mkdir()
    for index in range(1_000):
        (stage / f"file-{index:04d}.txt").touch()
    observations: list[tuple[int, int]] = []

    def validate(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        records = reader.file_records()
        assert reader.file_records() is records
        record_map_id = id(reader._records_by_path)
        entry_map_id = id(reader._entries_by_path)
        for record in records:
            expected = reader._expected_file(PurePosixPath(record.path))
            assert expected.record is record
        observations.append((record_map_id, entry_map_id))
        assert id(reader._records_by_path) == record_map_id
        assert id(reader._entries_by_path) == entry_map_id

    orphan = publish_staged_directory(
        stage,
        destination,
        validate_staged_directory=validate,
    )

    assert orphan is None
    assert len(observations) == 1
    assert len(list(destination.iterdir())) == 1_000


def test_reopen_authenticated_directory_freshly_matches_before_and_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parent_identity = publication_parent_identity(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    capture_calls = 0
    capture_descriptor = atomic_module._capture_posix_directory_descriptor

    def count_capture(*args: object, **kwargs: object) -> object:
        nonlocal capture_calls
        capture_calls += 1
        return capture_descriptor(*args, **kwargs)

    monkeypatch.setattr(
        atomic_module,
        "_capture_posix_directory_descriptor",
        count_capture,
    )
    saved_readers: list[object] = []

    def consume(reader: object) -> tuple[bytes, tuple[tuple[str, str], ...]]:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        saved_readers.append(reader)
        return reader.read_bytes("payload.txt", max_bytes=16), reader.inventory()

    result = atomic_module.reopen_authenticated_directory(
        directory,
        ownership,
        consume,
        expected_parent_identity=parent_identity,
    )

    assert result == (b"payload", (("payload.txt", "file"),))
    assert capture_calls == 2
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_readers[0].inventory()


def test_reopen_authenticated_directory_rejects_root_swap_when_ctime_advances(
    tmp_path: Path,
    filesystem_ctime_tick,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    held = tmp_path / "held"
    foreign = tmp_path / "foreign"
    observed: list[bytes] = []

    def consume(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        captured_ctime_ns = directory.stat().st_ctime_ns
        if sys.platform != "win32":
            # POSIX endpoint checks do not provide a namespace event history.
            # Wait for a deterministic metadata-version signal before swapping.
            filesystem_ctime_tick(captured_ctime_ns)
        directory.rename(held)
        if sys.platform != "win32":
            assert held.stat().st_ctime_ns != captured_ctime_ns
        _write_tree(directory, "payload.txt", "foreign")
        try:
            observed.append(reader.read_bytes("payload.txt", max_bytes=16))
        finally:
            directory.rename(foreign)
            held.rename(directory)

    with pytest.raises(RuntimeError, match="changed while it was consumed"):
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            consume,
        )

    assert observed == [b"payload"]
    assert (directory / "payload.txt").read_text(encoding="utf-8") == "payload"
    assert (foreign / "payload.txt").read_text(encoding="utf-8") == "foreign"


@pytest.mark.parametrize(
    "primary",
    [
        pytest.param(ValueError("callback failed"), id="value-error"),
        pytest.param(KeyboardInterrupt("callback interrupted"), id="keyboard"),
        pytest.param(SystemExit("callback exited"), id="system-exit"),
    ],
)
def test_reopen_callback_primary_survives_post_content_drift_detection(
    tmp_path: Path,
    primary: BaseException,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    saved_readers: list[object] = []

    def mutate_then_fail(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        saved_readers.append(reader)
        (directory / "payload.txt").write_text("mutated", encoding="utf-8")
        raise primary

    with pytest.raises(type(primary)) as caught:
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            mutate_then_fail,
        )

    assert caught.value is primary
    traceback_names: list[str] = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        traceback_names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "mutate_then_fail" in traceback_names
    assert any(
        "post-callback ownership validation also failed" in note
        and ("changed" in note or "differs" in note)
        for note in _exception_notes(primary)
    )
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_readers[0].inventory()


def test_reopen_active_outer_exception_raises_exact_postflight_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    outer_error = ValueError("ambient outer failure")
    post_error = OSError(errno.EIO, "injected tree post-capture failure")
    real_capture = atomic_module.PublicationDirectoryReader.capture_ownership
    capture_calls = 0
    saved_readers: list[atomic_module.PublicationDirectoryReader] = []

    def fail_post_capture(reader: object, **kwargs: object) -> object:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            raise post_error
        return real_capture(reader, **kwargs)

    monkeypatch.setattr(
        atomic_module.PublicationDirectoryReader,
        "capture_ownership",
        fail_post_capture,
    )

    def mutate_then_return(reader: atomic_module.PublicationDirectoryReader) -> int:
        saved_readers.append(reader)
        (directory / "payload.txt").write_text("mutated", encoding="utf-8")
        return 7

    try:
        raise outer_error
    except ValueError as active_outer:
        assert active_outer is outer_error
        with pytest.raises(OSError) as caught:
            atomic_module.reopen_authenticated_directory(
                directory,
                ownership,
                mutate_then_return,
            )

    assert caught.value is post_error
    assert capture_calls == 2
    assert _exception_notes(outer_error) == ()
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_readers[0].inventory()


def test_reopen_active_outer_exception_all_green_returns_result(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    outer_error = ValueError("ambient outer failure")
    saved_readers: list[atomic_module.PublicationDirectoryReader] = []

    def consume(reader: atomic_module.PublicationDirectoryReader) -> int:
        saved_readers.append(reader)
        return 7

    try:
        raise outer_error
    except ValueError as active_outer:
        assert active_outer is outer_error
        result = atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            consume,
        )

    assert result == 7
    assert _exception_notes(outer_error) == ()
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_readers[0].inventory()


def test_reopen_callback_primary_inside_active_outer_exception_is_local(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    outer_error = ValueError("ambient outer failure")
    callback_error = OSError(errno.EIO, "local callback failure")
    saved_readers: list[atomic_module.PublicationDirectoryReader] = []

    def mutate_then_fail(reader: atomic_module.PublicationDirectoryReader) -> None:
        saved_readers.append(reader)
        (directory / "payload.txt").write_text("mutated", encoding="utf-8")
        raise callback_error

    try:
        raise outer_error
    except ValueError as active_outer:
        assert active_outer is outer_error
        with pytest.raises(OSError) as caught:
            atomic_module.reopen_authenticated_directory(
                directory,
                ownership,
                mutate_then_fail,
            )

    assert caught.value is callback_error
    traceback_names: list[str] = []
    traceback = callback_error.__traceback__
    while traceback is not None:
        traceback_names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "mutate_then_fail" in traceback_names
    assert any(
        "post-callback ownership validation also failed" in note
        and ("changed" in note or "differs" in note)
        for note in _exception_notes(callback_error)
    )
    assert _exception_notes(outer_error) == ()
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_readers[0].inventory()


def test_reopen_callback_primary_survives_child_namespace_drift_detection(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    held = tmp_path / "held"
    primary = ValueError("callback failed after namespace drift")

    def replace_root_then_fail(_reader: object) -> None:
        directory.rename(held)
        _write_tree(directory, "payload.txt", "foreign")
        raise primary

    with pytest.raises(ValueError) as caught:
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            replace_root_then_fail,
        )

    assert caught.value is primary
    assert any(
        "child namespace validation also failed" in note
        and "namespace binding changed" in note
        for note in _exception_notes(primary)
    )


def test_reopen_callback_primary_survives_authority_path_binding_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    primary = ValueError("callback failed before authority verification")
    path_error = OSError(errno.EIO, "injected authority path verification failure")
    verification_calls = 0

    def fail_path_binding(_authority: object) -> None:
        nonlocal verification_calls
        verification_calls += 1
        raise path_error

    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "verify_path_binding",
        fail_path_binding,
    )

    with pytest.raises(ValueError) as caught:
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            lambda _reader: (_ for _ in ()).throw(primary),
        )

    assert caught.value is primary
    assert verification_calls == 1
    assert any(
        "authority path validation also failed" in note
        and "injected authority path verification failure" in note
        for note in _exception_notes(primary)
    )


def test_reopen_callback_primary_survives_post_capture_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    primary = ValueError("callback failed before post capture")
    post_error = OSError(errno.EIO, "injected authenticated post-capture failure")
    real_capture = atomic_module.PublicationDirectoryReader.capture_ownership
    capture_calls = 0

    def fail_post_capture(reader: object, **kwargs: object) -> object:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            raise post_error
        return real_capture(reader, **kwargs)

    monkeypatch.setattr(
        atomic_module.PublicationDirectoryReader,
        "capture_ownership",
        fail_post_capture,
    )

    with pytest.raises(ValueError) as caught:
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            lambda _reader: (_ for _ in ()).throw(primary),
        )

    assert caught.value is primary
    assert capture_calls == 2
    assert any(
        "post-callback ownership validation also failed" in note
        and "authenticated post-capture failure" in note
        for note in _exception_notes(primary)
    )


@pytest.mark.parametrize("callback_fails", [True, False], ids=["callback", "return"])
def test_reopen_postflight_faults_are_best_effort_and_ordered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    callback_fails: bool,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    held = tmp_path / "held"
    callback_error = ValueError("callback failed after replacing the child")
    tree_error = OSError(errno.EIO, "injected tree post-capture failure")
    validity_error = RuntimeError("injected reader validity failure")
    parent_error = RuntimeError("injected parent binding failure")
    cleanup_error = RuntimeError("injected authority cleanup failure")
    real_capture = atomic_module.PublicationDirectoryReader.capture_ownership
    real_close = atomic_module._PublicationAuthority.close
    capture_calls = 0
    validity_calls = 0
    saved_readers: list[atomic_module.PublicationDirectoryReader] = []

    def fail_tree_post_capture(reader: object, **kwargs: object) -> object:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            raise tree_error
        return real_capture(reader, **kwargs)

    def fail_reader_validity(_reader: object) -> None:
        nonlocal validity_calls
        validity_calls += 1
        if validity_calls == 2:
            raise validity_error

    def fail_parent_binding(_authority: object) -> None:
        raise parent_error

    def close_then_fail(authority: object) -> None:
        real_close(authority)
        raise cleanup_error

    monkeypatch.setattr(
        atomic_module.PublicationDirectoryReader,
        "capture_ownership",
        fail_tree_post_capture,
    )
    monkeypatch.setattr(
        atomic_module.PublicationDirectoryReader,
        "_require_valid",
        fail_reader_validity,
    )
    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "verify_path_binding",
        fail_parent_binding,
    )
    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "close",
        close_then_fail,
    )

    def replace_child_then_finish(
        reader: atomic_module.PublicationDirectoryReader,
    ) -> int:
        saved_readers.append(reader)
        directory.rename(held)
        _write_tree(directory, "payload.txt", "foreign")
        if callback_fails:
            raise callback_error
        return 7

    with pytest.raises(BaseException) as caught:
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            replace_child_then_finish,
        )

    expected_primary = callback_error if callback_fails else tree_error
    assert caught.value is expected_primary
    assert capture_calls == 2
    assert validity_calls == 2
    notes = _exception_notes(expected_primary)
    expected_note_fragments = (
        *(
            ("post-callback ownership validation also failed",)
            if callback_fails
            else ()
        ),
        "publication reader validity validation also failed",
        "publication reader child namespace validation also failed",
        "authenticated directory authority path validation also failed",
        "publication authority owner cleanup also failed",
    )
    note_positions = [
        next(index for index, note in enumerate(notes) if fragment in note)
        for fragment in expected_note_fragments
    ]
    assert note_positions == sorted(note_positions)
    if callback_fails:
        traceback_names: list[str] = []
        traceback = caught.value.__traceback__
        while traceback is not None:
            traceback_names.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert "replace_child_then_finish" in traceback_names
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_readers[0].inventory()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux fd accounting",
)
def test_reopen_authenticated_directory_rejects_suppressed_error_without_fd_leak(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    evil = tmp_path / "evil.txt"
    before = len(os.listdir("/proc/self/fd"))
    suppressed: list[bool] = []

    def consume(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        payload = directory / "payload.txt"
        held = directory / ".payload.txt.held"
        payload.rename(held)
        payload.write_bytes(b"foreign")
        try:
            try:
                reader.read_bytes("payload.txt", max_bytes=16)
            except RuntimeError:
                suppressed.append(True)
        finally:
            payload.rename(evil)
            held.rename(payload)

    with pytest.raises(RuntimeError, match="suppressed authentication failure"):
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            consume,
        )

    assert suppressed == [True]
    assert evil.read_bytes() == b"foreign"
    assert (directory / "payload.txt").read_bytes() == b"payload"
    assert len(os.listdir("/proc/self/fd")) <= before


def test_reopen_authenticated_directory_fake_windows_survives_root_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    directory_id = api.add_directory(api.root_id, "existing")
    api.add_file(directory_id, "payload.txt", b"payload")
    foreign_id = api.add_directory()
    api.add_file(foreign_id, "payload.txt", b"foreign")
    monkeypatch.setattr(atomic_module, "_windows_require_publication_api", lambda: None)
    _install_fake_windows_api(monkeypatch, api)
    path = Path("C:/authority/existing")
    authority = atomic_module._open_windows_publication_authority(
        path.parent,
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        ownership = authority.capture_child(
            path.name,
            path=path,
            label="existing",
        )
    finally:
        authority.close()
    assert api.handles == {}

    def consume(reader: object) -> bytes:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        root_children = api.nodes[api.root_id]["children"]
        assert isinstance(root_children, dict)
        root_children["held"] = root_children.pop("existing")
        root_children["existing"] = foreign_id
        try:
            return reader.read_bytes("payload.txt", max_bytes=16)
        finally:
            root_children["foreign"] = root_children.pop("existing")
            root_children["existing"] = root_children.pop("held")

    assert (
        atomic_module.reopen_authenticated_directory(path, ownership, consume)
        == b"payload"
    )
    assert api.handles == {}
    root_children = api.nodes[api.root_id]["children"]
    assert isinstance(root_children, dict)
    assert root_children["existing"] == directory_id
    assert root_children["foreign"] == foreign_id


def test_reopen_authenticated_directory_fake_windows_rejects_suppressed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    directory_id = api.add_directory(api.root_id, "existing")
    original_id = api.add_file(directory_id, "payload.txt", b"payload")
    foreign_id = api.add_file(directory_id, "foreign.txt", b"foreign")
    monkeypatch.setattr(atomic_module, "_windows_require_publication_api", lambda: None)
    _install_fake_windows_api(monkeypatch, api)
    path = Path("C:/authority/existing")
    authority = atomic_module._open_windows_publication_authority(
        path.parent,
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        ownership = authority.capture_child(
            path.name,
            path=path,
            label="existing",
        )
    finally:
        authority.close()
    suppressed: list[bool] = []

    def consume(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        children = api.nodes[directory_id]["children"]
        assert isinstance(children, dict)
        children["held.txt"] = children.pop("payload.txt")
        children["payload.txt"] = children.pop("foreign.txt")
        try:
            try:
                reader.read_bytes("payload.txt", max_bytes=16)
            except RuntimeError:
                suppressed.append(True)
        finally:
            children["foreign.txt"] = children.pop("payload.txt")
            children["payload.txt"] = children.pop("held.txt")

    with pytest.raises(RuntimeError, match="suppressed authentication failure"):
        atomic_module.reopen_authenticated_directory(path, ownership, consume)

    assert suppressed == [True]
    assert api.handles == {}
    children = api.nodes[directory_id]["children"]
    assert isinstance(children, dict)
    assert children["payload.txt"] == original_id
    assert children["foreign.txt"] == foreign_id


def test_reopen_authenticated_directory_fake_windows_checks_child_after_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    directory_id = api.add_directory(api.root_id, "existing")
    api.add_file(directory_id, "payload.txt", b"payload")
    foreign_id = api.add_directory()
    api.add_file(foreign_id, "payload.txt", b"foreign")
    monkeypatch.setattr(atomic_module, "_windows_require_publication_api", lambda: None)
    _install_fake_windows_api(monkeypatch, api)
    path = Path("C:/authority/existing")
    authority = atomic_module._open_windows_publication_authority(
        path.parent,
        parent_resource=None,
        expected_parent_identity=None,
    )
    try:
        ownership = authority.capture_child(
            path.name,
            path=path,
            label="existing",
        )
    finally:
        authority.close()
    assert api.handles == {}
    primary = KeyboardInterrupt("callback interrupted after child replacement")
    saved_readers: list[object] = []

    def replace_child_then_fail(reader: object) -> None:
        saved_readers.append(reader)
        children = api.nodes[api.root_id]["children"]
        assert isinstance(children, dict)
        children["held"] = children.pop("existing")
        children["existing"] = foreign_id
        raise primary

    with pytest.raises(KeyboardInterrupt) as caught:
        atomic_module.reopen_authenticated_directory(
            path,
            ownership,
            replace_child_then_fail,
        )

    assert caught.value is primary
    assert any(
        "child namespace validation also failed" in note
        and "namespace binding changed" in note
        for note in _exception_notes(primary)
    )
    assert api.handles == {}
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_readers[0].inventory()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_reopen_authenticated_directory_gates_swaps_and_handles(
    tmp_path: Path,
) -> None:
    import ctypes.wintypes as wintypes

    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    kernel32 = atomic_module.ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (
        wintypes.HANDLE,
        atomic_module.ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            atomic_module.ctypes.byref(count),
        ):
            raise atomic_module.ctypes.WinError(atomic_module.ctypes.get_last_error())
        return int(count.value)

    held_root = tmp_path / "held-root"

    def consume_original(reader: object) -> bytes:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        with pytest.raises(OSError):
            directory.rename(held_root)
        return reader.read_bytes("payload.txt", max_bytes=16)

    assert (
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            consume_original,
        )
        == b"payload"
    )

    before = handle_count()
    evil = tmp_path / "evil.txt"

    def suppress_file_swap(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        payload = directory / "payload.txt"
        held = directory / ".payload.txt.held"
        payload.rename(held)
        payload.write_bytes(b"foreign")
        try:
            try:
                reader.read_bytes("payload.txt", max_bytes=16)
            except RuntimeError:
                pass
        finally:
            payload.rename(evil)
            held.rename(payload)

    with pytest.raises(RuntimeError, match="suppressed authentication failure"):
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            suppress_file_swap,
        )

    assert evil.read_bytes() == b"foreign"
    assert (directory / "payload.txt").read_bytes() == b"payload"
    assert handle_count() <= before


def test_capture_directory_ownership_if_exists_captures_existing_tree(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")

    ownership = atomic_module.capture_directory_ownership_if_exists(directory)

    assert ownership is not None
    assert ownership == capture_directory_ownership(directory)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux fd accounting",
)
def test_capture_directory_ownership_if_exists_does_not_bless_missing_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "appeared"
    before = len(os.listdir("/proc/self/fd"))
    child_metadata = atomic_module._PublicationAuthority.child_metadata
    injected = False

    def inject_after_missing(
        authority: object,
        name: str,
        *,
        path: Path,
        label: str,
    ) -> object | None:
        nonlocal injected
        observed = child_metadata(
            authority,
            name,
            path=path,
            label=label,
        )
        if observed is None and path == directory and not injected:
            injected = True
            _write_tree(directory, "foreign.txt", "foreign")
        return observed

    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "child_metadata",
        inject_after_missing,
    )

    ownership = atomic_module.capture_directory_ownership_if_exists(directory)

    assert ownership is None
    assert injected
    assert (directory / "foreign.txt").read_text(encoding="utf-8") == "foreign"
    assert len(os.listdir("/proc/self/fd")) <= before
    source = inspect.getsource(atomic_module.capture_directory_ownership_if_exists)
    assert ".exists(" not in source
    assert "except RuntimeError" not in source


def test_capture_directory_ownership_if_exists_rejects_existing_root_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "original.txt", "original")
    held = tmp_path / "held"
    child_metadata = atomic_module._PublicationAuthority.child_metadata
    observations = 0

    def replace_after_first_observation(
        authority: object,
        name: str,
        *,
        path: Path,
        label: str,
    ) -> object | None:
        nonlocal observations
        observed = child_metadata(
            authority,
            name,
            path=path,
            label=label,
        )
        if path == directory and observed is not None:
            observations += 1
            if observations == 1:
                directory.rename(held)
                _write_tree(directory, "foreign.txt", "foreign")
        return observed

    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "child_metadata",
        replace_after_first_observation,
    )

    with pytest.raises(RuntimeError, match="changed while it was captured"):
        atomic_module.capture_directory_ownership_if_exists(directory)

    assert (held / "original.txt").read_text(encoding="utf-8") == "original"
    assert (directory / "foreign.txt").read_text(encoding="utf-8") == "foreign"


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_capture_directory_ownership_if_exists_rejects_unsafe_child_kind(
    tmp_path: Path,
    kind: str,
) -> None:
    child = tmp_path / "unsafe"
    if kind == "file":
        child.write_text("file", encoding="utf-8")
    else:
        child.symlink_to(tmp_path / "missing-target", target_is_directory=True)

    with pytest.raises(ValueError, match="not a directory or is a link"):
        atomic_module.capture_directory_ownership_if_exists(child)


def test_capture_directory_ownership_if_exists_fake_windows_missing_race_closes_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    _install_fake_windows_api(monkeypatch, api)
    path = Path("C:/authority/appeared")
    child_metadata = atomic_module._PublicationAuthority.child_metadata
    injected = False

    def inject_after_missing(
        authority: object,
        name: str,
        *,
        path: Path,
        label: str,
    ) -> object | None:
        nonlocal injected
        observed = child_metadata(
            authority,
            name,
            path=path,
            label=label,
        )
        if observed is None and not injected:
            injected = True
            foreign = api.add_directory(api.root_id, name)
            api.add_file(foreign, "foreign.txt", b"foreign")
        return observed

    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "child_metadata",
        inject_after_missing,
    )

    ownership = atomic_module.capture_directory_ownership_if_exists(path)

    assert ownership is None
    assert injected
    assert api.handles == {}


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows HANDLEs")
def test_windows_real_capture_if_exists_missing_race_closes_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes.wintypes as wintypes

    directory = tmp_path / "appeared"
    kernel32 = atomic_module.ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = (
        wintypes.HANDLE,
        atomic_module.ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetProcessHandleCount.restype = wintypes.BOOL

    def handle_count() -> int:
        count = wintypes.DWORD()
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            atomic_module.ctypes.byref(count),
        ):
            raise atomic_module.ctypes.WinError(atomic_module.ctypes.get_last_error())
        return int(count.value)

    atomic_module._windows_kernel_api()
    before = handle_count()
    child_metadata = atomic_module._PublicationAuthority.child_metadata
    injected = False

    def inject_after_missing(
        authority: object,
        name: str,
        *,
        path: Path,
        label: str,
    ) -> object | None:
        nonlocal injected
        observed = child_metadata(
            authority,
            name,
            path=path,
            label=label,
        )
        if observed is None and not injected:
            injected = True
            _write_tree(directory, "foreign.txt", "foreign")
        return observed

    monkeypatch.setattr(
        atomic_module._PublicationAuthority,
        "child_metadata",
        inject_after_missing,
    )

    ownership = atomic_module.capture_directory_ownership_if_exists(directory)

    assert ownership is None
    assert injected
    assert (directory / "foreign.txt").read_text(encoding="utf-8") == "foreign"
    assert handle_count() <= before


def _call_with_interrupt_after_store(
    function: object,
    local_name: str,
    callback: object,
    *,
    predicate: object | None = None,
    error: BaseException,
) -> None:
    assert callable(function)
    assert callable(callback)
    assert predicate is None or callable(predicate)
    code = function.__code__
    instructions = tuple(dis.get_instructions(function))
    result_store_indexes = {
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_FAST"
        and instruction.argval == local_name
        and index > 0
        and instructions[index - 1].opname.startswith("CALL")
    }
    opcode_offsets_after_store = {
        instructions[index + 1].offset for index in result_store_indexes
    }
    result_store_offsets = {
        instructions[index].offset for index in result_store_indexes
    }
    line_offsets_after_store = {
        instruction.offset
        for instruction in instructions
        if instruction.starts_line is not None
        and any(
            instruction.offset > store_offset for store_offset in result_store_offsets
        )
    }
    assert result_store_indexes
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            frame.f_code is code
            and (
                (event == "opcode" and frame.f_lasti in opcode_offsets_after_store)
                or (event == "line" and frame.f_lasti in line_offsets_after_store)
            )
            and int(frame.f_locals.get(local_name, -1)) >= 0
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
        assert injected, f"failed to inject after {local_name} result store"


def _call_with_interrupt_after_attribute_store(
    function: object,
    attribute_name: str,
    callback: object,
    *,
    predicate: object | None = None,
    error: BaseException,
) -> None:
    """Interrupt immediately after an acquisition owner publishes one field."""

    assert callable(function)
    assert callable(callback)
    assert predicate is None or callable(predicate)
    code = function.__code__
    instructions = tuple(dis.get_instructions(function))
    store_indexes = {
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_ATTR" and instruction.argval == attribute_name
    }
    assert store_indexes
    opcode_offsets_after_store = {
        instructions[index + 1].offset for index in store_indexes
    }
    store_offsets = {instructions[index].offset for index in store_indexes}
    line_offset_candidates = {
        instruction.offset
        for instruction in instructions
        if instruction.starts_line is not None
        and instruction.offset > max(store_offsets)
    }
    assert line_offset_candidates
    line_offsets_after_store = {min(line_offset_candidates)}
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            frame.f_code is code
            and (
                (event == "opcode" and frame.f_lasti in opcode_offsets_after_store)
                or (event == "line" and frame.f_lasti in line_offsets_after_store)
            )
            and (
                attribute_name == "identity"
                and getattr(frame.f_locals.get("record"), "identity", None) is not None
                or attribute_name == "descriptor"
                and getattr(frame.f_locals.get("record"), "descriptor", -1) >= 0
                or attribute_name == "handle"
                and bool(getattr(frame.f_locals.get("record"), "handle", 0))
            )
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
        assert injected, f"failed to inject after {attribute_name} publication"


def _call_with_interrupt_on_source_line(
    function: object,
    source_fragment: str,
    callback: object,
    *,
    predicate: object | None = None,
    occurrence: int = 1,
    error: BaseException,
) -> None:
    """Inject before one exact source line across supported trace versions."""

    assert callable(function)
    assert callable(callback)
    assert predicate is None or callable(predicate)
    assert type(occurrence) is int and occurrence > 0
    code = function.__code__
    source, first_line = inspect.getsourcelines(function)
    target_lines = {
        first_line + offset
        for offset, line in enumerate(source)
        if line.strip() == source_fragment
    }
    assert len(target_lines) == 1
    previous_trace = sys.gettrace()
    injected = False
    observed = 0

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected, observed
        if event == "call" and frame.f_code is code:
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is code
            and event == "line"
            and frame.f_lineno in target_lines
            and (predicate is None or predicate(frame.f_locals))
        ):
            observed += 1
            if observed != occurrence:
                return trace
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, f"failed to inject on source line: {source_fragment}"


def _source_statement_opcode_offsets(
    function: object,
    source_fragment: str,
    *,
    occurrence: int = 1,
) -> tuple[int, ...]:
    """Return every opcode mapped to one possibly multiline statement."""

    assert callable(function)
    assert type(occurrence) is int and occurrence > 0
    source, first_line = inspect.getsourcelines(function)
    starts = [
        offset for offset, line in enumerate(source) if line.strip() == source_fragment
    ]
    if occurrence == 1:
        assert len(starts) == 1
    assert len(starts) >= occurrence
    start = starts[occurrence - 1]
    balance = 0
    end = start
    for offset in range(start, len(source)):
        balance += source[offset].count("(") - source[offset].count(")")
        end = offset
        if balance <= 0:
            break
    start_line = first_line + start
    end_line = first_line + end
    fallback_line = function.__code__.co_firstlineno
    selected: list[int] = []
    for instruction in dis.get_instructions(function):
        if type(instruction.starts_line) is int:
            fallback_line = instruction.starts_line
        positions = getattr(instruction, "positions", None)
        current_line = getattr(positions, "lineno", None)
        if current_line is None:
            current_line = fallback_line
        if current_line is not None and start_line <= current_line <= end_line:
            selected.append(instruction.offset)
    assert selected
    return tuple(selected)


def _call_with_interrupt_at_opcode(
    function: object,
    opcode_offset: int,
    callback: object,
    *,
    call_occurrence: int | None = None,
    warmup: object | None = None,
    predicate: object | None = None,
    error: BaseException,
) -> None:
    """Interrupt before one exact opcode selected from the running version."""

    assert callable(function)
    assert callable(callback)
    assert call_occurrence is None or (
        type(call_occurrence) is int and call_occurrence > 0
    )
    assert warmup is None or callable(warmup)
    assert predicate is None or callable(predicate)
    code = function.__code__
    selected = tuple(
        instruction
        for instruction in dis.get_instructions(function)
        if instruction.offset == opcode_offset
    )
    assert len(selected) == 1
    positions = getattr(selected[0], "positions", None)
    target_line: int | None = None
    if selected[0].starts_line is True or type(selected[0].starts_line) is int:
        target_line = getattr(positions, "lineno", None)
        if target_line is None and type(selected[0].starts_line) is int:
            target_line = selected[0].starts_line
    previous_trace = sys.gettrace()
    injected = False
    matching_calls = 0
    selected_frame: object | None = None
    priming = warmup is not None

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected, matching_calls, selected_frame
        if event == "call" and frame.f_code is code:
            matching_calls += 1
            if call_occurrence is not None and matching_calls == call_occurrence:
                selected_frame = frame
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and not priming
            and frame.f_code is code
            and (call_occurrence is None or frame is selected_frame)
            and (
                (event == "opcode" and frame.f_lasti == opcode_offset)
                or (
                    event == "line"
                    and target_line is not None
                    and frame.f_lineno == target_line
                )
            )
            and (predicate is None or predicate(frame.f_locals))
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        if warmup is not None:
            warmup()
            matching_calls = 0
            selected_frame = None
            priming = False
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, f"failed to inject at opcode {opcode_offset}"


def _observed_opcode_offsets(
    function: object,
    callback: object,
    *,
    call_occurrence: int | None = None,
    warmup: object | None = None,
    predicate: object | None = None,
) -> tuple[int, ...]:
    """Record every opcode reached in selected calls to one function."""

    assert callable(function)
    assert callable(callback)
    assert call_occurrence is None or (
        type(call_occurrence) is int and call_occurrence > 0
    )
    assert warmup is None or callable(warmup)
    assert predicate is None or callable(predicate)
    code = function.__code__
    previous_trace = sys.gettrace()
    opcode_offsets: list[int] = []
    line_offsets: list[int] = []
    matching_calls = 0
    selected_frame: object | None = None
    priming = warmup is not None

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal matching_calls, selected_frame
        if event == "call" and frame.f_code is code:
            matching_calls += 1
            if call_occurrence is not None and matching_calls == call_occurrence:
                selected_frame = frame
            frame.f_trace_opcodes = True
            return trace
        if (
            frame.f_code is code
            and not priming
            and (call_occurrence is None or frame is selected_frame)
            and event in {"opcode", "line"}
            and (predicate is None or predicate(frame.f_locals))
        ):
            if event == "opcode":
                opcode_offsets.append(frame.f_lasti)
            else:
                line_offsets.append(frame.f_lasti)
        return trace

    sys.settrace(trace)
    try:
        if warmup is not None:
            warmup()
            matching_calls = 0
            selected_frame = None
            priming = False
        callback()
    finally:
        sys.settrace(previous_trace)
    observed = tuple(dict.fromkeys(opcode_offsets or line_offsets))
    assert observed
    return observed


def _call_with_interrupt_on_return(
    function: object,
    callback: object,
    *,
    call_occurrence: int | None = None,
    warmup: object | None = None,
    predicate: object,
    error: BaseException,
) -> None:
    """Inject when one selected function is handing its result to the caller."""

    assert callable(function)
    assert callable(callback)
    assert call_occurrence is None or (
        type(call_occurrence) is int and call_occurrence > 0
    )
    assert warmup is None or callable(warmup)
    assert callable(predicate)
    code = function.__code__
    previous_trace = sys.gettrace()
    injected = False
    matching_calls = 0
    selected_frame: object | None = None
    priming = warmup is not None

    def trace(frame: object, event: str, arg: object) -> object:
        nonlocal injected, matching_calls, selected_frame
        if event == "call" and frame.f_code is code:
            matching_calls += 1
            if call_occurrence is not None and matching_calls == call_occurrence:
                selected_frame = frame
            return trace
        if (
            not injected
            and not priming
            and event == "return"
            and frame.f_code is code
            and (call_occurrence is None or frame is selected_frame)
            and predicate(frame, arg)
        ):
            injected = True
            sys.settrace(None)
            raise error
        return trace

    sys.settrace(trace)
    try:
        if warmup is not None:
            warmup()
            matching_calls = 0
            selected_frame = None
            priming = False
        callback()
    finally:
        sys.settrace(previous_trace)
        assert injected, "failed to inject at the selected return handoff"


def _call_with_interrupt_after_call_result_store(
    function: object,
    local_name: str,
    callback: object,
    *,
    error: BaseException,
) -> None:
    """Interrupt after one CALL result is stored in a selected local."""

    assert callable(function)
    assert callable(callback)
    code = function.__code__
    instructions = tuple(dis.get_instructions(function))
    store_indexes = {
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_FAST"
        and instruction.argval == local_name
        and any(
            candidate.opname.startswith("CALL") for candidate in instructions[:index]
        )
    }
    opcode_offsets_after_store = {
        instructions[index + 1].offset for index in store_indexes
    }
    store_offsets = {instructions[index].offset for index in store_indexes}
    line_offsets_after_store = {
        instruction.offset
        for instruction in instructions
        if instruction.starts_line is not None
        and any(instruction.offset > store_offset for store_offset in store_offsets)
    }
    assert store_indexes
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            frame.f_code is code
            and (
                (event == "opcode" and frame.f_lasti in opcode_offsets_after_store)
                or (event == "line" and frame.f_lasti in line_offsets_after_store)
            )
            and local_name in frame.f_locals
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
        assert injected, f"failed to inject after {local_name} result store"


def _call_with_interrupt_at_back_edge(
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
    instructions = tuple(dis.get_instructions(function))
    back_edges = tuple(
        instruction
        for instruction in instructions
        if "JUMP" in instruction.opname
        and isinstance(instruction.argval, int)
        and instruction.argval < instruction.offset
    )
    assert back_edges
    # The ordered runner may also have a handler-local back-edge.  Select the
    # outer action-loop edge (the earliest destination) so a 3.12 line event
    # cannot inject at handler fallthrough before the real normal back-edge.
    loop_destination = min(instruction.argval for instruction in back_edges)
    opcode_offsets = {
        instruction.offset
        for instruction in back_edges
        if instruction.argval == loop_destination
    }
    line_offsets = {loop_destination}
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            frame.f_code is code
            and (
                (event == "opcode" and frame.f_lasti in opcode_offsets)
                or (event == "line" and frame.f_lasti in line_offsets)
            )
            and predicate(frame.f_locals)
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
        assert injected, "failed to inject at an ordered-action back-edge"


def _posix_authority_resources(
    authority: object,
) -> atomic_module._PosixResourceOwner:
    closure = authority._close_callback.__closure__ or ()
    matches = [
        cell.cell_contents
        for cell in closure
        if isinstance(cell.cell_contents, atomic_module._PosixResourceOwner)
    ]
    assert len(matches) == 1
    return matches[0]


def _assert_descriptor_closed(descriptor: int) -> None:
    with pytest.raises(OSError) as caught:
        os.fstat(descriptor)
    assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize("callback_fails", [True, False], ids=["primary", "return"])
def test_ordered_post_validations_resume_after_back_edge_cancellation(
    callback_fails: bool,
) -> None:
    callback_error = ValueError("callback primary")
    interruption = KeyboardInterrupt("post-validation back-edge")
    final_error = OSError(errno.EIO, "final validation failure")
    calls: list[str] = []

    def callback() -> int:
        if callback_fails:
            raise callback_error
        return 7

    def first_validation() -> None:
        calls.append("first")

    def final_validation() -> None:
        calls.append("final")
        raise final_error

    def invoke() -> None:
        atomic_module._run_callback_with_post_validations(
            callback,
            (
                ("first post-validation also failed", first_validation),
                ("final post-validation also failed", final_validation),
            ),
        )

    with pytest.raises(BaseException) as caught:
        _call_with_interrupt_at_back_edge(
            atomic_module._run_ordered_actions_pass,
            invoke,
            predicate=lambda local: (
                local["state"].next_index == 1 and calls == ["first"]
            ),
            error=interruption,
        )

    expected_primary = callback_error if callback_fails else interruption
    assert caught.value is expected_primary
    assert calls == ["first", "final"]
    notes = _exception_notes(expected_primary)
    expected_fragments = (
        *(
            ("publication callback post-validation iteration also failed",)
            if callback_fails
            else ()
        ),
        "final post-validation also failed",
    )
    positions = [
        next(index for index, note in enumerate(notes) if fragment in note)
        for fragment in expected_fragments
    ]
    assert positions == sorted(positions)


def test_ordered_post_validations_protect_back_edge_after_action_error() -> None:
    callback_error = ValueError("callback primary")
    first_error = OSError(errno.EIO, "first validation failure")
    interruption = SystemExit("validation error-handler back-edge")
    calls: list[str] = []

    def first_validation() -> None:
        calls.append("first")
        raise first_error

    def final_validation() -> None:
        calls.append("final")

    def invoke() -> None:
        atomic_module._run_callback_with_post_validations(
            lambda: (_ for _ in ()).throw(callback_error),
            (
                ("first post-validation also failed", first_validation),
                ("final post-validation also failed", final_validation),
            ),
        )

    with pytest.raises(ValueError) as caught:
        _call_with_interrupt_at_back_edge(
            atomic_module._run_ordered_actions_pass,
            invoke,
            predicate=lambda local: (
                local["state"].next_index == 1 and calls == ["first"]
            ),
            error=interruption,
        )

    assert caught.value is callback_error
    assert calls == ["first", "final"]
    notes = _exception_notes(callback_error)
    first_position = next(
        index
        for index, note in enumerate(notes)
        if "first post-validation also failed" in note
    )
    interruption_position = next(
        index
        for index, note in enumerate(notes)
        if "post-validation iteration also failed" in note
    )
    assert first_position < interruption_position


@pytest.mark.parametrize("body_fails", [True, False], ids=["primary", "return"])
def test_ordered_cleanup_resumes_after_back_edge_cancellation(
    body_fails: bool,
) -> None:
    body_error = ValueError("context body primary")
    interruption = KeyboardInterrupt("cleanup back-edge")
    final_error = OSError(errno.EIO, "final cleanup failure")
    calls: list[str] = []

    def first_cleanup() -> None:
        calls.append("first")

    def final_cleanup() -> None:
        calls.append("final")
        raise final_error

    def invoke() -> None:
        with atomic_module._run_context_with_cleanup_actions(
            (
                ("first cleanup also failed", first_cleanup),
                ("final cleanup also failed", final_cleanup),
            )
        ):
            if body_fails:
                raise body_error

    with pytest.raises(BaseException) as caught:
        _call_with_interrupt_at_back_edge(
            atomic_module._run_ordered_actions_pass,
            invoke,
            predicate=lambda local: (
                local["state"].next_index == 1 and calls == ["first"]
            ),
            error=interruption,
        )

    expected_primary = body_error if body_fails else interruption
    assert caught.value is expected_primary
    assert calls == ["first", "final"]
    notes = _exception_notes(expected_primary)
    expected_fragments = (
        *(
            ("publication authenticated cleanup iteration also failed",)
            if body_fails
            else ()
        ),
        "final cleanup also failed",
    )
    positions = [
        next(index for index, note in enumerate(notes) if fragment in note)
        for fragment in expected_fragments
    ]
    assert positions == sorted(positions)


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX descriptors",
)
def test_cleanup_back_edge_cancellation_closes_remaining_posix_descriptors() -> None:
    first_read, first_write = os.pipe()
    final_read, final_write = os.pipe()
    all_descriptors = (first_read, first_write, final_read, final_write)
    body_error = ValueError("context body primary")
    interruption = KeyboardInterrupt("descriptor cleanup back-edge")
    calls: list[str] = []

    def close_first() -> None:
        os.close(first_read)
        calls.append("first")

    def close_final() -> None:
        os.close(final_read)
        calls.append("final")

    def invoke() -> None:
        with atomic_module._run_context_with_cleanup_actions(
            (
                ("first descriptor cleanup also failed", close_first),
                ("final descriptor cleanup also failed", close_final),
            )
        ):
            raise body_error

    try:
        with pytest.raises(ValueError) as caught:
            _call_with_interrupt_at_back_edge(
                atomic_module._run_ordered_actions_pass,
                invoke,
                predicate=lambda local: (
                    local["state"].next_index == 1 and calls == ["first"]
                ),
                error=interruption,
            )

        assert caught.value is body_error
        assert calls == ["first", "final"]
        _assert_descriptor_closed(first_read)
        _assert_descriptor_closed(final_read)
    finally:
        for descriptor in all_descriptors:
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def test_cleanup_back_edge_cancellation_closes_remaining_windows_handles() -> None:
    api = _FakeWindowsApi()
    first_handle = api.create_directory_handle(Path("C:/authority"))
    final_handle = api.duplicate_handle(first_handle)
    body_error = ValueError("context body primary")
    interruption = KeyboardInterrupt("HANDLE cleanup back-edge")
    calls: list[str] = []

    def close_first() -> None:
        api.close(first_handle)
        calls.append("first")

    def close_final() -> None:
        api.close(final_handle)
        calls.append("final")

    def invoke() -> None:
        with atomic_module._run_context_with_cleanup_actions(
            (
                ("first HANDLE cleanup also failed", close_first),
                ("final HANDLE cleanup also failed", close_final),
            )
        ):
            raise body_error

    try:
        with pytest.raises(ValueError) as caught:
            _call_with_interrupt_at_back_edge(
                atomic_module._run_ordered_actions_pass,
                invoke,
                predicate=lambda local: (
                    local["state"].next_index == 1 and calls == ["first"]
                ),
                error=interruption,
            )

        assert caught.value is body_error
        assert calls == ["first", "final"]
        assert api.handles == {}
    finally:
        for handle in tuple(api.handles):
            api.close(handle)


@pytest.mark.parametrize("surface", ["post-validation", "cleanup"])
def test_hostile_add_note_cannot_replace_primary_or_skip_actions(
    surface: str,
) -> None:
    class HostilePrimary(BaseException):
        def __init__(self) -> None:
            super().__init__("hostile primary")
            self.override_calls = 0

        def add_note(self, _note: str) -> None:
            self.override_calls += 1
            raise RuntimeError("hostile add_note override")

    primary = HostilePrimary()
    secondary = OSError(errno.EIO, "secondary action failure")
    calls: list[str] = []

    def first_action() -> None:
        calls.append("first")
        raise secondary

    def final_action() -> None:
        calls.append("final")

    def invoke() -> None:
        actions = (
            ("first ordered action also failed", first_action),
            ("final ordered action also failed", final_action),
        )
        if surface == "post-validation":
            atomic_module._run_callback_with_post_validations(
                lambda: (_ for _ in ()).throw(primary),
                actions,
            )
        else:
            with atomic_module._run_context_with_cleanup_actions(actions):
                raise primary

    with pytest.raises(HostilePrimary) as caught:
        invoke()

    assert caught.value is primary
    assert primary.override_calls == 0
    assert calls == ["first", "final"]
    assert any(
        "first ordered action also failed" in note
        and "secondary action failure" in note
        for note in _exception_notes(primary)
    )


def _interrupt_ordered_action_before_call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    label: str,
    errors: tuple[BaseException, ...],
) -> list[str]:
    real_attempt = atomic_module._attempt_ordered_action
    remaining = list(errors)
    events: list[str] = []

    def interrupt(state: object, ordered: object) -> object:
        if ordered.label == label and remaining:
            error = remaining.pop(0)
            events.append(type(error).__name__)
            raise error
        return real_attempt(state, ordered)

    monkeypatch.setattr(atomic_module, "_attempt_ordered_action", interrupt)
    return events


def _call_with_interrupt_at_ordered_action_call(
    callback: object,
    *,
    error: BaseException,
) -> None:
    """Inject at the real action CALL, not around the runner helper."""

    assert callable(callback)
    function = atomic_module._attempt_ordered_action
    code = function.__code__
    source, first_line = inspect.getsourcelines(function)
    action_lines = {
        first_line + offset
        for offset, line in enumerate(source)
        if "ordered.action()" in line
    }
    assert len(action_lines) == 1
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is code
            and event == "line"
            and frame.f_lineno in action_lines
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
        assert injected, "failed to inject at the ordered action CALL"


def _interrupt_ordered_runner_entries(
    monkeypatch: pytest.MonkeyPatch,
    *,
    errors: tuple[BaseException, ...],
    action_index: int = 0,
) -> list[BaseException]:
    """Raise repeatedly at the protected call into the ordered runner pass."""

    real_pass = atomic_module._run_ordered_actions_pass
    remaining = list(errors)
    observed: list[BaseException] = []

    def interrupt(state: object) -> bool:
        if state.next_index == action_index and remaining:
            error = remaining.pop(0)
            observed.append(error)
            raise error
        return real_pass(state)

    monkeypatch.setattr(
        atomic_module,
        "_run_ordered_actions_pass",
        interrupt,
    )
    return observed


def test_callback_post_validations_resume_after_repeated_runner_entry_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_error = ValueError("callback failed before post-validation")
    first = KeyboardInterrupt("first post-validation runner entry")
    second = SystemExit("second post-validation runner entry")
    calls: list[str] = []
    observed = _interrupt_ordered_runner_entries(
        monkeypatch,
        errors=(first, second),
    )

    with pytest.raises(ValueError) as caught:
        atomic_module._run_callback_with_post_validations(
            lambda: (_ for _ in ()).throw(callback_error),
            (
                ("first post-validation also failed", lambda: calls.append("first")),
                ("final post-validation also failed", lambda: calls.append("final")),
            ),
        )

    assert caught.value is callback_error
    assert observed == [first, second]
    assert calls == ["first", "final"]
    assert any(
        "post-validation iteration also failed" in note
        and "first post-validation runner entry" in note
        for note in _exception_notes(callback_error)
    )


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX directory descriptors",
)
def test_posix_authenticated_cleanup_resumes_after_runner_entry_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "publication"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_bytes(b"payload")
    reader, root_descriptor = _posix_cleanup_test_reader(root)
    body_error = ValueError("authenticated body failed")
    first = KeyboardInterrupt("first POSIX cleanup runner entry")
    second = SystemExit("second POSIX cleanup runner entry")
    owners: list[atomic_module._PosixResourceOwner] = []
    real_close_all = atomic_module._PosixResourceOwner.close_all

    def capture_owner(owner: atomic_module._PosixResourceOwner) -> None:
        if not any(candidate is owner for candidate in owners):
            owners.append(owner)
        real_close_all(owner)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "close_all",
        capture_owner,
    )
    observed = _interrupt_ordered_runner_entries(
        monkeypatch,
        errors=(first, second),
    )

    def invoke() -> None:
        with reader.open_authenticated_file(
            "nested/payload.txt",
            max_bytes=16,
        ) as authenticated:
            assert authenticated.read() == b"payload"
            raise body_error

    try:
        with pytest.raises(ValueError) as caught:
            invoke()

        assert caught.value is body_error
        assert observed == [first, second]
        assert owners and all(owner.closed for owner in owners)
        assert any(
            "cleanup iteration also failed" in note
            and "first POSIX cleanup runner entry" in note
            for note in _exception_notes(body_error)
        )
    finally:
        reader._deactivate()
        os.close(root_descriptor)


def test_windows_authenticated_cleanup_resumes_after_runner_entry_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, reader, root_handle = _windows_cleanup_test_reader()
    body_error = ValueError("authenticated body failed")
    first = SystemExit("first Windows cleanup runner entry")
    second = KeyboardInterrupt("second Windows cleanup runner entry")
    owners: list[atomic_module._WindowsResourceOwner] = []
    real_close_all = atomic_module._WindowsResourceOwner.close_all

    def capture_owner(owner: atomic_module._WindowsResourceOwner) -> None:
        if not any(candidate is owner for candidate in owners):
            owners.append(owner)
        real_close_all(owner)

    monkeypatch.setattr(
        atomic_module._WindowsResourceOwner,
        "close_all",
        capture_owner,
    )
    observed = _interrupt_ordered_runner_entries(
        monkeypatch,
        errors=(first, second),
    )

    def invoke() -> None:
        with reader.open_authenticated_file(
            "nested/payload.txt",
            max_bytes=16,
        ) as authenticated:
            assert authenticated.read() == b"payload"
            raise body_error

    try:
        with pytest.raises(ValueError) as caught:
            invoke()

        assert caught.value is body_error
        assert observed == [first, second]
        assert owners and all(owner.closed for owner in owners)
        assert set(api.handles) == {root_handle}
        assert any(
            "cleanup iteration also failed" in note
            and "first Windows cleanup runner entry" in note
            for note in _exception_notes(body_error)
        )
    finally:
        reader._deactivate()
        api.close(root_handle)

    assert api.handles == {}


def test_callback_post_validations_bound_permanent_runner_entry_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_error = ValueError("callback failed before post-validation")
    interruptions = tuple(
        KeyboardInterrupt(f"post-validation runner entry {index}")
        for index in range(20)
    )
    calls: list[str] = []
    observed = _interrupt_ordered_runner_entries(
        monkeypatch,
        errors=interruptions,
    )

    with pytest.raises(ValueError) as caught:
        atomic_module._run_callback_with_post_validations(
            lambda: (_ for _ in ()).throw(callback_error),
            (
                (
                    "blocked post-validation also failed",
                    lambda: calls.append("blocked"),
                ),
                ("later post-validation also failed", lambda: calls.append("later")),
            ),
        )

    expected_attempts = atomic_module._MAX_ORDERED_ACTION_CANCELLATION_RETRIES + 1
    assert caught.value is callback_error
    assert observed == list(interruptions[:expected_attempts])
    assert calls == ["later"]
    notes = _exception_notes(callback_error)
    assert "post-validation runner entry 0" in notes[0]
    assert "cancellation retry limit" in notes[-1]


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX descriptors",
)
def test_posix_owner_is_retained_after_permanent_runner_entry_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = atomic_module._PosixResourceOwner()
    descriptor = owner.open(os.devnull, os.O_RDONLY)
    record = owner.record_for_cleanup(descriptor)
    primary = ValueError("body failed before POSIX cleanup")
    interruptions = tuple(
        KeyboardInterrupt(f"POSIX runner entry {index}") for index in range(20)
    )
    later_calls: list[str] = []
    cleanup_complete = False

    def close_owner() -> None:
        nonlocal cleanup_complete
        owner.close_all()
        cleanup_complete = True

    state = atomic_module._OrderedActionState(
        actions=(
            atomic_module._OrderedAction(
                label="POSIX owner cleanup also failed",
                action=close_owner,
                complete=lambda: cleanup_complete,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            ),
            ("later cleanup also failed", lambda: later_calls.append("later")),
        ),
        iteration_failure_label="POSIX cleanup iteration also failed",
        primary_error=primary,
    )
    observed = _interrupt_ordered_runner_entries(
        monkeypatch,
        errors=interruptions,
    )

    try:
        atomic_module._run_ordered_actions(state)

        expected_attempts = atomic_module._MAX_ORDERED_ACTION_CANCELLATION_RETRIES + 1
        assert observed == list(interruptions[:expected_attempts])
        assert state.primary_error is primary
        assert state.next_index == 2
        assert later_calls == ["later"]
        assert record.descriptor == descriptor
        os.fstat(descriptor)
        assert primary.publication_cleanup_owners == (owner,)
    finally:
        owner.close_all()

    assert owner.closed


def test_windows_owner_is_retained_after_permanent_runner_entry_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    handle = owner.acquire(lambda: api.create_directory_handle(Path("C:/authority")))
    record = owner.record_for_cleanup(handle)
    primary = ValueError("body failed before Windows cleanup")
    interruptions = tuple(
        SystemExit(f"Windows runner entry {index}") for index in range(20)
    )
    later_calls: list[str] = []
    cleanup_complete = False

    def close_owner() -> None:
        nonlocal cleanup_complete
        owner.close_all()
        cleanup_complete = True

    state = atomic_module._OrderedActionState(
        actions=(
            atomic_module._OrderedAction(
                label="Windows owner cleanup also failed",
                action=close_owner,
                complete=lambda: cleanup_complete,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            ),
            ("later cleanup also failed", lambda: later_calls.append("later")),
        ),
        iteration_failure_label="Windows cleanup iteration also failed",
        primary_error=primary,
    )
    observed = _interrupt_ordered_runner_entries(
        monkeypatch,
        errors=interruptions,
    )

    atomic_module._run_ordered_actions(state)

    expected_attempts = atomic_module._MAX_ORDERED_ACTION_CANCELLATION_RETRIES + 1
    assert observed == list(interruptions[:expected_attempts])
    assert state.primary_error is primary
    assert state.next_index == 2
    assert later_calls == ["later"]
    assert record.handle == handle
    assert set(api.handles) == {handle}
    assert primary.publication_cleanup_owners == (owner,)
    owner.close_all()
    assert owner.closed
    assert api.handles == {}


def test_outer_trampoline_entry_failure_retains_pending_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ValueError("body failed before cleanup")
    boundary_error = KeyboardInterrupt("before the C trampoline started")
    calls: list[str] = []

    class RetryOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    owner = RetryOwner()
    monkeypatch.setattr(
        atomic_module,
        "_run_ordered_actions",
        lambda _state: (_ for _ in ()).throw(boundary_error),
    )

    with pytest.raises(ValueError) as caught:
        with atomic_module._run_context_with_cleanup_actions(
            (
                atomic_module._OrderedAction(
                    label="pending owner cleanup also failed",
                    action=owner.close,
                    complete=lambda: owner.closed,
                    retry_incomplete="cancellation",
                    incomplete_owner=owner,
                ),
                ("later cleanup also failed", lambda: calls.append("later")),
            )
        ):
            raise primary

    assert caught.value is primary
    assert not owner.closed
    assert calls == []
    assert primary.publication_cleanup_owners == (owner,)
    assert any(
        "before the C trampoline started" in note for note in _exception_notes(primary)
    )
    owner.close()
    assert owner.closed


def test_context_finalizer_entry_interrupt_has_bound_unwind_state() -> None:
    primary = OSError(errno.EIO, "context body primary")
    interruption = KeyboardInterrupt("context finalizer entry")
    completed = False

    def close_exact_owner() -> None:
        nonlocal completed
        completed = True

    owner = atomic_module._ExactResourceCleanupOwner(
        action=close_exact_owner,
        complete=lambda: completed,
    )

    def invoke() -> None:
        with atomic_module._run_context_with_cleanup_actions(
            (
                atomic_module._OrderedAction(
                    label="exact cleanup also failed",
                    action=owner.close,
                    complete=lambda: owner.closed,
                    incomplete_owner=owner,
                ),
            )
        ):
            raise primary

    with pytest.raises(OSError) as caught:
        _call_with_interrupt_on_source_line(
            atomic_module._run_context_with_cleanup_actions.__wrapped__,
            "locally_unwinding = context_error is not None",
            invoke,
            error=interruption,
        )

    assert caught.value is primary
    assert not owner.closed
    assert primary.publication_cleanup_owners == (owner,)
    assert any("context finalizer entry" in note for note in _exception_notes(primary))
    owner.close()
    assert owner.closed


def test_completion_aware_action_retries_real_pre_call_cancellation() -> None:
    interruption = KeyboardInterrupt("action CALL cancellation")
    completed = False
    calls: list[str] = []

    def cleanup() -> None:
        nonlocal completed
        calls.append("cleanup")
        completed = True

    state = atomic_module._OrderedActionState(
        actions=(
            atomic_module._OrderedAction(
                label="real cleanup CALL also failed",
                action=cleanup,
                complete=lambda: completed,
                retry_incomplete="cancellation",
            ),
            ("final validation also failed", lambda: calls.append("final")),
        ),
        iteration_failure_label="real cleanup iteration also failed",
        primary_error=None,
    )

    _call_with_interrupt_at_ordered_action_call(
        lambda: atomic_module._run_ordered_actions(state),
        error=interruption,
    )

    assert state.primary_error is interruption
    assert state.next_index == 2
    assert completed
    assert calls == ["cleanup", "final"]


def test_completion_aware_action_retries_repeated_pre_call_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = KeyboardInterrupt("first pre-call cancellation")
    second = SystemExit("second pre-call cancellation")
    completed = False
    calls = 0

    def finish() -> None:
        nonlocal calls, completed
        calls += 1
        completed = True

    label = "test completion-aware cleanup also failed"
    events = _interrupt_ordered_action_before_call(
        monkeypatch,
        label=label,
        errors=(first, second),
    )
    state = atomic_module._OrderedActionState(
        actions=(
            atomic_module._OrderedAction(
                label=label,
                action=finish,
                complete=lambda: completed,
                retry_incomplete="cancellation",
            ),
        ),
        iteration_failure_label="test iteration also failed",
        primary_error=None,
    )

    atomic_module._run_ordered_actions(state)

    assert events == ["KeyboardInterrupt", "SystemExit"]
    assert calls == 1
    assert completed
    assert state.next_index == 1
    assert state.primary_error is first
    assert not any(repr(second) in note for note in _exception_notes(first))


@pytest.mark.parametrize(
    "interruption_type",
    [KeyboardInterrupt, SystemExit, GeneratorExit],
)
@pytest.mark.parametrize("surface", ["planning", "pre-call", "action", "advance"])
@pytest.mark.parametrize("body_fails", [True, False], ids=["body", "cleanup"])
def test_completion_aware_action_bounds_permanent_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    interruption_type: type[BaseException],
    surface: str,
    body_fails: bool,
) -> None:
    interruption = interruption_type("permanent cleanup cancellation")
    body_error = ValueError("body failed before permanent cleanup cancellation")
    attempts = 0
    later_calls = 0
    real_attempt = atomic_module._attempt_ordered_action
    real_advance = atomic_module._advance_ordered_action
    real_coerce = atomic_module._coerce_ordered_action

    class RetryOwner:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    owner = RetryOwner()

    def interrupt_before_call(state: object, ordered: object) -> object:
        nonlocal attempts
        if surface == "pre-call":
            attempts += 1
            raise interruption
        return real_attempt(state, ordered)

    def permanently_interrupt() -> None:
        nonlocal attempts
        if surface == "action":
            attempts += 1
            raise interruption
        owner.close()

    def interrupt_planning(value: object) -> object:
        nonlocal attempts
        if surface == "planning" and isinstance(value, atomic_module._OrderedAction):
            attempts += 1
            raise interruption
        return real_coerce(value)

    def interrupt_advance(state: object) -> None:
        nonlocal attempts
        if surface == "advance":
            attempts += 1
            raise interruption
        real_advance(state)

    def run_later() -> None:
        nonlocal later_calls
        later_calls += 1

    monkeypatch.setattr(
        atomic_module,
        "_attempt_ordered_action",
        interrupt_before_call,
    )
    monkeypatch.setattr(
        atomic_module,
        "_coerce_ordered_action",
        interrupt_planning,
    )
    monkeypatch.setattr(
        atomic_module,
        "_advance_ordered_action",
        interrupt_advance,
    )
    state = atomic_module._OrderedActionState(
        actions=(
            atomic_module._OrderedAction(
                label="permanently interrupted cleanup also failed",
                action=permanently_interrupt,
                complete=lambda: owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            ),
            ("later cleanup also failed", run_later),
        ),
        iteration_failure_label="permanent cleanup iteration also failed",
        primary_error=(body_error if body_fails else None),
    )

    atomic_module._run_ordered_actions(state)

    assert attempts == atomic_module._MAX_ORDERED_ACTION_CANCELLATION_RETRIES + 1
    assert later_calls == 1
    assert state.next_index == 2
    expected = body_error if body_fails else interruption
    assert state.primary_error is expected
    notes = _exception_notes(expected)
    assert len(notes) == (2 if body_fails else 1)
    assert "cancellation retry limit" in notes[-1]
    assert str(atomic_module._MAX_ORDERED_ACTION_CANCELLATION_RETRIES) in notes[-1]
    expected_owners = () if surface == "advance" else (owner,)
    assert getattr(expected, "publication_cleanup_owners", ()) == expected_owners
    owner.close()
    assert owner.closed


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX directory descriptors",
)
def test_posix_authenticated_cleanup_retries_pre_call_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "publication"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_bytes(b"payload")
    reader, root_descriptor = _posix_cleanup_test_reader(root)
    interruption = KeyboardInterrupt("descriptor cleanup pre-call cancellation")
    owners: list[atomic_module._PosixResourceOwner] = []
    real_close_all = atomic_module._PosixResourceOwner.close_all

    def capture_owner(
        owner: atomic_module._PosixResourceOwner,
    ) -> None:
        owners.append(owner)
        real_close_all(owner)

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "close_all",
        capture_owner,
    )
    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "record_for_cleanup",
        lambda _owner, _descriptor: (_ for _ in ()).throw(
            AssertionError("authenticated cleanup rebuilt a descriptor plan")
        ),
    )
    events = _interrupt_ordered_action_before_call(
        monkeypatch,
        label=(
            "publication authenticated file descriptor cleanup also failed; "
            "publication authenticated directory descriptor cleanup also failed"
        ),
        errors=(interruption,),
    )
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            with reader.open_authenticated_file(
                "nested/payload.txt",
                max_bytes=16,
            ):
                pass

        assert caught.value is interruption
        assert events == ["KeyboardInterrupt"]
        assert owners and all(owner.closed for owner in owners)
    finally:
        reader._deactivate()
        os.close(root_descriptor)


def test_windows_authenticated_cleanup_retries_pre_call_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, reader, root_handle = _windows_cleanup_test_reader()
    interruption = SystemExit("HANDLE cleanup pre-call cancellation")
    owners: list[atomic_module._WindowsResourceOwner] = []
    real_close_all = atomic_module._WindowsResourceOwner.close_all

    def capture_owner(
        owner: atomic_module._WindowsResourceOwner,
    ) -> None:
        owners.append(owner)
        real_close_all(owner)

    monkeypatch.setattr(
        atomic_module._WindowsResourceOwner,
        "close_all",
        capture_owner,
    )
    events = _interrupt_ordered_action_before_call(
        monkeypatch,
        label=(
            "publication authenticated file HANDLE cleanup also failed; "
            "publication authenticated directory HANDLE cleanup also failed"
        ),
        errors=(interruption,),
    )
    try:
        with pytest.raises(SystemExit) as caught:
            with reader.open_authenticated_file(
                "nested/payload.txt",
                max_bytes=16,
            ):
                pass

        assert caught.value is interruption
        assert events == ["SystemExit"]
        assert owners and all(owner.closed for owner in owners)
    finally:
        reader._deactivate()
        api.close(root_handle)
    assert api.handles == {}


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX directory descriptors",
)
def test_posix_authenticated_cleanup_owns_open_before_caller_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "publication"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_bytes(b"payload")
    reader, root_descriptor = _posix_cleanup_test_reader(root)
    interruption = KeyboardInterrupt("after owned open before caller store")
    escaped: list[tuple[atomic_module._PosixResourceOwner, int]] = []
    real_open = atomic_module._PosixResourceOwner.open

    def open_then_interrupt(
        owner: atomic_module._PosixResourceOwner,
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(owner, path, flags, mode, dir_fd=dir_fd)
        if path == "nested" and not escaped:
            escaped.append((owner, descriptor))
            raise interruption
        return descriptor

    monkeypatch.setattr(
        atomic_module._PosixResourceOwner,
        "open",
        open_then_interrupt,
    )
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            with reader.open_authenticated_file(
                "nested/payload.txt",
                max_bytes=16,
            ):
                pass

        assert caught.value is interruption
        assert len(escaped) == 1
        owner, descriptor = escaped[0]
        assert owner.closed
        _assert_descriptor_closed(descriptor)
    finally:
        reader._deactivate()
        os.close(root_descriptor)


def test_windows_authenticated_cleanup_owns_acquire_before_caller_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, reader, root_handle = _windows_cleanup_test_reader()
    interruption = SystemExit("after owned HANDLE acquire before caller store")
    escaped: list[tuple[atomic_module._WindowsResourceOwner, int]] = []
    real_acquire = atomic_module._WindowsResourceOwner.acquire

    def acquire_then_interrupt(
        owner: atomic_module._WindowsResourceOwner,
        callback: object,
    ) -> int:
        assert callable(callback)
        handle = real_acquire(owner, callback)
        if not escaped:
            escaped.append((owner, handle))
            raise interruption
        return handle

    monkeypatch.setattr(
        atomic_module._WindowsResourceOwner,
        "acquire",
        acquire_then_interrupt,
    )
    try:
        with pytest.raises(SystemExit) as caught:
            with reader.open_authenticated_file(
                "nested/payload.txt",
                max_bytes=16,
            ):
                pass

        assert caught.value is interruption
        assert len(escaped) == 1
        owner, handle = escaped[0]
        assert owner.closed
        assert handle not in api.handles
    finally:
        reader._deactivate()
        api.close(root_handle)
    assert api.handles == {}


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX directory descriptors",
)
@pytest.mark.parametrize("body_fails", [True, False], ids=["body", "cleanup"])
def test_posix_authenticated_cleanup_retains_owner_after_persistent_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_fails: bool,
) -> None:
    root = tmp_path / "publication"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_bytes(b"payload")
    reader, root_descriptor = _posix_cleanup_test_reader(root)
    real_close = atomic_module.os.close
    body_error = ValueError("authenticated body failed")
    close_error = OSError(errno.EIO, "persistent descriptor close failure")
    failed_descriptor = -1
    retained_owner: atomic_module._PosixResourceOwner | None = None

    def fail_one_descriptor(descriptor: int) -> None:
        nonlocal failed_descriptor
        if failed_descriptor < 0:
            failed_descriptor = descriptor
        if descriptor == failed_descriptor:
            raise close_error
        real_close(descriptor)

    try:
        with monkeypatch.context() as faults:
            faults.setattr(atomic_module.os, "close", fail_one_descriptor)
            with pytest.raises(BaseException) as caught:
                with reader.open_authenticated_file(
                    "nested/payload.txt",
                    max_bytes=16,
                ):
                    if body_fails:
                        raise body_error

        expected = body_error if body_fails else close_error
        assert caught.value is expected
        owners = expected.publication_cleanup_owners
        matches = [
            owner
            for owner in owners
            if isinstance(owner, atomic_module._PosixResourceOwner)
        ]
        assert len(matches) == 1
        retained_owner = matches[0]
        assert not retained_owner.closed
        live = [
            record.descriptor
            for record in retained_owner._records
            if record.descriptor >= 0
        ]
        assert live == [failed_descriptor]
        os.fstat(failed_descriptor)
    finally:
        if retained_owner is not None:
            retained_owner.close()
        reader._deactivate()
        real_close(root_descriptor)

    assert retained_owner is not None and retained_owner.closed
    _assert_descriptor_closed(failed_descriptor)


@pytest.mark.parametrize("body_fails", [True, False], ids=["body", "cleanup"])
def test_windows_authenticated_cleanup_retains_owner_after_persistent_eio(
    monkeypatch: pytest.MonkeyPatch,
    body_fails: bool,
) -> None:
    api, reader, root_handle = _windows_cleanup_test_reader()
    real_close = api.close
    body_error = ValueError("authenticated body failed")
    close_error = OSError(errno.EIO, "persistent HANDLE close failure")
    failed_handle = 0
    retained_owner: atomic_module._WindowsResourceOwner | None = None

    def fail_one_handle(handle: int) -> None:
        nonlocal failed_handle
        if not failed_handle:
            failed_handle = handle
        if handle == failed_handle:
            raise close_error
        real_close(handle)

    try:
        with monkeypatch.context() as faults:
            faults.setattr(api, "close", fail_one_handle)
            with pytest.raises(BaseException) as caught:
                with reader.open_authenticated_file(
                    "nested/payload.txt",
                    max_bytes=16,
                ):
                    if body_fails:
                        raise body_error

        expected = body_error if body_fails else close_error
        assert caught.value is expected
        owners = expected.publication_cleanup_owners
        matches = [
            owner
            for owner in owners
            if isinstance(owner, atomic_module._WindowsResourceOwner)
        ]
        assert len(matches) == 1
        retained_owner = matches[0]
        assert not retained_owner.closed
        live = [record.handle for record in retained_owner._records if record.handle]
        assert live == [failed_handle]
        assert failed_handle in api.handles
    finally:
        if retained_owner is not None:
            retained_owner.close()
        reader._deactivate()
        real_close(root_handle)

    assert retained_owner is not None and retained_owner.closed
    assert api.handles == {}


def test_windows_child_open_retains_exact_owner_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    api.add_file(api.root_id, "payload.txt", b"payload")
    root_handle = api.create_directory_handle(Path("C:/authority"))
    entry = atomic_module._windows_find_child(api, root_handle, "payload.txt")
    assert entry is not None
    real_metadata = api.metadata
    real_close = api.close
    metadata_calls = 0
    opened_handle = 0
    metadata_error = OSError(errno.EIO, "post-acquire metadata failure")
    close_error = OSError(errno.EIO, "persistent local HANDLE close failure")
    aggregate = atomic_module._WindowsResourceOwner(api)
    unrelated_handle = aggregate.acquire(
        lambda: api.create_directory_handle(Path("C:/unrelated"))
    )
    retained_owner: object | None = None

    def fail_second_metadata(handle: int) -> object:
        nonlocal metadata_calls, opened_handle
        if handle != root_handle:
            metadata_calls += 1
            opened_handle = handle
            if metadata_calls == 2:
                raise metadata_error
        return real_metadata(handle)

    def fail_opened_close(handle: int) -> None:
        if handle == opened_handle:
            raise close_error
        real_close(handle)

    try:
        with monkeypatch.context() as faults:
            faults.setattr(api, "metadata", fail_second_metadata)
            faults.setattr(api, "close", fail_opened_close)
            with pytest.raises(OSError) as caught:
                atomic_module._windows_open_child_by_id(
                    api,
                    root_handle,
                    entry,
                    desired_access=atomic_module._WINDOWS_FILE_READ_DATA,
                    expected_directory=False,
                    resource_owner=aggregate,
                )

        assert caught.value is metadata_error
        owners = metadata_error.publication_cleanup_owners
        assert len(owners) == 1
        retained_owner = owners[0]
        assert not retained_owner.closed
        assert opened_handle in api.handles
    finally:
        if retained_owner is not None:
            retained_owner.close()
        assert unrelated_handle in api.handles
        aggregate.close()
        real_close(root_handle)

    assert retained_owner is not None and retained_owner.closed
    assert api.handles == {}


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX descriptors",
)
def test_posix_record_cleanup_entry_interrupt_retains_original_exact_owner(
    tmp_path: Path,
) -> None:
    owner = atomic_module._PosixResourceOwner()
    child = owner.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    unrelated = owner.open(
        tmp_path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    record = owner.record_for_cleanup(child)
    primary = OSError(errno.EIO, "POSIX operation primary")
    interruption = KeyboardInterrupt("POSIX exact cleanup helper entry")
    retained_owner: object | None = None

    try:
        _call_with_interrupt_on_source_line(
            atomic_module._PosixResourceOwner._run_record_cleanup_after_error,
            "cleanup.primary_error = primary_error",
            lambda: owner.close_record_after_error(record, primary),
            predicate=lambda local: local["cleanup"].primary_error is primary,
            error=interruption,
        )

        owners = primary.publication_cleanup_owners
        assert len(owners) == 1
        retained_owner = owners[0]
        assert isinstance(retained_owner, atomic_module._ExactResourceCleanupOwner)
        assert not retained_owner.closed
        os.fstat(child)
        os.fstat(unrelated)
        assert any(
            "POSIX exact cleanup helper entry" in note
            for note in _exception_notes(primary)
        )
    finally:
        if retained_owner is not None:
            retained_owner.close()
        os.fstat(unrelated)
        owner.close()

    assert retained_owner is not None and retained_owner.closed
    _assert_descriptor_closed(child)
    _assert_descriptor_closed(unrelated)


def test_windows_child_cleanup_entry_interrupt_retains_original_exact_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    api.add_file(api.root_id, "payload.txt", b"payload")
    root_handle = api.create_directory_handle(Path("C:/authority"))
    entry = atomic_module._windows_find_child(api, root_handle, "payload.txt")
    assert entry is not None
    owner = atomic_module._WindowsResourceOwner(api)
    unrelated = owner.acquire(lambda: api.create_directory_handle(Path("C:/unrelated")))
    primary = OSError(errno.EIO, "Windows child metadata primary")
    interruption = SystemExit("Windows exact cleanup helper entry")
    real_metadata = api.metadata
    metadata_calls = 0
    child_handle = 0
    retained_owner: object | None = None

    def fail_child_recheck(handle: int) -> object:
        nonlocal metadata_calls, child_handle
        if handle != root_handle and handle != unrelated:
            metadata_calls += 1
            child_handle = handle
            if metadata_calls == 2:
                raise primary
        return real_metadata(handle)

    try:
        with monkeypatch.context() as faults:
            faults.setattr(api, "metadata", fail_child_recheck)
            with pytest.raises(OSError) as caught:
                _call_with_interrupt_on_source_line(
                    atomic_module._WindowsResourceOwner._run_record_cleanup_after_error,
                    "cleanup.primary_error = primary_error",
                    lambda: atomic_module._windows_open_child_by_id(
                        api,
                        root_handle,
                        entry,
                        desired_access=atomic_module._WINDOWS_FILE_READ_DATA,
                        expected_directory=False,
                        resource_owner=owner,
                    ),
                    predicate=lambda local: local["cleanup"].primary_error is primary,
                    error=interruption,
                )

        assert caught.value is primary
        owners = primary.publication_cleanup_owners
        assert len(owners) == 1
        retained_owner = owners[0]
        assert isinstance(retained_owner, atomic_module._ExactResourceCleanupOwner)
        assert not retained_owner.closed
        assert child_handle in api.handles
        assert unrelated in api.handles
        assert any(
            "Windows exact cleanup helper entry" in note
            for note in _exception_notes(primary)
        )
    finally:
        if retained_owner is not None:
            retained_owner.close()
        assert unrelated in api.handles
        owner.close()
        api.close(root_handle)

    assert retained_owner is not None and retained_owner.closed
    assert api.handles == {}


def test_windows_owned_file_read_primary_survives_exact_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    api.add_file(api.root_id, "payload.txt", b"payload")
    root_handle = api.create_directory_handle(Path("C:/authority"))
    entry = atomic_module._windows_find_child(api, root_handle, "payload.txt")
    assert entry is not None
    owner = atomic_module._WindowsResourceOwner(api)
    unrelated = owner.acquire(lambda: api.create_directory_handle(Path("C:/unrelated")))
    read_error = OSError(errno.EIO, "Windows ownership file read failure")
    close_error = OSError(errno.EIO, "Windows ownership file close failure")
    real_read = api.read
    real_close = api.close
    child_handle = 0
    retained_owner: object | None = None

    def fail_child_read(handle: int, size: int) -> bytes:
        nonlocal child_handle
        if handle not in {root_handle, unrelated}:
            child_handle = handle
            raise read_error
        return real_read(handle, size)

    def fail_child_close(handle: int) -> None:
        if handle == child_handle:
            raise close_error
        real_close(handle)

    try:
        with monkeypatch.context() as faults:
            faults.setattr(api, "read", fail_child_read)
            faults.setattr(api, "close", fail_child_close)
            with pytest.raises(OSError) as caught:
                atomic_module._windows_owned_file_record(
                    api,
                    root_handle,
                    entry,
                    Path("C:/authority/payload.txt"),
                    root_device=7,
                    budget=atomic_module._OwnershipBudget(),
                    relative="payload.txt",
                    entry_policy=None,
                    resource_owner=owner,
                )

        assert caught.value is read_error
        owners = read_error.publication_cleanup_owners
        assert len(owners) == 1
        retained_owner = owners[0]
        assert isinstance(retained_owner, atomic_module._ExactResourceCleanupOwner)
        assert not retained_owner.closed
        assert child_handle in api.handles
        assert unrelated in api.handles
        assert any(
            "Windows ownership file close failure" in note
            for note in _exception_notes(read_error)
        )
    finally:
        if retained_owner is not None:
            retained_owner.close()
        assert unrelated in api.handles
        owner.close()
        real_close(root_handle)

    assert retained_owner is not None and retained_owner.closed
    assert api.handles == {}


def test_windows_owned_directory_scan_primary_survives_exact_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    child_id = api.add_directory(api.root_id, "nested")
    root_handle = api.create_directory_handle(Path("C:/authority"))
    owner = atomic_module._WindowsResourceOwner(api)
    unrelated = owner.acquire(lambda: api.create_directory_handle(Path("C:/unrelated")))
    scan_error = OSError(errno.EIO, "Windows ownership directory scan failure")
    close_error = OSError(errno.EIO, "Windows ownership directory close failure")
    real_iter = api.iter_directory
    real_close = api.close
    child_handle = 0
    retained_owner: object | None = None

    def fail_child_scan(handle: int):
        nonlocal child_handle
        if api.handles[handle] == child_id:
            child_handle = handle
            raise scan_error
        yield from real_iter(handle)

    def fail_child_close(handle: int) -> None:
        if handle == child_handle:
            raise close_error
        real_close(handle)

    try:
        with monkeypatch.context() as faults:
            faults.setattr(api, "iter_directory", fail_child_scan)
            faults.setattr(api, "close", fail_child_close)
            with pytest.raises(OSError) as caught:
                atomic_module._scan_windows_owned_directory(
                    api,
                    root_handle,
                    Path("C:/authority"),
                    (),
                    root_device=7,
                    budget=atomic_module._OwnershipBudget(),
                    inventory=[],
                    file_records=[],
                    entry_identities=[],
                    entry_policy=None,
                    depth=0,
                    resource_owner=owner,
                )

        assert caught.value is scan_error
        owners = scan_error.publication_cleanup_owners
        assert len(owners) == 1
        retained_owner = owners[0]
        assert isinstance(retained_owner, atomic_module._ExactResourceCleanupOwner)
        assert not retained_owner.closed
        assert child_handle in api.handles
        assert unrelated in api.handles
        assert any(
            "Windows ownership directory close failure" in note
            for note in _exception_notes(scan_error)
        )
    finally:
        if retained_owner is not None:
            retained_owner.close()
        assert unrelated in api.handles
        owner.close()
        real_close(root_handle)

    assert retained_owner is not None and retained_owner.closed
    assert api.handles == {}


@pytest.mark.parametrize("use_parent_resource", [False, True])
def test_windows_authority_factory_retains_incomplete_local_owner(
    monkeypatch: pytest.MonkeyPatch,
    use_parent_resource: bool,
) -> None:
    api = _FakeWindowsApi()
    external_handle = (
        api.create_directory_handle(Path("C:/authority")) if use_parent_resource else 0
    )
    primary = RuntimeError("authority construction failed")
    close_error = OSError(errno.EIO, "persistent authority HANDLE close failure")
    real_close = api.close
    retained_owner: object | None = None

    def fail_authority_init(_authority: object, **_kwargs: object) -> None:
        raise primary

    def fail_close(_handle: int) -> None:
        raise close_error

    try:
        with monkeypatch.context() as faults:
            _install_fake_windows_api(faults, api)
            faults.setattr(
                atomic_module._PublicationAuthority,
                "__init__",
                fail_authority_init,
            )
            faults.setattr(api, "close", fail_close)
            with pytest.raises(RuntimeError) as caught:
                atomic_module._open_windows_publication_authority(
                    Path("C:/authority"),
                    parent_resource=(external_handle or None),
                    expected_parent_identity=None,
                )

        assert caught.value is primary
        assert any(
            "authority cleanup also failed" in note
            for note in _exception_notes(primary)
        )
        assert len(primary.publication_cleanup_owners) == 1
        retained_owner = primary.publication_cleanup_owners[0]
        expected_type = (
            atomic_module._WindowsResourceOwner
            if use_parent_resource
            else atomic_module._WindowsLexicalAuthorityOwner
        )
        assert isinstance(retained_owner, expected_type)
        assert not retained_owner.closed
        assert api.handles
    finally:
        if retained_owner is not None:
            retained_owner.close()
        if external_handle:
            real_close(external_handle)

    assert retained_owner is not None and retained_owner.closed
    assert api.handles == {}


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX directory descriptors",
)
def test_posix_authority_factory_retains_incomplete_local_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("authority construction failed")
    close_error = OSError(errno.EIO, "persistent authority descriptor close failure")
    retained_owner: atomic_module._PosixResourceOwner | None = None

    def fail_authority_init(_authority: object, **_kwargs: object) -> None:
        raise primary

    def fail_close(_descriptor: int) -> None:
        raise close_error

    try:
        with monkeypatch.context() as faults:
            faults.setattr(
                atomic_module._PublicationAuthority,
                "__init__",
                fail_authority_init,
            )
            faults.setattr(atomic_module.os, "close", fail_close)
            with pytest.raises(RuntimeError) as caught:
                atomic_module._open_posix_publication_authority(
                    tmp_path,
                    parent_resource=None,
                    expected_parent_identity=None,
                )

        assert caught.value is primary
        assert any(
            "authority cleanup also failed" in note
            for note in _exception_notes(primary)
        )
        assert len(primary.publication_cleanup_owners) == 1
        retained_owner = primary.publication_cleanup_owners[0]
        assert isinstance(retained_owner, atomic_module._PosixResourceOwner)
        assert not retained_owner.closed
    finally:
        if retained_owner is not None:
            retained_owner.close()

    assert retained_owner is not None and retained_owner.closed


@pytest.mark.parametrize("body_fails", [True, False], ids=["body", "cleanup"])
def test_ordered_cleanup_retains_multiple_incomplete_owners(
    body_fails: bool,
) -> None:
    body_error = ValueError("ordered cleanup body failed")
    cleanup_errors = (
        OSError(errno.EIO, "first persistent cleanup failure"),
        OSError(errno.EIO, "second persistent cleanup failure"),
    )

    class RetryOwner:
        def __init__(self, error: OSError) -> None:
            self.error = error
            self.fail = True
            self.closed = False

        def close(self) -> None:
            if self.fail:
                raise self.error
            self.closed = True

    owners = tuple(RetryOwner(error) for error in cleanup_errors)
    actions = tuple(
        atomic_module._OrderedAction(
            label=f"owner {index} cleanup also failed",
            action=owner.close,
            complete=lambda owner=owner: owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=owner,
        )
        for index, owner in enumerate(owners)
    )

    with pytest.raises(BaseException) as caught:
        with atomic_module._run_context_with_cleanup_actions(actions):
            if body_fails:
                raise body_error

    expected = body_error if body_fails else cleanup_errors[0]
    assert caught.value is expected
    assert expected.publication_cleanup_owners == owners
    assert all(not owner.closed for owner in owners)
    for owner in owners:
        owner.fail = False
        owner.close()
    assert all(owner.closed for owner in owners)


def test_cleanup_owner_attachment_bypasses_hostile_exception_attributes() -> None:
    class HostilePrimary(BaseException):
        def __getattribute__(self, name: str) -> object:
            if name == "publication_cleanup_owners":
                raise RuntimeError("hostile cleanup owner read")
            return super().__getattribute__(name)

        def __setattr__(self, name: str, value: object) -> None:
            if name == "publication_cleanup_owners":
                raise RuntimeError("hostile cleanup owner write")
            super().__setattr__(name, value)

    class RetryOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    primary = HostilePrimary("hostile primary")
    owner = RetryOwner()

    with pytest.raises(HostilePrimary) as caught:
        with atomic_module._run_context_with_cleanup_actions(
            (
                atomic_module._OrderedAction(
                    label="hostile cleanup also failed",
                    action=lambda: (_ for _ in ()).throw(
                        OSError(errno.EIO, "cleanup failure")
                    ),
                    complete=lambda: False,
                    incomplete_owner=owner,
                ),
            )
        ):
            raise primary

    assert caught.value is primary
    assert BaseException.__getattribute__(
        primary,
        "publication_cleanup_owners",
    ) == (owner,)
    atomic_module._attach_publication_cleanup_owner(primary, owner)
    assert BaseException.__getattribute__(
        primary,
        "publication_cleanup_owners",
    ) == (owner,)
    owner.close()


def test_cleanup_owner_attachment_preserves_exact_builtin_tuple() -> None:
    class RetryOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    primary = ValueError("cleanup failed")
    first = RetryOwner()
    second = RetryOwner()
    BaseException.__setattr__(
        primary,
        "publication_cleanup_owners",
        (first,),
    )

    atomic_module._attach_publication_cleanup_owner(primary, second)

    retained = BaseException.__getattribute__(
        primary,
        "publication_cleanup_owners",
    )
    assert type(retained) is tuple
    assert retained == (first, second)


def test_cleanup_owner_attachment_does_not_iterate_tuple_subclass() -> None:
    class HostileTuple(tuple):
        def __iter__(self) -> object:
            raise RuntimeError("hostile tuple iteration")

    class RetryOwner:
        closed = False

        def close(self) -> None:
            self.closed = True

    primary = ValueError("cleanup failed")
    owner = RetryOwner()
    BaseException.__setattr__(
        primary,
        "publication_cleanup_owners",
        HostileTuple((object(),)),
    )

    atomic_module._attach_publication_cleanup_owner(primary, owner)

    retained = BaseException.__getattribute__(
        primary,
        "publication_cleanup_owners",
    )
    assert type(retained) is tuple
    assert retained == (owner,)


def test_completion_observer_failure_does_not_block_later_cleanup() -> None:
    observer_error = OSError(errno.EIO, "persistent completion observation failure")
    calls: list[str] = []
    state = atomic_module._OrderedActionState(
        actions=(
            atomic_module._OrderedAction(
                label="first cleanup also failed",
                action=lambda: calls.append("first"),
                complete=lambda: (_ for _ in ()).throw(observer_error),
                retry_incomplete="cancellation",
            ),
            ("later cleanup also failed", lambda: calls.append("later")),
        ),
        iteration_failure_label="cleanup iteration also failed",
        primary_error=None,
    )

    atomic_module._run_ordered_actions(state)

    assert calls == ["first", "later"]
    assert state.next_index == 2
    assert state.primary_error is observer_error


def test_exact_record_cleanup_does_not_scan_retained_records() -> None:
    class NoIterationList(list[object]):
        def __iter__(self) -> object:
            raise AssertionError("exact record cleanup scanned retained records")

    posix_owner = atomic_module._PosixResourceOwner()
    read_descriptor, write_descriptor = os.pipe()
    duplicate = posix_owner.duplicate(read_descriptor)
    posix_record = posix_owner.record_for_cleanup(duplicate)
    posix_owner._records = NoIterationList(posix_owner._records)
    try:
        posix_owner.close_record(posix_record)
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)

    api = _FakeWindowsApi()
    windows_owner = atomic_module._WindowsResourceOwner(api)
    handle = windows_owner.acquire(lambda: api.create_directory_handle(Path("C:/")))
    windows_record = windows_owner.record_for_cleanup(handle)
    windows_owner._records = NoIterationList(windows_owner._records)
    windows_owner.close_record(windows_record)

    assert posix_record.descriptor < 0
    assert windows_record.handle == 0
    assert api.handles == {}


def test_plain_ordered_action_is_not_repeated_if_cursor_cleanup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    interruption = KeyboardInterrupt("after plain action cursor advance")
    real_plain = atomic_module._run_plain_ordered_action
    interrupted = False

    def interrupt_after_action(state: object, ordered: object) -> None:
        nonlocal interrupted
        real_plain(state, ordered)
        if not interrupted:
            interrupted = True
            raise interruption

    monkeypatch.setattr(
        atomic_module,
        "_run_plain_ordered_action",
        interrupt_after_action,
    )
    state = atomic_module._OrderedActionState(
        actions=(
            ("first validation also failed", lambda: calls.append("first")),
            ("second validation also failed", lambda: calls.append("second")),
        ),
        iteration_failure_label="validation iteration also failed",
        primary_error=None,
    )

    atomic_module._run_ordered_actions(state)

    assert calls == ["first", "second"]
    assert state.primary_error is interruption


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor reuse semantics",
)
def test_ordered_posix_record_does_not_close_reentrant_replacement() -> None:
    resources = atomic_module._PosixResourceOwner()
    source_read, source_write = os.pipe()
    foreign_read, foreign_write = os.pipe()
    target = resources.duplicate(source_read)
    record = resources.record_for_cleanup(target)
    real_close = atomic_module.os.close
    cancellation = KeyboardInterrupt("after descriptor close")
    replacement = -1
    cleanup_complete = False

    def close_then_replace(descriptor: int) -> None:
        nonlocal replacement
        real_close(descriptor)
        if descriptor == target:
            replacement = os.dup2(foreign_read, target)
            assert replacement == target
            raise cancellation

    def close_resources() -> None:
        nonlocal cleanup_complete
        resources.close_all()
        cleanup_complete = True

    atomic_module.os.close = close_then_replace
    try:
        state = atomic_module._OrderedActionState(
            actions=(
                atomic_module._OrderedAction(
                    label="descriptor cleanup also failed",
                    action=close_resources,
                    complete=lambda: cleanup_complete,
                    retry_incomplete="cancellation",
                    incomplete_owner=resources,
                ),
            ),
            iteration_failure_label="descriptor iteration also failed",
            primary_error=None,
        )
        atomic_module._run_ordered_actions(state)
        assert state.primary_error is cancellation
        assert record.descriptor < 0
        assert cleanup_complete
        assert not getattr(cancellation, "publication_cleanup_owners", ())
        os.fstat(replacement)
    finally:
        atomic_module.os.close = real_close
        resources.close_all()
        real_close(source_read)
        real_close(source_write)
        real_close(foreign_read)
        real_close(foreign_write)


def test_ordered_windows_record_does_not_close_reentrant_replacement() -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    target = owner.acquire(lambda: api.create_directory_handle(Path("C:/authority")))
    record = owner.record_for_cleanup(target)
    foreign_id = api.add_directory()
    real_close = api.close
    cancellation = SystemExit("after HANDLE close")
    replacement = 0
    cleanup_complete = False

    def close_then_replace(handle: int) -> None:
        nonlocal replacement
        real_close(handle)
        if handle == target:
            api.next_handle = handle
            replacement = api._new_handle(foreign_id)
            assert replacement == target
            raise cancellation

    def close_resources() -> None:
        nonlocal cleanup_complete
        owner.close_all()
        cleanup_complete = True

    api.close = close_then_replace
    try:
        state = atomic_module._OrderedActionState(
            actions=(
                atomic_module._OrderedAction(
                    label="HANDLE cleanup also failed",
                    action=close_resources,
                    complete=lambda: cleanup_complete,
                    retry_incomplete="cancellation",
                    incomplete_owner=owner,
                ),
            ),
            iteration_failure_label="HANDLE iteration also failed",
            primary_error=None,
        )
        atomic_module._run_ordered_actions(state)
        assert state.primary_error is cancellation
        assert record.handle == 0
        assert cleanup_complete
        assert not getattr(cancellation, "publication_cleanup_owners", ())
        assert api.handles[replacement] == foreign_id
    finally:
        api.close = real_close
        owner.close_all()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor reuse semantics",
)
def test_posix_owner_completion_flag_reports_foreign_reuse() -> None:
    resources = atomic_module._PosixResourceOwner()
    source_read, source_write = os.pipe()
    foreign_read, foreign_write = os.pipe()
    target = resources.duplicate(source_read)
    record = resources.record_for_cleanup(target)
    cleanup_complete = False

    def close_resources() -> None:
        nonlocal cleanup_complete
        resources.close_all()
        cleanup_complete = True

    try:
        os.close(target)
        replacement = os.dup2(foreign_read, target)
        assert replacement == target
        state = atomic_module._OrderedActionState(
            actions=(
                atomic_module._OrderedAction(
                    label="descriptor cleanup also failed",
                    action=close_resources,
                    complete=lambda: cleanup_complete,
                    retry_incomplete="cancellation",
                    incomplete_owner=resources,
                ),
            ),
            iteration_failure_label="descriptor iteration also failed",
            primary_error=None,
        )

        atomic_module._run_ordered_actions(state)

        assert isinstance(state.primary_error, RuntimeError)
        assert "ownership changed" in str(state.primary_error)
        assert not cleanup_complete
        assert record.descriptor < 0
        assert not getattr(
            state.primary_error,
            "publication_cleanup_owners",
            (),
        )
        os.fstat(replacement)
    finally:
        resources.close_all()
        for descriptor in (
            source_read,
            source_write,
            foreign_read,
            foreign_write,
            target,
        ):
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise


def test_windows_owner_completion_flag_reports_foreign_reuse() -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    target = owner.acquire(lambda: api.create_directory_handle(Path("C:/authority")))
    record = owner.record_for_cleanup(target)
    foreign_id = api.add_directory()
    cleanup_complete = False

    def close_resources() -> None:
        nonlocal cleanup_complete
        owner.close_all()
        cleanup_complete = True

    api.close(target)
    api.next_handle = target
    replacement = api._new_handle(foreign_id)
    assert replacement == target
    state = atomic_module._OrderedActionState(
        actions=(
            atomic_module._OrderedAction(
                label="HANDLE cleanup also failed",
                action=close_resources,
                complete=lambda: cleanup_complete,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            ),
        ),
        iteration_failure_label="HANDLE iteration also failed",
        primary_error=None,
    )

    atomic_module._run_ordered_actions(state)

    assert isinstance(state.primary_error, RuntimeError)
    assert "ownership changed" in str(state.primary_error)
    assert not cleanup_complete
    assert record.handle == 0
    assert not getattr(state.primary_error, "publication_cleanup_owners", ())
    assert api.handles[replacement] == foreign_id
    owner.close_all()
    api.close(replacement)


def test_reopen_interrupt_after_callback_result_store_runs_post_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    real_capture = atomic_module.PublicationDirectoryReader.capture_ownership
    capture_calls = 0
    saved_readers: list[object] = []
    interruption = KeyboardInterrupt("after callback result store")

    def count_capture(reader: object, **kwargs: object) -> object:
        nonlocal capture_calls
        capture_calls += 1
        return real_capture(reader, **kwargs)

    def consume(reader: object) -> int:
        saved_readers.append(reader)
        return 7

    monkeypatch.setattr(
        atomic_module.PublicationDirectoryReader,
        "capture_ownership",
        count_capture,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_after_store(
            atomic_module._run_callback_with_post_validations,
            "result",
            lambda: atomic_module.reopen_authenticated_directory(
                directory,
                ownership,
                consume,
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert capture_calls == 2
    with pytest.raises(RuntimeError, match="no longer active"):
        saved_readers[0].inventory()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
@pytest.mark.parametrize("attribute", ["descriptor", "identity"])
def test_posix_owner_recovers_interrupt_after_open_result_store(
    tmp_path: Path,
    attribute: str,
) -> None:
    owner = atomic_module._PosixResourceOwner()
    error = KeyboardInterrupt(f"after owned {attribute} publication")

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_after_attribute_store(
            atomic_module._PosixResourceOwner.open,
            attribute,
            lambda: owner.open(
                tmp_path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            ),
            error=error,
        )

    assert caught.value is error
    assert owner.closed


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires POSIX descriptor semantics",
)
@pytest.mark.parametrize("operation", ["open", "duplicate"])
def test_posix_owner_publishes_record_before_native_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    owner = atomic_module._PosixResourceOwner()
    primary = KeyboardInterrupt("record construction interrupted")
    construction_calls = 0
    acquired: list[int] = []
    real_open = atomic_module.os.open
    real_dup = atomic_module.os.dup
    source = -1

    def fail_record_construction(_descriptor: int) -> object:
        nonlocal construction_calls
        construction_calls += 1
        raise primary

    def capture_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)
        acquired.append(descriptor)
        return descriptor

    def capture_duplicate(descriptor: int) -> int:
        duplicate = real_dup(descriptor)
        acquired.append(duplicate)
        return duplicate

    monkeypatch.setattr(
        atomic_module,
        "_PosixDescriptorRecord",
        fail_record_construction,
    )
    try:
        if operation == "open":
            monkeypatch.setattr(atomic_module.os, "open", capture_open)

            def callback() -> int:
                return owner.open(
                    tmp_path,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )

        else:
            source = real_open(
                tmp_path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            monkeypatch.setattr(atomic_module.os, "dup", capture_duplicate)

            def callback() -> int:
                return owner.duplicate(source)

        with pytest.raises(KeyboardInterrupt) as caught:
            callback()

        assert caught.value is primary
        assert construction_calls == 1
        assert acquired == []
        assert owner.closed
    finally:
        for descriptor in acquired:
            os.close(descriptor)
        if source >= 0:
            os.close(source)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires POSIX descriptor semantics",
)
@pytest.mark.parametrize("operation", ["open", "duplicate"])
def test_posix_acquisition_cleanup_retains_primary_and_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    owner = atomic_module._PosixResourceOwner()
    primary = OSError(errno.EIO, "descriptor identity binding failed")
    cleanup_interruption = KeyboardInterrupt("descriptor cleanup was interrupted")
    real_identity = atomic_module._resource_owner_identity
    identity_calls = 0
    source = -1

    def fail_binding_then_cleanup(metadata: object) -> tuple[int, ...]:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise primary
        raise cleanup_interruption

    monkeypatch.setattr(
        atomic_module,
        "_resource_owner_identity",
        fail_binding_then_cleanup,
    )
    try:
        if operation == "open":

            def callback() -> int:
                return owner.open(
                    tmp_path,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )

        else:
            source = os.open(
                tmp_path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )

            def callback() -> int:
                return owner.duplicate(source)

        with pytest.raises(OSError) as caught:
            callback()

        assert caught.value is primary
        assert identity_calls > 1
        retained = BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        )
        assert len(retained) == 1
        assert not retained[0].closed
        live = [
            record.descriptor for record in owner._records if record.descriptor >= 0
        ]
        assert len(live) == 1
        os.fstat(live[0])
    finally:
        monkeypatch.setattr(
            atomic_module,
            "_resource_owner_identity",
            real_identity,
        )
        for retry_owner in BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        ):
            retry_owner.close()
        if source >= 0:
            os.close(source)

    assert owner.closed


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires POSIX descriptor semantics",
)
@pytest.mark.parametrize("operation", ["open", "duplicate"])
def test_posix_acquisition_cleanup_contains_result_store_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    owner = atomic_module._PosixResourceOwner()
    primary = OSError(errno.EIO, "descriptor identity binding failed")
    close_error = OSError(errno.EIO, "persistent descriptor close failure")
    interruption = KeyboardInterrupt("after descriptor close result store")
    real_identity = atomic_module._resource_owner_identity
    real_close = atomic_module.os.close
    identity_calls = 0
    source = -1

    def fail_initial_identity(metadata: object) -> tuple[int, ...]:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise primary
        return real_identity(metadata)

    def fail_owned_close(descriptor: int) -> None:
        if any(record.descriptor == descriptor for record in owner._records):
            raise close_error
        real_close(descriptor)

    monkeypatch.setattr(
        atomic_module,
        "_resource_owner_identity",
        fail_initial_identity,
    )
    monkeypatch.setattr(atomic_module.os, "close", fail_owned_close)
    try:
        if operation == "open":

            def callback() -> int:
                return owner.open(
                    tmp_path,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )

        else:
            source = os.open(
                tmp_path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )

            def callback() -> int:
                return owner.duplicate(source)

        with pytest.raises(OSError) as caught:
            _call_with_interrupt_after_call_result_store(
                atomic_module._PosixResourceOwner.close_record,
                "close_error",
                callback,
                error=interruption,
            )

        assert caught.value is primary
        retained = BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        )
        assert len(retained) == 1
        live = [
            record.descriptor for record in owner._records if record.descriptor >= 0
        ]
        assert len(live) == 1
        os.fstat(live[0])
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        monkeypatch.setattr(
            atomic_module,
            "_resource_owner_identity",
            real_identity,
        )
        for retry_owner in BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        ):
            retry_owner.close()
        if source >= 0:
            real_close(source)

    assert owner.closed


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires POSIX descriptor semantics",
)
@pytest.mark.parametrize("operation", ["open", "duplicate"])
def test_posix_acquisition_cleanup_contains_runner_entry_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    owner = atomic_module._PosixResourceOwner()
    primary = OSError(errno.EIO, "descriptor identity binding failed")
    interruption = KeyboardInterrupt("descriptor cleanup runner entry")
    real_identity = atomic_module._resource_owner_identity
    source = -1
    identity_calls = 0

    def fail_initial_identity(metadata: object) -> tuple[int, ...]:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise primary
        return real_identity(metadata)

    monkeypatch.setattr(
        atomic_module,
        "_resource_owner_identity",
        fail_initial_identity,
    )
    observed = _interrupt_ordered_runner_entries(
        monkeypatch,
        errors=(interruption,),
    )
    try:
        if operation == "open":

            def callback() -> int:
                return owner.open(
                    tmp_path,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )

        else:
            source = os.open(
                tmp_path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )

            def callback() -> int:
                return owner.duplicate(source)

        with pytest.raises(OSError) as caught:
            callback()

        assert caught.value is primary
        assert observed == [interruption]
        assert owner.closed
        assert any(
            "descriptor cleanup runner entry" in note
            for note in _exception_notes(primary)
        )
    finally:
        monkeypatch.setattr(
            atomic_module,
            "_resource_owner_identity",
            real_identity,
        )
        owner.close()
        if source >= 0:
            os.close(source)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_owner_closes_same_directory_after_permission_change(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "mutable-mode"
    directory.mkdir()
    directory.chmod(0o755)
    owner = atomic_module._PosixResourceOwner()
    descriptor = owner.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    before = os.fstat(descriptor)
    try:
        directory.chmod(0o700)
        after = os.fstat(descriptor)
        assert atomic_module._ownership_binding_identity(before) != (
            atomic_module._ownership_binding_identity(after)
        )
        assert atomic_module._resource_owner_identity(before) == (
            atomic_module._resource_owner_identity(after)
        )

        owner.close_all()

        assert owner.closed
        _assert_descriptor_closed(descriptor)
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_owner_retries_persistent_eio_after_permission_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "mutable-mode-eio"
    directory.mkdir()
    directory.chmod(0o755)
    owner = atomic_module._PosixResourceOwner()
    descriptor = owner.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    directory.chmod(0o700)
    real_close = atomic_module.os.close
    error = OSError(errno.EIO, "persistent close failure after chmod")

    def fail_owned_descriptor(candidate: int) -> None:
        if candidate == descriptor:
            raise error
        real_close(candidate)

    try:
        monkeypatch.setattr(atomic_module.os, "close", fail_owned_descriptor)
        for _attempt in range(2):
            with pytest.raises(OSError) as caught:
                owner.close_all()
            assert caught.value is error
            assert not owner.closed
            os.fstat(descriptor)
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        owner.close_all()

    assert owner.closed
    _assert_descriptor_closed(descriptor)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_factory_recovers_interrupt_in_child_open_registration(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "registration-child"
    parent.mkdir()
    error = SystemExit("after child descriptor store")

    with pytest.raises(SystemExit) as caught:
        _call_with_interrupt_after_attribute_store(
            atomic_module._PosixResourceOwner.open,
            "descriptor",
            lambda: atomic_module._open_posix_publication_authority(
                parent,
                parent_resource=None,
                expected_parent_identity=None,
            ),
            predicate=lambda local: local["path"] == parent.name,
            error=error,
        )

    assert caught.value is error


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
@pytest.mark.parametrize("attribute", ["descriptor", "identity"])
def test_posix_owner_recovers_interrupt_after_dup_result_store(
    tmp_path: Path,
    attribute: str,
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    owner = atomic_module._PosixResourceOwner()
    error = KeyboardInterrupt(f"after owned {attribute} publication")
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            _call_with_interrupt_after_attribute_store(
                atomic_module._PosixResourceOwner.duplicate,
                attribute,
                lambda: owner.duplicate(descriptor),
                error=error,
            )
        assert caught.value is error
        assert owner.closed
        os.fstat(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_authority_handoff_closes_on_interrupt_before_caller_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "owned"
    _write_tree(directory, "payload.txt", "payload")
    real_open = atomic_module._open_posix_publication_authority
    opened: list[object] = []
    error = KeyboardInterrupt("after authority factory return")

    def open_then_interrupt(*args: object, **kwargs: object) -> object:
        authority = real_open(*args, **kwargs)
        opened.append(authority)
        raise error

    monkeypatch.setattr(
        atomic_module,
        "_open_posix_publication_authority",
        open_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        capture_directory_ownership(directory)

    assert caught.value is error
    assert len(opened) == 1
    authority = opened[0]
    assert authority._closed
    assert _posix_authority_resources(authority).closed


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_authority_close_before_close_interrupt_is_retryable_and_cleans_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = atomic_module._open_posix_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    resources = _posix_authority_resources(authority)
    descriptors = tuple(record.descriptor for record in resources._records)
    target = descriptors[-1]
    real_close = atomic_module.os.close
    error = KeyboardInterrupt("before real close")
    injected = False

    def interrupt_before_close(descriptor: int) -> None:
        nonlocal injected
        if descriptor == target and not injected:
            injected = True
            raise error
        real_close(descriptor)

    try:
        monkeypatch.setattr(atomic_module.os, "close", interrupt_before_close)
        with pytest.raises(KeyboardInterrupt) as caught:
            authority.close()
        assert caught.value is error
        assert injected
        assert not authority._closed
        os.fstat(target)
        for descriptor in descriptors[:-1]:
            _assert_descriptor_closed(descriptor)
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        authority.close()

    assert authority._closed
    _assert_descriptor_closed(target)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_authority_close_after_close_system_exit_closes_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = atomic_module._open_posix_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    resources = _posix_authority_resources(authority)
    descriptors = tuple(record.descriptor for record in resources._records)
    target = descriptors[-1]
    real_close = atomic_module.os.close
    error = SystemExit("after real close")
    injected = False

    def interrupt_after_close(descriptor: int) -> None:
        nonlocal injected
        real_close(descriptor)
        if descriptor == target and not injected:
            injected = True
            raise error

    monkeypatch.setattr(atomic_module.os, "close", interrupt_after_close)
    with pytest.raises(SystemExit) as caught:
        authority.close()

    assert caught.value is error
    assert injected
    assert authority._closed
    assert resources.closed
    for descriptor in descriptors:
        _assert_descriptor_closed(descriptor)
    authority.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_authority_persistent_eio_retains_only_failed_owner_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = atomic_module._open_posix_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    resources = _posix_authority_resources(authority)
    descriptors = tuple(record.descriptor for record in resources._records)
    target = descriptors[-1]
    real_close = atomic_module.os.close
    error = OSError(errno.EIO, "persistent close failure")

    def fail_target(descriptor: int) -> None:
        if descriptor == target:
            raise error
        real_close(descriptor)

    try:
        monkeypatch.setattr(atomic_module.os, "close", fail_target)
        for _attempt in range(2):
            with pytest.raises(OSError) as caught:
                authority.close()
            assert caught.value is error
            assert not authority._closed
            os.fstat(target)
        for descriptor in descriptors[:-1]:
            _assert_descriptor_closed(descriptor)
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        authority.close()

    assert authority._closed
    _assert_descriptor_closed(target)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_authority_close_preserves_first_error_and_attempts_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = atomic_module._open_posix_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    resources = _posix_authority_resources(authority)
    descriptors = tuple(record.descriptor for record in resources._records)
    first_target, second_target = descriptors[-1], descriptors[-2]
    real_close = atomic_module.os.close
    first_error = KeyboardInterrupt("first close failure")
    second_error = SystemExit("second close failure")

    def fail_two(descriptor: int) -> None:
        if descriptor == first_target:
            raise first_error
        if descriptor == second_target:
            raise second_error
        real_close(descriptor)

    try:
        monkeypatch.setattr(atomic_module.os, "close", fail_two)
        with pytest.raises(KeyboardInterrupt) as caught:
            authority.close()
        assert caught.value is first_error
        assert not authority._closed
        notes = (
            *getattr(first_error, "__notes__", ()),
            *getattr(first_error, "_codenib_cleanup_notes", ()),
        )
        assert any("second close failure" in note for note in notes)
        os.fstat(first_target)
        os.fstat(second_target)
        for descriptor in descriptors[:-2]:
            _assert_descriptor_closed(descriptor)
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        authority.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_authority_owner_preserves_operation_error_over_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_owner = atomic_module._PublicationAuthorityOwner()
    authority = atomic_module._open_posix_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
        authority_owner=authority_owner,
    )
    resources = _posix_authority_resources(authority)
    descriptors = tuple(record.descriptor for record in resources._records)
    target = descriptors[-1]
    real_close = atomic_module.os.close
    primary_error = SystemExit("original operation interruption")
    close_error = OSError(errno.EIO, "cleanup failure")

    def fail_target(descriptor: int) -> None:
        if descriptor == target:
            raise close_error
        real_close(descriptor)

    try:
        monkeypatch.setattr(atomic_module.os, "close", fail_target)
        with pytest.raises(SystemExit) as caught:
            with authority_owner:
                raise primary_error
        assert caught.value is primary_error
        notes = (
            *getattr(primary_error, "__notes__", ()),
            *getattr(primary_error, "_codenib_cleanup_notes", ()),
        )
        assert any("cleanup failure" in note for note in notes)
        assert not authority._closed
        assert primary_error.publication_cleanup_owners == (authority_owner,)
        os.fstat(target)
        for descriptor in descriptors[:-1]:
            _assert_descriptor_closed(descriptor)
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        authority_owner.close()


def test_publication_authority_close_preserves_close_error_over_state_probe() -> None:
    close_error = KeyboardInterrupt("primary authority close interruption")
    probe_error = SystemExit("secondary close-state interruption")

    def fail_close(_resource: int) -> None:
        raise close_error

    def fail_probe() -> bool:
        raise probe_error

    authority = atomic_module._PublicationAuthority(
        display_parent=Path("/authority"),
        identity=(1, 2),
        backend_tag="test",
        resource=7,
        close_callback=fail_close,
        metadata_callback=lambda _name, _path, _label: None,
        reader_callback=lambda *_args: None,
        rename_callback=lambda _source, _destination: None,
        verify_callback=lambda: None,
        close_complete_callback=fail_probe,
    )
    authority_owner = atomic_module._PublicationAuthorityOwner()
    authority_owner.install(authority)

    with pytest.raises(KeyboardInterrupt) as caught:
        authority_owner.close()

    assert caught.value is close_error
    notes = (
        *getattr(close_error, "__notes__", ()),
        *getattr(close_error, "_codenib_cleanup_notes", ()),
    )
    assert any("secondary close-state interruption" in note for note in notes)
    assert not authority._close_state
    assert authority_owner.authority is authority
    authority._close_callback = lambda _resource: None
    authority._close_complete_callback = lambda: True
    authority_owner.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_close_reconciliation_does_not_close_observable_reused_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = atomic_module._open_posix_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
    )
    resources = _posix_authority_resources(authority)
    descriptors = tuple(record.descriptor for record in resources._records)
    target = descriptors[-1]
    foreign_path = tmp_path / "foreign-directory"
    foreign_path.mkdir()
    foreign_source = os.open(
        foreign_path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    foreign_identity = atomic_module._resource_owner_identity(os.fstat(foreign_source))
    real_close = atomic_module.os.close
    error = KeyboardInterrupt("after close and observable fd reuse")
    replacement_installed = False

    def close_then_reuse(descriptor: int) -> None:
        nonlocal replacement_installed
        real_close(descriptor)
        if descriptor == target and not replacement_installed:
            os.dup2(foreign_source, target)
            replacement_installed = True
            raise error

    try:
        monkeypatch.setattr(atomic_module.os, "close", close_then_reuse)
        with pytest.raises(KeyboardInterrupt) as caught:
            authority.close()
        assert caught.value is error
        assert replacement_installed
        assert authority._closed
        assert resources.closed
        assert atomic_module._resource_owner_identity(os.fstat(target)) == (
            foreign_identity
        )
        authority.close()
        assert atomic_module._resource_owner_identity(os.fstat(target)) == (
            foreign_identity
        )
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        real_close(target)
        real_close(foreign_source)


def _open_fake_windows_authority(
    api: _FakeWindowsApi,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)
    return atomic_module._open_windows_publication_authority(
        Path("C:/authority"),
        parent_resource=None,
        expected_parent_identity=None,
    )


def test_windows_owner_closes_same_file_after_content_change() -> None:
    api = _FakeWindowsApi()
    file_id = api.add_file(api.root_id, "mutable.txt", b"one")
    handle = api._new_handle(file_id)
    owner = atomic_module._WindowsResourceOwner(api)
    owned_handle = owner.acquire(lambda: handle)
    before = api.metadata(owned_handle)

    node = api.nodes[file_id]
    node["data"] = b"changed content"
    node["version"] = int(node["version"]) + 1
    after = api.metadata(owned_handle)
    assert atomic_module._ownership_binding_identity(before) != (
        atomic_module._ownership_binding_identity(after)
    )
    assert atomic_module._resource_owner_identity(before) == (
        atomic_module._resource_owner_identity(after)
    )

    owner.close_all()

    assert owner.closed
    assert owned_handle not in api.handles


def test_windows_authority_close_before_close_interrupt_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    authority = _open_fake_windows_authority(api, monkeypatch)
    real_close = api.close
    error = KeyboardInterrupt("before CloseHandle")
    target: int | None = None

    def interrupt_before_close(handle: int) -> None:
        nonlocal target
        if target is None:
            target = handle
            raise error
        real_close(handle)

    try:
        monkeypatch.setattr(api, "close", interrupt_before_close)
        with pytest.raises(KeyboardInterrupt) as caught:
            authority.close()
        assert caught.value is error
        assert not authority._closed
        assert target is not None
        assert set(api.handles) == {target}
    finally:
        monkeypatch.setattr(api, "close", real_close)
        authority.close()

    assert authority._closed
    assert api.handles == {}


def test_windows_authority_close_after_close_system_exit_closes_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    authority = _open_fake_windows_authority(api, monkeypatch)
    real_close = api.close
    error = SystemExit("after CloseHandle")
    injected = False

    def interrupt_after_close(handle: int) -> None:
        nonlocal injected
        real_close(handle)
        if not injected:
            injected = True
            raise error

    monkeypatch.setattr(api, "close", interrupt_after_close)
    with pytest.raises(SystemExit) as caught:
        authority.close()

    assert caught.value is error
    assert injected
    assert authority._closed
    assert api.handles == {}
    authority.close()


def test_windows_authority_persistent_eio_retains_failed_handle_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    authority = _open_fake_windows_authority(api, monkeypatch)
    real_close = api.close
    target = max(api.handles)
    error = OSError(errno.EIO, "persistent CloseHandle failure")

    def fail_target(handle: int) -> None:
        if handle == target:
            raise error
        real_close(handle)

    try:
        monkeypatch.setattr(api, "close", fail_target)
        for _attempt in range(2):
            with pytest.raises(OSError) as caught:
                authority.close()
            assert caught.value is error
            assert not authority._closed
            assert set(api.handles) == {target}
    finally:
        monkeypatch.setattr(api, "close", real_close)
        authority.close()

    assert authority._closed
    assert api.handles == {}


def test_windows_authority_close_preserves_first_error_and_attempts_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    authority = _open_fake_windows_authority(api, monkeypatch)
    first_target, second_target = sorted(api.handles, reverse=True)
    real_close = api.close
    first_error = KeyboardInterrupt("first CloseHandle failure")
    second_error = SystemExit("second CloseHandle failure")

    def fail_two(handle: int) -> None:
        if handle == first_target:
            raise first_error
        if handle == second_target:
            raise second_error
        real_close(handle)

    try:
        monkeypatch.setattr(api, "close", fail_two)
        with pytest.raises(KeyboardInterrupt) as caught:
            authority.close()
        assert caught.value is first_error
        assert not authority._closed
        notes = (
            *getattr(first_error, "__notes__", ()),
            *getattr(first_error, "_codenib_cleanup_notes", ()),
        )
        assert any("second CloseHandle failure" in note for note in notes)
        assert set(api.handles) == {first_target, second_target}
    finally:
        monkeypatch.setattr(api, "close", real_close)
        authority.close()

    assert api.handles == {}


def test_windows_close_reconciliation_does_not_close_observable_reused_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    foreign_id = api.add_directory()
    authority = _open_fake_windows_authority(api, monkeypatch)
    target = max(api.handles)
    real_close = api.close
    error = KeyboardInterrupt("after close and observable HANDLE reuse")
    replacement_installed = False

    def close_then_reuse(handle: int) -> None:
        nonlocal replacement_installed
        real_close(handle)
        if handle == target and not replacement_installed:
            api.handles[target] = foreign_id
            api.offsets[target] = 0
            replacement_installed = True
            raise error

    try:
        monkeypatch.setattr(api, "close", close_then_reuse)
        with pytest.raises(KeyboardInterrupt) as caught:
            authority.close()
        assert caught.value is error
        assert replacement_installed
        assert authority._closed
        assert api.handles == {target: foreign_id}
        authority.close()
        assert api.handles == {target: foreign_id}
    finally:
        monkeypatch.setattr(api, "close", real_close)
        real_close(target)


@pytest.mark.parametrize("use_duplicate", [False, True])
def test_windows_factory_recovers_interrupt_after_registered_handle_return(
    monkeypatch: pytest.MonkeyPatch,
    use_duplicate: bool,
) -> None:
    api = _FakeWindowsApi()
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)
    external = api.create_directory_handle(Path("C:/external")) if use_duplicate else 0
    real_acquire = atomic_module._WindowsResourceOwner.acquire
    real_own = atomic_module._WindowsLexicalAuthorityOwner.own
    error = KeyboardInterrupt("after registered HANDLE return")
    injected = False

    def acquire_then_interrupt(self: object, callback: object) -> int:
        nonlocal injected
        handle = real_acquire(self, callback)
        if not injected:
            injected = True
            raise error
        return handle

    monkeypatch.setattr(
        atomic_module._WindowsResourceOwner,
        "acquire",
        acquire_then_interrupt,
    )

    def own_then_interrupt(self: object, resource: object) -> None:
        nonlocal injected
        real_own(self, resource)
        if (
            not use_duplicate
            and isinstance(resource, windows_authority_module.WindowsDirectoryAuthority)
            and not injected
        ):
            injected = True
            raise error

    monkeypatch.setattr(
        atomic_module._WindowsLexicalAuthorityOwner,
        "own",
        own_then_interrupt,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        atomic_module._open_windows_publication_authority(
            Path("C:/authority"),
            parent_resource=external or None,
            expected_parent_identity=None,
        )

    assert caught.value is error
    assert injected
    assert set(api.handles) == ({external} if external else set())
    if external:
        api.close(external)


@pytest.mark.parametrize("attribute", ["handle", "identity"])
def test_windows_owner_recovers_interrupt_after_handle_result_store(
    attribute: str,
) -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    error = KeyboardInterrupt(f"after owned {attribute} publication")

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_after_attribute_store(
            atomic_module._WindowsResourceOwner.acquire,
            attribute,
            lambda: owner.acquire(
                lambda: api.create_directory_handle(Path("C:/authority"))
            ),
            error=error,
        )

    assert caught.value is error
    assert owner.closed
    assert api.handles == {}


def test_windows_owner_publishes_record_before_native_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    primary = KeyboardInterrupt("record construction interrupted")
    construction_calls = 0
    acquisition_calls = 0

    def fail_record_construction(_handle: int) -> object:
        nonlocal construction_calls
        construction_calls += 1
        raise primary

    def acquire_handle() -> int:
        nonlocal acquisition_calls
        acquisition_calls += 1
        return api.create_directory_handle(Path("C:/authority"))

    monkeypatch.setattr(
        atomic_module,
        "_WindowsHandleRecord",
        fail_record_construction,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        owner.acquire(acquire_handle)

    assert caught.value is primary
    assert construction_calls == 1
    assert acquisition_calls == 0
    assert owner.closed
    assert api.handles == {}


def test_windows_acquisition_cleanup_retains_primary_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    primary = OSError(errno.EIO, "HANDLE identity binding failed")
    cleanup_interruption = SystemExit("HANDLE cleanup was interrupted")
    real_identity = atomic_module._resource_owner_identity
    identity_calls = 0

    def fail_binding_then_cleanup(metadata: object) -> tuple[int, ...]:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise primary
        raise cleanup_interruption

    monkeypatch.setattr(
        atomic_module,
        "_resource_owner_identity",
        fail_binding_then_cleanup,
    )
    try:
        with pytest.raises(OSError) as caught:
            owner.acquire(lambda: api.create_directory_handle(Path("C:/authority")))

        assert caught.value is primary
        assert identity_calls > 1
        retained = BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        )
        assert len(retained) == 1
        assert not retained[0].closed
        live = [record.handle for record in owner._records if record.handle]
        assert len(live) == 1
        assert live[0] in api.handles
    finally:
        monkeypatch.setattr(
            atomic_module,
            "_resource_owner_identity",
            real_identity,
        )
        for retry_owner in BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        ):
            retry_owner.close()

    assert owner.closed
    assert api.handles == {}


def test_windows_acquisition_cleanup_contains_result_store_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    primary = OSError(errno.EIO, "HANDLE identity binding failed")
    close_error = OSError(errno.EIO, "persistent HANDLE close failure")
    interruption = SystemExit("after HANDLE close result store")
    real_identity = atomic_module._resource_owner_identity
    real_close = api.close
    identity_calls = 0

    def fail_initial_identity(metadata: object) -> tuple[int, ...]:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise primary
        return real_identity(metadata)

    def fail_owned_close(handle: int) -> None:
        if any(record.handle == handle for record in owner._records):
            raise close_error
        real_close(handle)

    monkeypatch.setattr(
        atomic_module,
        "_resource_owner_identity",
        fail_initial_identity,
    )
    monkeypatch.setattr(api, "close", fail_owned_close)
    try:
        with pytest.raises(OSError) as caught:
            _call_with_interrupt_after_call_result_store(
                atomic_module._WindowsResourceOwner.close_record,
                "close_error",
                lambda: owner.acquire(
                    lambda: api.create_directory_handle(Path("C:/authority"))
                ),
                error=interruption,
            )

        assert caught.value is primary
        retained = BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        )
        assert len(retained) == 1
        live = [record.handle for record in owner._records if record.handle]
        assert len(live) == 1
        assert live[0] in api.handles
    finally:
        monkeypatch.setattr(api, "close", real_close)
        monkeypatch.setattr(
            atomic_module,
            "_resource_owner_identity",
            real_identity,
        )
        for retry_owner in BaseException.__getattribute__(
            primary,
            "publication_cleanup_owners",
        ):
            retry_owner.close()

    assert owner.closed
    assert api.handles == {}


def test_windows_acquisition_cleanup_contains_runner_entry_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    primary = OSError(errno.EIO, "HANDLE identity binding failed")
    interruption = SystemExit("HANDLE cleanup runner entry")
    real_identity = atomic_module._resource_owner_identity
    identity_calls = 0

    def fail_initial_identity(metadata: object) -> tuple[int, ...]:
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            raise primary
        return real_identity(metadata)

    monkeypatch.setattr(
        atomic_module,
        "_resource_owner_identity",
        fail_initial_identity,
    )
    observed = _interrupt_ordered_runner_entries(
        monkeypatch,
        errors=(interruption,),
    )
    try:
        with pytest.raises(OSError) as caught:
            owner.acquire(lambda: api.create_directory_handle(Path("C:/authority")))

        assert caught.value is primary
        assert observed == [interruption]
        assert owner.closed
        assert any(
            "HANDLE cleanup runner entry" in note for note in _exception_notes(primary)
        )
    finally:
        monkeypatch.setattr(
            atomic_module,
            "_resource_owner_identity",
            real_identity,
        )
        owner.close()

    assert api.handles == {}


def test_windows_child_open_return_interrupt_remains_authority_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    api.add_directory(api.root_id, "child")
    authority = _open_fake_windows_authority(api, monkeypatch)
    baseline_handles = set(api.handles)
    real_acquire = atomic_module._WindowsResourceOwner.acquire
    error = SystemExit("after child HANDLE owner return")
    injected = False

    def acquire_then_interrupt(self: object, callback: object) -> int:
        nonlocal injected
        handle = real_acquire(self, callback)
        if not injected:
            injected = True
            raise error
        return handle

    monkeypatch.setattr(
        atomic_module._WindowsResourceOwner,
        "acquire",
        acquire_then_interrupt,
    )

    with pytest.raises(SystemExit) as caught:
        authority.child_metadata(
            "child",
            path=Path("C:/authority/child"),
            label="child",
        )

    assert caught.value is error
    assert injected
    assert set(api.handles) > baseline_handles
    authority.close()
    assert api.handles == {}


def test_resource_owner_documents_native_p2_boundary() -> None:
    posix_source = inspect.getsource(atomic_module._PosixResourceOwner)
    windows_source = inspect.getsource(atomic_module._WindowsResourceOwner)
    reconciliation_source = inspect.getsource(
        atomic_module._PosixResourceOwner._close_record
    )

    assert "raw" in posix_source and "native owning object" in posix_source
    assert "raw HANDLE" in windows_source and "native owning object" in windows_source
    assert "exact dup2 ABA" in reconciliation_source


def test_project_directory_ownership_subtree_matches_capture_without_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer = tmp_path / "outer"
    selected = outer / "selected"
    nested = selected / "nested"
    empty = selected / "empty"
    nested.mkdir(parents=True)
    empty.mkdir()
    (outer / "outside.txt").write_bytes(b"outside")
    (selected / "root.txt").write_bytes(b"root")
    (nested / "payload.bin").write_bytes(b"payload")
    outer_ownership = capture_directory_ownership(outer)
    selected_ownership = capture_directory_ownership(selected)
    nested_ownership = capture_directory_ownership(nested)

    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("subtree ownership projection performed I/O")

    for name in ("open", "read", "stat", "fstat", "scandir"):
        monkeypatch.setattr(atomic_module.os, name, unexpected_io)

    assert (
        atomic_module.project_directory_ownership_subtree(
            outer_ownership,
            "selected",
        )
        == selected_ownership
    )
    assert (
        atomic_module.project_directory_ownership_subtree(
            outer_ownership,
            PurePosixPath("selected/nested"),
        )
        == nested_ownership
    )
    assert (
        atomic_module.project_directory_ownership_subtree(
            outer_ownership,
            "selected/empty",
        ).inventory
        == ()
    )
    assert "project_directory_ownership_subtree" in atomic_module.__all__


@pytest.mark.parametrize(
    "prefix",
    ["", ".", "..", "/selected", "selected/", "selected//nested", "x/../y"],
)
def test_project_directory_ownership_subtree_rejects_noncanonical_prefix(
    tmp_path: Path,
    prefix: str,
) -> None:
    outer = tmp_path / "outer"
    (outer / "selected").mkdir(parents=True)
    ownership = capture_directory_ownership(outer)

    with pytest.raises(ValueError, match="subtree prefix"):
        atomic_module.project_directory_ownership_subtree(ownership, prefix)


def test_project_directory_ownership_subtree_rejects_forged_token(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    selected = outer / "selected"
    selected.mkdir(parents=True)
    (selected / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(outer)
    record = ownership.file_records[0]
    forged = (
        replace(ownership, digest="0" * 64),
        replace(ownership, entries=ownership.entries + 1),
        replace(ownership, byte_count=ownership.byte_count + 1),
        replace(ownership, inventory=tuple(reversed(ownership.inventory))),
        replace(
            ownership,
            file_records=(replace(record, sha256=" " * 64),),
        ),
    )

    for token in forged:
        with pytest.raises(RuntimeError, match="directory ownership token"):
            atomic_module.project_directory_ownership_subtree(token, "selected")


@pytest.mark.parametrize(
    "error_type",
    [RuntimeError, ValueError, KeyboardInterrupt, BaseException],
)
def test_directory_ownership_token_validation_stops_before_poisoned_future(
    tmp_path: Path,
    error_type: type[BaseException],
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    (root / "b").write_bytes(b"b")
    ownership = capture_directory_ownership(root)
    poisoned = False

    class PoisonedInventory(tuple[tuple[str, str], ...]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal poisoned
            yield tuple.__getitem__(self, 0)
            poisoned = True
            raise AssertionError(
                "ownership token validation consumed its poisoned future item"
            )

    forged = replace(
        ownership,
        inventory=PoisonedInventory(ownership.inventory),
    )
    stop = error_type("injected ownership token cancellation")

    def check_cancelled() -> None:
        raise stop

    with pytest.raises(BaseException) as caught:
        atomic_module._validate_directory_ownership_token(
            forged,
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poisoned


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("entries", "entry count is inconsistent"),
        ("identities", "identity count is inconsistent"),
        ("too-many-records", "file record count is inconsistent"),
    ],
)
def test_directory_ownership_token_top_count_error_precedes_first_poll(
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    (root / "b").write_bytes(b"b")
    ownership = capture_directory_ownership(root)
    if case == "entries":
        forged = replace(ownership, entries=ownership.entries + 1)
    elif case == "identities":
        forged = replace(
            ownership,
            entry_identities=ownership.entry_identities[:-1],
        )
    else:
        forged = replace(
            ownership,
            file_records=ownership.file_records + (ownership.file_records[-1],),
        )
    stop = KeyboardInterrupt("pending ownership inventory stop")
    cancellation_calls = 0

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        raise stop

    with pytest.raises(RuntimeError, match=message) as raised:
        atomic_module._validate_directory_ownership_token(
            forged,
            check_cancelled=check_cancelled,
            require_exact_types=True,
        )

    assert raised.value is not stop
    assert cancellation_calls == 0


def test_directory_ownership_token_final_accounting_mismatch_precedes_stop(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "only").write_bytes(b"payload")
    ownership = capture_directory_ownership(root)
    forged = replace(ownership, byte_count=ownership.byte_count + 1)
    stop = KeyboardInterrupt("must not precede final token accounting")
    cancellation_calls = 0

    def check_cancelled() -> None:
        nonlocal cancellation_calls
        cancellation_calls += 1
        raise stop

    with pytest.raises(RuntimeError, match="accounting is inconsistent"):
        atomic_module._validate_directory_ownership_token(
            forged,
            check_cancelled=check_cancelled,
        )

    assert cancellation_calls == 0


def test_project_directory_ownership_subtree_stops_before_projection_poison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer = tmp_path / "outer"
    selected = outer / "selected"
    selected.mkdir(parents=True)
    (selected / "a").write_bytes(b"a")
    (selected / "b").write_bytes(b"b")
    ownership = capture_directory_ownership(outer)
    selected_entry = next(
        (kind, identity)
        for path, kind, identity in ownership.entry_identities
        if path == "selected"
    )
    poisoned = False

    class PoisonedInventory(tuple[tuple[str, str], ...]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal poisoned
            yield tuple.__getitem__(self, 0)
            poisoned = True
            raise AssertionError("subtree projection consumed its poisoned future")

    poisoned_ownership = replace(
        ownership,
        inventory=PoisonedInventory(ownership.inventory),
    )
    stop = BaseException("injected subtree projection cancellation")

    def check_cancelled() -> None:
        raise stop

    def accept_outer_token(
        token: object,
        *,
        check_cancelled: object = None,
    ) -> dict[str, object]:
        assert token is poisoned_ownership
        assert check_cancelled is not None
        return {"selected": selected_entry}

    monkeypatch.setattr(
        atomic_module,
        "_validate_directory_ownership_token",
        accept_outer_token,
    )

    with pytest.raises(BaseException) as caught:
        atomic_module.project_directory_ownership_subtree(
            poisoned_ownership,
            "selected",
            check_cancelled=check_cancelled,
        )

    assert caught.value is stop
    assert not poisoned


def test_ownership_token_cancellation_parameters_are_keyword_only() -> None:
    validation = inspect.signature(atomic_module._validate_directory_ownership_token)
    projection = inspect.signature(atomic_module.project_directory_ownership_subtree)

    assert validation.parameters["check_cancelled"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert validation.parameters["check_cancelled"].default is None
    assert validation.parameters["require_exact_types"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert validation.parameters["require_exact_types"].default is False
    assert projection.parameters["check_cancelled"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert projection.parameters["check_cancelled"].default is None


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX raw path names")
def test_project_directory_ownership_subtree_preserves_raw_posix_names(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    outer.mkdir()
    outer_descriptor = os.open(outer, os.O_RDONLY | os.O_DIRECTORY)
    raw_name = b"raw\xff"
    try:
        os.mkdir(raw_name, dir_fd=outer_descriptor)
        child_descriptor = os.open(
            raw_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=outer_descriptor,
        )
        try:
            payload = os.open(
                b"payload\xfe.bin",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=child_descriptor,
            )
            try:
                os.write(payload, b"payload")
            finally:
                os.close(payload)
            expected = atomic_module._capture_posix_directory_descriptor(
                child_descriptor,
                outer / os.fsdecode(raw_name),
                required_root_file=None,
                allow_empty_root=False,
                entry_policy=None,
            )
        finally:
            os.close(child_descriptor)
    finally:
        os.close(outer_descriptor)

    outer_ownership = capture_directory_ownership(outer)
    assert (
        atomic_module.project_directory_ownership_subtree(
            outer_ownership,
            os.fsdecode(raw_name),
        )
        == expected
    )


def test_authenticated_reader_subtree_is_prefix_relative_and_lifetime_bound(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    nested = outer / "selected" / "nested"
    nested.mkdir(parents=True)
    (outer / "outside.bin").write_bytes(b"outside")
    (outer / "selected" / "root.bin").write_bytes(b"root")
    (nested / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(outer)
    saved: list[atomic_module.PublicationDirectoryReader] = []

    def consume(reader: atomic_module.PublicationDirectoryReader) -> bytes:
        selected = reader.subtree("selected")
        nested_reader = selected.subtree("nested")
        saved.extend((selected, nested_reader))
        assert selected.inventory() == (
            ("nested", "directory"),
            ("nested/payload.bin", "file"),
            ("root.bin", "file"),
        )
        assert tuple(record.path for record in selected.file_records()) == (
            "nested/payload.bin",
            "root.bin",
        )
        assert selected.capture_ownership() == (
            atomic_module.project_directory_ownership_subtree(ownership, "selected")
        )
        for escaped in ("../outside.bin", "/outside.bin", "nested/../../outside.bin"):
            with pytest.raises(ValueError, match="publication reader"):
                selected.read_bytes(escaped, max_bytes=64)
        snapshot = nested_reader.authenticated_snapshot(
            "payload.bin",
            max_bytes=64,
        )
        assert snapshot.record.path == "payload.bin"
        return snapshot.read_bytes()

    assert (
        atomic_module.reopen_authenticated_directory(
            outer,
            ownership,
            consume,
        )
        == b"payload"
    )
    for reader in saved:
        with pytest.raises(RuntimeError, match="no longer active"):
            reader.inventory()


@pytest.mark.parametrize(
    "error_type",
    (ValueError, KeyboardInterrupt, SystemExit),
)
@pytest.mark.parametrize("mutate", (False, True), ids=("clean", "mutated"))
def test_authenticated_directory_final_capture_reconciles_exact_stop(
    tmp_path: Path,
    error_type: type[BaseException],
    mutate: bool,
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    payload = directory / "payload.bin"
    payload.write_bytes(b"current")
    (directory / "second.bin").write_bytes(b"second")
    ownership = capture_directory_ownership(directory)
    stop = error_type("injected final ownership stop")
    callback_returned = False
    final_poll_seen = False

    def check_cancelled() -> None:
        nonlocal final_poll_seen
        if final_poll_seen:
            raise stop
        if any(
            frame.function == "validate_after_ownership" for frame in inspect.stack()
        ):
            final_poll_seen = True
            if mutate:
                payload.write_bytes(b"changed")
            raise stop

    def consume(_reader: atomic_module.PublicationDirectoryReader) -> object:
        nonlocal callback_returned
        callback_returned = True
        return object()

    with pytest.raises(BaseException) as caught:
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            consume,
            check_cancelled=check_cancelled,
        )

    assert callback_returned
    assert final_poll_seen
    if mutate:
        assert isinstance(caught.value, RuntimeError)
        assert caught.value is not stop
        assert caught.value.__cause__ is stop
    else:
        assert caught.value is stop
        assert "suppressed authentication failure" not in "\n".join(
            _exception_notes(stop)
        )


def test_fake_windows_reader_subtree_keeps_prefix_relative_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    owned_id = api.add_directory(api.root_id, "owned")
    selected_id = api.add_directory(owned_id, "sélected")
    nested_id = api.add_directory(selected_id, "子")
    api.add_file(owned_id, "outside.bin", b"outside")
    api.add_file(selected_id, "root.bin", b"root")
    api.add_file(nested_id, "payload.bin", b"payload")
    owned_handle = api._new_handle(owned_id)
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            owned_handle,
            Path("C:/authority/owned"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
    finally:
        api.close(owned_handle)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> bytes:
        selected = reader.subtree("sélected")
        assert tuple(record.path for record in selected.file_records()) == (
            "root.bin",
            "子/payload.bin",
        )
        with pytest.raises(ValueError, match="publication reader"):
            selected.read_bytes("../outside.bin", max_bytes=64)
        return selected.subtree("子").read_bytes("payload.bin", max_bytes=64)

    _install_fake_windows_api(monkeypatch, api)

    assert (
        atomic_module.reopen_authenticated_directory(
            Path("C:/authority/owned"),
            ownership,
            consume,
        )
        == b"payload"
    )
    assert api.handles == {}


def test_reopen_closes_manual_file_and_hidden_iterator_escapes(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload-data")
    ownership = capture_directory_ownership(directory)
    escaped_files: list[atomic_module.PublicationAuthenticatedFile] = []
    escaped_contexts: list[object] = []
    escaped_iterators: list[object] = []
    hidden_readers: list[object] = []

    def consume(
        reader: atomic_module.PublicationDirectoryReader,
    ) -> atomic_module.PublicationAuthenticatedFile:
        file_context = reader.open_authenticated_file(
            "payload.bin",
            max_bytes=64,
        )
        authenticated = file_context.__enter__()
        assert authenticated.read(2) == b"pa"
        chunk_context = reader.iter_authenticated_chunks(
            "payload.bin",
            max_bytes=64,
            chunk_size=3,
        )
        chunks = chunk_context.__enter__()
        assert next(chunks) == b"pay"
        escaped_files.extend((authenticated,))
        escaped_contexts.extend((file_context, chunk_context))
        escaped_iterators.append(chunks)
        hidden_readers.append(lambda: authenticated.read(1))
        return authenticated

    returned = atomic_module.reopen_authenticated_directory(
        directory,
        ownership,
        consume,
    )

    assert returned is escaped_files[0]
    assert returned.record.sha256 == hashlib.sha256(b"payload-data").hexdigest()
    with pytest.raises(ValueError, match="closed"):
        returned.read(1)
    with pytest.raises(ValueError, match="closed"):
        hidden_readers[0]()
    with pytest.raises(StopIteration):
        next(escaped_iterators[0])
    assert escaped_contexts[0].__exit__(None, None, None) is False
    assert escaped_contexts[1].__exit__(None, None, None) is False


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_posix_manual_stream_escape_aborts_without_drain_on_callback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    payload = b"prefix-" + (b"x" * (2 << 20))
    (directory / "payload.bin").write_bytes(payload)
    ownership = capture_directory_ownership(directory)
    real_read = atomic_module.os.read
    target = -1
    target_reads: list[bytes] = []
    escaped: list[atomic_module.PublicationAuthenticatedFile] = []
    primary = primary_type("callback primary")

    def track_read(descriptor: int, size: int) -> bytes:
        block = real_read(descriptor, size)
        if descriptor == target and escaped and not escaped[0]._closed:
            target_reads.append(block)
        return block

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        nonlocal target
        context = reader.open_authenticated_file(
            "payload.bin",
            max_bytes=len(payload),
        )
        authenticated = context.__enter__()
        escaped.append(authenticated)
        target = next(
            cell.cell_contents
            for cell in (authenticated._read_callback.__closure__ or ())
            if isinstance(cell.cell_contents, int)
        )
        assert authenticated.read(7) == b"prefix-"
        raise primary

    monkeypatch.setattr(atomic_module.os, "read", track_read)
    with pytest.raises(primary_type) as caught:
        atomic_module.reopen_authenticated_directory(directory, ownership, consume)

    assert caught.value is primary
    assert target_reads == [b"prefix-"]
    with pytest.raises(ValueError, match="closed"):
        escaped[0].read(1)
    with pytest.raises(RuntimeError, match="after context verification"):
        _ = escaped[0].record


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_manual_stream_aborts_on_callback_result_return_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    payload = b"prefix-" + (b"x" * (2 << 20))
    (directory / "payload.bin").write_bytes(payload)
    ownership = capture_directory_ownership(directory)
    real_read = atomic_module.os.read
    target = -1
    target_reads: list[bytes] = []
    escaped: list[atomic_module.PublicationAuthenticatedFile] = []
    interruption = KeyboardInterrupt("callback return cancellation")

    def track_read(descriptor: int, size: int) -> bytes:
        block = real_read(descriptor, size)
        if descriptor == target and escaped and not escaped[0]._closed:
            target_reads.append(block)
        return block

    def consume(reader: atomic_module.PublicationDirectoryReader) -> int:
        nonlocal target
        context = reader.open_authenticated_file(
            "payload.bin",
            max_bytes=len(payload),
        )
        authenticated = context.__enter__()
        escaped.append(authenticated)
        target = next(
            cell.cell_contents
            for cell in (authenticated._read_callback.__closure__ or ())
            if isinstance(cell.cell_contents, int)
        )
        assert authenticated.read(7) == b"prefix-"
        return 7

    monkeypatch.setattr(atomic_module.os, "read", track_read)

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_after_store(
            atomic_module._run_publication_reader_callback,
            "result",
            lambda: atomic_module.reopen_authenticated_directory(
                directory,
                ownership,
                consume,
            ),
            error=interruption,
        )

    assert caught.value is interruption
    assert target_reads == [b"prefix-"]
    with pytest.raises(ValueError, match="closed"):
        escaped[0].read(1)


@pytest.mark.parametrize("primary_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_fake_windows_manual_stream_aborts_without_drain_on_callback_failure(
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
) -> None:
    api = _FakeWindowsApi()
    child_id = api.add_directory(api.root_id, "owned")
    payload = b"prefix-" + (b"x" * (2 << 20))
    api.add_file(child_id, "payload.bin", payload)
    child_handle = api._new_handle(child_id)
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            child_handle,
            Path("C:/authority/owned"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
    finally:
        api.close(child_handle)
    real_read = api.read
    target = 0
    target_reads: list[bytes] = []
    escaped: list[atomic_module.PublicationAuthenticatedFile] = []
    primary = primary_type("callback primary")

    def track_read(handle: int, size: int) -> bytes:
        block = real_read(handle, size)
        if handle == target and escaped and not escaped[0]._closed:
            target_reads.append(block)
        return block

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        nonlocal target
        context = reader.open_authenticated_file(
            "payload.bin",
            max_bytes=len(payload),
        )
        authenticated = context.__enter__()
        escaped.append(authenticated)
        target = next(
            cell.cell_contents
            for cell in (authenticated._read_callback.__closure__ or ())
            if isinstance(cell.cell_contents, int)
        )
        assert authenticated.read(7) == b"prefix-"
        raise primary

    _install_fake_windows_api(monkeypatch, api)
    monkeypatch.setattr(api, "read", track_read)
    with pytest.raises(primary_type) as caught:
        atomic_module.reopen_authenticated_directory(
            Path("C:/authority/owned"),
            ownership,
            consume,
        )

    assert caught.value is primary
    assert target_reads == [b"prefix-"]
    with pytest.raises(ValueError, match="closed"):
        escaped[0].read(1)
    assert api.handles == {}


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_public_reopen_retains_posix_stream_close_for_explicit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(directory)
    real_close = atomic_module.os.close
    close_error = OSError(errno.EIO, "persistent authenticated close failure")
    target = -1

    def fail_stream_close(descriptor: int) -> None:
        nonlocal target
        if target < 0 and stat.S_ISREG(os.fstat(descriptor).st_mode):
            target = descriptor
        if descriptor == target:
            raise close_error
        real_close(descriptor)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        monkeypatch.setattr(atomic_module.os, "close", fail_stream_close)
        with reader.open_authenticated_file(
            "payload.bin",
            max_bytes=64,
        ) as authenticated:
            assert authenticated.read(2) == b"pa"

    try:
        with pytest.raises(OSError) as caught:
            atomic_module.reopen_authenticated_directory(
                directory,
                ownership,
                consume,
            )
        assert caught.value is close_error
        assert target >= 0
        os.fstat(target)
        assert len(atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS) == 1
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        atomic_module.retry_retained_publication_cleanup()

    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []
    _assert_descriptor_closed(target)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_escaped_stream_cleanup_preserves_primary_and_attempts_every_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "first.bin").write_bytes(b"first")
    (directory / "second.bin").write_bytes(b"second")
    ownership = capture_directory_ownership(directory)
    real_close = atomic_module.os.close
    primary = KeyboardInterrupt("callback primary")
    targets: list[int] = []
    close_calls: dict[int, int] = {}
    close_errors: dict[int, OSError] = {}

    def fail_stream_closes(descriptor: int) -> None:
        if descriptor in targets:
            close_calls[descriptor] = close_calls.get(descriptor, 0) + 1
            error = close_errors.setdefault(
                descriptor,
                OSError(errno.EIO, f"persistent close failure {descriptor}"),
            )
            raise error
        real_close(descriptor)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        for path in ("first.bin", "second.bin"):
            context = reader.open_authenticated_file(path, max_bytes=64)
            authenticated = context.__enter__()
            authenticated.read(1)
            descriptor = next(
                cell.cell_contents
                for cell in (authenticated._read_callback.__closure__ or ())
                if isinstance(cell.cell_contents, int)
            )
            targets.append(descriptor)
        monkeypatch.setattr(atomic_module.os, "close", fail_stream_closes)
        raise primary

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            atomic_module.reopen_authenticated_directory(
                directory,
                ownership,
                consume,
            )
        assert caught.value is primary
        assert len(set(targets)) == 2
        assert all(close_calls[target] >= 2 for target in targets)
        notes = _exception_notes(primary)
        for error in close_errors.values():
            assert any(repr(error) in note for note in notes)
        assert len(atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS) == 1
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        atomic_module.retry_retained_publication_cleanup()

    for target in targets:
        _assert_descriptor_closed(target)


def test_public_reopen_retains_fake_windows_stream_close_for_explicit_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    api = _FakeWindowsApi()
    child_id = api.add_directory(api.root_id, "owned")
    api.add_file(child_id, "payload.bin", b"payload")
    child_handle = api._new_handle(child_id)
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            child_handle,
            Path("C:/authority/owned"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
    finally:
        api.close(child_handle)
    real_close = api.close
    close_error = OSError(errno.EIO, "persistent authenticated HANDLE failure")
    target = 0

    def fail_stream_close(handle: int) -> None:
        nonlocal target
        if not target and stat.S_ISREG(api.metadata(handle).st_mode):
            target = handle
        if handle == target:
            raise close_error
        real_close(handle)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        monkeypatch.setattr(api, "close", fail_stream_close)
        with reader.open_authenticated_file(
            "payload.bin",
            max_bytes=64,
        ) as authenticated:
            assert authenticated.read(2) == b"pa"

    _install_fake_windows_api(monkeypatch, api)
    try:
        with pytest.raises(OSError) as caught:
            atomic_module.reopen_authenticated_directory(
                Path("C:/authority/owned"),
                ownership,
                consume,
            )
        assert caught.value is close_error
        assert target in api.handles
        assert len(atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS) == 1
    finally:
        monkeypatch.setattr(api, "close", real_close)
        atomic_module.retry_retained_publication_cleanup()

    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []
    assert api.handles == {}


def test_retry_retained_cleanup_preserves_first_and_attempts_every_owner() -> None:
    atomic_module.retry_retained_publication_cleanup()
    first = KeyboardInterrupt("first retained cleanup failure")
    second = SystemExit("second retained cleanup failure")
    failures: list[BaseException | None] = [first, second]
    calls = [0, 0]
    closed = [False, False]
    owners: list[atomic_module._PublicationAuthorityOwner] = []

    for index in range(2):

        def close_resource(_resource: int, *, selected: int = index) -> None:
            calls[selected] += 1
            failure = failures[selected]
            if failure is not None:
                raise failure
            closed[selected] = True

        authority = atomic_module._PublicationAuthority(
            display_parent=Path("/authority"),
            identity=(1, index + 1),
            backend_tag="test",
            resource=index + 1,
            close_callback=close_resource,
            metadata_callback=lambda _name, _path, _label: None,
            reader_callback=lambda *_args: None,
            rename_callback=lambda _source, _destination: None,
            verify_callback=lambda: None,
            close_complete_callback=lambda selected=index: closed[selected],
        )
        owner = atomic_module._PublicationAuthorityOwner()
        owner.install(authority)
        owners.append(owner)

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            atomic_module.retry_retained_publication_cleanup()
        assert caught.value is first
        assert calls == [1, 1]
        assert any(repr(second) in note for note in _exception_notes(first))
        assert all(owner.authority is not None for owner in owners)
    finally:
        failures[:] = [None, None]
        atomic_module.retry_retained_publication_cleanup()

    assert closed == [True, True]
    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "fork"),
    reason="requires real POSIX fork semantics",
)
def test_retained_authority_registry_rehomes_and_closes_in_real_child(
    tmp_path: Path,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    owner = atomic_module._PublicationAuthorityOwner()
    authority = atomic_module._open_posix_publication_authority(
        tmp_path,
        parent_resource=None,
        expected_parent_identity=None,
        authority_owner=owner,
    )
    resources = _posix_authority_resources(authority)
    inherited = tuple(
        record.descriptor for record in resources._records if record.descriptor >= 0
    )
    child = os.fork()
    if child == 0:
        try:
            try:
                authority.verify_path_binding()
            except RuntimeError as error:
                if "process boundary" not in str(error):
                    os._exit(11)
            else:
                os._exit(12)
            atomic_module.retry_retained_publication_cleanup()
            if atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS:
                os._exit(13)
            for descriptor in inherited:
                try:
                    os.fstat(descriptor)
                except OSError as error:
                    if error.errno != errno.EBADF:
                        os._exit(14)
                else:
                    os._exit(15)
            os._exit(0)
        except BaseException:  # noqa: B036 - child reports failure by exit status
            os._exit(16)

    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    authority.verify_path_binding()
    for descriptor in inherited:
        os.fstat(descriptor)
    owner.close()
    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "fork"),
    reason="requires real POSIX fork semantics",
)
def test_reader_and_authenticated_file_reject_real_fork_escape(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(directory)

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        with reader.open_authenticated_file(
            "payload.bin",
            max_bytes=64,
        ) as authenticated:
            child = os.fork()
            if child == 0:
                try:
                    for callback in (
                        reader.inventory,
                        lambda: authenticated.read(1),
                    ):
                        try:
                            callback()
                        except RuntimeError as error:
                            if "process boundary" not in str(error):
                                os._exit(21)
                        else:
                            os._exit(22)
                    os._exit(0)
                except BaseException:  # noqa: B036 - child reports by exit status
                    os._exit(23)
            waited, status = os.waitpid(child, 0)
            assert waited == child
            assert os.waitstatus_to_exitcode(status) == 0
            assert authenticated.read() == b"payload"

    atomic_module.reopen_authenticated_directory(directory, ownership, consume)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_capture_retains_posix_regular_file_close_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    real_close = atomic_module.os.close
    close_error = OSError(errno.EIO, "persistent ownership file close failure")
    target = -1

    def fail_regular_file_close(descriptor: int) -> None:
        nonlocal target
        if target < 0 and stat.S_ISREG(os.fstat(descriptor).st_mode):
            target = descriptor
        if descriptor == target:
            raise close_error
        real_close(descriptor)

    try:
        monkeypatch.setattr(atomic_module.os, "close", fail_regular_file_close)
        with pytest.raises(OSError) as caught:
            atomic_module.capture_directory_ownership(directory)
        assert caught.value is close_error
        assert target >= 0
        os.fstat(target)
        assert len(atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS) == 1
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        atomic_module.retry_retained_publication_cleanup()

    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []
    _assert_descriptor_closed(target)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "fork"),
    reason="requires real POSIX fork semantics",
)
def test_capture_retained_regular_file_cleanup_rehomes_across_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    real_close = atomic_module.os.close
    close_error = OSError(errno.EIO, "persistent ownership file close failure")
    target = -1

    def fail_regular_file_close(descriptor: int) -> None:
        nonlocal target
        if target < 0 and stat.S_ISREG(os.fstat(descriptor).st_mode):
            target = descriptor
        if descriptor == target:
            raise close_error
        real_close(descriptor)

    monkeypatch.setattr(atomic_module.os, "close", fail_regular_file_close)
    with pytest.raises(OSError) as caught:
        atomic_module.capture_directory_ownership(directory)
    assert caught.value is close_error
    assert target >= 0
    assert len(atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS) == 1
    monkeypatch.setattr(atomic_module.os, "close", real_close)

    child = os.fork()
    if child == 0:
        try:
            atomic_module.retry_retained_publication_cleanup()
            if atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS:
                os._exit(31)
            try:
                os.fstat(target)
            except OSError as error:
                if error.errno != errno.EBADF:
                    os._exit(32)
            else:
                os._exit(33)
            os._exit(0)
        except BaseException:  # noqa: B036 - child reports by exit status
            os._exit(34)

    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    os.fstat(target)
    atomic_module.retry_retained_publication_cleanup()
    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []
    _assert_descriptor_closed(target)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_post_callback_capture_close_failure_keeps_callback_primary_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(directory)
    real_close = atomic_module.os.close
    callback_error = ValueError("callback primary")
    close_error = OSError(errno.EIO, "post-callback ownership close failure")
    target = -1

    def fail_regular_file_close(descriptor: int) -> None:
        nonlocal target
        if target < 0 and stat.S_ISREG(os.fstat(descriptor).st_mode):
            target = descriptor
        if descriptor == target:
            raise close_error
        real_close(descriptor)

    def consume(_reader: atomic_module.PublicationDirectoryReader) -> None:
        monkeypatch.setattr(atomic_module.os, "close", fail_regular_file_close)
        raise callback_error

    try:
        with pytest.raises(ValueError) as caught:
            atomic_module.reopen_authenticated_directory(
                directory,
                ownership,
                consume,
            )
        assert caught.value is callback_error
        assert target >= 0
        os.fstat(target)
        assert any(
            repr(close_error) in note for note in _exception_notes(callback_error)
        )
        assert len(atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS) == 1
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        atomic_module.retry_retained_publication_cleanup()

    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []
    _assert_descriptor_closed(target)


def test_capture_retains_fake_windows_regular_file_close_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    api = _FakeWindowsApi()
    owned_id = api.add_directory(api.root_id, "owned")
    api.add_file(owned_id, "payload.bin", b"payload")
    owned_handle = api._new_handle(owned_id)
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            owned_handle,
            Path("C:/authority/owned"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
    finally:
        api.close(owned_handle)
    real_close = api.close
    close_error = OSError(errno.EIO, "persistent ownership HANDLE close failure")
    target = 0

    def fail_regular_file_close(handle: int) -> None:
        nonlocal target
        if not target and stat.S_ISREG(api.metadata(handle).st_mode):
            target = handle
        if handle == target:
            raise close_error
        real_close(handle)

    _install_fake_windows_api(monkeypatch, api)
    try:
        monkeypatch.setattr(api, "close", fail_regular_file_close)
        with pytest.raises(OSError) as caught:
            atomic_module.reopen_authenticated_directory(
                Path("C:/authority/owned"),
                ownership,
                lambda _reader: None,
            )
        assert caught.value is close_error
        assert target in api.handles
        assert len(atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS) == 1
    finally:
        monkeypatch.setattr(api, "close", real_close)
        atomic_module.retry_retained_publication_cleanup()

    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []
    assert api.handles == {}


def _call_with_interrupt_before_reader_inactive_transition(
    callback: object,
    *,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    assert callable(callback)
    real_transition = atomic_module._set_publication_reader_inactive
    injected = False

    def interrupt_once(
        lifetime: atomic_module._PublicationReaderLifetime,
    ) -> None:
        nonlocal injected
        if not injected and lifetime.active:
            injected = True
            raise error
        real_transition(lifetime)

    with monkeypatch.context() as fault:
        fault.setattr(
            atomic_module,
            "_set_publication_reader_inactive",
            interrupt_once,
        )
        callback()
    assert injected, "failed to inject before reader inactive transition"


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor reuse semantics",
)
def test_posix_reader_deactivation_interrupt_closes_child_and_rejects_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(directory)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    foreign_source = os.open(
        foreign,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    foreign_identity = atomic_module._resource_owner_identity(os.fstat(foreign_source))
    real_close = atomic_module.os.close
    deactivation_error = KeyboardInterrupt("reader deactivation interruption")
    close_error = SystemExit("child close ABA interruption")
    target = -1
    reused = False
    saved: list[atomic_module.PublicationDirectoryReader] = []

    def close_then_reuse(descriptor: int) -> None:
        nonlocal reused
        real_close(descriptor)
        if descriptor == target and not reused:
            os.dup2(foreign_source, descriptor)
            reused = True
            raise close_error

    def consume(reader: atomic_module.PublicationDirectoryReader) -> int:
        nonlocal target
        saved.append(reader)
        target = next(
            cell.cell_contents
            for cell in (reader._capture.__closure__ or ())
            if isinstance(cell.cell_contents, int)
        )
        monkeypatch.setattr(atomic_module.os, "close", close_then_reuse)
        return 7

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            _call_with_interrupt_before_reader_inactive_transition(
                lambda: atomic_module.reopen_authenticated_directory(
                    directory,
                    ownership,
                    consume,
                ),
                monkeypatch=monkeypatch,
                error=deactivation_error,
            )
        assert caught.value is deactivation_error
        assert reused
        assert any(repr(close_error) in note for note in _exception_notes(caught.value))
        assert atomic_module._resource_owner_identity(os.fstat(target)) == (
            foreign_identity
        )
        with pytest.raises(RuntimeError, match="no longer active"):
            saved[0].read_bytes("payload.bin", max_bytes=64)
        assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        if target >= 0:
            try:
                real_close(target)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
        real_close(foreign_source)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor reuse semantics",
)
def test_reader_deactivation_and_child_cleanup_keep_callback_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(directory)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    foreign_source = os.open(
        foreign,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    real_close = atomic_module.os.close
    callback_error = ValueError("callback primary")
    deactivation_error = KeyboardInterrupt("reader deactivation interruption")
    close_error = SystemExit("child close ABA interruption")
    target = -1
    reused = False
    saved: list[atomic_module.PublicationDirectoryReader] = []

    def close_then_reuse(descriptor: int) -> None:
        nonlocal reused
        real_close(descriptor)
        if descriptor == target and not reused:
            os.dup2(foreign_source, descriptor)
            reused = True
            raise close_error

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        nonlocal target
        saved.append(reader)
        target = next(
            cell.cell_contents
            for cell in (reader._capture.__closure__ or ())
            if isinstance(cell.cell_contents, int)
        )
        monkeypatch.setattr(atomic_module.os, "close", close_then_reuse)
        raise callback_error

    try:
        with pytest.raises(ValueError) as caught:
            _call_with_interrupt_before_reader_inactive_transition(
                lambda: atomic_module.reopen_authenticated_directory(
                    directory,
                    ownership,
                    consume,
                ),
                monkeypatch=monkeypatch,
                error=deactivation_error,
            )
        assert caught.value is callback_error
        assert reused
        notes = _exception_notes(callback_error)
        deactivation_note = next(
            index
            for index, note in enumerate(notes)
            if repr(deactivation_error) in note
        )
        child_note = next(
            index for index, note in enumerate(notes) if repr(close_error) in note
        )
        assert deactivation_note < child_note
        with pytest.raises(RuntimeError, match="no longer active"):
            saved[0].inventory()
    finally:
        monkeypatch.setattr(atomic_module.os, "close", real_close)
        if target >= 0:
            try:
                real_close(target)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
        real_close(foreign_source)


def test_fake_windows_reader_deactivation_interrupt_rejects_handle_aba(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    api = _FakeWindowsApi()
    owned_id = api.add_directory(api.root_id, "owned")
    api.add_file(owned_id, "payload.bin", b"payload")
    foreign_id = api.add_directory()
    owned_handle = api._new_handle(owned_id)
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            owned_handle,
            Path("C:/authority/owned"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
    finally:
        api.close(owned_handle)
    real_close = api.close
    deactivation_error = KeyboardInterrupt("reader deactivation interruption")
    close_error = SystemExit("child HANDLE ABA interruption")
    target = 0
    reused = False
    saved: list[atomic_module.PublicationDirectoryReader] = []

    def close_then_reuse(handle: int) -> None:
        nonlocal reused
        real_close(handle)
        if handle == target and not reused:
            api.handles[handle] = foreign_id
            api.offsets[handle] = 0
            reused = True
            raise close_error

    def consume(reader: atomic_module.PublicationDirectoryReader) -> int:
        nonlocal target
        saved.append(reader)
        target = next(
            cell.cell_contents
            for cell in (reader._capture.__closure__ or ())
            if isinstance(cell.cell_contents, int)
        )
        monkeypatch.setattr(api, "close", close_then_reuse)
        return 7

    _install_fake_windows_api(monkeypatch, api)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            _call_with_interrupt_before_reader_inactive_transition(
                lambda: atomic_module.reopen_authenticated_directory(
                    Path("C:/authority/owned"),
                    ownership,
                    consume,
                ),
                monkeypatch=monkeypatch,
                error=deactivation_error,
            )
        assert caught.value is deactivation_error
        assert reused
        assert any(repr(close_error) in note for note in _exception_notes(caught.value))
        assert api.handles[target] == foreign_id
        with pytest.raises(RuntimeError, match="no longer active"):
            saved[0].read_bytes("payload.bin", max_bytes=64)
        assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []
    finally:
        monkeypatch.setattr(api, "close", real_close)
        if target in api.handles:
            real_close(target)

    assert api.handles == {}


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux publication authority",
)
def test_posix_reader_deactivation_retries_repeated_pre_call_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(directory)
    saved: list[atomic_module.PublicationDirectoryReader] = []
    first = KeyboardInterrupt("first reader cancellation")
    events = _interrupt_ordered_action_before_call(
        monkeypatch,
        label="publication reader deactivation also failed",
        errors=(first, SystemExit("second reader cancellation")),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        atomic_module.reopen_authenticated_directory(
            directory,
            ownership,
            lambda reader: saved.append(reader),
        )

    assert caught.value is first
    assert events == ["KeyboardInterrupt", "SystemExit"]
    assert saved and not saved[0]._lifetime.active
    with pytest.raises(RuntimeError, match="no longer active"):
        saved[0].inventory()


def test_fake_windows_reader_deactivation_retries_repeated_pre_call_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWindowsApi()
    owned_id = api.add_directory(api.root_id, "owned")
    api.add_file(owned_id, "payload.bin", b"payload")
    handle = api._new_handle(owned_id)
    try:
        ownership = atomic_module._capture_windows_directory_handle(
            api,
            handle,
            Path("C:/authority/owned"),
            required_root_file=None,
            allow_empty_root=False,
            entry_policy=None,
        )
    finally:
        api.close(handle)
    saved: list[atomic_module.PublicationDirectoryReader] = []
    first = KeyboardInterrupt("first Windows reader cancellation")
    events = _interrupt_ordered_action_before_call(
        monkeypatch,
        label="Windows publication reader deactivation also failed",
        errors=(first, SystemExit("second Windows reader cancellation")),
    )
    _install_fake_windows_api(monkeypatch, api)

    with pytest.raises(KeyboardInterrupt) as caught:
        atomic_module.reopen_authenticated_directory(
            Path("C:/authority/owned"),
            ownership,
            lambda reader: saved.append(reader),
        )

    assert caught.value is first
    assert events == ["KeyboardInterrupt", "SystemExit"]
    assert saved and not saved[0]._lifetime.active
    assert api.handles == {}


def test_retained_cleanup_retries_registry_release_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    closed = False

    def close_resource(_resource: int) -> None:
        nonlocal closed
        closed = True

    authority = atomic_module._PublicationAuthority(
        display_parent=Path("/authority"),
        identity=(1, 2),
        backend_tag="test",
        resource=7,
        close_callback=close_resource,
        metadata_callback=lambda *_args: None,
        reader_callback=lambda *_args: None,
        rename_callback=lambda *_args: None,
        verify_callback=lambda: None,
        close_complete_callback=lambda: closed,
    )
    owner = atomic_module._PublicationAuthorityOwner()
    owner.install(authority)
    real_forget = atomic_module._forget_publication_authority_owner
    errors = [
        KeyboardInterrupt("first registry release"),
        SystemExit("second registry release"),
    ]

    def interrupt_forget(candidate: object) -> None:
        if candidate is owner and errors:
            raise errors.pop(0)
        real_forget(candidate)

    monkeypatch.setattr(
        atomic_module,
        "_forget_publication_authority_owner",
        interrupt_forget,
    )
    with pytest.raises(KeyboardInterrupt):
        atomic_module.retry_retained_publication_cleanup()

    assert closed
    assert errors == []
    assert owner.authority is None
    assert all(
        retained is not owner
        for retained in atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS
    )


def _interrupt_retained_owner_close_call(
    callback: object,
    *,
    error: BaseException,
) -> None:
    assert callable(callback)
    function = atomic_module._RetainedAuthorityCloseAttempt.__call__
    code = function.__code__
    source, first_line = inspect.getsourcelines(function)
    call_lines = {
        first_line + offset
        for offset, line in enumerate(source)
        if "self.owner.close(" in line
    }
    assert len(call_lines) == 1
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is code
            and event == "line"
            and frame.f_lineno in call_lines
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
        assert injected, "failed to inject before retained owner close call"


def test_retained_cleanup_retries_owner_close_call_cancellation() -> None:
    atomic_module.retry_retained_publication_cleanup()
    closed = False
    cancellation = KeyboardInterrupt("owner close cancellation")

    def close_resource(_resource: int) -> None:
        nonlocal closed
        closed = True

    authority = atomic_module._PublicationAuthority(
        display_parent=Path("/authority"),
        identity=(1, 2),
        backend_tag="test",
        resource=7,
        close_callback=close_resource,
        metadata_callback=lambda *_args: None,
        reader_callback=lambda *_args: None,
        rename_callback=lambda *_args: None,
        verify_callback=lambda: None,
        close_complete_callback=lambda: closed,
    )
    owner = atomic_module._PublicationAuthorityOwner()
    owner.install(authority)

    with pytest.raises(KeyboardInterrupt) as caught:
        _interrupt_retained_owner_close_call(
            atomic_module.retry_retained_publication_cleanup,
            error=cancellation,
        )

    assert caught.value is cancellation
    assert closed
    assert owner.authority is None
    assert all(
        retained is not owner
        for retained in atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS
    )


def test_retained_cleanup_does_not_spin_on_persistent_close_cancellation() -> None:
    atomic_module.retry_retained_publication_cleanup()
    cancellation = KeyboardInterrupt("persistent retained close cancellation")
    calls = 0

    def fail_close(_resource: int) -> None:
        nonlocal calls
        calls += 1
        raise cancellation

    authority = atomic_module._PublicationAuthority(
        display_parent=Path("/authority"),
        identity=(1, 2),
        backend_tag="test",
        resource=7,
        close_callback=fail_close,
        metadata_callback=lambda *_args: None,
        reader_callback=lambda *_args: None,
        rename_callback=lambda *_args: None,
        verify_callback=lambda: None,
        close_complete_callback=lambda: False,
    )
    owner = atomic_module._PublicationAuthorityOwner()
    owner.install(authority)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            atomic_module.retry_retained_publication_cleanup()

        assert caught.value is cancellation
        assert calls == 1
        assert owner.authority is authority
        assert any(
            retained is owner
            for retained in atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS
        )
    finally:
        authority._close_callback = lambda _resource: None
        authority._close_complete_callback = lambda: True
        atomic_module.retry_retained_publication_cleanup()


def _call_with_interrupt_at_context_boundary(
    function: object,
    callback: object,
    *,
    source_fragment: str,
    select_last: bool = False,
    error: BaseException,
) -> None:
    """Inject at an exact source-line boundary in a context cleanup method."""

    assert callable(function)
    assert callable(callback)
    code = function.__code__
    source, first_line = inspect.getsourcelines(function)
    matching_lines = sorted(
        first_line + offset
        for offset, line in enumerate(source)
        if source_fragment in line
    )
    if select_last:
        assert matching_lines
        target_line = matching_lines[-1]
    else:
        assert len(matching_lines) == 1
        target_line = matching_lines[0]
    previous_trace = sys.gettrace()
    injected = False

    def trace(frame: object, event: str, _arg: object) -> object:
        nonlocal injected
        if event == "call" and frame.f_code is code:
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is code
            and event == "line"
            and frame.f_lineno == target_line
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
        assert injected, "failed to inject at the context cleanup boundary"


@pytest.mark.parametrize("boundary", ["wrapper-exit-call", "forget"])
@pytest.mark.parametrize("operation", ["finish", "abort"])
def test_authenticated_file_outer_exit_retries_real_boundary_cancellation(
    operation: str,
    boundary: str,
) -> None:
    record = atomic_module.TreeFileRecord(
        path="payload.bin",
        mode=0o644,
        size=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    ownership = atomic_module._TreeOwnership(
        root_identity=(1, 2),
        root_version_identity=(1, 2, 3),
        digest="test",
        entries=1,
        byte_count=0,
        metadata_bytes=0,
        inventory=(("payload.bin", "file"),),
        file_records=(record,),
        entry_identities=(("payload.bin", "file", (3, 4)),),
    )
    authenticated = atomic_module.PublicationAuthenticatedFile(
        path=record.path,
        mode=record.mode,
        size=record.size,
        read_callback=lambda _size: b"",
        verify_callback=lambda: None,
    )
    backend_error = (
        SystemExit(f"persistent {operation} backend exit")
        if boundary == "wrapper-exit-call"
        else None
    )

    class BackendContext:
        def __init__(self) -> None:
            self.exit_calls = 0

        def __enter__(self) -> atomic_module.PublicationAuthenticatedFile:
            return authenticated

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: object,
        ) -> bool:
            self.exit_calls += 1
            authenticated._record = record
            authenticated._closed = True
            authenticated._finalized = True
            if backend_error is not None:
                raise backend_error
            return False

    backend = BackendContext()
    reader = atomic_module.PublicationDirectoryReader(
        Path("/authority/owned"),
        ownership.root_identity,
        lambda *_args: ownership,
        lambda *_args: backend,
        ownership,
    )
    context = reader.open_authenticated_file("payload.bin", max_bytes=0)
    assert context.__enter__() is authenticated
    cancellation = KeyboardInterrupt(f"pre-{operation} backend exit")

    def finish() -> None:
        reader._close_open_files(None)

    if operation == "finish":
        function = atomic_module._PublicationAuthenticatedFileContext._finish
        callback = finish
    else:
        function = atomic_module._PublicationAuthenticatedFileContext._abort_and_close
        callback = reader._abort_open_files

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_at_context_boundary(
            function,
            callback,
            source_fragment=(
                "context.__exit__("
                if boundary == "wrapper-exit-call"
                else "self._reader._forget_open_file(self)"
            ),
            select_last=boundary == "forget",
            error=cancellation,
        )

    assert caught.value is cancellation
    assert backend.exit_calls == 1
    assert context._finished
    assert reader._lifetime.open_files == []
    notes = _exception_notes(cancellation)
    if backend_error is not None:
        # A cancellation at the nested Python call boundary can replace an
        # exception raised by the delegated context before its caller can
        # observe that value. The handoff remains at-most-once and the retained
        # authority still owns the physical resource.
        assert not any(repr(backend_error) in note for note in notes)


def test_authenticated_file_nested_exit_handoff_keeps_body_primary() -> None:
    record = atomic_module.TreeFileRecord(
        path="payload.bin",
        mode=0o644,
        size=0,
        sha256=hashlib.sha256(b"").hexdigest(),
    )
    ownership = atomic_module._TreeOwnership(
        root_identity=(1, 2),
        root_version_identity=(1, 2, 3),
        digest="test",
        entries=1,
        byte_count=0,
        metadata_bytes=0,
        inventory=(("payload.bin", "file"),),
        file_records=(record,),
        entry_identities=(("payload.bin", "file", (3, 4)),),
    )
    authenticated = atomic_module.PublicationAuthenticatedFile(
        path=record.path,
        mode=record.mode,
        size=record.size,
        read_callback=lambda _size: b"",
        verify_callback=lambda: None,
    )

    class BackendContext:
        def __init__(self) -> None:
            self.exit_calls = 0

        def __enter__(self) -> atomic_module.PublicationAuthenticatedFile:
            return authenticated

        def __exit__(self, *_exc: object) -> bool:
            self.exit_calls += 1
            raise AssertionError("nested backend exit must remain at-most-once")

    backend = BackendContext()
    reader = atomic_module.PublicationDirectoryReader(
        Path("/authority/owned"),
        ownership.root_identity,
        lambda *_args: ownership,
        lambda *_args: backend,
        ownership,
    )
    context = reader.open_authenticated_file("payload.bin", max_bytes=0)
    assert context.__enter__() is authenticated
    body_error = ValueError("body primary")
    cancellation = KeyboardInterrupt("nested backend exit handoff")
    results: list[bool] = []

    _call_with_interrupt_at_context_boundary(
        atomic_module._PublicationAuthenticatedFileBackendContext.__exit__,
        lambda: results.append(
            context.__exit__(type(body_error), body_error, body_error.__traceback__)
        ),
        source_fragment="return self._context.__exit__",
        error=cancellation,
    )

    assert results == [False]
    assert backend.exit_calls == 0
    assert context._backend_context is not None
    assert context._backend_context.exit_handed_off
    assert context._finished
    assert reader._authentication_failed
    assert reader._lifetime.open_files == []
    assert any(repr(cancellation) in note for note in _exception_notes(body_error))

    # The handoff is deliberately at-most-once.  A later public exit cannot
    # guess that the arbitrary custom backend never entered its delegate.
    assert context.__exit__(type(body_error), body_error, None) is False
    assert backend.exit_calls == 0


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="requires POSIX directory descriptors",
)
def test_posix_nested_exit_handoff_defers_cleanup_to_authority(
    tmp_path: Path,
) -> None:
    atomic_module.retry_retained_publication_cleanup()
    directory = tmp_path / "owned"
    directory.mkdir()
    (directory / "payload.bin").write_bytes(b"payload")
    ownership = capture_directory_ownership(directory)
    body_error = ValueError("body primary")
    cancellation = KeyboardInterrupt("nested POSIX backend exit handoff")
    owners: list[atomic_module._PosixResourceOwner] = []
    authority_owners: list[atomic_module._PublicationAuthorityOwner] = []
    file_records: list[atomic_module._PosixDescriptorRecord] = []

    def consume(reader: atomic_module.PublicationDirectoryReader) -> None:
        matches = [
            cell.cell_contents
            for cell in (reader._open_file.__closure__ or ())
            if isinstance(cell.cell_contents, atomic_module._PosixResourceOwner)
        ]
        assert len(matches) == 1
        owner = matches[0]
        owners.append(owner)
        retained = tuple(atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS)
        assert len(retained) == 1
        assert retained[0].authority is not None
        assert _posix_authority_resources(retained[0].authority) is owner
        authority_owners.append(retained[0])
        with reader.open_authenticated_file(
            "payload.bin",
            max_bytes=64,
        ) as authenticated:
            assert authenticated.read(1) == b"p"
            file_record = owner._records[-1]
            assert file_record.descriptor >= 0
            file_records.append(file_record)
            raise body_error

    with pytest.raises(ValueError) as caught:
        _call_with_interrupt_at_context_boundary(
            atomic_module._PublicationAuthenticatedFileBackendContext.__exit__,
            lambda: atomic_module.reopen_authenticated_directory(
                directory,
                ownership,
                consume,
            ),
            source_fragment="return self._context.__exit__",
            error=cancellation,
        )

    assert caught.value is body_error
    assert any(repr(cancellation) in note for note in _exception_notes(body_error))
    assert len(owners) == len(authority_owners) == len(file_records) == 1
    assert file_records[0].descriptor < 0
    assert owners[0].closed
    assert authority_owners[0].authority is None
    assert atomic_module._RETAINED_PUBLICATION_AUTHORITY_OWNERS == []


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="native workspace replacement is Linux-only",
)
def test_native_replacement_dual_readers_never_cross_route_and_receipt_is_candidate_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _adopt_fake_native_replacement(tmp_path, monkeypatch)
    replacement = prepared.replacement
    candidate_before = capture_directory_ownership(prepared.stage)
    incumbent_before = capture_directory_ownership(prepared.destination)

    def payload(reader: atomic_module.PublicationDirectoryReader) -> bytes:
        return reader.read_bytes("payload.bin", max_bytes=16)

    try:
        assert (
            prepared.authority.read_child(
                prepared.stage.name,
                path=prepared.stage,
                label="candidate",
                expected_ownership=candidate_before,
                callback=payload,
            )
            == b"new"
        )
        assert (
            replacement.read_incumbent(
                prepared.destination.name,
                prepared.destination,
                "incumbent",
                incumbent_before,
                payload,
            )
            == b"old"
        )
        with pytest.raises(RuntimeError, match="not the replacement candidate"):
            replacement.read_candidate(
                prepared.destination.name,
                prepared.destination,
                "candidate",
                None,
                payload,
            )
        with pytest.raises(RuntimeError, match="not the replacement incumbent"):
            replacement.read_incumbent(
                prepared.stage.name,
                prepared.stage,
                "incumbent",
                None,
                payload,
            )

        assert replacement.exchange(1) is prepared.receipt_token
        candidate_after = replacement.read_candidate(
            prepared.destination.name,
            prepared.destination,
            "candidate",
            None,
            lambda reader: reader.capture_ownership(allow_empty_root=True),
        )
        incumbent_after = replacement.read_incumbent(
            prepared.stage.name,
            prepared.stage,
            "incumbent",
            None,
            lambda reader: reader.capture_ownership(allow_empty_root=True),
        )
        assert (
            prepared.authority.read_child(
                prepared.destination.name,
                path=prepared.destination,
                label="candidate",
                expected_ownership=candidate_after,
                callback=payload,
            )
            == b"new"
        )
        assert (
            replacement.read_incumbent(
                prepared.stage.name,
                prepared.stage,
                "incumbent",
                incumbent_after,
                payload,
            )
            == b"old"
        )

        prepared.native_state["value"] = "replacement-receipted"
        replacement.mark_receipted()

        def forbidden_native_verifier(_owner: object) -> None:
            raise AssertionError("postreceipt read reached the native verifier")

        monkeypatch.setattr(
            atomic_module._native_workspace_owner,
            "verify_owner_authority",
            forbidden_native_verifier,
        )
        displaced = prepared.parent / ".displaced-incumbent"
        prepared.stage.rename(displaced)
        assert (
            prepared.authority.read_child(
                prepared.destination.name,
                path=prepared.destination,
                label="candidate",
                expected_ownership=candidate_after,
                callback=payload,
            )
            == b"new"
        )
        with pytest.raises(RuntimeError, match="no incumbent reader"):
            _ = replacement.incumbent_name

        prepared.authority_owner.close()
        os.fstat(prepared.parent_descriptor)
        os.fstat(prepared.candidate_descriptor)
        os.fstat(prepared.incumbent_descriptor)
    finally:
        _close_fake_native_replacement(prepared)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="native workspace replacement is Linux-only",
)
def test_native_replacement_helper_orders_exchange_validation_commit_and_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    prepared = _adopt_fake_native_replacement(
        tmp_path,
        monkeypatch,
        events=events,
    )
    expected_candidate = capture_directory_ownership(prepared.stage)
    expected_incumbent = capture_directory_ownership(prepared.destination)
    observed: list[tuple[object, object, DirectoryOrphan, object]] = []
    orphan: DirectoryOrphan | None = None

    def validate_staged(reader: atomic_module.PublicationDirectoryReader) -> None:
        assert reader.read_bytes("payload.bin", max_bytes=16) == b"new"
        events.append("validate-staged")

    def validate_published(reader: atomic_module.PublicationDirectoryReader) -> None:
        assert reader.read_bytes("payload.bin", max_bytes=16) == b"new"
        assert (prepared.stage / "payload.bin").read_bytes() == b"old"
        events.append("validate-published")

    def commit(
        staged: object,
        published: object,
        displaced: DirectoryOrphan,
        receipt_token: object,
    ) -> None:
        assert events == ["validate-staged", "exchange", "validate-published"]
        assert receipt_token is prepared.receipt_token
        observed.append((staged, published, displaced, receipt_token))
        prepared.native_state["value"] = "replacement-receipted"
        prepared.replacement.mark_receipted()
        events.append("commit")

    try:
        orphan = atomic_module._publish_native_replacement_with_authority(
            prepared.authority,
            prepared.replacement,
            prepared.stage,
            prepared.destination,
            expected_stage_root_ownership=expected_candidate,
            expected_destination_ownership=expected_incumbent,
            deadline_ns=1,
            validate_staged_directory=validate_staged,
            validate_published_destination=validate_published,
            commit_callback=commit,
        )
        assert events == [
            "validate-staged",
            "exchange",
            "validate-published",
            "commit",
        ]
        assert len(observed) == 1
        assert observed[0][0] == expected_candidate
        assert observed[0][2] is orphan
        assert orphan.locator.backend_tag == "linux-renameat2"
        assert prepared.exchange_calls == [
            (
                os.fsencode(prepared.stage.name),
                os.fsencode(prepared.destination.name),
                1,
            )
        ]
    finally:
        _close_fake_native_replacement(prepared)

    assert orphan is not None
    assert (
        orphan.reopen(lambda reader: reader.read_bytes("payload.bin", max_bytes=16))
        == b"old"
    )
    parked = orphan.path.with_name(".parked-displaced-incumbent")
    orphan.path.rename(parked)
    _write_tree(orphan.path, "payload.bin", "old")
    with pytest.raises(RuntimeError, match="directory orphan|root differs"):
        orphan.reopen(lambda _reader: None)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="native workspace replacement is Linux-only",
)
def test_native_replacement_does_not_poll_cancellation_after_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _adopt_fake_native_replacement(tmp_path, monkeypatch)
    expected_candidate = capture_directory_ownership(prepared.stage)
    expected_incumbent = capture_directory_ownership(prepared.destination)
    cancellation = KeyboardInterrupt("latched at native exchange")
    polls = 0
    commits = 0

    def check_cancelled() -> None:
        nonlocal polls
        polls += 1
        if prepared.native_state["value"] != "replacement-adopted":
            raise cancellation

    def commit(
        _staged: object,
        _published: object,
        _displaced: DirectoryOrphan,
        receipt_token: object,
    ) -> None:
        nonlocal commits
        assert receipt_token is prepared.receipt_token
        commits += 1
        prepared.native_state["value"] = "replacement-receipted"
        prepared.replacement.mark_receipted()

    try:
        atomic_module._publish_native_replacement_with_authority(
            prepared.authority,
            prepared.replacement,
            prepared.stage,
            prepared.destination,
            expected_stage_root_ownership=expected_candidate,
            expected_destination_ownership=expected_incumbent,
            deadline_ns=1,
            validate_published_destination=lambda reader: (
                reader.read_bytes("payload.bin", max_bytes=16)
            ),
            commit_callback=commit,
            check_cancelled=check_cancelled,
        )
        assert polls > 0
        assert commits == 1
        assert prepared.native_state["value"] == "replacement-receipted"
        assert (prepared.destination / "payload.bin").read_bytes() == b"new"
        with pytest.raises(KeyboardInterrupt) as caught:
            check_cancelled()
        assert caught.value is cancellation
    finally:
        _close_fake_native_replacement(prepared)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="native workspace replacement is Linux-only",
)
def test_native_replacement_authority_traps_the_generic_publication_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _adopt_fake_native_replacement(tmp_path, monkeypatch)
    expected_candidate = capture_directory_ownership(prepared.stage)
    expected_incumbent = capture_directory_ownership(prepared.destination)
    try:
        with pytest.raises(RuntimeError, match="dedicated exchange path"):
            atomic_module._publish_staged_directory_with_authority(
                prepared.authority,
                prepared.stage,
                prepared.destination,
                expected_stage_root_ownership=expected_candidate,
                expected_destination_ownership=expected_incumbent,
            )
        assert prepared.exchange_calls == []
        assert (prepared.stage / "payload.bin").read_bytes() == b"new"
        assert (prepared.destination / "payload.bin").read_bytes() == b"old"
        assert not tuple(prepared.parent.glob(".published.previous-*"))
    finally:
        _close_fake_native_replacement(prepared)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="native workspace replacement is Linux-only",
)
def test_native_replacement_helper_rejects_an_exact_wrong_authority_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _adopt_fake_native_replacement(tmp_path, monkeypatch)
    foreign_owner = atomic_module._PublicationAuthorityOwner()
    events: list[str] = []

    def forbidden_exchange(
        _source: bytes,
        _destination: bytes,
        _deadline_ns: int,
    ) -> object:
        events.append("exchange")
        raise AssertionError("wrong replacement pair reached its token callback")

    foreign_authority, foreign_replacement = (
        atomic_module._adopt_native_posix_replacement_authority(
            prepared.parent,
            native_owner=prepared.native_owner,
            parent_descriptor=prepared.parent_descriptor,
            candidate_descriptor=prepared.candidate_descriptor,
            incumbent_descriptor=prepared.incumbent_descriptor,
            expected_parent_identity=publication_parent_identity(
                prepared.parent_descriptor
            ),
            expected_incumbent_identity=(
                atomic_module._directory_inode_identity(
                    os.fstat(prepared.incumbent_descriptor)
                )
            ),
            destination_name=prepared.destination.name,
            replacement_slot=prepared.stage.name,
            exchange_callback=forbidden_exchange,
            authority_owner=foreign_owner,
        )
    )
    assert type(foreign_authority) is type(prepared.authority)
    assert type(foreign_replacement) is type(prepared.replacement)
    expected_candidate = capture_directory_ownership(prepared.stage)
    expected_incumbent = capture_directory_ownership(prepared.destination)

    try:
        with pytest.raises(
            TypeError,
            match="authority.*pair|pair.*authority|pairing",
        ):
            atomic_module._publish_native_replacement_with_authority(
                prepared.authority,
                foreign_replacement,
                prepared.stage,
                prepared.destination,
                expected_stage_root_ownership=expected_candidate,
                expected_destination_ownership=expected_incumbent,
                deadline_ns=1,
                validate_staged_directory=lambda _reader: events.append(
                    "validate-staged"
                ),
                validate_published_destination=lambda _reader: events.append(
                    "validate-published"
                ),
                commit_callback=lambda *_args: events.append("commit"),
            )

        assert events == []
        assert prepared.exchange_calls == []
        assert not prepared.replacement.exchanged
        assert not foreign_replacement.exchanged
        assert (prepared.stage / "payload.bin").read_bytes() == b"new"
        assert (prepared.destination / "payload.bin").read_bytes() == b"old"
    finally:
        foreign_owner.close()
        _close_fake_native_replacement(prepared)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="native workspace replacement is Linux-only",
)
def test_native_replacement_helper_requires_the_exact_replacement_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _adopt_fake_native_replacement(tmp_path, monkeypatch)
    events: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> object:
        events.append("forged-callback")
        raise AssertionError("forged replacement reached a protected callback")

    forged = SimpleNamespace(
        replacement_slot=prepared.stage.name,
        destination_name=prepared.destination.name,
        verify_current=forbidden,
        capture_incumbent=forbidden,
        exchange=forbidden,
    )
    expected_candidate = capture_directory_ownership(prepared.stage)
    expected_incumbent = capture_directory_ownership(prepared.destination)
    try:
        with pytest.raises(TypeError, match="replacement.*invalid"):
            atomic_module._publish_native_replacement_with_authority(
                prepared.authority,
                forged,  # type: ignore[arg-type]
                prepared.stage,
                prepared.destination,
                expected_stage_root_ownership=expected_candidate,
                expected_destination_ownership=expected_incumbent,
                deadline_ns=1,
                validate_staged_directory=lambda _reader: events.append(
                    "validate-staged"
                ),
                validate_published_destination=lambda _reader: events.append(
                    "validate-published"
                ),
                commit_callback=lambda *_args: events.append("commit"),
            )

        assert events == []
        assert prepared.exchange_calls == []
        assert not prepared.replacement.exchanged
        assert (prepared.stage / "payload.bin").read_bytes() == b"new"
        assert (prepared.destination / "payload.bin").read_bytes() == b"old"
    finally:
        _close_fake_native_replacement(prepared)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="native workspace replacement is Linux-only",
)
def test_native_replacement_validator_cannot_intercept_late_native_globals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _adopt_fake_native_replacement(tmp_path, monkeypatch)
    expected_candidate = capture_directory_ownership(prepared.stage)
    expected_incumbent = capture_directory_ownership(prepared.destination)
    interceptions: list[str] = []

    def intercept_verify(_owner: object) -> None:
        interceptions.append("verify")
        raise AssertionError("validator intercepted native verification")

    def intercept_state(_owner: object) -> str:
        interceptions.append("state")
        raise AssertionError("validator intercepted native receipt state")

    def validate_staged(_reader: atomic_module.PublicationDirectoryReader) -> None:
        monkeypatch.setattr(
            atomic_module._native_workspace_owner,
            "verify_owner_authority",
            intercept_verify,
        )
        monkeypatch.setattr(
            atomic_module._native_workspace_owner,
            "owner_state",
            intercept_state,
        )

    def commit(
        _staged: object,
        _published: object,
        _displaced: DirectoryOrphan,
        _receipt_token: object,
    ) -> None:
        prepared.native_state["value"] = "replacement-receipted"
        prepared.replacement.mark_receipted()

    try:
        atomic_module._publish_native_replacement_with_authority(
            prepared.authority,
            prepared.replacement,
            prepared.stage,
            prepared.destination,
            expected_stage_root_ownership=expected_candidate,
            expected_destination_ownership=expected_incumbent,
            deadline_ns=1,
            validate_staged_directory=validate_staged,
            commit_callback=commit,
        )
        assert interceptions == []
        assert (prepared.destination / "payload.bin").read_bytes() == b"new"
    finally:
        _close_fake_native_replacement(prepared)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="native workspace replacement is Linux-only",
)
def test_native_replacement_freezes_receipt_digest_default_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _adopt_fake_native_replacement(tmp_path, monkeypatch)
    expected_candidate = capture_directory_ownership(prepared.stage)
    expected_incumbent = capture_directory_ownership(prepared.destination)
    digest_function = atomic_module._directory_orphan_receipt_digest
    defaults = digest_function.__kwdefaults__
    assert type(defaults) is dict
    original_fsencode = defaults["_fsencode"]
    original_sha256 = defaults["_sha256"]
    original_parent_path_validator = defaults["_require_parent_path_bytes"]
    interceptions: list[str] = []

    def intercept_fsencode(_value: object) -> bytes:
        interceptions.append("fsencode")
        return b"intercepted"

    def intercept_sha256() -> object:
        interceptions.append("sha256")
        return original_sha256()

    def intercept_parent_path(_value: object) -> bytes:
        interceptions.append("parent-path")
        return b"intercepted"

    def validate_staged(_reader: atomic_module.PublicationDirectoryReader) -> None:
        defaults["_fsencode"] = intercept_fsencode
        defaults["_sha256"] = intercept_sha256
        defaults["_require_parent_path_bytes"] = intercept_parent_path

    def commit(
        _staged: object,
        _published: object,
        _displaced: DirectoryOrphan,
        _receipt_token: object,
    ) -> None:
        prepared.native_state["value"] = "replacement-receipted"
        prepared.replacement.mark_receipted()

    try:
        orphan = atomic_module._publish_native_replacement_with_authority(
            prepared.authority,
            prepared.replacement,
            prepared.stage,
            prepared.destination,
            expected_stage_root_ownership=expected_candidate,
            expected_destination_ownership=expected_incumbent,
            deadline_ns=1,
            validate_staged_directory=validate_staged,
            commit_callback=commit,
        )
    finally:
        defaults["_fsencode"] = original_fsencode
        defaults["_sha256"] = original_sha256
        defaults["_require_parent_path_bytes"] = original_parent_path_validator

    try:
        assert interceptions == []
        assert prepared.native_state["value"] == "replacement-receipted"
        locator = orphan.locator
        assert locator.receipt_digest == digest_function(
            parent_path_bytes=locator.parent_path_bytes,
            child_name=locator.child_name,
            backend_tag=locator.backend_tag,
            parent_identity=locator.parent_identity,
            ownership=locator.ownership,
            verified_at_isolation=orphan.verified_at_isolation,
            _fsencode=original_fsencode,
            _sha256=original_sha256,
        )
    finally:
        _close_fake_native_replacement(prepared)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "fork"),
    reason="native workspace replacement fork fencing requires Linux fork",
)
def test_native_replacement_authority_rejects_fork_without_closing_parent_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _adopt_fake_native_replacement(tmp_path, monkeypatch)
    expected_candidate = capture_directory_ownership(prepared.stage)
    child = os.fork()
    if child == 0:  # pragma: no branch - child reports exact status
        try:
            prepared.authority.read_child(
                prepared.stage.name,
                path=prepared.stage,
                label="candidate",
                expected_ownership=expected_candidate,
                callback=lambda _reader: None,
            )
        except RuntimeError as error:
            os._exit(0 if "process boundary" in str(error) else 2)
        except BaseException:  # noqa: B036 - child reports exact failure
            os._exit(3)
        os._exit(4)

    try:
        _, status = os.waitpid(child, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
        assert (
            prepared.authority.read_child(
                prepared.stage.name,
                path=prepared.stage,
                label="candidate",
                expected_ownership=expected_candidate,
                callback=lambda reader: reader.read_bytes(
                    "payload.bin",
                    max_bytes=16,
                ),
            )
            == b"new"
        )
        os.fstat(prepared.parent_descriptor)
        os.fstat(prepared.candidate_descriptor)
        os.fstat(prepared.incumbent_descriptor)
    finally:
        _close_fake_native_replacement(prepared)
