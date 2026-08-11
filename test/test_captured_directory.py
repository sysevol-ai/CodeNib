# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

import codenib._atomic_directory as atomic_directory
from codenib._atomic_directory import (
    capture_directory_ownership,
    directory_ownership_file_records,
)
from codenib._captured_directory import CapturedDirectoryReader, OwnedDirectoryStage


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
