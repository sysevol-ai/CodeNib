# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Find a function, method or class by name, for jumping straight to its code.

The Wiki's jump palette asks this while the reader types. Matches come from
the indexed graph, so every result is a definition the index recorded, with
the file and the line it starts on.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from ..wiki.importance import is_peripheral_file

# Definitions a reader jumps to. Fields, variables and imports are left out:
# they would bury the functions under thousands of one-line names.
SYMBOL_TYPES = frozenset(
    {"function", "method", "class", "struct", "interface", "trait", "enum"}
)
MAX_LIMIT = 50
_CACHE_SIZE = 8


@dataclass(frozen=True)
class _Entry:
    label: str
    full: str
    leaf: str
    kind: str
    file: str
    line: int
    end_line: int | None
    penalty: int


def _label(unified_name: str) -> str:
    """``src/requests/sessions.py:Session.send()`` → ``Session.send()``."""
    return unified_name.rpartition(":")[2] or unified_name


def _key(text: str) -> str:
    return re.sub(r"\(\)$", "", text.strip()).lower()


def _private(label: str) -> bool:
    leaf = _key(label).rsplit(".", 1)[-1].rsplit("::", 1)[-1]
    return leaf.startswith("_") and not (leaf.startswith("__") and leaf.endswith("__"))


class SymbolIndex:
    """Every definition in one graph, ready to be matched by name."""

    def __init__(self, entries: list[_Entry]):
        self._entries = entries

    @classmethod
    def from_graph(cls, graph: Any) -> "SymbolIndex":
        entries: list[_Entry] = []
        seen: set[tuple[str, int, str]] = set()
        for vertex in graph.get_graph().vs:
            attrs = vertex.attributes()
            unified = (attrs.get("unified_name") or "").strip()
            file = attrs.get("file")
            start = attrs.get("start_line")
            if (
                not unified
                or attrs.get("type") not in SYMBOL_TYPES
                or not isinstance(file, str)
                or not file
                or not isinstance(start, int)
            ):
                continue
            label = _label(unified)
            identity = (file, start, label)
            if identity in seen:
                continue
            seen.add(identity)
            end = attrs.get("end_line")
            full = _key(label)
            entries.append(
                _Entry(
                    label=label,
                    full=full,
                    leaf=re.split(r"\.|::", full)[-1],
                    kind=str(attrs.get("type")),
                    file=file,
                    # The graph counts lines from zero; readers count from one.
                    line=start + 1,
                    end_line=end + 1 if isinstance(end, int) else None,
                    # Tests, examples, benchmarks and vendored code come last.
                    penalty=(2 if is_peripheral_file(file) else 0)
                    + (1 if _private(label) else 0),
                )
            )
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        needle = _key(query)
        if not needle:
            return []
        limit = max(1, min(int(limit), MAX_LIMIT))
        ranked: list[tuple[tuple, _Entry]] = []
        for entry in self._entries:
            if entry.leaf == needle or entry.full == needle:
                tier = 0
            elif entry.leaf.startswith(needle):
                tier = 1
            elif entry.full.startswith(needle):
                tier = 2
            elif re.search(rf"(^|[._:]){re.escape(needle)}", entry.full):
                tier = 3
            elif needle in entry.full:
                tier = 4
            else:
                continue
            # The project's own code comes first whatever the match; inside
            # it, the closer match, then public before private, then shorter.
            ranked.append(
                (
                    (
                        entry.penalty >= 2,
                        tier,
                        entry.penalty,
                        len(entry.full),
                        entry.file,
                        entry.line,
                    ),
                    entry,
                )
            )
        ranked.sort(key=lambda item: item[0])
        return [
            {
                "name": entry.label,
                "kind": entry.kind,
                "file": entry.file,
                "line": entry.line,
                "end_line": entry.end_line,
            }
            for _, entry in ranked[:limit]
        ]


_lock = threading.Lock()
_cache: "OrderedDict[tuple[str, int], SymbolIndex]" = OrderedDict()


def symbol_index(repo_id: str, graph: Any) -> SymbolIndex:
    """One index per loaded graph; a rebuilt graph gets a fresh one."""

    key = (repo_id, id(graph))
    with _lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
            return found
    built = SymbolIndex.from_graph(graph)
    with _lock:
        _cache[key] = built
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return built
