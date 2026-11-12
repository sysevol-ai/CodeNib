# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Open pull requests that change the code a thread is about.

An agent working one thread cannot see that another pull request is changing
the same function. This module keeps a small index of what each open pull
request changes, read from GitHub's file listing, and matches it against the
code locations of a thread.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .anchor import CodeAnchor, is_source_path
from .github_source import GitHubClient, _is_bot, _login
from .log_store import ThreadStore
from .touched import _HUNK_RE

INDEX_NAME = "open_pulls.json"
DEFAULT_MAX_PULLS = 100
_MAX_FILES_PER_PULL = 300


@dataclass
class OpenPull:
    number: int
    title: str
    author: str
    updated_at: str
    draft: bool = False
    # path -> line ranges the pull request changes in the base file.
    files: dict[str, list[list[int]]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return self.__dict__

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "OpenPull":
        return cls(**data)


def base_ranges(patch: str) -> list[list[int]]:
    """Line ranges of the base file that the hunks of a patch replace."""

    ranges = []
    for line in (patch or "").splitlines():
        match = _HUNK_RE.match(line)
        if match:
            start, count = int(match.group(1)), int(match.group(2) or 1)
            ranges.append([start, start + max(count, 1) - 1])
    return ranges


def fetch_open_pulls(
    client: GitHubClient, repo: str, limit: int = DEFAULT_MAX_PULLS
) -> list[OpenPull]:
    """The most recently updated open pull requests with what they change."""

    listed = client.pages(
        f"repos/{repo}/pulls?state=open&sort=updated&direction=desc", max_pages=1
    )
    pulls = []
    for item in listed[:limit]:
        if _is_bot(item.get("user")):
            continue
        files = client.pages(f"repos/{repo}/pulls/{item['number']}/files", max_pages=3)
        pulls.append(
            OpenPull(
                number=item["number"],
                title=item.get("title") or "",
                author=_login(item.get("user")),
                updated_at=item.get("updated_at") or "",
                draft=bool(item.get("draft")),
                files={
                    f["filename"]: base_ranges(f.get("patch") or "")
                    for f in files[:_MAX_FILES_PER_PULL]
                    if f.get("filename")
                },
            )
        )
    return pulls


def write_index(store: ThreadStore, pulls: list[OpenPull]) -> None:
    store.dir.mkdir(parents=True, exist_ok=True)
    path = store.dir / INDEX_NAME
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps([p.to_json() for p in pulls]), encoding="utf-8")
    tmp.replace(path)


def read_index(store: ThreadStore) -> list[OpenPull]:
    path = store.dir / INDEX_NAME
    if not path.exists():
        return []
    return [OpenPull.from_json(p) for p in json.loads(path.read_text("utf-8"))]


@dataclass
class InFlight:
    """One open pull request that touches the code of a thread."""

    pull: OpenPull
    symbols: list[str]  # anchored symbols whose lines it changes
    files: list[str]  # anchored files it changes elsewhere

    def line(self) -> str:
        where = []
        if self.symbols:
            where.append("changes " + ", ".join(f"`{s}`" for s in self.symbols[:3]))
        if self.files:
            where.append("also edits " + ", ".join(self.files[:3]))
        draft = "draft " if self.pull.draft else ""
        return (
            f"- #{self.pull.number} ({draft}open PR by {self.pull.author}, updated "
            f"{self.pull.updated_at[:10]}) {self.pull.title} — {'; '.join(where)}"
        )


def match_in_flight(
    anchors: list[CodeAnchor], pulls: list[OpenPull], exclude: set[int]
) -> list[InFlight]:
    """Open pull requests that change an anchored symbol or file, closest first.

    A pull request that changes lines inside an anchored symbol ranks above one
    that only edits the same file somewhere else.
    """

    found = []
    for pull in pulls:
        if pull.number in exclude:
            continue
        symbols: list[str] = []
        files: list[str] = []
        for anchor in anchors:
            # Generated docs, snapshots and fixtures are shared by unrelated
            # changes; only product source says two changes meet.
            if not is_source_path(anchor.path):
                continue
            ranges = pull.files.get(anchor.path)
            if ranges is None:
                continue
            inside = anchor.start_line and any(
                start <= anchor.end_line and end >= anchor.start_line
                for start, end in ranges
            )
            if inside and anchor.symbol:
                if anchor.symbol not in symbols:
                    symbols.append(anchor.symbol)
            elif anchor.path not in files:
                files.append(anchor.path)
        if symbols or files:
            found.append(InFlight(pull, symbols, files))
    found.sort(key=lambda m: m.pull.updated_at, reverse=True)
    found.sort(key=lambda m: (-len(m.symbols), -len(m.files)))
    return found


__all__ = [
    "InFlight",
    "OpenPull",
    "base_ranges",
    "fetch_open_pulls",
    "match_in_flight",
    "read_index",
    "write_index",
]
