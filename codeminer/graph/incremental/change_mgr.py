"""Change manager: git-based change detection for incremental updates."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from ...log_utils import get_logger

logger = get_logger(__name__)

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _shorten_ref(ref: str) -> str:
    """Shorten a git ref to 12 chars only if it is a full 40-char hex SHA."""
    if _FULL_SHA_RE.fullmatch(ref):
        return ref[:12]
    return ref


def detect_changed_files(
    project_root: str,
    base_commit: str,
    target_commit: str = "HEAD",
    extensions: Optional[set[str]] = None,
) -> dict:
    """Detect changed files between two commits using git diff.

    Args:
        project_root: Path to the git repository root.
        base_commit: Base commit hash or ref.
        target_commit: Target commit hash or ref (default HEAD).
        extensions: Only include files with these extensions.

    Returns:
        Dict with keys: modified, added, deleted, renamed.
        Each value is a list of relative file paths (renamed is list of
        (old_path, new_path) tuples).
    """
    result = {"modified": [], "added": [], "deleted": [], "renamed": []}
    try:
        output = subprocess.check_output(
            [
                "git",
                "diff",
                "--name-status",
                _shorten_ref(base_commit),
                _shorten_ref(target_commit),
            ],
            cwd=project_root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"git diff failed: {e}")
        return result

    for line in output.strip().splitlines():
        parts = line.split("\t")
        if not parts:
            continue

        status = parts[0]
        if status == "M" and len(parts) >= 2:
            path = parts[1]
            if extensions is None or Path(path).suffix in extensions:
                result["modified"].append(path)
        elif status == "A" and len(parts) >= 2:
            path = parts[1]
            if extensions is None or Path(path).suffix in extensions:
                result["added"].append(path)
        elif status == "D" and len(parts) >= 2:
            path = parts[1]
            if extensions is None or Path(path).suffix in extensions:
                result["deleted"].append(path)
        elif status.startswith("R") and len(parts) >= 3:
            old_path, new_path = parts[1], parts[2]
            if extensions is None or Path(new_path).suffix in extensions:
                result["renamed"].append((old_path, new_path))

    return result


def get_changed_line_ranges(
    project_root: str,
    file_path: str,
    base_commit: str,
    target_commit: str = "HEAD",
) -> list[tuple[int, int, int, int]]:
    """Get changed line ranges between two commits for a single file.

    Uses ``git diff -U0`` to extract both the OLD and NEW side of each hunk
    so callers can compare line counts (case 3 vs case 4 in the patcher
    classification): if ``(old_end - old_start) == (new_end - new_start)``,
    the body length is preserved and the patcher's fast path is safe.

    Args:
        project_root: Path to the git repository root.
        file_path: Relative path to the file within the repo.
        base_commit: Base commit hash or ref.
        target_commit: Target commit hash or ref (default HEAD).

    Returns:
        List of (old_start, old_end, new_start, new_end) tuples
        (0-indexed, inclusive). For pure-insertion hunks the old side
        collapses to a marker (old_start == old_end); for pure-deletion
        hunks the new side collapses similarly.
    """
    try:
        output = subprocess.check_output(
            [
                "git",
                "diff",
                "-U0",
                _shorten_ref(base_commit),
                _shorten_ref(target_commit),
                "--",
                file_path,
            ],
            cwd=project_root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        logger.debug(f"git diff failed for {file_path}")
        return []

    ranges: list[tuple[int, int, int, int]] = []
    pattern = r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
    for match in re.finditer(pattern, output):
        old_start_1 = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) else 1
        new_start_1 = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) else 1

        old_start_0 = old_start_1 - 1
        new_start_0 = new_start_1 - 1

        # Preserve the historical "deletion marker" quirk: pure-deletion
        # hunks (new_count==0) land at max(0, new_start_0 - 1) on the new
        # side, since there's no actual new line to point at.
        if new_count == 0:
            new_marker = max(0, new_start_0 - 1)
            new_s, new_e = new_marker, new_marker
        else:
            new_s = new_start_0
            new_e = new_start_0 + new_count - 1

        # Mirror semantics for pure-insertion hunks on the old side.
        if old_count == 0:
            old_marker = max(0, old_start_0 - 1)
            old_s, old_e = old_marker, old_marker
        else:
            old_s = old_start_0
            old_e = old_start_0 + old_count - 1

        ranges.append((old_s, old_e, new_s, new_e))
    return ranges
