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
from codenib._atomic_directory import (
    capture_directory_ownership,
    directory_ownership_inventory,
    publish_staged_directory,
)


def test_publish_staged_directory_replaces_existing_tree(tmp_path: Path) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    nested = destination / "nested"
    nested.mkdir()
    (nested / "one.txt").write_text("one", encoding="utf-8")
    (nested / "two.txt").write_text("two", encoding="utf-8")
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

    monkeypatch.setattr(
        atomic_module,
        "_directory_identity",
        lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_mode),
    )
    monkeypatch.setattr(atomic_module.tempfile, "mkdtemp", mutate_then_allocate)

    with pytest.raises(RuntimeError, match="changed at the publication boundary"):
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
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_mkdtemp = atomic_module.tempfile.mkdtemp

    def mutate_then_allocate(*args, **kwargs):
        (nested / "late.txt").write_text("late", encoding="utf-8")
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(
        atomic_module,
        "_directory_identity",
        lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_mode),
    )
    monkeypatch.setattr(atomic_module.tempfile, "mkdtemp", mutate_then_allocate)

    with pytest.raises(RuntimeError, match="changed at the publication boundary"):
        publish_staged_directory(stage, destination)

    assert (destination / "nested" / "old.txt").read_text(encoding="utf-8") == "old"
    assert (destination / "nested" / "late.txt").read_text(encoding="utf-8") == ("late")
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_rejects_nested_stage_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    nested = stage / "nested"
    nested.mkdir(parents=True)
    (nested / "new.txt").write_text("new", encoding="utf-8")
    real_mkdtemp = atomic_module.tempfile.mkdtemp

    def mutate_then_allocate(*args, **kwargs):
        (nested / "late.txt").write_text("late", encoding="utf-8")
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(
        atomic_module,
        "_directory_identity",
        lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_mode),
    )
    monkeypatch.setattr(atomic_module.tempfile, "mkdtemp", mutate_then_allocate)

    with pytest.raises(RuntimeError, match="before destination publication"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "nested" / "new.txt").read_text(encoding="utf-8") == "new"
    assert (stage / "nested" / "late.txt").read_text(encoding="utf-8") == "late"


