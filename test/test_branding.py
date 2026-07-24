# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.check_branding import (
    _ASSET_MIRRORS,
    _asset_drift,
    _candidate_files,
    _sync_assets,
)


def test_display_branding_and_asset_mirrors() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_branding.py")],
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
