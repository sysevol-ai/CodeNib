# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Module-level ("which file depends on which") view over the symbol graph.

Issue #390 asked for "at least a graph of modules". The symbol codemap cannot
answer that: it walks symbol-to-symbol ``reference`` edges, so a re-export
barrel like preact's ``src/index.js`` comes back as one node with no edges.

This builds the module view by *projecting* the symbol graph through its file
attribution — if ``src/render.js:render()`` references ``src/diff/index.js:diff()``
then ``src/render.js`` depends on ``src/diff/index.js``, with the reference count
as the coupling weight. Two consequences worth stating:

* It needs no new edge type and no re-indexing. ``EDGE_TYPE_IMPORT`` is declared
  in ``codenib/types.py`` but no decoder emits it, so every graph CodeNib builds
  carries ``reference`` + ``contain`` only. A projection works on the graphs that
  already exist.
* It is *stricter* than parsing import statements. An import that nothing uses
  produces no edge, and a barrel re-export shows where the symbol actually
  lives. The tradeoff is the reverse case: a module coupled only through an
  unused import is invisible here, which is the right default for a dependency
  map but means this is not a substitute for real import edges.

Payload shape mirrors ``codemap`` (``{nodes, edges, hierarchy, mermaid}``) so the
frontend can reuse its renderers.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from ..graph.code_graph import CodeGraph
from ..types import NODE_TYPE_FILE
from ..utils import is_test_file
from .codemap import _DerivedFiles
from .entrypoints import discover_entry_points

# Symbols that can carry a file attribution (excludes file/directory/root nodes).
_SYMBOL_TYPES = frozenset({"function", "method", "class", "field", "symbol"})
GRANULARITIES = ("auto", "file", "directory")
# Above this many files, "auto" rolls up to directories: a 400-file map is a
# hairball whatever the layout engine does.
_AUTO_FILE_LIMIT = 60
# Per module edge; the true count stays in ``weight``.
_ANCHOR_SAMPLE = 12
_MERMAID_MAX_LABEL = 42
_ROOT_DIR = "."


def _module_of(file_path: str, granularity: str) -> str:
    """Map a file to its module key at the requested granularity."""
    normalized = file_path.replace("\\", "/").strip("/")
    if granularity != "directory":
        return normalized
    parent = normalized.rsplit("/", 1)[0] if "/" in normalized else ""
    return parent or _ROOT_DIR


def _module_label(module: str, granularity: str) -> str:
    """Display form: a directory keeps a trailing slash, a file does not."""
    return module if granularity == "file" else f"{module}/"


def _short(label: str) -> str:
    if len(label) <= _MERMAID_MAX_LABEL:
        return label
    return "…" + label[-(_MERMAID_MAX_LABEL - 1) :]


class _Projection:
    """Modules and their weighted dependencies, plus what was left out."""

    def __init__(self) -> None:
        self.symbol_count: Counter[str] = Counter()
        self.pairs: Dict[Tuple[str, str], Set[Tuple[str, str]]] = defaultdict(set)
        self.anchors: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        self.anchor_total: Counter[Tuple[str, str]] = Counter()
        self.skipped_tests = 0
        self.skipped_derived = 0

    def degree(self) -> Dict[str, int]:
        """Showable modules, keyed to their total coupling weight.

        A module qualifies if it declares a symbol or participates in a
        dependency. That admits a re-export barrel, whose file node carries edges
        but no symbols of its own, while dropping the empty ``__init__.py`` tail
        — real files, but nothing a dependency map can say about them.
        """
        out: Counter[str] = Counter()
        for (src, tgt), refs in self.pairs.items():
            out[src] += len(refs)
            out[tgt] += len(refs)
        for module, symbols in self.symbol_count.items():
            if symbols:
                out.setdefault(module, 0)
        return out


