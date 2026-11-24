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

# Code a repository carries that is not the project itself: other projects
# vendored into it, and the examples, benchmarks, docs and development tooling
# around it. The main path neither starts there nor passes through it.
_VENDORED_DIRS = frozenset(
    {
        "deps",
        "extern",
        "external",
        "externals",
        "node_modules",
        "third-party",
        "third_party",
        "thirdparty",
        "vendor",
        "vendored",
    }
)
# Matched against each word of a directory name, so ``ruff_dev`` and
# ``tokio-test`` count as well as ``examples``.
_PERIPHERAL_WORDS = frozenset(
    {
        "bench",
        "benches",
        "benchmark",
        "benchmarks",
        "ci",
        "demo",
        "demos",
        "dev",
        "doc",
        "docs",
        "documentation",
        "example",
        "examples",
        "fixture",
        "fixtures",
        "fuzz",
        "hack",
        "mock",
        "mocks",
        "sample",
        "samples",
        "script",
        "scripts",
        "stress",
        "tools",
        "xtask",
    }
)
_PERIPHERAL_FILES = frozenset({"build.rs", "conftest.py", "noxfile.py", "setup.py"})
_TEST_NAME_RE = re.compile(r"^(?:test|bench)(?:_|[A-Z0-9]|$)|_test$")
# Reporting a failure is a side road from any main path: ``_serverAssert()``,
# ``warn()``, ``IoError::new_internal()``.
_REPORTING_WORDS = frozenset(
    {
        "abort",
        "assert",
        "assertion",
        "bug",
        "crash",
        "debug",
        "err",
        "error",
        "errors",
        "fatal",
        "log",
        "logger",
        "panic",
        "warn",
        "warning",
        "warnings",
    }
)
# A callable called from this many files (or the repository's top 1%, if that
# is more) is a helper everything uses, not a step on one path.
HELPER_MIN_CALLER_FILES = 8
HELPER_CALLER_SHARE = 0.01
REPORTING_MIN_CALLER_FILES = 3
# A callable this short that calls nothing (``Span::unknown()``) only answers
# a question for its caller; a path that ends there ends on a detail.
TINY_LEAF_LINES = 5
MAX_TEST_CHECKS = 40
MAX_README_CALL_MATCHES = 12
# The named package must leave this share of the project's files outside it to
# say anything: in a single-package library (xarray) it is everything.
MIN_OUTSIDE_OWN_PACKAGE = 0.2


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


def is_peripheral_file(file: str) -> bool:
    """Whether a file belongs to what surrounds the project, not the project.

    That is a vendored project (``deps/lua/src/lua.c``), an example, benchmark,
    doc or script, a test-support or development package (``tokio-test/``,
    ``crates/ruff_dev/``), a build script, or a type stub. A directory named
    like a vendor directory counts only with a project below it: Hugo's own
    ``deps/deps.go`` is Hugo.
    """

    parts = [part for part in file.replace("\\", "/").lower().split("/") if part]
    if not parts:
        return False
    if parts[-1] in _PERIPHERAL_FILES or parts[-1].endswith(".pyi"):
        return True
    stem = parts[-1].split(".", 1)[0]
    if re.search(r"(?:^|[-_])tests?$|^tests?[-_]", stem):
        return True
    directories = parts[:-1]
    for index, directory in enumerate(directories):
        if directory in _VENDORED_DIRS and index + 1 < len(directories):
            return True
        for word in re.split(r"[-_.]", directory):
            if word in _PERIPHERAL_WORDS or word.startswith("test"):
                return True
            if word.endswith(("test", "tests")):
                return True
    return False


def _symbol_words(name: str) -> set[str]:
    """``IoError.new_internal()`` -> ``{"io", "error", "new", "internal"}``."""

    symbol = re.sub(r"\([^)]*\)$", "", name.rsplit(":", 1)[-1])
    symbol = re.sub(r"<[^>]*>", "", symbol)
    return {
        word.lower()
        for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", symbol)
    }


def _leaf_name(name: str) -> str:
    symbol = re.sub(r"\([^)]*\)$", "", name.rsplit(":", 1)[-1])
    return symbol.rsplit(".", 1)[-1]


