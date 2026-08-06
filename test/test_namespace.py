# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.check_namespace import (
    _ASSET_MIRRORS,
    _asset_drift,
    _candidate_files,
    _sync_assets,
    _unapproved_namespace,
)


def test_namespace_and_asset_mirrors() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_namespace.py")],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_sync_assets_copies_canonical_bytes(tmp_path: Path) -> None:
    for index, canonical_name in enumerate(_ASSET_MIRRORS):
        canonical = tmp_path / canonical_name
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(f"asset-{index}".encode())

    assert _sync_assets(tmp_path) == []
    assert _asset_drift(tmp_path) == []


def test_candidate_files_ignore_untracked_worktree_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("tracked")
    staged = tmp_path / "staged.txt"
    staged.write_text("staged")
    subprocess.run(
        ["git", "add", tracked.name, staged.name],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "scratch.txt").write_text("untracked")

    candidates = {path.relative_to(tmp_path) for path in _candidate_files(tmp_path)}

    assert candidates == {Path(tracked.name), Path(staged.name)}


def test_namespace_check_rejects_former_names_in_text_and_paths(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    former = "code" + "miner"
    text_path = tmp_path / "config.txt"
    text_path.write_text(f"{former}-mcp\n")
    path_with_former_name = tmp_path / f"{former}_config.py"
    path_with_former_name.write_text("clean\n")
    subprocess.run(
        ["git", "add", text_path.name, path_with_former_name.name],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    failures = _unapproved_namespace(tmp_path)

    assert any("config.txt:1" in failure for failure in failures)
    assert any("former identifier in tracked path" in failure for failure in failures)


def test_namespace_check_rejects_former_external_identity(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    former = "code" + "miner"
    external = tmp_path / "external.txt"
    external.write_text(f"fishmingyu/{former}-base-dataset\n")
    subprocess.run(
        ["git", "add", external.name],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    failures = _unapproved_namespace(tmp_path)

    assert any("external.txt:1" in failure for failure in failures)