def _project(
    graph: CodeGraph,
    granularity: str,
    derived: _DerivedFiles,
    include_tests: bool,
) -> _Projection:
    """Aggregate symbol references into module-to-module dependencies."""
    projection = _Projection()
    g = graph.get_graph()

    module_of_vertex: Dict[int, str] = {}
    seen_files: Dict[str, Optional[str]] = {}
    for index, file_path, is_symbol in _attributed_vertices(graph):
        if file_path not in seen_files:
            if not include_tests and is_test_file(file_path):
                seen_files[file_path] = None
                projection.skipped_tests += 1
            elif derived(file_path):
                seen_files[file_path] = None
                projection.skipped_derived += 1
            else:
                seen_files[file_path] = _module_of(file_path, granularity)
        module = seen_files[file_path]
        if module is None:
            continue
        module_of_vertex[index] = module
        if is_symbol:
            projection.symbol_count[module] += 1
        else:
            # Keep a bare file node discoverable even if it declares no symbols.
            projection.symbol_count.setdefault(module, 0)

    for edge in g.es:
        if edge.attributes().get("type") != "reference":
            continue
        source = module_of_vertex.get(edge.source)
        target = module_of_vertex.get(edge.target)
        if source is None or target is None or source == target:
            continue
        key = (source, target)
        projection.pairs[key].add(
            (g.vs[edge.source]["name"], g.vs[edge.target]["name"])
        )
        projection.anchor_total[key] += 1
        anchor_file = edge.attributes().get("anchor_file")
        if anchor_file and len(projection.anchors[key]) < _ANCHOR_SAMPLE:
            anchor_line = edge.attributes().get("anchor_line")
            # anchor_line is 0-based (tree-sitter/LSP); expose 1-based.
            entry = {
                "file": anchor_file,
                "line": (anchor_line + 1) if isinstance(anchor_line, int) else None,
            }
            if entry not in projection.anchors[key]:
                projection.anchors[key].append(entry)
    return projection


def _attributed_vertices(graph: CodeGraph) -> Iterator[Tuple[int, str, bool]]:
    """Yield ``(vertex index, file path, is a symbol)`` for every attributed node.

    A file node's path is its *identity*, not a ``file`` attribute, and it has no
    symbol of its own to count. They are included because the TS decoder points a
    re-export barrel's references straight at file nodes — which is exactly the
    module-level coupling this view wants, and the reason preact's
    ``src/index.js`` comes back empty from the symbol codemap.
    """
    for vertex in graph.get_graph().vs:
        attrs = vertex.attributes()
        node_type = attrs.get("type")
        if node_type == NODE_TYPE_FILE:
            file_path, is_symbol = attrs.get("name"), False
        elif node_type in _SYMBOL_TYPES:
            file_path, is_symbol = attrs.get("file"), True
        else:
            continue
        if isinstance(file_path, str) and file_path:
            yield vertex.index, file_path, is_symbol


def _resolve_granularity(
    graph: CodeGraph, granularity: str, derived: _DerivedFiles, include_tests: bool
) -> str:
    """Resolve ``auto`` by counting the repo's own source files."""
    if granularity in ("file", "directory"):
        return granularity
    files: Set[str] = set()
    for _index, file_path, _is_symbol in _attributed_vertices(graph):
        if (not include_tests and is_test_file(file_path)) or derived(file_path):
            continue
        files.add(file_path)
        if len(files) > _AUTO_FILE_LIMIT:
            return "directory"
    return "file"


def _resolve_focus(modules: Sequence[str], focus: str) -> Optional[str]:
    """Match a user-supplied path to a module key (exact, then suffix, then substring)."""
    needle = focus.replace("\\", "/").strip("/").lower()
    if not needle:
        return None
    best: Optional[str] = None
    for module in modules:
        low = module.lower()
        if low == needle:
            return module
        if low.endswith("/" + needle) or needle in low:
            if best is None or len(module) < len(best):
                best = module
    return best


def _select(
    degree: Dict[str, int],
    projection: _Projection,
    focus: Optional[str],
    depth: int,
    max_nodes: int,
) -> Tuple[List[str], bool]:
    """Choose which modules to show: a focus neighbourhood, or the busiest ones."""
    if focus is None:
        ranked = sorted(degree, key=lambda m: (-degree[m], m))
        return ranked[:max_nodes], len(ranked) > max_nodes

    adjacency: Dict[str, Set[str]] = defaultdict(set)
    for src, tgt in projection.pairs:
        adjacency[src].add(tgt)
        adjacency[tgt].add(src)

    chosen = [focus]
    seen = {focus}
    truncated = False
    queue: deque[Tuple[str, int]] = deque([(focus, 0)])
    while queue:
        module, hops = queue.popleft()
        if hops >= depth:
            continue
        # Strongest coupling first, so the cap keeps the meaningful neighbours.
        for neighbour in sorted(
            adjacency[module],
            key=lambda n: (
                -len(projection.pairs.get((module, n), ()))
                - len(projection.pairs.get((n, module), ())),
                n,
            ),
        ):
            if neighbour in seen:
                continue
            if len(chosen) >= max_nodes:
                truncated = True
                break
            seen.add(neighbour)
            chosen.append(neighbour)
            queue.append((neighbour, hops + 1))
    return chosen, truncated


