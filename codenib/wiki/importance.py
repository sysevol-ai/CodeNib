# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rank Wiki pages by where the recorded call graph puts its weight.

A reader should meet the pages that matter first. The outline says what the
pages are; the index says which code everything else passes through. Both come
from the repository, so ranking needs no model call.

Tiers decide how a reader first sees a page:

- ``primary``: listed up front, and worth a figure.
- ``key``: a child page worth a figure, listed once its parent is opened.
- ``more``: a top-level page behind the "More topics" disclosure.
- ``detail``: a child page listed once its parent is opened.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ..types import (
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)

TIER_PRIMARY = "primary"
TIER_KEY = "key"
TIER_MORE = "more"
TIER_DETAIL = "detail"
FIGURE_TIERS = frozenset({TIER_PRIMARY, TIER_KEY})
CALLABLE_TYPES = frozenset({NODE_TYPE_FUNCTION, NODE_TYPE_METHOD})

# The Overview plus these many pages are what a reader sees before choosing to
# go deeper, whatever the size of the repository.
MAX_PRIMARY_PAGES = 5
MIN_KEY_CHILD_PAGES = 3
KEY_CHILD_SHARE = 3


def file_weights(
    graph: Any,
    *,
    relative_path: Callable[[str], str | None] = lambda file: file,
    is_test: Callable[[str], bool] = lambda _file: False,
) -> dict[str, float]:
    """Return each source file's share of the call graph's weight.

    A symbol weighs more when other code reaches it and when it reaches other
    code. Multiplying the forward and reverse PageRank favours symbols on the
    way through, such as a dispatcher, over a leaf helper that everything calls
    or a script that nothing calls. Containment edges and tests carry no weight.
    """

    if graph is None or not graph.vcount() or not graph.ecount():
        return {}
    attributes = set(graph.vs.attributes())
    if "file" not in attributes:
        return {}
    types = graph.vs["type"] if "type" in attributes else [""] * graph.vcount()
    # Many symbols share a file, and resolving a path can touch the source.
    resolved: dict[str, str | None] = {}

    def source_file(raw_file: str) -> str | None:
        if raw_file not in resolved:
            file = relative_path(raw_file)
            resolved[raw_file] = (
                file.replace("\\", "/") if file and not is_test(file) else None
            )
        return resolved[raw_file]

    files: dict[int, str] = {}
    for vertex, raw_file, kind in zip(graph.vs, graph.vs["file"], types, strict=True):
        if kind in {NODE_TYPE_FILE, NODE_TYPE_DIRECTORY} or not raw_file:
            continue
        file = source_file(str(raw_file))
        if file:
            files[vertex.index] = file
    if len(files) < 2:
        return {}
    edge_types = (
        graph.es["type"]
        if "type" in graph.es.attributes()
        else [EDGE_TYPE_REFERENCE] * graph.ecount()
    )
    edges = [
        edge.index
        for edge, kind in zip(graph.es, edge_types, strict=True)
        if kind == EDGE_TYPE_REFERENCE
        and edge.source in files
        and edge.target in files
        and edge.source != edge.target
    ]
    if not edges:
        return {}
    subgraph = graph.subgraph_edges(edges, delete_vertices=True)
    forward = subgraph.pagerank(directed=True)
    subgraph.reverse_edges()
    reverse = subgraph.pagerank(directed=True)
    weights: dict[str, float] = {}
    for vertex, ahead, behind in zip(subgraph.vs, forward, reverse, strict=True):
        file = source_file(str(vertex["file"]))
        if file:
            weights[file] = weights.get(file, 0.0) + (ahead * behind) ** 0.5
    total = sum(weights.values())
    return {file: value / total for file, value in weights.items()} if total else {}


