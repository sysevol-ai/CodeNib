# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Place threads, open pull requests and recent changes on the Wiki pages they concern.

A Wiki page cites files and line ranges at one commit. A thread names code
locations, an open pull request changes line ranges of its base files, and a
merged change touches symbols. Where these meet a page's citations, the page
can say what is happening on the code it explains: the open issues that point
at it, the pull requests that change it, and the recent changes that explain
why it looks the way it does.

The join is computed by code alone, from git and the stored thread logs; no
model is called and no code graph is needed. Matches are graded:

- ``symbol``: the item reaches a cited line range, or names the cited symbol
  in the cited file;
- ``file``: the item reaches a cited file elsewhere.

A file-level match says little when the file is hot, touched by a large share
of recent commits, or when the item reaches many pages by file alone. Both are
dropped, so one wide refactoring, or a file that every change edits, does not
fill every page.

Titles in the overlay are thread text written by anyone who can comment.
Whatever renders the overlay must treat them as text, never as markup.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .anchor import AnchorResolver, CodeAnchor, is_source_path
from .touched import touched_between

OVERLAY_VERSION = 1
MAX_PER_PAGE = 8
# An item that reaches more pages than this only at file level is too wide to
# say anything about any one of them.
WIDE_FILE_PAGES = 4
# A file touched by more than this share of recent commits (and by more than
# the floor) is hot: touching it says little about any page that cites it.
HOT_FILE_SHARE = 0.03
HOT_FILE_FLOOR = 5
HEAT_WINDOW = 300
_PR_NUMBER_RE = re.compile(r"\(#(\d+)\)\s*$")
# Wiki citations may name a symbol by its chunk id, ``path/to/file.rs:name()``.
_PATH_PREFIX_RE = re.compile(r"^[^:\s]+\.\w+:(?!:)")
_STRENGTH_RANK = {"symbol": 0, "file": 1}
_SECTIONS = (("issue", "issues"), ("pull", "pulls"), ("change", "changes"))


# ------------------------------------------------------------------ pages ---


@dataclass(frozen=True)
class CitedRange:
    """One citation of a Wiki page, 1-based and inclusive; 0 for the whole file."""

    file: str
    start_line: int = 0
    end_line: int = 0
    name: str = ""


@dataclass
class WikiPage:
    id: str
    title: str
    citations: list[CitedRange] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "WikiPage":
        citations = []
        for raw in data.get("citations") or ():
            file = str(raw.get("file") or "").strip("/")
            if not file:
                continue
            start = _line(raw.get("start_line"))
            end = _line(raw.get("end_line")) or start
            citations.append(
                CitedRange(
                    file, start, max(start, end), str(raw.get("node_name") or "")
                )
            )
        return cls(str(data["id"]), str(data.get("title") or data["id"]), citations)