def _hops_from(
    projection: _Projection, root: str, selected: Set[str]
) -> Dict[str, int]:
    """BFS distance from *root* over the undirected projection, for node depth."""
    adjacency: Dict[str, Set[str]] = defaultdict(set)
    for src, tgt in projection.pairs:
        if src in selected and tgt in selected:
            adjacency[src].add(tgt)
            adjacency[tgt].add(src)
    hops = {root: 0}
    queue: deque[str] = deque([root])
    while queue:
        module = queue.popleft()
        for neighbour in adjacency[module]:
            if neighbour not in hops:
                hops[neighbour] = hops[module] + 1
                queue.append(neighbour)
    return hops


def _score(
    names: Sequence[str], edges: Sequence[Tuple[Tuple[str, str], int]]
) -> Tuple[Dict[str, float], Dict[str, int], Dict[str, int], Dict[str, float]]:
    """Weighted PageRank + community + in/out degree over the projected graph.

    Same contract as the codemap's ``_score`` — rank-percentile importance in
    [0,1], a cluster id, an in-degree ``ref_count`` and an ``entry_score`` that
    is high for modules that depend on much and are depended on by little.
    Best-effort: neutral scores on any failure.
    """
    importance = {name: 0.0 for name in names}
    community = {name: 0 for name in names}
    ref_count = {name: 0 for name in names}
    entry = {name: 0.0 for name in names}
    if len(names) < 3 or not edges:
        return importance, community, ref_count, entry
    try:
        import igraph

        index = {name: i for i, name in enumerate(names)}
        projected = igraph.Graph(n=len(names), directed=True)
        projected.add_edges([(index[s], index[t]) for (s, t), _ in edges])
        weights = [w for _, w in edges]
        ranks = projected.pagerank(directed=True, damping=0.85, weights=weights)
        order = sorted(range(len(ranks)), key=lambda i: ranks[i])
        denom = max(1, len(ranks) - 1)
        for rank, i in enumerate(order):
            importance[names[i]] = round(rank / denom, 4)
        indegree = projected.indegree()
        outdegree = projected.outdegree()
        for i, name in enumerate(names):
            ref_count[name] = int(indegree[i])
            total = indegree[i] + outdegree[i]
            entry[name] = round(outdegree[i] / total, 4) if total else 0.0
        if projected.vcount() >= 12:
            try:
                undirected = projected.as_undirected(mode="collapse")
                clusters = undirected.community_leiden(
                    objective_function="modularity", n_iterations=-1
                )
                if clusters.modularity is not None and clusters.modularity >= 0.2:
                    for i, name in enumerate(names):
                        community[name] = int(clusters.membership[i])
            except Exception:  # noqa: BLE001 - community is optional polish
                pass
    except Exception:  # noqa: BLE001 - scoring must never 500 the map
        pass
    return importance, community, ref_count, entry


