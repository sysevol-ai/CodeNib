# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Git diff detection for incremental indexing.

Detects which source files changed between two commits using ``git diff``,
so the incremental pipeline can rechunk and re-embed only the affected files.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set

from ...log_utils import get_logger

logger = get_logger(__name__)

# Default set of source-code extensions that the chunker supports.
# Mirrors the defaults in RepoChunkingConfig.
_DEFAULT_EXTENSIONS: Set[str] = {
    # Python
    ".py",
    ".pyx",
    ".pyi",
    # C / C++
    ".cpp",
    ".cxx",
    ".cc",
    ".c",
    ".hpp",
    ".h",
    ".hxx",
    # Rust
    ".rs",
    # Go
    ".go",
    # JavaScript / TypeScript
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
}


@dataclass
class RepoChanges:
    """Files that changed between two git commits."""

    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    old_commit: str = ""
    new_commit: str = ""

    @property
    def affected(self) -> List[str]:
        """All files that need rechunking (added + modified)."""
        return self.added + self.modified

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)


class GitDiffDetector:
    """
    Detects source-file changes between two git commits.

    Args:
        supported_extensions: File extensions to track. Defaults to all
            extensions recognised by CodeChunker. Pass a custom set when
            the project uses a subset of languages.
    """

    def __init__(
        self,
        supported_extensions: Set[str] = None,
    ) -> None:
        self._extensions = (
            supported_extensions
            if supported_extensions is not None
            else _DEFAULT_EXTENSIONS
        )

    def get_current_commit(self, repo_path: str) -> str:
        """Return the current HEAD SHA, or empty string if not a git repo."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            logger.warning("Could not determine HEAD commit in %s", repo_path)
            return ""

    def detect_changes(self, repo_path: str, since_commit: str) -> RepoChanges:
        """
        Return files that changed between ``since_commit`` and HEAD.

        Only files whose extension is in ``supported_extensions`` are included.
        Absolute paths are returned.

        Args:
            repo_path: Absolute path to the git repository root.
            since_commit: The git commit SHA that was used for the last index
                build (i.e. the baseline to diff against).

        Returns:
            ``RepoChanges`` with absolute file paths split by change type.

        Raises:
            subprocess.CalledProcessError: If the git command fails.
        """
        new_commit = self.get_current_commit(repo_path)

        if not since_commit:
            logger.warning(
                "No since_commit provided; treating all supported files as added."
            )
            return self._all_files_as_added(repo_path, new_commit)

        # Validate commit SHA to prevent argument injection
        if not re.fullmatch(r"[0-9a-fA-F]{4,40}", since_commit):
            raise ValueError(
                f"Invalid commit SHA: {since_commit!r} — "
                "expected 4-40 hex characters"
            )

        if since_commit == new_commit:
            logger.debug("No commits since last index build (%s).", since_commit)
            return RepoChanges(old_commit=since_commit, new_commit=new_commit)

        result = subprocess.run(
            ["git", "diff", "--name-status", since_commit, "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )

        changes = RepoChanges(old_commit=since_commit, new_commit=new_commit)
        repo_root = Path(repo_path).resolve()

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue

            parts = line.split("\t", maxsplit=1)
            if len(parts) != 2:
                continue

            status_code, rel_path = parts[0].strip(), parts[1].strip()

            # Renames look like "R100\told_path\tnew_path" — git uses two tabs
            # In that case parts[1] contains "old_path\tnew_path"; take new path
            # and explicitly delete the old path to avoid ghost entries.
            if status_code.startswith("R"):
                subparts = rel_path.split("\t", maxsplit=1)
                old_rel = subparts[0].strip()
                rel_path = subparts[-1].strip()
                old_abs = str((repo_root / old_rel).resolve())
                if self._is_supported(old_abs):
                    changes.deleted.append(old_abs)
                status_code = "M"  # treat rename as modify of the destination

            abs_path = str((repo_root / rel_path).resolve())

            if not self._is_supported(abs_path):
                continue

            if status_code == "A":
                changes.added.append(abs_path)
            elif status_code in ("M", "C"):
                changes.modified.append(abs_path)
            elif status_code == "D":
                changes.deleted.append(abs_path)
            else:
                logger.debug(
                    "Unhandled git diff status %r for %s", status_code, abs_path
                )

        logger.info(
            "Detected %d added, %d modified, %d deleted source files since %s",
            len(changes.added),
            len(changes.modified),
            len(changes.deleted),
            since_commit[:8],
        )
        return changes

    def _is_supported(self, path: str) -> bool:
        return Path(path).suffix in self._extensions

    # Directories to skip when walking the repo for the first-time fallback.
    _SKIP_DIRS = frozenset(
        {
            ".git",
            "node_modules",
            "__pycache__",
            ".tox",
            ".mypy_cache",
            ".pytest_cache",
            "vendor",
            "third_party",
            ".venv",
            "venv",
        }
    )

    def _all_files_as_added(self, repo_path: str, new_commit: str) -> RepoChanges:
        """Fallback: walk the repo and treat every supported file as added."""
        changes = RepoChanges(old_commit="", new_commit=new_commit)
        repo_root = Path(repo_path).resolve()
        for p in repo_root.rglob("*"):
            if any(part in self._SKIP_DIRS for part in p.relative_to(repo_root).parts):
                continue
            if p.is_file() and self._is_supported(str(p)):
                changes.added.append(str(p))
        return changes