def _line(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def load_wiki_pages(directory: Path) -> list[WikiPage]:
    """The pages of a Wiki: a static export's repository directory, or page files.

    ``directory`` holds either ``wiki.json`` with a ``pages/`` directory beside
    it, as ``codenib pages`` publishes them, or page JSON files directly.
    """

    directory = Path(directory)
    page_dir = directory / "pages" if (directory / "pages").is_dir() else directory
    order: list[str] = []
    tree = directory / "wiki.json"
    if tree.is_file():

        def walk(nodes):
            for node in nodes or ():
                order.append(str(node.get("id")))
                walk(node.get("children"))

        walk(json.loads(tree.read_text("utf-8")).get("pages"))
    pages: dict[str, WikiPage] = {}
    for path in sorted(page_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "id" in data:
            page = WikiPage.from_json(data)
            pages[page.id] = page
    ranked = [pages.pop(page_id) for page_id in order if page_id in pages]
    return ranked + list(pages.values())


class PageIndex:
    """Citations by file, for matching code locations to pages."""

    def __init__(self, pages: Iterable[WikiPage]) -> None:
        self.pages = list(pages)
        self._by_file: dict[str, list[tuple[str, CitedRange]]] = {}
        for page in self.pages:
            for cited in page.citations:
                self._by_file.setdefault(cited.file, []).append((page.id, cited))

    def files(self) -> set[str]:
        return set(self._by_file)

    def match(
        self,
        path: str,
        ranges: Iterable[tuple[int, int]] = (),
        names: Iterable[str] = (),
    ) -> dict[str, tuple[str, list[str]]]:
        """Pages whose citations ``path`` reaches: ``{page id: (strength, names)}``.

        ``ranges`` are line ranges in ``path``; ``names`` are symbol names the
        item concerns there, matched against the cited symbol's last component
        so that a match survives lines moving between commits.
        """

        ranges = [(a, b) for a, b in ranges if a > 0 and b >= a]
        bare = {_bare(n) for n in names if n}
        found: dict[str, tuple[str, list[str]]] = {}
        for page_id, cited in self._by_file.get(path, ()):
            hit_symbol = (
                cited.start_line
                and any(
                    a <= cited.end_line and b >= cited.start_line for a, b in ranges
                )
            ) or (cited.name and _bare(cited.name) in bare)
            strength = "symbol" if hit_symbol else "file"
            prior = found.get(page_id)
            label = _PATH_PREFIX_RE.sub("", cited.name) or (
                f"{cited.file}:{cited.start_line}"
            )
            if prior is None or _STRENGTH_RANK[strength] < _STRENGTH_RANK[prior[0]]:
                found[page_id] = (strength, [label] if hit_symbol else [])
            elif hit_symbol and label not in prior[1]:
                prior[1].append(label)
        return found


def _bare(name: str) -> str:
    """``Foo.bar()`` and ``path.rs:Foo::bar`` both become ``bar``."""

    name = name.rsplit(":", 1)[-1].rstrip("()")
    return re.split(r"\.|::", name)[-1]


# ------------------------------------------------------------------ items ---


@dataclass
class PageItem:
    """An issue, pull request or change, and where it reaches one page."""

    kind: str  # issue | pull | change
    number: int
    title: str
    state: str = ""
    author: str = ""
    at: str = ""
    draft: bool = False
    commit: str = ""
    strength: str = "file"
    symbols: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    cites: list[str] = field(default_factory=list)

    def to_json(self, repo: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "number": self.number,
            "title": self.title,
            "strength": self.strength,
            "symbols": self.symbols[:6],
            "files": self.files[:6],
            "at": self.at,
        }
        if self.kind == "change":
            data["commit"] = self.commit
        else:
            data.update(state=self.state, author=self.author, cites=self.cites[:3])
        if self.kind == "pull":
            data["draft"] = self.draft
        if self.number:
            route = "issues" if self.kind == "issue" else "pull"
            data["url"] = f"https://github.com/{repo}/{route}/{self.number}"
        return data


class _Collector:
    """Gathers per-page items and drops file-level hits that carry little.

    A file-level hit is dropped when the file is hot (most changes touch it,
    so touching it says little about the page) or when the item reaches too
    many pages by file alone.
    """

    def __init__(self, index: PageIndex, hot: Iterable[str] = ()) -> None:
        self.index = index
        self.hot = set(hot)
        self.hits: list[tuple[str, PageItem]] = []
        self.hot_dropped = 0

    def add(self, template: PageItem, reaches: list[tuple[str, list, list]]) -> None:
        """``reaches`` lists ``(path, ranges, names)`` the item touches."""

        per_page: dict[str, PageItem] = {}
        for path, ranges, names in reaches:
            for page_id, (strength, labels) in self.index.match(
                path, ranges, names
            ).items():
                if strength == "file" and path in self.hot:
                    self.hot_dropped += 1
                    continue
                item = per_page.get(page_id)
                if item is None:
                    item = per_page[page_id] = PageItem(
                        **{
                            **template.__dict__,
                            "symbols": [],
                            "files": [],
                            "cites": list(template.cites),
                        }
                    )
                    item.strength = strength
                elif _STRENGTH_RANK[strength] < _STRENGTH_RANK[item.strength]:
                    item.strength = strength
                for label in labels:
                    if label not in item.symbols:
                        item.symbols.append(label)
                if path not in item.files:
                    item.files.append(path)
        file_only = [p for p, item in per_page.items() if item.strength == "file"]
        if len(file_only) > WIDE_FILE_PAGES:
            for page_id in file_only:
                per_page.pop(page_id)
        self.hits.extend(per_page.items())

    def pages(self) -> dict[str, dict[str, list[PageItem]]]:
        out: dict[str, dict[str, list[PageItem]]] = {}
        for page_id, item in self.hits:
            out.setdefault(page_id, {}).setdefault(item.kind, []).append(item)
        for groups in out.values():
            for kind, items in groups.items():
                items.sort(key=lambda i: i.at, reverse=True)
                items.sort(key=lambda i: (_STRENGTH_RANK[i.strength], -len(i.symbols)))
                groups[kind] = items[:MAX_PER_PAGE]
        return out


# ---------------------------------------------------------------- sources ---


def thread_reaches(anchors: list[CodeAnchor]) -> list[tuple[str, list, list]]:
    """What a thread's resolved code locations reach, product source only."""

    reaches = []
    for anchor in anchors:
        if not is_source_path(anchor.path):
            continue
        ranges = [(anchor.start_line, anchor.end_line)] if anchor.start_line else []
        names = [anchor.symbol] if anchor.symbol else []
        reaches.append((anchor.path, ranges, names))
    return reaches


def add_threads(
    collector: _Collector,
    store,
    checkout: Path,
    rev: str = "HEAD",
    *,
    include_closed: bool = False,
    skip: set[int] | None = None,
) -> int:
    """Place every stored thread on the pages its own posts point at.

    Linked threads are not followed: each thread is placed by what it says
    itself, so an issue does not inherit the files of a refactoring it links.
    Returns how many threads reached a page.
    """

    from .serve import ThreadView, _cited_mentions

    resolver = AnchorResolver(checkout, rev)
    placed = 0
    for number in store.numbers():
        if skip and number in skip:
            continue
        header = store.header(number)
        events = store.events(number) if header else []
        if header is None or not events:
            continue
        view = ThreadView(header, events)
        state = view.state()
        if state != "open" and not include_closed:
            continue
        anchors = resolver.resolve(_cited_mentions([view]))
        before = len(collector.hits)
        collector.add(
            PageItem(
                kind="pull" if header.is_pr else "issue",
                number=number,
                title=header.title,
                state=state,
                author=header.author,
                at=events[-1].at,
                cites=[c for a in anchors for c in a.cites][:3],
            ),
            thread_reaches(anchors),
        )
        placed += len(collector.hits) > before
    return placed


def add_open_pulls(collector: _Collector, pulls: Iterable[Any]) -> set[int]:
    """Place indexed open pull requests by the base line ranges they change."""

    numbers = set()
    for pull in pulls:
        if pull.number in numbers:
            continue
        numbers.add(pull.number)
        reaches = [
            (path, [tuple(r) for r in ranges], [])
            for path, ranges in pull.files.items()
            if is_source_path(path)
        ]
        collector.add(
            PageItem(
                kind="pull",
                number=pull.number,
                title=pull.title,
                state="open",
                author=pull.author,
                at=pull.updated_at,
                draft=pull.draft,
            ),
            reaches,
        )
    return numbers


def file_heat(checkout: Path, rev: str, window: int) -> tuple[dict[str, int], int]:
    """How many of the last ``window`` first-parent commits touched each file.

    Also returns how many commits were read, which is fewer than ``window`` in
    a young or shallow repository.
    """

    proc = subprocess.run(
        [
            "git", "-C", str(checkout), "log", "--first-parent",
            "--diff-merges=first-parent", f"-n{int(window)}", "--format=%x00",
            "--name-only", rev, "--",
        ],  # fmt: skip
        capture_output=True,
        text=True,
        errors="replace",
    )
    heat: dict[str, int] = {}
    commits = 0
    for path in proc.stdout.splitlines():
        if path == "\x00":
            commits += 1
        elif path:
            heat[path] = heat.get(path, 0) + 1
    return heat, commits


def hot_files(
    heat: Mapping[str, int],
    window: int,
    *,
    share: float | None = None,
    floor: int | None = None,
) -> set[str]:
    """Files touched by more than ``share`` of ``window`` commits, and ``floor``."""

    share = HOT_FILE_SHARE if share is None else share
    floor = HOT_FILE_FLOOR if floor is None else floor
    limit = max(floor, share * window)
    return {path for path, count in heat.items() if count > limit}


def recent_commits(checkout: Path, rev: str, limit: int) -> list[dict[str, str]]:
    """The last ``limit`` first-parent commits up to ``rev``, newest first."""

    proc = subprocess.run(
        [
            "git", "-C", str(checkout), "log", "--first-parent", f"-n{int(limit)}",
            "--format=%H%x00%P%x00%cI%x00%an%x00%s", rev, "--",
        ],  # fmt: skip
        capture_output=True,
        text=True,
        errors="replace",
    )
    commits = []
    for row in proc.stdout.splitlines():
        parts = row.split("\x00")
        if len(parts) != 5 or not parts[1]:
            continue  # a root commit has nothing to compare with
        sha, parents, at, author, subject = parts
        commits.append(
            {
                "sha": sha,
                "parent": parents.split()[0],
                "at": at,
                "author": author,
                "subject": subject,
            }
        )
    return commits


def add_recent_changes(
    collector: _Collector, checkout: Path, rev: str = "HEAD", limit: int = 200
) -> int:
    """Place recent first-parent commits by the symbols they changed.

    A squash-merged pull request is one such commit, and its number is read
    from the ``(#N)`` GitHub appends to the subject. Symbols are matched by
    name, so lines that moved since the commit do not matter.
    """

    placed = 0
    for commit in recent_commits(checkout, rev, limit):
        touched = touched_between(checkout, commit["parent"], commit["sha"])
        if touched is None:
            continue
        names_by_file: dict[str, list[str]] = {}
        for node_id in touched.symbols:
            path, _, name = node_id.partition(":")
            names_by_file.setdefault(path, []).append(name)
        reaches = [
            (path, [], names_by_file.get(path, []))
            for path in sorted(touched.files)
            if is_source_path(path)
        ]
        number = _PR_NUMBER_RE.search(commit["subject"])
        before = len(collector.hits)
        collector.add(
            PageItem(
                kind="change",
                number=int(number.group(1)) if number else 0,
                title=commit["subject"],
                author=commit["author"],
                at=commit["at"],
                commit=commit["sha"],
            ),
            reaches,
        )
        placed += len(collector.hits) > before
    return placed


# ---------------------------------------------------------------- overlay ---


def build_overlay(
    pages: list[WikiPage],
    *,
    repo: str,
    checkout: Path,
    rev: str = "HEAD",
    store=None,
    open_pulls: Iterable[Any] = (),
    history: int = 200,
    heat_window: int = HEAT_WINDOW,
    include_closed: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """The overlay for one Wiki: per page, its issues, pull requests and changes.

    ``heat_window`` is how many recent commits decide which files are hot;
    0 keeps every file-level match.
    """

    index = PageIndex(pages)
    heat, read = file_heat(checkout, rev, heat_window) if heat_window else ({}, 0)
    hot = hot_files(heat, read)
    collector = _Collector(index, hot & index.files())
    indexed = add_open_pulls(collector, open_pulls)
    threads = 0
    if store is not None:
        # An open pull request in the index is placed by its diff, which is
        # more precise than what its thread names.
        threads = add_threads(
            collector, store, checkout, rev, include_closed=include_closed, skip=indexed
        )
    changes = add_recent_changes(collector, checkout, rev, history) if history else 0
    grouped = collector.pages()
    resolved = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", rev],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "version": OVERLAY_VERSION,
        "repo": repo,
        "rev": resolved or rev,
        "generated_at": now
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "pages": {
            page.id: {
                section: [item.to_json(repo) for item in grouped[page.id][kind]]
                for kind, section in _SECTIONS
                if grouped[page.id].get(kind)
            }
            for page in pages
            if page.id in grouped
        },
        "summary": {
            "pages": len(pages),
            "pages_with_items": len(grouped),
            "threads_placed": threads,
            "open_pulls_indexed": len(indexed),
            "changes_placed": changes,
            "hot_files": sorted(hot & index.files(), key=lambda f: -heat[f]),
            "hot_file_matches_dropped": collector.hot_dropped,
        },
    }


__all__ = [
    "CitedRange",
    "PageIndex",
    "PageItem",
    "WikiPage",
    "build_overlay",
    "file_heat",
    "hot_files",
    "load_wiki_pages",
]