def _span(vertex: Any) -> int | None:
    attributes = vertex.attributes()
    start, end = attributes.get("start_line"), attributes.get("end_line")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) for value in (start, end)
    ):
        return None
    return end - start + 1


def _call_graph(
    graph: Any,
    relative_path: Callable[[str], str | None],
    is_test: Callable[[str], bool],
) -> tuple[dict[int, str], dict[int, list[int]], set[int]]:
    """Source callables with their files, their recorded calls, and the callees."""

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
    return callables, outgoing, called


def path_stops(
    graph: Any,
    *,
    relative_path: Callable[[str], str | None] = lambda file: file,
    is_test: Callable[[str], bool] = lambda _file: False,
    _calls: tuple[dict[int, str], dict[int, list[int]], set[int]] | None = None,
) -> set[int]:
    """Callables a main path does not step into.

    A main path follows the work, so it stops before code that only serves it:
    a helper called from all over the repository (``translate!()``,
    ``jv_free()``), failure reporting (``_serverAssert()``, ``warn()``), and a
    leaf of a few lines (``Span::unknown()``). Each is fine to call; none is
    where the work it was called for gets done.
    """

    if graph is None or not graph.vcount() or not graph.ecount():
        return set()
    if not {"file", "type"} <= set(graph.vs.attributes()):
        return set()
    callables, outgoing, called = _calls or _call_graph(graph, relative_path, is_test)
    caller_files: dict[int, set[str]] = {}
    for source, targets in outgoing.items():
        if is_peripheral_file(callables[source]):
            continue
        for target in targets:
            caller_files.setdefault(target, set()).add(callables[source])
    counts = sorted((len(files) for files in caller_files.values()), reverse=True)
    top_share = counts[int(len(counts) * HELPER_CALLER_SHARE)] if counts else 0
    helper_floor = max(HELPER_MIN_CALLER_FILES, top_share)
    stops: set[int] = set()
    for vertex in called:
        fan_in = len(caller_files.get(vertex, ()))
        name = str(graph.vs[vertex]["unified_name"] or "")
        if fan_in >= helper_floor:
            stops.add(vertex)
        elif fan_in >= REPORTING_MIN_CALLER_FILES and (
            _symbol_words(name) & _REPORTING_WORDS
        ):
            stops.add(vertex)
        elif vertex not in outgoing:
            span = _span(graph.vs[vertex])
            if span is not None and span <= TINY_LEAF_LINES:
                stops.add(vertex)
    return stops


def _package_directory(file: str) -> str:
    """The directory of the package an entry file belongs to; "" at the root.

    ``crates/ruff/src/lib.rs`` -> ``crates/ruff``; ``tokio/src/lib.rs`` ->
    ``tokio``; ``src/requests/__init__.py`` -> ``src/requests``.
    """

    parts = file.replace("\\", "/").split("/")
    if parts[-1] == "__init__.py":
        return "/".join(parts[:-1])
    for index, part in enumerate(parts[:-1]):
        if part in {"src", "bin", "lib"}:
            return "/".join(parts[:index])
    return "/".join(parts[:-1])


