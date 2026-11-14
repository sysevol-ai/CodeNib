# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve code mentions in a thread to files and symbols at one commit.

Resolution reads the repository through git at a pinned revision, so it works
on a checkout or a bare clone and never depends on the working tree.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .extract import Mention
from .touched import _blob_id, _chunk_spans, language_for_path

_DEFINITION_KEYWORDS = (
    "fn|def|class|struct|enum|trait|type|func|interface|const|static|mod|macro_rules!"
)
_MAX_SYMBOL_DEFINITIONS = 3
_MAX_SYMBOLS_PER_QUERY = 60


_NON_SOURCE_RE = re.compile(
    r"(^|/)(tests?|testdata|fixtures?|snapshots?|resources|docs?|examples?)(/|$)"
    r"|_test\.go$|(^|/)test_[^/]*\.py$|\.(md|snap|json|toml|ya?ml|lock)$"
)


def is_source_path(path: str) -> bool:
    """Whether a path is product source, not a test, fixture, doc or data file."""

    return not _NON_SOURCE_RE.search(path)


@dataclass
class CodeAnchor:
    """A code location a thread concerns, with the events that point at it."""

    path: str
    symbol: str = ""
    start_line: int = 0  # 1-based, 0 when the whole file is meant
    end_line: int = 0
    basis: str = "path"  # frame | review | path | symbol | diff
    cites: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.symbol)

    def location(self) -> str:
        if self.start_line:
            return f"{self.path}:{self.start_line}-{self.end_line}"
        return self.path

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "basis": self.basis,
            "cites": self.cites,
        }


_BASIS_RANK = {"frame": 0, "review": 1, "path": 2, "symbol": 3, "diff": 4}