def _hierarchy(nodes: Sequence[Dict[str, Any]], granularity: str) -> Dict[str, Any]:
    """Directory tree over the shown modules.

    The codemap's ``hierarchy_from_view_nodes`` assumes every view node is a
    symbol inside a file; here the view nodes *are* the files/directories, so
    this builds the containment tree directly instead.
    """
    hierarchy_nodes: List[Dict[str, Any]] = [
        {
            "id": "hier::root",
            "parent": None,
            "kind": "root",
            "label": "root",
            "path": "",
            "depth": 0,
            "child_count": 0,
            "symbol_count": 0,
            "doi": 1.0,
            "open_by_default": True,
            "external": False,
        }
    ]
    by_id = {"hier::root": hierarchy_nodes[0]}

    def ensure_dir(path: str) -> str:
        if not path or path == _ROOT_DIR:
            return "hier::root"
        node_id = f"hier::dir::{path}"
        if node_id in by_id:
            return node_id
        parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
        parent = ensure_dir(parent_path)
        node = {
            "id": node_id,
            "parent": parent,
            "kind": "directory",
            "label": f"{path.rsplit('/', 1)[-1]}/",
            "path": path,
            "depth": path.count("/") + 1,
            "child_count": 0,
            "symbol_count": 0,
            "doi": 0.0,
            "open_by_default": True,
            "external": False,
        }
        by_id[node_id] = node
        hierarchy_nodes.append(node)
        return node_id

    for node in nodes:
        path = node["path"]
        if granularity == "directory":
            node["hierarchy_id"] = ensure_dir(path)
            continue
        parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
        parent = ensure_dir(parent_path)
        node_id = f"hier::file::{path}"
        entry = {
            "id": node_id,
            "parent": parent,
            "kind": "file",
            "label": path.rsplit("/", 1)[-1],
            "path": path,
            "file": path,
            "node_id": node["id"],
            "depth": path.count("/") + 1,
            "child_count": 0,
            "symbol_count": node.get("symbol_count", 0),
            "doi": node.get("importance", 0.0),
            "open_by_default": bool(node.get("is_root")),
            "external": False,
        }
        by_id[node_id] = entry
        hierarchy_nodes.append(entry)
        node["hierarchy_id"] = node_id

    for node in hierarchy_nodes:
        parent = node.get("parent")
        if parent and parent in by_id:
            by_id[parent]["child_count"] += 1

    return {
        "root": "hier::root",
        "nodes": hierarchy_nodes,
        "open_files": [
            n["path"] for n in nodes if n.get("is_root") and granularity == "file"
        ],
        "source_root": "",
    }