def test_publish_staged_directory_rejects_same_size_destination_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    previous = destination / "old.txt"
    previous.write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_mkdtemp = atomic_module.tempfile.mkdtemp

    def mutate_then_allocate(*args, **kwargs):
        previous.write_text("NEW", encoding="utf-8")
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(
        atomic_module,
        "_directory_identity",
        lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_mode),
    )
    monkeypatch.setattr(atomic_module.tempfile, "mkdtemp", mutate_then_allocate)

    with pytest.raises(RuntimeError, match="changed at the publication boundary"):
        publish_staged_directory(stage, destination)

    assert previous.read_text(encoding="utf-8") == "NEW"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_preserves_raced_missing_target_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_mkdtemp = atomic_module.tempfile.mkdtemp

    def mutate_then_allocate(*args, **kwargs):
        (destination / "late.txt").write_text("late", encoding="utf-8")
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(
        atomic_module,
        "_directory_identity",
        lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_mode),
    )
    monkeypatch.setattr(atomic_module.tempfile, "mkdtemp", mutate_then_allocate)

    with pytest.raises(RuntimeError, match="raced content remains"):
        publish_staged_directory(
            stage,
            destination,
            expected_destination_identity=None,
            validate_moved_destination=lambda _path: None,
            validate_published_destination=lambda _path: None,
        )

    assert (destination / "late.txt").read_text(encoding="utf-8") == "late"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


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
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    stage.mkdir()
    for name, content in files.items():
        (stage / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(atomic_module, constant, limit)

    with pytest.raises(RuntimeError, match=error):
        publish_staged_directory(stage, destination)

    assert not destination.exists()
    for name, content in files.items():
        assert (stage / name).read_text(encoding="utf-8") == content


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
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    expected = capture_directory_ownership(destination)
    stolen = tmp_path / "stolen"
    destination.rename(stolen)
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed before directory publication"):
        publish_staged_directory(
            stage,
            destination,
            expected_destination_ownership=expected,
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"


def test_expected_stage_root_preserves_substituted_cleanup_path(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    expected = capture_directory_ownership(stage)
    stolen = tmp_path / "stolen-stage"
    stage.rename(stolen)
    stage.mkdir()
    (stage / "foreign.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(RuntimeError, match="root changed before publication"):
        publish_staged_directory(
            stage,
            destination,
            expected_stage_root_ownership=expected,
        )
    atomic_module.discard_owned_directory(stage, expected)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert stolen.is_dir()
    assert (stage / "foreign.txt").read_text(encoding="utf-8") == "preserve"


def test_publish_staged_directory_restores_old_tree_on_scan_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_require = atomic_module._require_tree_ownership

    def interrupt_moved_tree(path, expected, *, label):
        if label == "moved destination":
            raise KeyboardInterrupt("injected ownership interruption")
        return real_require(path, expected, label=label)

    monkeypatch.setattr(
        atomic_module,
        "_require_tree_ownership",
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
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_directory_or_missing = atomic_module._directory_or_missing

    def interrupt_moved_lstat(path: Path, *, label: str):
        if label == "moved destination":
            raise KeyboardInterrupt("injected moved lstat interruption")
        return real_directory_or_missing(path, label=label)

    monkeypatch.setattr(
        atomic_module,
        "_directory_or_missing",
        interrupt_moved_lstat,
    )

    with pytest.raises(KeyboardInterrupt, match="moved lstat interruption"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_publish_restores_old_tree_when_rename_completes_then_interrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_replace = atomic_module.os.replace

    def interrupt_after_old_rename(source, target):
        result = real_replace(source, target)
        if Path(source) == destination and Path(target).name.startswith(
            ".published.previous-"
        ):
            raise KeyboardInterrupt("injected post-rename interruption")
        return result

    monkeypatch.setattr(atomic_module.os, "replace", interrupt_after_old_rename)

    with pytest.raises(KeyboardInterrupt, match="post-rename interruption"):
        publish_staged_directory(stage, destination)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_publish_restores_old_tree_when_final_stage_lstat_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_directory_or_missing = atomic_module._directory_or_missing
    staged_calls = 0

    def interrupt_final_stage_lstat(path: Path, *, label: str):
        nonlocal staged_calls
        if label == "staged directory":
            staged_calls += 1
            if staged_calls == 3:
                raise KeyboardInterrupt("injected final stage lstat interruption")
        return real_directory_or_missing(path, label=label)

    monkeypatch.setattr(
        atomic_module,
        "_directory_or_missing",
        interrupt_final_stage_lstat,
    )

    with pytest.raises(KeyboardInterrupt, match="final stage lstat interruption"):
        publish_staged_directory(stage, destination)

    assert staged_calls == 3
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_first_publication_works_without_safe_ownership_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr(atomic_module, "_SAFE_OWNERSHIP_DIRECTORY_FDS", False)

    publish_staged_directory(
        stage,
        destination,
        expected_destination_ownership=None,
        validate_staged_directory=lambda _path: None,
        validate_published_destination=lambda _path: None,
    )

    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"


def test_existing_publication_fails_closed_without_safe_ownership_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr(atomic_module, "_SAFE_OWNERSHIP_DIRECTORY_FDS", False)

    with pytest.raises(RuntimeError, match="non-empty trees"):
        publish_staged_directory(
            stage,
            destination,
            validate_staged_directory=lambda _path: None,
            validate_published_destination=lambda _path: None,
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
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


def test_published_callback_cannot_bypass_exact_tree_token(
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

    def add_empty_directory_after_publish(source, target):
        result = real_replace(source, target)
        if Path(source) == stage and Path(target) == destination:
            (destination / "late-empty").mkdir()
        return result

    monkeypatch.setattr(atomic_module.os, "replace", add_empty_directory_after_publish)

    with pytest.raises(RuntimeError, match="quarantined"):
        publish_staged_directory(
            stage,
            destination,
            validate_staged_directory=lambda _path: None,
            validate_published_destination=lambda _path: None,
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "late-empty").is_dir()


def test_published_boundary_quarantines_new_before_lost_backup_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    stolen = tmp_path / "stolen-old"
    real_replace = os.replace

    def lose_backup_after_publish(source, target):
        result = real_replace(source, target)
        if Path(source) == stage and Path(target) == destination:
            backup = next(tmp_path.glob(".published.previous-*"))
            backup.rename(stolen)
            backup.mkdir()
            (backup / "foreign.txt").write_text("preserve", encoding="utf-8")
            (destination / "late.txt").write_text("late", encoding="utf-8")
        return result

    monkeypatch.setattr(atomic_module.os, "replace", lose_backup_after_publish)

    with pytest.raises(
        RuntimeError,
        match=(
            "suspect output was quarantined at .*previous output identity lost; "
            "active destination is absent"
        ),
    ):
        publish_staged_directory(stage, destination)

    assert not destination.exists()
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    backups = list(tmp_path.glob(".published.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "new.txt").read_text(encoding="utf-8") == "new"
    assert (quarantines[0] / "late.txt").read_text(encoding="utf-8") == "late"


def test_published_callback_quarantines_new_before_lost_backup_check(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    stolen = tmp_path / "stolen-old"

    def lose_backup_during_published_validation(_path: Path) -> None:
        backup = next(tmp_path.glob(".published.previous-*"))
        backup.rename(stolen)
        backup.mkdir()
        (backup / "foreign.txt").write_text("preserve", encoding="utf-8")
        raise RuntimeError("injected published-token validation failure")

    with pytest.raises(
        RuntimeError,
        match=(
            "suspect output was quarantined at .*previous output identity lost; "
            "active destination is absent"
        ),
    ):
        publish_staged_directory(
            stage,
            destination,
            validate_published_destination=lose_backup_during_published_validation,
        )

    assert not destination.exists()
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    backups = list(tmp_path.glob(".published.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_rejects_mounted_tree_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    mounted = destination / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "external.txt").write_text("preserve", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    original_mount_check = atomic_module._path_is_mount_point

    def fake_mount_check(
        path: Path,
        **kwargs: object,
    ) -> bool:
        return Path(path).name == "mounted" or original_mount_check(path, **kwargs)

    monkeypatch.setattr(atomic_module, "_path_is_mount_point", fake_mount_check)

    with pytest.raises(RuntimeError, match="ownership scan refuses mounted"):
        publish_staged_directory(stage, destination)

    assert (destination / "mounted" / "external.txt").read_text(
        encoding="utf-8"
    ) == "preserve"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.quarantine-*"))


def test_publish_staged_directory_fails_closed_without_safe_cleanup_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "foreign.txt").write_text("preserve", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr(atomic_module, "_SAFE_REMOVAL_DIRECTORY_FDS", False)

    with pytest.raises(RuntimeError, match="safe cleanup validation"):
        publish_staged_directory(stage, destination)

    assert (destination / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    quarantines = list(tmp_path.glob(".published.quarantine-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "new.txt").read_text(encoding="utf-8") == "new"


def test_publish_staged_directory_removes_empty_sentinel_without_cleanup_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    monkeypatch.setattr(atomic_module, "_SAFE_REMOVAL_DIRECTORY_FDS", False)

    publish_staged_directory(stage, destination)

    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".published.previous-*"))


def test_publish_staged_directory_rejects_real_stage_swap_before_destination_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    expected_stage_identity = atomic_module._directory_identity(stage.lstat())
    stolen = tmp_path / "stolen-stage"
    real_mkdtemp = atomic_module.tempfile.mkdtemp

    def swap_stage_then_allocate(*args, **kwargs):
        allocated = real_mkdtemp(*args, **kwargs)
        stage.rename(stolen)
        stage.mkdir()
        (stage / "foreign.txt").write_text("preserve", encoding="utf-8")
        return allocated

    monkeypatch.setattr(atomic_module.tempfile, "mkdtemp", swap_stage_then_allocate)

    with pytest.raises(RuntimeError, match="before destination publication"):
        publish_staged_directory(
            stage,
            destination,
            expected_stage_identity=expected_stage_identity,
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stolen / "new.txt").read_text(encoding="utf-8") == "new"
    assert (stage / "foreign.txt").read_text(encoding="utf-8") == "preserve"


def test_publish_staged_directory_does_not_reacquire_swapped_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    stolen = tmp_path / "stolen-old"
    original_remove = atomic_module._remove_owned_directory

    def swap_backup_then_remove(
        path: Path,
        expected_identity: tuple[int, ...],
        **kwargs: object,
    ) -> None:
        path.rename(stolen)
        path.mkdir()
        (path / "foreign.txt").write_text("preserve", encoding="utf-8")
        original_remove(path, expected_identity, **kwargs)

    monkeypatch.setattr(
        atomic_module,
        "_remove_owned_directory",
        swap_backup_then_remove,
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "publication committed; cleanup incomplete; previous output identity lost"
        ),
    ):
        publish_staged_directory(stage, destination)

    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    backups = list(tmp_path.glob(".published.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob(".published.quarantine-*"))


def test_publish_staged_directory_never_restores_swapped_callback_backup(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    stolen = tmp_path / "stolen-old"
    callback_calls = 0

    def swap_backup_on_second_validation(path: Path) -> None:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 2:
            path.rename(stolen)
            path.mkdir()
            (path / "foreign.txt").write_text("preserve", encoding="utf-8")
            raise RuntimeError("injected second validation failure")

    with pytest.raises(
        RuntimeError,
        match=(
            "publication committed; cleanup incomplete; previous output identity lost"
        ),
    ):
        publish_staged_directory(
            stage,
            destination,
            validate_moved_destination=swap_backup_on_second_validation,
        )

    assert callback_calls == 2
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert (stolen / "old.txt").read_text(encoding="utf-8") == "old"
    backups = list(tmp_path.glob(".published.previous-*"))
    assert len(backups) == 1
    assert (backups[0] / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob(".published.quarantine-*"))


def test_publish_staged_directory_does_not_rollback_after_cleanup_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "published"
    destination.mkdir()
    (destination / "one.txt").write_text("one", encoding="utf-8")
    (destination / "two.txt").write_text("two", encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    real_unlink = atomic_module.os.unlink
    unlink_calls = 0

    def fail_second_unlink(path, *args, **kwargs):
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 2:
            raise OSError("injected second unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(atomic_module.os, "unlink", fail_second_unlink)

    with pytest.raises(RuntimeError, match="publication committed; cleanup incomplete"):
        publish_staged_directory(stage, destination)

    assert unlink_calls == 2
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    backups = list(tmp_path.glob(".published.previous-*"))
    assert len(backups) == 1
    assert len(list(backups[0].iterdir())) == 1
    assert not list(tmp_path.glob(".published.quarantine-*"))


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
