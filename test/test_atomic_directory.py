# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib._atomic_directory as atomic_module
from codenib._atomic_directory import publish_staged_directory


def test_publish_staged_directory_replaces_existing_tree(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / ".published.tmp"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")

    publish_staged_directory(stage, destination)

    assert not stage.exists()
    assert not (destination / "old.txt").exists()
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_restores_previous_tree_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / ".published.tmp"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_replace = os.replace

    def fail_stage_publish(source, target):
        if Path(source) == stage and Path(target) == destination:
            raise OSError("injected final rename failure")
        return real_replace(source, target)

    monkeypatch.setattr("codenib._atomic_directory.os.replace", fail_stage_publish)

    with pytest.raises(OSError, match="injected final rename failure"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_restores_missing_target_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_replace = os.replace

    def fail_stage_publish(source, target):
        if Path(source) == stage and Path(target) == destination:
            raise OSError("injected final rename failure")
        return real_replace(source, target)

    monkeypatch.setattr("codenib._atomic_directory.os.replace", fail_stage_publish)

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
    destination_file = tmp_path / "published"
    destination_file.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="must differ"):
        publish_staged_directory(stage, stage)
    with pytest.raises(ValueError, match="not a directory"):
        publish_staged_directory(stage, destination_file)


def test_publish_staged_directory_does_not_follow_destination_symlink(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    destination = tmp_path / "published"
    destination.symlink_to(victim, target_is_directory=True)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")

    with pytest.raises(ValueError, match="link"):
        publish_staged_directory(stage, destination)

    assert destination.is_symlink()
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (victim / "new.txt").exists()


def test_publish_staged_directory_detects_destination_swap_at_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stolen = tmp_path / "stolen"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_mkdtemp = atomic_module.tempfile.mkdtemp

    def swap_then_allocate(*args, **kwargs):
        destination.rename(stolen)
        destination.symlink_to(victim, target_is_directory=True)
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(atomic_module.tempfile, "mkdtemp", swap_then_allocate)

    with pytest.raises(RuntimeError, match="changed at the publication boundary"):
        publish_staged_directory(stage, destination)

    assert destination.is_symlink()
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (victim / "new.txt").exists()
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_preserves_late_destination_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_mkdtemp = atomic_module.tempfile.mkdtemp

    def mutate_then_allocate(*args, **kwargs):
        (destination / "late.txt").write_text("late", encoding="utf-8")
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(atomic_module.tempfile, "mkdtemp", mutate_then_allocate)

    with pytest.raises(RuntimeError, match="changed at the publication boundary"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (destination / "late.txt").read_text(encoding="utf-8") == "late"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_quarantines_failed_published_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_replace = os.replace

    def mutate_after_publish(source, target):
        result = real_replace(source, target)
        if Path(source) == stage and Path(target) == destination:
            (destination / "late.txt").write_text("late", encoding="utf-8")
        return result

    monkeypatch.setattr(atomic_module.os, "replace", mutate_after_publish)

    with pytest.raises(RuntimeError, match="quarantined"):
        publish_staged_directory(stage, destination)

    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "new.txt").read_text(encoding="utf-8") == "new"
    assert (quarantines[0] / "late.txt").read_text(encoding="utf-8") == "late"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not stage.exists()


def test_quarantine_replace_failure_preserves_original_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()

    def fail_replace(_source, _target):
        raise OSError("injected quarantine rename failure")

    monkeypatch.setattr(atomic_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected quarantine rename failure"):
        atomic_module._quarantine_destination(destination)

    assert destination.is_dir()
    assert not list(tmp_path.glob(".published.quarantine-*"))


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
