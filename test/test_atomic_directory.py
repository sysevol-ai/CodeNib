# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from pathlib import Path

import pytest

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
