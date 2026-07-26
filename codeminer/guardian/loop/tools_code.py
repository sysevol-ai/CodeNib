# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Read-only repository exploration tools for Guardian's outer loop."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".cxx",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".php",
        ".py",
        ".pyi",
        ".pyx",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sql",
        ".svelte",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)


def _safe_repo_path(repo_path: str, relative_path: str) -> Path:
    root = Path(repo_path).resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path must remain inside the repository") from exc
    if candidate.is_symlink():
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "symlink target must remain inside the repository"
            ) from exc
    return candidate


def read_code(
    repo_path: str,
    path: str,
    *,
    start_line: int = 1,
    end_line: Optional[int] = None,
    max_chars: int = 12_000,
) -> str:
    """Read a bounded, numbered source range from inside the repository."""
    candidate = _safe_repo_path(repo_path, path)
    if not candidate.is_file():
        raise ValueError(f"not a regular file: {path}")
    start = max(1, int(start_line))
    stop = int(end_line) if end_line is not None else start + 300
    if stop < start:
        raise ValueError("end_line must be greater than or equal to start_line")
    lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[start - 1 : stop]
    rendered = "\n".join(
        f"{line_no:>6}  {line}" for line_no, line in enumerate(selected, start=start)
    )
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n[truncated]"
    return rendered or "(empty range)"


def search_code(
    repo_path: str,
    query: str,
    *,
    retriever: object = None,
    top_k: int = 8,
    max_chars: int = 12_000,
) -> str:
    """Search symbols and source text without hiding retriever failures.

    The compiled index is optimized for paths and symbol names.  The text pass
    complements it with ranked, case-insensitive term matches over source
    lines, so a model can use a natural multi-identifier query without needing
    to know which repository view contains each term.
    """
    symbol_matches = ""
    symbol_error = ""
    if retriever is not None:
        try:
            results = retriever.query(query, top_k=top_k)
            if results:
                symbol_matches = "\n\n".join(str(item) for item in results[:top_k])
        except Exception as exc:  # noqa: BLE001 - failure is useful model context
            symbol_error = f"Symbol index unavailable ({type(exc).__name__}: {exc})."

    # Dependency-free full-text pass.  os.walk avoids shell interpretation and
    # keeps the tool usable in the sandbox image without ripgrep.
    phrase = query.casefold().strip()
    terms = list(
        dict.fromkeys(
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9_./:-]+", query)
            if len(token) > 1
        )
    )
    matches = []
    for current, dirs, files in os.walk(repo_path):
        current_path = Path(current)
        dirs[:] = [
            item
            for item in dirs
            if item
            not in {
                ".git",
                ".guardian",
                ".codeminer_cache",
                "__pycache__",
                "node_modules",
            }
            and not (current_path.name == ".claude" and item == "worktrees")
        ]
        for filename in files:
            path = Path(current) / filename
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                relative = path.relative_to(repo_path)
                for line_no, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    folded = line.casefold()
                    matched = [term for term in terms if term in folded]
                    if not matched and (not phrase or phrase not in folded):
                        continue
                    # Exact phrases sort first, then lines containing more of
                    # the requested identifiers. Source files outrank copied
                    # logs and prose containing the same words. Stable
                    # path/line tie-breaks keep replay output deterministic.
                    score = len(matched) + (len(terms) + 1 if phrase in folded else 0)
                    if path.suffix.casefold() in _SOURCE_SUFFIXES:
                        score += len(terms) + 1
                    matches.append(
                        (
                            -score,
                            str(relative),
                            line_no,
                            f"{relative}:{line_no}: {line.strip()}",
                        )
                    )
            except (OSError, UnicodeError):
                continue
    matches.sort()
    source_matches = ""
    if matches:
        source_matches = "\n".join(item[3] for item in matches[:top_k])

    if symbol_matches and source_matches:
        rendered = (
            "Symbol matches:\n"
            + symbol_matches
            + "\n\nSource matches:\n"
            + source_matches
        )
    else:
        rendered = symbol_matches or source_matches
    if symbol_error:
        rendered = symbol_error + ("\n\n" + rendered if rendered else "")
    return rendered[:max_chars] or "(no matches)"
