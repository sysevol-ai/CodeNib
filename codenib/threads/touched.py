# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the diff between two commits to the files and symbols it changes.

A changed line is attributed to the innermost chunk that encloses it, on the
side of the diff the line belongs to: removed and modified lines are looked up
in the base file, added and modified lines in the head file. A symbol is
identified by its chunk node id (``path:qualified.name``), never by line, so
two diffs taken against different base commits still agree on what they touch.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath

from ..code_chunking import create_chunker
from ..languages import normalize_chunker_language

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MAX_BLOB_BYTES = 2_000_000

# Extension to chunker language. Kept local and small: only languages whose
# chunkers emit stable qualified names are worth resolving to symbols.
_EXTENSION_LANGUAGE = {
    ".rs": "rust",
    ".py": "python",
    ".go": "go",
    ".c": "cpp",
    ".h": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


@dataclass(frozen=True)
class FileChange:
    """Changed line ranges of one file, 1-based and inclusive."""

    old_path: str | None
    new_path: str | None
    old_ranges: tuple[tuple[int, int], ...]
    new_ranges: tuple[tuple[int, int], ...]


@dataclass
class TouchedSet:
    """What one diff changes."""

    files: set[str] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)
    source_lines: int = 0
    mapped_lines: int = 0

    def to_json(self) -> dict:
        return {
            "files": sorted(self.files),
            "symbols": sorted(self.symbols),
            "source_lines": self.source_lines,
            "mapped_lines": self.mapped_lines,
        }

    @classmethod
    def from_json(cls, data: dict) -> "TouchedSet":
        return cls(
            files=set(data["files"]),
            symbols=set(data["symbols"]),
            source_lines=data["source_lines"],
            mapped_lines=data["mapped_lines"],
        )


def language_for_path(path: str) -> str | None:
    language = _EXTENSION_LANGUAGE.get(PurePosixPath(path).suffix.lower())
    return normalize_chunker_language(language) if language else None


def parse_unified_diff(diff_text: str) -> list[FileChange]:
    """Parse ``git diff -U0`` output into per-file changed ranges."""

    changes: list[FileChange] = []
    old_path: str | None = None
    new_path: str | None = None
    old_ranges: list[tuple[int, int]] = []
    new_ranges: list[tuple[int, int]] = []
    in_file = False

    def flush() -> None:
        if in_file and (old_path or new_path):
            changes.append(
                FileChange(old_path, new_path, tuple(old_ranges), tuple(new_ranges))
            )

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            old_path = new_path = None
            old_ranges, new_ranges = [], []
            in_file = True
        elif line.startswith("--- "):
            old_path = _diff_path(line[4:], "a/")
        elif line.startswith("+++ "):
            new_path = _diff_path(line[4:], "b/")
        else:
            match = _HUNK_RE.match(line)
            if not match:
                continue
            old_start, old_count, new_start, new_count = (
                int(match.group(1)),
                int(match.group(2) or 1),
                int(match.group(3)),
                int(match.group(4) or 1),
            )
            if old_count:
                old_ranges.append((old_start, old_start + old_count - 1))
            if new_count:
                new_ranges.append((new_start, new_start + new_count - 1))
    flush()
    return changes


def _diff_path(raw: str, prefix: str) -> str | None:
    raw = raw.split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    return raw[len(prefix) :] if raw.startswith(prefix) else raw


def _git(repo: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None


def merge_base(repo: Path, left: str, right: str) -> str | None:
    out = _git(repo, "merge-base", left, right)
    return out.strip() if out else None


@lru_cache(maxsize=8192)
def _chunk_spans(repo: str, blob: str, path: str, language: str):
    """Chunks of one blob as ``(start, end, node_id)``, 1-based inclusive."""

    source = _git(Path(repo), "cat-file", "blob", blob)
    if source is None or len(source) > _MAX_BLOB_BYTES:
        return ()
    chunker = _chunker(language)
    try:
        chunks = chunker.chunk_source(source, file_path=path, relative_path=path)
    except Exception:  # noqa: BLE001 - a parser failure degrades to file level
        return ()
    return tuple(
        (chunk.start_line + 1, chunk.end_line + 1, chunk.node_id)
        for chunk in chunks
        if chunk.node_id and not _is_anonymous(chunk.node_id)
    )


def _is_anonymous(node_id: str) -> bool:
    # An ``impl`` block the chunker cannot name gets the id ``path:impl``, shared
    # by every such block in the file. Treating it as a symbol would make
    # unrelated changes look shared, so those lines stay at file level. Named
    # blocks (``path:impl Foo``) are kept.
    return node_id.rsplit(":", 1)[-1] == "impl"


@lru_cache(maxsize=None)
def _chunker(language: str):
    # Containers are kept so that a change to a struct field or an impl header
    # still resolves to a symbol; the innermost chunk wins for member bodies.
    return create_chunker(language, chunk_depth=2, l2_level_exclusive=False)


def _blob_id(repo: Path, commit: str, path: str) -> str | None:
    out = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}")
    return out.strip() if out else None


def _map_ranges(spans, ranges, touched: TouchedSet) -> None:
    for start, end in ranges:
        for line in range(start, end + 1):
            touched.source_lines += 1
            best = None
            for span_start, span_end, node_id in spans:
                if span_start <= line <= span_end and (
                    best is None or span_end - span_start < best[0]
                ):
                    best = (span_end - span_start, node_id)
            if best is not None:
                touched.mapped_lines += 1
                touched.symbols.add(best[1])


def touched_between(repo: Path, base: str, head: str) -> TouchedSet | None:
    """Files and symbols changed from ``base`` to ``head`` in a git repository.

    Returns ``None`` when either commit is missing. ``base`` should be the
    merge base with the target branch, so that only the branch's own changes
    are attributed to it.
    """

    diff = _git(
        repo, "diff", "-U0", "--no-color", "--no-ext-diff", "-M", base, head, "--"
    )
    if diff is None:
        return None
    touched = TouchedSet()
    for change in parse_unified_diff(diff):
        for path in (change.old_path, change.new_path):
            if path:
                touched.files.add(path)
        for commit, path, ranges in (
            (base, change.old_path, change.old_ranges),
            (head, change.new_path, change.new_ranges),
        ):
            if not path or not ranges:
                continue
            language = language_for_path(path)
            if language is None:
                continue
            blob = _blob_id(repo, commit, path)
            spans = _chunk_spans(str(repo), blob, path, language) if blob else ()
            _map_ranges(spans, ranges, touched)
    return touched


__all__ = [
    "FileChange",
    "TouchedSet",
    "language_for_path",
    "merge_base",
    "parse_unified_diff",
    "touched_between",
]