class AnchorResolver:
    def __init__(self, repo: Path, rev: str = "HEAD") -> None:
        self.repo = Path(repo)
        self.rev = rev
        self._files: list[str] | None = None
        self._by_name: dict[str, list[str]] = defaultdict(list)

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            errors="replace",
        )
        return proc.stdout if proc.returncode in (0, 1) else ""

    def files(self) -> list[str]:
        if self._files is None:
            out = self._git("ls-tree", "-r", "--name-only", self.rev)
            self._files = out.splitlines()
            for path in self._files:
                self._by_name[path.rsplit("/", 1)[-1]].append(path)
        return self._files

    def resolve_path(self, mention: str) -> str | None:
        """An exact path, or the only file the mention is a suffix of."""

        files = self.files()
        mention = mention.strip("/")
        candidates = self._by_name.get(mention.rsplit("/", 1)[-1], [])
        if mention in candidates:
            return mention
        matches = [p for p in candidates if p.endswith("/" + mention)]
        if len(matches) == 1:
            return matches[0]
        # An absolute path from a traceback: match its longest repository tail.
        parts = mention.split("/")
        for start in range(1, len(parts) - 1):
            tail = "/".join(parts[start:])
            hits = [p for p in candidates if p == tail or p.endswith("/" + tail)]
            if len(hits) == 1:
                return hits[0]
        return None if files else None

    def _spans(self, path: str):
        language = language_for_path(path)
        if language is None:
            return ()
        blob = _blob_id(self.repo, self.rev, path)
        return _chunk_spans(str(self.repo), blob, path, language) if blob else ()

    def source(self, path: str, start: int, end: int) -> str:
        """Lines ``start`` to ``end`` (1-based, inclusive) of a file."""

        blob = _blob_id(self.repo, self.rev, path)
        text = self._git("cat-file", "blob", blob) if blob else ""
        return "\n".join(text.splitlines()[start - 1 : end])

    def symbol_at(self, path: str, line: int) -> tuple[str, int, int] | None:
        best = None
        for start, end, node_id in self._spans(path):
            if start <= line <= end and (
                best is None or end - start < best[2] - best[1]
            ):
                best = (node_id.split(":", 1)[-1], start, end)
        return best

    def symbol_in(self, path: str, name: str) -> tuple[str, int, int] | None:
        for start, end, node_id in self._spans(path):
            qualified = node_id.split(":", 1)[-1]
            bare = qualified.rstrip("()").rsplit(".", 1)[-1]
            if bare == name:
                return (qualified, start, end)
        return None

    def definitions(self, names: list[str]) -> dict[str, list[tuple[str, int]]]:
        """Where each name is defined, for names with few definitions."""

        names = [n for n in dict.fromkeys(names) if re.fullmatch(r"\w+", n)]
        names = names[:_MAX_SYMBOLS_PER_QUERY]
        if not names:
            return {}
        pattern = (
            rf"\b({_DEFINITION_KEYWORDS})\s+({'|'.join(names)})\b"
            rf"|^\s*({'|'.join(names)})\s*(=|:=)"
        )
        out = self._git("grep", "-n", "-I", "-E", pattern, self.rev, "--")
        found: dict[str, list[tuple[str, int]]] = defaultdict(list)
        prefix = self.rev + ":"
        for row in out.splitlines():
            if row.startswith(prefix):
                row = row[len(prefix) :]
            try:
                path, line, text = row.split(":", 2)
            except ValueError:
                continue
            if language_for_path(path) is None:
                continue
            for name in names:
                if re.search(rf"\b{re.escape(name)}\b", text):
                    found[name].append((path, int(line)))
                    break
        return {
            name: hits
            for name, hits in found.items()
            if len(hits) <= _MAX_SYMBOL_DEFINITIONS
        }

    def resolve(self, cited: list[tuple[str, Mention]]) -> list[CodeAnchor]:
        """Anchors for ``(event address, mention)`` pairs, strongest first."""

        anchors: dict[tuple[str, str], CodeAnchor] = {}

        def add(path, symbol, start, end, basis, cite):
            anchor = anchors.get((path, symbol))
            if anchor is None:
                anchor = anchors[(path, symbol)] = CodeAnchor(
                    path, symbol, start, end, basis
                )
            elif _BASIS_RANK[basis] < _BASIS_RANK[anchor.basis]:
                anchor.basis = basis
            if cite not in anchor.cites:
                anchor.cites.append(cite)

        symbol_cites: dict[str, list[str]] = defaultdict(list)
        mentioned_paths: set[str] = set()
        for cite, mention in cited:
            if mention.kind in ("path", "frame", "review", "diff"):
                path = self.resolve_path(mention.path)
                if path is None:
                    continue
                mentioned_paths.add(path)
                located = self.symbol_at(path, mention.line) if mention.line else None
                if located is None and mention.symbol:
                    located = self.symbol_in(path, mention.symbol)
                if located:
                    add(path, located[0], located[1], located[2], mention.kind, cite)
                else:
                    add(path, "", 0, 0, mention.kind, cite)
            elif mention.kind == "symbol":
                symbol_cites[mention.symbol].append(cite)

        unresolved = []
        for name, cites in symbol_cites.items():
            # A name is most likely meant in a file the thread already names.
            hit = next(
                (
                    (path, located)
                    for path in sorted(mentioned_paths)
                    if (located := self.symbol_in(path, name))
                ),
                None,
            )
            if hit:
                for cite in cites:
                    add(hit[0], hit[1][0], hit[1][1], hit[1][2], "symbol", cite)
            else:
                unresolved.append(name)
        for name, hits in self.definitions(unresolved).items():
            for path, line in hits:
                located = self.symbol_at(path, line) or (name, line, line)
                for cite in symbol_cites[name]:
                    add(path, located[0], located[1], located[2], "symbol", cite)

        return sorted(
            anchors.values(),
            key=lambda a: (
                _BASIS_RANK[a.basis],
                not is_source_path(a.path),
                -len(a.cites),
                a.path,
                a.start_line,
            ),
        )


__all__ = ["AnchorResolver", "CodeAnchor", "is_source_path"]