def own_entry_files(entries: Iterable[Any], repository: str) -> set[str]:
    """The entry files of the package the repository is named after.

    That is the workspace member that shares the repository's name
    (``crates/ruff`` in astral-sh/ruff), or else the top-level package's own
    targets (``src/main.rs`` in nushell), not every member a workspace lists.
    """

    def normal(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    name = normal(repository.replace("\\", "/").rsplit("/", 1)[-1])
    entries = list(entries)
    named = {
        str(entry.path)
        for entry in entries
        if name and normal(str(getattr(entry, "label", ""))) == name
    }
    if named:
        return named
    return {str(entry.path) for entry in entries if getattr(entry, "root", False)}


def main_path_start(
    graph: Any,
    *,
    relative_path: Callable[[str], str | None] = lambda file: file,
    is_test: Callable[[str], bool] = lambda _file: False,
    entry_files: Iterable[str] = (),
    own_files: Iterable[str] = (),
    program_files: Iterable[str] = (),
    run_files: Iterable[str] = (),
    linked_files: Iterable[str] = (),
    readme_calls: Iterable[tuple[str, str]] = (),
    stops: set[int] | None = None,
    is_test_callable: Callable[[int], bool] | None = None,
    max_hops: int = 5,
) -> int | None:
    """Return the vertex where the heaviest public call path begins.

    Which call path shows how a project runs should not depend on which code a
    search happened to retrieve. From each public callable that no other source
    code calls, this walks towards the weightiest callee and adds up what the
    walk passed through. A path starts in the project itself, never in a
    vendored, example, benchmark or development file (``is_peripheral_file``),
    and its walk skips the helpers a path stops before (``path_stops``).

    Among those starts the doors the README points at come first: a file it
    tells readers to run (*run_files*), then a file it links to
    (*linked_files*). Either counts only when its walk leaves the file, since a
    script whose path stays inside itself (a demo's ``main()``) shows that
    script, not the project. Next is a call the README's code examples make
    (*readme_calls*, such as ``("requests", "get")``), which may start a path
    even though the project calls it too. Then come the entry files of the
    package the repository is named after (*own_files*) and that package's
    directory, a program (a ``main()``, or a binary or script the manifest
    declares in *program_files*), and any other manifest entry point
    (*entry_files*).

    A start needs one of those reasons. A path from whichever function happens
    to weigh most (``nansum()`` in a library of array functions) tells a reader
    nothing about how the project is used, so without a reason there is no
    start. ``is_test_callable`` is asked about the best candidates in order and
    turns away a test the file's name does not give away (Rust's ``#[test]``).
    Returns None when no candidate qualifies.
    """

    if graph is None or not graph.vcount() or not graph.ecount():
        return None
    attributes = set(graph.vs.attributes())
    if not {"file", "type"} <= attributes:
        return None
    entries = {file.replace("\\", "/") for file in entry_files}
    owns = {file.replace("\\", "/") for file in own_files}
    own_directories = {
        directory for directory in map(_package_directory, owns) if directory
    }
    programs = {file.replace("\\", "/") for file in program_files}
    runs = {file.replace("\\", "/") for file in run_files}
    links = {file.replace("\\", "/") for file in linked_files}
    calls = _call_graph(graph, relative_path, is_test)
    callables, outgoing, called = calls
    if stops is None:
        stops = path_stops(graph, _calls=calls)
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

    def leaf(vertex: int) -> str:
        return _leaf_name(str(graph.vs[vertex]["unified_name"] or ""))

    def is_public(vertex: int) -> bool:
        name = leaf(vertex)
        return bool(name) and (not name.startswith("_") or name.startswith("__"))

    def may_start(vertex: int) -> bool:
        return (
            is_public(vertex)
            and not is_peripheral_file(callables[vertex])
            and not _TEST_NAME_RE.search(leaf(vertex))
        )

    def walk(start: int) -> tuple[float, bool, int]:
        """The walk's summed weight, whether it leaves the start's file, and
        how many hops it takes."""

        total = weight[start]
        seen = {start}
        current = start
        leaves = False
        hops = 0
        for hops in range(1, max_hops + 1):
            options = [
                item
                for item in outgoing.get(current, ())
                if item not in seen
                and item not in stops
                and not is_peripheral_file(callables[item])
            ]
            if not options:
                hops -= 1
                break
            current = max(options, key=lambda item: (weight[item], -item))
            seen.add(current)
            total += weight[current]
            leaves = leaves or callables[current] != callables[start]
        return total, leaves, hops

    project_files = {
        file for file in callables.values() if not is_peripheral_file(file)
    }

    def in_directory(file: str, directory: str) -> bool:
        return file.startswith(directory + "/")

    # The named package is a reason to start only when the repository holds
    # much more than it: in a single-package library it is everything.
    distinct_directories = {
        directory
        for directory in own_directories
        if sum(not in_directory(file, directory) for file in project_files)
        >= MIN_OUTSIDE_OWN_PACKAGE * len(project_files)
    }

    def own_package(vertex: int) -> bool:
        return any(in_directory(callables[vertex], d) for d in own_directories)

    def own_subpackage(vertex: int) -> bool:
        return any(in_directory(callables[vertex], d) for d in distinct_directories)

    # A named package that is a program (docusaurus's CLI) is how the project
    # runs; a named library inside a larger repository is only its largest part.
    program_directories = {
        _package_directory(file) for file in owns & programs
    } & distinct_directories

    def own_program_package(vertex: int) -> bool:
        return any(in_directory(callables[vertex], d) for d in program_directories)

    called_in_readme = {
        item
        for item in _readme_called(graph, callables, readme_calls, may_start)
        if item in weight
    }
    starts = [item for item in outgoing if item not in called and may_start(item)]
    # A cycle can leave a project with no uncalled public function at all.
    starts = starts or [item for item in outgoing if may_start(item)]
    starts = list(dict.fromkeys([*starts, *sorted(called_in_readme)]))
    if not starts:
        return None
    walks = {item: walk(item) for item in starts}

    # A library's other programs (astropy's ``volint``) are side tools; its
    # own program, or any program of a repository that is not a library, is
    # how the project runs.
    own_library = bool(owns - programs)

    def program(item: int) -> bool:
        file = callables[item]
        if file in owns and file in programs:
            return True
        if leaf(item) == "main" and own_subpackage(item):
            return True
        return not own_library and (file in programs or leaf(item) == "main")

    # When the README names its doors, a start it does not back is the wrong
    # way in (#1132: AMA-Bench's data generators).
    readme_doors = bool(runs or links)

    def reasons(item: int) -> tuple[bool, ...]:
        file = callables[item]
        door = (
            file in runs and walks[item][1],
            file in links and walks[item][1],
            item in called_in_readme,
        )
        if readme_doors:
            return (*door, False, False, False)
        return (
            *door,
            file in owns and file in programs,
            own_program_package(item),
            program(item),
        )

    # A path needs three steps; a start whose walk stops sooner has none.
    ranked = sorted(
        (item for item in starts if walks[item][2] >= 2 and any(reasons(item))),
        key=lambda item: (
            *reasons(item)[:4],
            own_package(item),
            program(item),
            callables[item] in entries,
            walks[item][0],
            -len(str(graph.vs[item]["unified_name"] or "")),
            -item,
        ),
        reverse=True,
    )
    if is_test_callable is None:
        return ranked[0] if ranked else None
    # Reading source is slow; the first few candidates settle it in practice.
    for item in ranked[:MAX_TEST_CHECKS]:
        if not is_test_callable(item):
            return item
    return None


def _readme_called(
    graph: Any,
    callables: Mapping[int, str],
    readme_calls: Iterable[tuple[str, str]],
    may_start: Callable[[int], bool],
) -> set[int]:
    """The callables a README's code examples call.

    A capitalised owner names the type (``TcpListener::bind`` is
    ``TcpListener.bind()``). A lowercase one is a variable or a module; a
    module that names a directory or file narrows the match to its own
    functions (``tokio::spawn`` is the free ``spawn()`` under ``tokio/``, not
    a scheduler's ``Handle.spawn()``). A name that still matches more than a
    few callables (``read()``) says too little to count.
    """

    by_leaf: dict[str, list[int]] = {}
    for vertex in callables:
        if may_start(vertex):
            by_leaf.setdefault(
                _leaf_name(str(graph.vs[vertex]["unified_name"] or "")), []
            ).append(vertex)
    found: set[int] = set()
    for owner, name in readme_calls:
        hits = by_leaf.get(name, [])
        if owner[:1].isupper():
            hits = [
                vertex
                for vertex in hits
                if _owner_name(str(graph.vs[vertex]["unified_name"] or "")) == owner
            ]
        elif owner:
            module = [
                vertex
                for vertex in hits
                if owner.lower() in re.split(r"[/.]", callables[vertex].lower())
            ]
            if module:
                # A module's function, not a method of something inside it.
                hits = [
                    vertex
                    for vertex in module
                    if _owner_name(str(graph.vs[vertex]["unified_name"] or ""))
                    in {"", owner}
                ] or module
        if 0 < len(hits) <= MAX_README_CALL_MATCHES:
            found.update(hits)
    return found


def _owner_name(name: str) -> str:
    """``tokio/src/net/tcp/listener.rs:TcpListener.bind()`` -> ``TcpListener``."""

    symbol = re.sub(r"\([^)]*\)$", "", name.rsplit(":", 1)[-1])
    owner = symbol.rsplit(".", 1)[0] if "." in symbol else ""
    return re.sub(r"<[^>]*>", "", owner.rsplit(".", 1)[-1])


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
    "is_peripheral_file",
    "main_path_start",
    "own_entry_files",
    "path_stops",
    "page_tiers",
]
