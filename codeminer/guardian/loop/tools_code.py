# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Read-only repository exploration tools for Guardian's outer loop."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


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
    max_chars: int = 20_000,
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
    max_chars: int = 20_000,
) -> str:
    """Search repository views, preferring the compiled retrieval index."""
    if retriever is not None:
        try:
            results = retriever.query(query, top_k=top_k)
            if results:
                rendered = "\n\n".join(str(item) for item in results[:top_k])
                return rendered[:max_chars]
        except Exception:
            pass

    # Dependency-free literal fallback. os.walk avoids shell interpretation and
    # keeps the tool usable in the sandbox image without ripgrep.
    matches = []
    query_lower = query.lower()
    for current, dirs, files in os.walk(repo_path):
        dirs[:] = [
            item
            for item in dirs
            if item not in {".git", ".codeminer_cache", "__pycache__", "node_modules"}
        ]
        for filename in files:
            path = Path(current) / filename
            try:
                relative = path.relative_to(repo_path)
                for line_no, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if query_lower in line.lower():
                        matches.append(f"{relative}:{line_no}: {line.strip()}")
                        if len(matches) >= top_k:
                            return "\n".join(matches)[:max_chars]
            except (OSError, UnicodeError):
                continue
    return "\n".join(matches)[:max_chars] or "(no matches)"
