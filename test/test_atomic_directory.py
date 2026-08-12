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
        try:
            file_id = children[name]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc
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
    ) -> None:
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
                commit_callback=lambda sealed, _published, _previous: commits.append(
                    sealed
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
                commit_callback=lambda sealed, _published, _previous: commits.append(
                    sealed
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
                commit_callback=lambda sealed, _published, _previous: commits.append(
                    sealed
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
                commit_callback=lambda sealed, _published, _previous: commits.append(
                    sealed
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
    ) -> None:
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
    enumerate_directory = api.enumerate_directory

    def enumerate_with_zero_id(handle: int) -> tuple[object, ...]:
        return tuple(
            atomic_module._WindowsDirectoryEntry(
                name=entry.name,
                file_id=0,
                attributes=entry.attributes,
            )
            for entry in enumerate_directory(handle)
        )

    api.enumerate_directory = enumerate_with_zero_id  # type: ignore[method-assign]
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
def test_reader_callbacks_survive_reversible_root_swap(
    tmp_path: Path,
    window: str,
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
            active.rename(held)
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


def test_reopen_authenticated_directory_rejects_reversible_root_swap(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "existing"
    _write_tree(directory, "payload.txt", "payload")
    ownership = capture_directory_ownership(directory)
    held = tmp_path / "held"
    foreign = tmp_path / "foreign"
    observed: list[bytes] = []

    def consume(reader: object) -> None:
        assert isinstance(reader, atomic_module.PublicationDirectoryReader)
        directory.rename(held)
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
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    monkeypatch.setattr(atomic_module, "_windows_require_publication_api", lambda: None)
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)
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
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    monkeypatch.setattr(atomic_module, "_windows_require_publication_api", lambda: None)
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)
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
    monkeypatch.setattr(atomic_module.sys, "platform", "win32")
    monkeypatch.setattr(atomic_module, "_windows_kernel_api", lambda: api)
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
                (
                    event == "opcode"
                    and frame.f_lasti in opcode_offsets_after_store
                )
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


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux descriptor semantics",
)
def test_posix_owner_recovers_interrupt_after_open_result_store(tmp_path: Path) -> None:
    owner = atomic_module._PosixResourceOwner()
    error = KeyboardInterrupt("after os.open result store")

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_after_store(
            atomic_module._PosixResourceOwner.open,
            "descriptor",
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
        _call_with_interrupt_after_store(
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
def test_posix_owner_recovers_interrupt_after_dup_result_store(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    owner = atomic_module._PosixResourceOwner()
    error = KeyboardInterrupt("after os.dup result store")
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            _call_with_interrupt_after_store(
                atomic_module._PosixResourceOwner.duplicate,
                "duplicate",
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


def test_windows_owner_recovers_interrupt_after_handle_result_store() -> None:
    api = _FakeWindowsApi()
    owner = atomic_module._WindowsResourceOwner(api)
    error = KeyboardInterrupt("after raw HANDLE result store")

    with pytest.raises(KeyboardInterrupt) as caught:
        _call_with_interrupt_after_store(
            atomic_module._WindowsResourceOwner.acquire,
            "handle",
            lambda: owner.acquire(
                lambda: api.create_directory_handle(Path("C:/authority"))
            ),
            error=error,
        )

    assert caught.value is error
    assert owner.closed
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