def main_path_start(
    graph: Any,
    *,
    relative_path: Callable[[str], str | None] = lambda file: file,
    is_test: Callable[[str], bool] = lambda _file: False,
    entry_files: Iterable[str] = (),
    max_hops: int = 5,
) -> int | None:
    """Return the vertex where the heaviest public call path begins.

    Which call path shows how a project runs should not depend on which code a
    search happened to retrieve. From each public callable that no other source
    code calls, this walks towards the weightiest callee and adds up what the
    walk passed through. A manifest entry point is preferred when one has a path
    at all, since that is the door a user actually comes in by.
    """

    if graph is None or not graph.vcount() or not graph.ecount():
        return None
    attributes = set(graph.vs.attributes())
    if not {"file", "type"} <= attributes:
        return None
    entries = {file.replace("\\", "/") for file in entry_files}
    resolved: dict[str, str | None] = {}

    def source_file(raw_file: str) -> str | None:
        if raw_file not in resolved:
            file = relative_path(raw_file)
            resolved[raw_file] = (
                file.replace("\\", "/") if file and not is_test(file) else None
            )
        return resolved[raw_file]

    callables: dict[int, str] = {}
    for vertex in graph.vs:
        if vertex["type"] in CALLABLE_TYPES and vertex["file"]:
            file = source_file(str(vertex["file"]))
            if file:
                callables[vertex.index] = file
    edge_types = (
        graph.es["type"]
        if "type" in graph.es.attributes()
        else [EDGE_TYPE_REFERENCE] * graph.ecount()
    )
    outgoing: dict[int, list[int]] = {}
    called: set[int] = set()
    for edge, kind in zip(graph.es, edge_types, strict=True):
        if (
            kind == EDGE_TYPE_REFERENCE
            and edge.source in callables
            and edge.target in callables
            and edge.source != edge.target
        ):
            outgoing.setdefault(edge.source, []).append(edge.target)
            called.add(edge.target)
    if not outgoing:
        return None
    ids = sorted({*outgoing, *called})
    position = {vertex: index for index, vertex in enumerate(ids)}
    path_graph = type(graph)(
        n=len(ids),
        edges=[
            (position[source], position[target])
            for source, targets in outgoing.items()
            for target in targets
        ],
        directed=True,
    )
    forward = path_graph.pagerank(directed=True)
    path_graph.reverse_edges()
    reverse = path_graph.pagerank(directed=True)
    weight = {
        vertex: (forward[position[vertex]] * reverse[position[vertex]]) ** 0.5
        for vertex in ids
    }

    def is_public(vertex: int) -> bool:
        name = str(graph.vs[vertex]["unified_name"] or "")
        leaf = re.sub(r"\([^)]*\)$", "", name.rsplit(":", 1)[-1]).rsplit(".", 1)[-1]
        return bool(leaf) and (not leaf.startswith("_") or leaf.startswith("__"))

    def walk_weight(start: int) -> float:
        total = weight[start]
        seen = {start}
        current = start
        for _hop in range(max_hops):
            options = [item for item in outgoing.get(current, ()) if item not in seen]
            if not options:
                break
            current = max(options, key=lambda item: (weight[item], -item))
            seen.add(current)
            total += weight[current]
        return total

    starts = [item for item in outgoing if item not in called and is_public(item)]
    # A cycle can leave a project with no uncalled public function at all.
    starts = starts or [item for item in outgoing if is_public(item)]
    if not starts:
        return None
    return max(
        starts,
        key=lambda item: (
            callables[item] in entries,
            walk_weight(item),
            -len(str(graph.vs[item]["unified_name"] or "")),
            -item,
        ),
    )


def _page_files(page: Mapping[str, Any]) -> list[str]:
    files = page.get("files") or ()
    if isinstance(files, (str, bytes)):
        return []
    return [
        file.replace("\\", "/").strip().strip("/")
        for file in files
        if isinstance(file, str) and file.strip()
    ]


def _children(page: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [child for child in page.get("children") or [] if isinstance(child, Mapping)]


def page_tiers(
    outline_pages: Sequence[Mapping[str, Any]],
    weights: Mapping[str, float] | None = None,
) -> dict[str, str]:
    """Assign every outline page a tier; without weights, keep outline order.

    A file named by several pages shares its weight among them. A top-level
    page also counts its children, because it is the way into that whole area.
    """

    weights = weights or {}
    owners: dict[str, int] = {}

    def count_owners(pages: Iterable[Mapping[str, Any]]) -> None:
        for page in pages:
            for file in dict.fromkeys(_page_files(page)):
                owners[file] = owners.get(file, 0) + 1
            count_owners(_children(page))

    count_owners(outline_pages)

    def own_score(page: Mapping[str, Any]) -> float:
        return sum(
            weights.get(file, 0.0) / owners[file]
            for file in dict.fromkeys(_page_files(page))
        )

    def area_score(page: Mapping[str, Any]) -> float:
        return own_score(page) + sum(area_score(item) for item in _children(page))

    tiers: dict[str, str] = {}
    top_level = [page for page in outline_pages if isinstance(page, Mapping)]
    ranked = [page for page in top_level if str(page.get("id")) != "overview"]
    order = {id(page): index for index, page in enumerate(ranked)}
    # A stable tie-break on outline order keeps tiers steady without a graph.
    ranked = sorted(ranked, key=lambda page: (-area_score(page), order[id(page)]))
    primary = {id(page) for page in ranked[:MAX_PRIMARY_PAGES]}
    children: list[Mapping[str, Any]] = []

    def collect(pages: Iterable[Mapping[str, Any]]) -> None:
        for page in pages:
            children.append(page)
            collect(_children(page))

    for page in top_level:
        page_id = str(page.get("id"))
        if page_id == "overview" or id(page) in primary:
            tiers[page_id] = TIER_PRIMARY
        else:
            tiers[page_id] = TIER_MORE
        collect(_children(page))

    limit = max(MIN_KEY_CHILD_PAGES, len(children) // KEY_CHILD_SHARE)
    scored = sorted(
        (page for page in children if own_score(page) > 0),
        key=own_score,
        reverse=True,
    )
    key_pages = {id(page) for page in scored[:limit]}
    for page in children:
        tiers[str(page.get("id"))] = TIER_KEY if id(page) in key_pages else TIER_DETAIL
    return tiers


__all__ = [
    "FIGURE_TIERS",
    "MAX_PRIMARY_PAGES",
    "TIER_DETAIL",
    "TIER_KEY",
    "TIER_MORE",
    "TIER_PRIMARY",
    "file_weights",
    "main_path_start",
    "page_tiers",
]
