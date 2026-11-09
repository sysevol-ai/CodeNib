# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Carry a published Wiki's unaffected pages forward to a new commit.

Every cached page is keyed by its commit and source fingerprint, so a push
would otherwise rewrite the whole Wiki. After a Wiki passes its audit, its
outline and ready pages are saved beside it. On the next commit, a page whose
recorded source files are all unchanged is copied under the new commit's key;
only the others, and anything never generated, are written by the model.

The outline is kept as it was, so new areas of the code appear only after a
full rebuild. A snapshot is used only when its prompt versions match and Git
can name every file changed since its commit; otherwise the Wiki is rebuilt.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

SNAPSHOT_NAME = "carry-forward.json"
SNAPSHOT_VERSION = 1
# Keys whose string values name repository files in a page's payload.
_FILE_KEYS = frozenset(
    {
        "file",
        "anchor_file",
        "source_file",
        "caller_file",
        "callee_file",
        "spelling_file",
        "artifact_path",
        "path",
    }
)


@dataclass
class CarryForward:
    """What a carry-forward reused, and why it could not reuse more."""

    previous_commit: str = ""
    outline_reused: bool = False
    reused: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    reason: str = ""


def _prompt_versions() -> dict[str, str]:
    from .agent_wiki import _OUTLINE_PROMPT_VERSION, _PAGE_PROMPT_VERSION

    return {"outline": _OUTLINE_PROMPT_VERSION, "page": _PAGE_PROMPT_VERSION}


def _flatten(pages: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    flat: list[Mapping[str, Any]] = []
    for page in pages:
        if isinstance(page, Mapping):
            flat.append(page)
            flat.extend(_flatten(page.get("children") or []))
    return flat


def _page_meta(builder: Any, page_id: str, pages: list) -> Optional[dict]:
    meta = builder._find(page_id, pages)
    if meta is not None and page_id == "overview":
        meta = builder._overview_page_meta(meta, pages[1:])
    return meta


def page_files(page: Any) -> set[str]:
    """Repository-relative files a page's citations and evidence name."""

    files: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in _FILE_KEYS and isinstance(item, str):
                    path = item.strip()
                    while path.startswith("./"):
                        path = path[2:]
                    if path and not path.startswith("/") and ".." not in path:
                        files.add(path)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(page)
    return files


def changed_files(repo: Path, old: str, new: str) -> Optional[set[str]]:
    """Files that differ between two commits, or None if Git cannot say."""

    if not old or not new:
        return None
    if old == new:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "--no-renames", old, new],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def save_snapshot(builder: Any, path: Path, commit: str) -> int:
    """Save a passed Wiki's outline and ready pages; return the page count."""

    outline = builder.cached_outline()
    if outline is None:
        raise ValueError("there is no cached outline to carry forward")
    pages = {}
    for meta in _flatten(outline.get("pages") or []):
        page_id = str(meta.get("id") or "")
        page = builder.cached_page(page_id)
        if page is not None:
            pages[page_id] = page
    payload = {
        "version": SNAPSHOT_VERSION,
        "commit": commit,
        "prompt_versions": _prompt_versions(),
        "outline": outline,
        "pages": pages,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)
    return len(pages)


def carry_forward(builder: Any, path: Path, repo: Path, commit: str) -> CarryForward:
    """Copy the snapshot's unaffected pages under ``commit``'s cache key."""

    result = CarryForward()
    if not path.is_file():
        result.reason = "no earlier Wiki"
        return result
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result.reason = "the earlier Wiki could not be read"
        return result
    if not isinstance(snapshot, dict) or snapshot.get("version") != SNAPSHOT_VERSION:
        result.reason = "the earlier Wiki has another format"
        return result
    result.previous_commit = str(snapshot.get("commit") or "")
    if snapshot.get("prompt_versions") != _prompt_versions():
        result.reason = "the Wiki prompts changed"
        return result
    changed = changed_files(repo, result.previous_commit, commit)
    if changed is None:
        result.reason = (
            f"Git cannot compare {result.previous_commit[:12]} with {commit[:12]}"
        )
        return result
    outline = snapshot.get("outline")
    pages = snapshot.get("pages")
    if not isinstance(outline, dict) or not isinstance(pages, dict):
        result.reason = "the earlier Wiki is incomplete"
        return result
    tree = outline.get("pages")
    if not isinstance(tree, list) or builder.cached_outline() is not None:
        result.reason = "this commit already has an outline"
        return result
    builder._write_cache("outline", outline)
    result.outline_reused = True
    for meta in _flatten(tree):
        page_id = str(meta.get("id") or "")
        page = pages.get(page_id)
        files = page_files(page) if isinstance(page, dict) else set()
        # A page that names no file cannot show it is unaffected.
        if not files or files & changed:
            result.rewritten.append(page_id)
            continue
        suffix_meta = _page_meta(builder, page_id, tree)
        if suffix_meta is None:
            result.rewritten.append(page_id)
            continue
        builder._write_cache(builder._page_cache_suffix(suffix_meta), page)
        result.reused.append(page_id)
    return result


__all__ = [
    "SNAPSHOT_NAME",
    "CarryForward",
    "carry_forward",
    "changed_files",
    "page_files",
    "save_snapshot",
]