def _attach_routes(
    edges: List[Dict[str, Any]], hierarchy_of: Dict[str, str], hierarchy: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Containment route per edge, for hierarchical edge bundling in the UI."""
    parent_by_id = {n["id"]: n.get("parent") for n in hierarchy["nodes"]}
    kind_by_id = {n["id"]: n.get("kind") for n in hierarchy["nodes"]}

    def chain(node_id: str) -> List[str]:
        out: List[str] = []
        current: Optional[str] = node_id
        while current:
            out.append(current)
            current = parent_by_id.get(current)
        return out

    for edge in edges:
        source_hid = hierarchy_of.get(edge["source"], "hier::root")
        target_hid = hierarchy_of.get(edge["target"], "hier::root")
        source_chain = chain(source_hid)
        target_chain = chain(target_hid)
        target_index = {node_id: i for i, node_id in enumerate(target_chain)}
        path, lca = [source_hid, target_hid], ""
        for i, node_id in enumerate(source_chain):
            if node_id in target_index:
                path = source_chain[: i + 1] + list(
                    reversed(target_chain[: target_index[node_id]])
                )
                lca = node_id
                break
        edge["source_hierarchy"] = source_hid
        edge["target_hierarchy"] = target_hid
        edge["bundle_path"] = path
        edge["bundle_lca"] = lca
        edge["bundle_lca_kind"] = kind_by_id.get(lca) or ""
    return edges


def _mermaid(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> str:
    lines = ["graph LR"]
    for node in nodes:
        label = node["short"].replace('"', "'").replace("[", "(").replace("]", ")")
        lines.append(f'  {node["id"]}["{label}"]')  # noqa: B907
    for edge in edges:
        lines.append(f'  {edge["source"]} -->|{edge["weight"]}| {edge["target"]}')
    lines.append("  classDef default fill:#ffffff,stroke:#c9ccd1,color:#1f2937;")
    return "\n".join(lines)


def build_modulemap(
    graph: CodeGraph,
    focus: Optional[str] = None,
    granularity: str = "auto",
    depth: int = 2,
    max_nodes: int = 60,
    repo_dir: Optional[str] = None,
    include_tests: bool = False,
    repo_commit: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the repo's module dependency graph, projected from symbol references.

    Args:
        graph: the repo's loaded :class:`CodeGraph`.
        focus: a file or directory to centre on; ``None`` shows the busiest
            modules repo-wide.
        granularity: ``"file"``, ``"directory"``, or ``"auto"`` (files for a
            small repo, directories once it exceeds ``_AUTO_FILE_LIMIT``).
        depth: hops from *focus* (clamped 1..4); ignored without a focus.
        max_nodes: module cap (clamped 2..200).
        include_tests: include test files, which otherwise dominate the
            coupling ranking of a well-tested repo.
    """
    granularity = granularity if granularity in GRANULARITIES else "auto"
    depth = max(1, min(int(depth), 4))
    max_nodes = max(2, min(int(max_nodes), 200))
    derived = _DerivedFiles(repo_dir, repo_commit)
    # Historical graphs cannot use manifest data from the current checkout.
    entries = [] if repo_commit else discover_entry_points(repo_dir)
    resolved = _resolve_granularity(graph, granularity, derived, include_tests)
    projection = _project(graph, resolved, derived, include_tests)
    candidates = projection.degree()

    if not candidates:
        return {
            "available": False,
            "granularity": resolved,
            "focus": "",
            "nodes": [],
            "edges": [],
            "hierarchy": {"root": "hier::root", "nodes": [], "open_files": []},
            "mermaid": "",
            "note": "No file-attributed symbols in this repo's graph.",
        }

    note = ""
    root: Optional[str] = None
    if focus:
        root = _resolve_focus(sorted(candidates), focus)
        if root is None:
            note = f"{focus!r} is not a module in this graph — showing the repo map."

    selected, truncated = _select(candidates, projection, root, depth, max_nodes)
    selected_set = set(selected)

    id_of = {module: f"m{i}" for i, module in enumerate(selected)}
    kept = [
        (pair, len(refs))
        for pair, refs in sorted(projection.pairs.items())
        if pair[0] in selected_set and pair[1] in selected_set
    ]
    importance, community, ref_count, entry = _score(selected, kept)

    hops = _hops_from(projection, root, selected_set) if root else {}
    # What the build manifest says this repo exposes, keyed to module granularity
    # so a directory rollup still marks the directory an entry lives in.
    entry_labels: Dict[str, str] = {}
    for declared in entries:
        entry_labels.setdefault(_module_of(declared.path, resolved), declared.label)
    nodes: List[Dict[str, Any]] = []
    for module in selected:
        label = _module_label(module, resolved)
        nodes.append(
            {
                "id": id_of[module],
                "name": module,
                "path": module,
                "label": label,
                "short": _short(label),
                "file": module if resolved == "file" else None,
                # ``line``/``end_line``/``depth`` are not meaningful for a module
                # but keep the node shape assignable to the codemap's, so the
                # existing graph renderers accept this payload unchanged. Line 1
                # lets a file module's peek open the top of the file.
                "line": 1 if resolved == "file" else None,
                "end_line": None,
                "depth": hops.get(module, 0),
                "kind": resolved,
                "symbol_count": projection.symbol_count.get(module, 0),
                "is_root": module == root,
                "external": False,
                "importance": importance[module],
                "community": community[module],
                "ref_count": ref_count[module],
                "entry_score": entry[module],
                # Non-null when the build manifest names this module, carrying
                # what it called it ("preact/compat"). Distinct from
                # ``entry_score``, which is a graph shape, not a declaration.
                "entry_point": entry_labels.get(module),
            }
        )

    edges: List[Dict[str, Any]] = []
    for (source, target), weight in kept:
        key = (source, target)
        anchors = projection.anchors.get(key, [])
        edges.append(
            {
                "source": id_of[source],
                "target": id_of[target],
                # Distinct symbol pairs behind this dependency — how coupled the
                # two modules are, not how often the call runs.
                "weight": weight,
                "call_sites": projection.anchor_total.get(key, 0),
                "anchors": anchors,
                "hidden_anchors": max(
                    0, projection.anchor_total.get(key, 0) - len(anchors)
                ),
            }
        )

    hierarchy = _hierarchy(nodes, resolved)
    _attach_routes(
        edges, {n["id"]: n.pop("hierarchy_id", "hier::root") for n in nodes}, hierarchy
    )

    return {
        "available": True,
        "granularity": resolved,
        "focus": root or "",
        "focus_label": ("" if root is None else _module_label(root, resolved)),
        "depth": depth if root else 0,
        "truncated": truncated,
        "total_modules": len(candidates),
        # Never let a filter hide silently — the UI can say what was left out.
        "excluded": {
            "test_files": projection.skipped_tests,
            "derived_files": projection.skipped_derived,
        },
        "nodes": nodes,
        "edges": edges,
        "hierarchy": hierarchy,
        "mermaid": _mermaid(nodes, edges),
        "note": note,
    }
