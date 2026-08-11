# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Repo-level compound graph model for code visualization.

The raw :class:`CodeGraph` already contains both structural ``contain`` edges
and reference/call edges, but most UI consumers want those relations separated:

``containment`` is a tree-like backbone (repo -> directory -> file -> symbol),
while ``dependencies`` are a graph overlay routed through that backbone.
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field
from typing import AbstractSet, Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE, is_symbol_node
from .code_graph import CodeGraph

HIERARCHY_ROOT = "hier::root"
EXTERNAL_FILE = "· external"
_CONTAINER_SYMBOL_TYPES = frozenset(
    {"class", "interface", "trait", "struct", "enum", "type", "module", "namespace"}
)


@dataclass
class ContainmentNode:
    id: str
    parent: Optional[str]
    kind: str
    label: str
    path: str = ""
    depth: int = 0
    child_count: int = 0
    symbol_count: int = 0
    doi: float = 0.0
    open_by_default: bool = False
    file: Optional[str] = None
    node_id: Optional[str] = None
    line: Optional[int] = None
    end_line: Optional[int] = None
    importance: float = 0.0
    external: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return {k: v for k, v in data.items() if v is not None}


@dataclass
class DependencyEdge:
    source: str
    target: str
    kind: str = EDGE_TYPE_REFERENCE
    weight: int = 1
    anchors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not self.anchors:
            data.pop("anchors", None)
        return data


@dataclass
class HierarchicalCodeGraph:
    """A compound graph: containment tree plus dependency overlay."""

    root: str
    containment: List[ContainmentNode]
    dependencies: List[DependencyEdge]
    source_root: str = ""

    def __post_init__(self) -> None:
        self._nodes_by_id = {node.id: node for node in self.containment}
        self._parent_by_id = {node.id: node.parent for node in self.containment}
        self._symbol_hierarchy_by_name = {
            node.node_id: node.id
            for node in self.containment
            if node.kind == "symbol" and node.node_id
        }

    @property
    def nodes_by_id(self) -> Dict[str, ContainmentNode]:
        return self._nodes_by_id

    def symbol_hierarchy_id(self, graph_name: str) -> Optional[str]:
        return self._symbol_hierarchy_by_name.get(graph_name)

    def route(self, source_hid: str, target_hid: str) -> Tuple[List[str], str]:
        """Route a dependency edge through the containment tree."""

        def chain(node_id: str) -> List[str]:
            out: List[str] = []
            cur: Optional[str] = node_id
            while cur:
                out.append(cur)
                cur = self._parent_by_id.get(cur)
            return out

        src_chain = chain(source_hid)
        tgt_chain = chain(target_hid)
        tgt_index = {node_id: i for i, node_id in enumerate(tgt_chain)}
        for i, node_id in enumerate(src_chain):
            if node_id in tgt_index:
                route = src_chain[: i + 1] + list(
                    reversed(tgt_chain[: tgt_index[node_id]])
                )
                return route, node_id
        return [source_hid, target_hid], ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "nodes": [node.to_dict() for node in self.containment],
            "dependencies": [edge.to_dict() for edge in self.dependencies],
            "source_root": self.source_root,
        }


def empty_hierarchy() -> Dict[str, Any]:
    return {"root": HIERARCHY_ROOT, "nodes": [], "open_files": []}


def _file_parts(file: str) -> List[str]:
    if file.startswith("·"):
        return [file]
    return [p for p in file.replace("\\", "/").split("/") if p]


def _common_source_root(files: Sequence[str]) -> Optional[str]:
    concrete = [
        _file_parts(f)
        for f in files
        if f and not f.startswith("·") and len(_file_parts(f)) > 1
    ]
    if not concrete:
        return None
    first = concrete[0][0]
    if all(p[0] == first for p in concrete):
        return first

    counts: Dict[str, int] = {}
    for parts in concrete:
        counts[parts[0]] = counts.get(parts[0], 0) + 1
    root, count = max(counts.items(), key=lambda item: item[1])
    # Repo roots often have setup.py/pyproject/docs next to one dominant package
    # directory. Keep that package out of labels when it clearly dominates.
    return root if count >= 2 and count / len(concrete) >= 0.6 else None


def _hierarchy_path(file: str, source_root: Optional[str]) -> List[str]:
    parts = _file_parts(file)
    if source_root and parts and parts[0] == source_root:
        return parts[1:]
    return parts


def _captured_source_path(
    file: str,
    repo_dir: Optional[str],
    source_paths: AbstractSet[str],
) -> Optional[str]:
    candidate = file.replace("\\", os.sep).replace("/", os.sep)
    if os.path.isabs(candidate):
        if not repo_dir:
            return None
        try:
            candidate = os.path.relpath(candidate, os.path.abspath(repo_dir))
        except ValueError:
            return None
    normalized = os.path.normpath(candidate)
    if normalized in {"", ".", ".."} or normalized.startswith(".." + os.sep):
        return None
    relative = normalized.replace(os.sep, "/")
    return relative if relative in source_paths else None


def _is_external_symbol(
    attrs: Dict[str, Any],
    repo_dir: Optional[str],
    source_paths: Optional[AbstractSet[str]] = None,
) -> bool:
    file = attrs.get("file")
    start = attrs.get("start_line")
    if not isinstance(file, str) or not file or not isinstance(start, int):
        return True
    if source_paths is not None:
        return _captured_source_path(file, repo_dir, source_paths) is None
    if repo_dir:
        safe = os.path.normpath(file).lstrip(os.sep).lstrip("/")
        return not os.path.isfile(os.path.join(repo_dir, safe))
    return False


def _file_key(
    attrs: Dict[str, Any],
    repo_dir: Optional[str],
    source_paths: Optional[AbstractSet[str]] = None,
) -> str:
    file = attrs.get("file")
    if not isinstance(file, str) or not file:
        return EXTERNAL_FILE
    if source_paths is not None:
        return _captured_source_path(file, repo_dir, source_paths) or EXTERNAL_FILE
    if _is_external_symbol(attrs, repo_dir):
        return EXTERNAL_FILE
    return file.replace("\\", "/")


def _symbol_label(attrs: Dict[str, Any], name: str) -> str:
    label = (attrs.get("unified_name") or "").strip() or name
    return label.split(":", 1)[-1] if ":" in label else label.rsplit("/", 1)[-1]


def _contains_line(parent: Dict[str, Any], child: Dict[str, Any]) -> bool:
    parent_start = parent.get("start_line")
    parent_end = parent.get("end_line")
    child_start = child.get("start_line")
    if not isinstance(parent_start, int) or not isinstance(child_start, int):
        return True
    if child_start < parent_start:
        return False
    return not isinstance(parent_end, int) or child_start <= parent_end


def _infer_symbol_parent(
    name: str,
    attrs: Dict[str, Any],
    names_in_file: Sequence[str],
    attrs_by_name: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """Infer scope from readable symbol names when explicit contain edges lack."""

    label = _symbol_label(attrs, name)
    if not label:
        return None

    best: Optional[str] = None
    best_len = -1
    for candidate in names_in_file:
        if candidate == name:
            continue
        c_attrs = attrs_by_name[candidate]
        if c_attrs.get("type") not in _CONTAINER_SYMBOL_TYPES:
            continue
        c_label = _symbol_label(c_attrs, candidate)
        if not c_label or len(c_label) <= best_len:
            continue
        if not (label.startswith(f"{c_label}.") or label.startswith(f"{c_label}::")):
            continue
        if not _contains_line(c_attrs, attrs):
            continue
        best = candidate
        best_len = len(c_label)
    return best


def _hier_dir_id(path: str) -> str:
    return f"hier::dir::{path}"


def _hier_file_id(file: str) -> str:
    return f"hier::file::{file}"


def _hier_symbol_id(name: str) -> str:
    return f"hier::symbol::{name}"


def _reference_importance(
    graph: CodeGraph, symbol_names: Iterable[str]
) -> Dict[str, float]:
    names = set(symbol_names)
    degree: Dict[str, int] = {name: 0 for name in names}
    g = graph.get_graph()
    for edge in g.es:
        if edge.attributes().get("type") != EDGE_TYPE_REFERENCE:
            continue
        src = g.vs[edge.source]["name"]
        tgt = g.vs[edge.target]["name"]
        if src in names:
            degree[src] += 1
        if tgt in names:
            degree[tgt] += 1
    max_degree = max(degree.values(), default=0)
    if max_degree <= 0:
        return {name: 0.0 for name in names}
    denom = math.log1p(max_degree)
    return {name: round(math.log1p(count) / denom, 4) for name, count in degree.items()}


def _recompute_counts(nodes: List[ContainmentNode]) -> None:
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        node.child_count = 0
        node.symbol_count = 1 if node.kind == "symbol" else 0
    for node in nodes:
        if node.parent in by_id:
            by_id[node.parent].child_count += 1
    for node in sorted(nodes, key=lambda n: n.depth, reverse=True):
        if node.parent in by_id:
            parent = by_id[node.parent]
            parent.symbol_count += node.symbol_count
            parent.doi = round(max(parent.doi, node.doi), 4)


def _assign_depths(nodes: List[ContainmentNode]) -> None:
    by_id = {node.id: node for node in nodes}

    def depth(node: ContainmentNode, seen: Optional[set[str]] = None) -> int:
        if node.parent is None or node.parent not in by_id:
            return 0
        if seen is None:
            seen = set()
        if node.id in seen:
            node.parent = None
            return 0
        seen.add(node.id)
        return depth(by_id[node.parent], seen) + 1

    for node in nodes:
        node.depth = depth(node)


def build_hierarchical_code_graph(
    graph: CodeGraph,
    repo_dir: Optional[str] = None,
    *,
    source_paths: Optional[AbstractSet[str]] = None,
) -> HierarchicalCodeGraph:
    """Build a reusable repo-level compound graph from a :class:`CodeGraph`."""

    g = graph.get_graph()
    symbol_vids: Dict[int, str] = {}
    attrs_by_name: Dict[str, Dict[str, Any]] = {}
    file_by_name: Dict[str, str] = {}
    for vertex in g.vs:
        attrs = vertex.attributes()
        if not is_symbol_node(attrs.get("type")):
            continue
        name = attrs.get("name")
        if not isinstance(name, str) or not name:
            continue
        symbol_vids[vertex.index] = name
        attrs_by_name[name] = attrs
        file_by_name[name] = _file_key(attrs, repo_dir, source_paths)

    source_root = _common_source_root(list(file_by_name.values()))
    importance = _reference_importance(graph, attrs_by_name.keys())

    nodes: List[ContainmentNode] = [
        ContainmentNode(
            id=HIERARCHY_ROOT,
            parent=None,
            kind="root",
            label="root",
            open_by_default=True,
        )
    ]
    by_id: Dict[str, ContainmentNode] = {HIERARCHY_ROOT: nodes[0]}

    def add(node: ContainmentNode) -> ContainmentNode:
        existing = by_id.get(node.id)
        if existing is not None:
            return existing
        by_id[node.id] = node
        nodes.append(node)
        return node

    def ensure_dirs(file: str) -> str:
        if file == EXTERNAL_FILE:
            add(
                ContainmentNode(
                    id=_hier_dir_id(EXTERNAL_FILE),
                    parent=HIERARCHY_ROOT,
                    kind="directory",
                    label="external",
                    path=EXTERNAL_FILE,
                    external=True,
                )
            )
            return _hier_dir_id(EXTERNAL_FILE)

        parent = HIERARCHY_ROOT
        accum: List[str] = []
        parts = _hierarchy_path(file, source_root)
        for part in parts[:-1]:
            accum.append(part)
            path = "/".join(accum)
            parent = add(
                ContainmentNode(
                    id=_hier_dir_id(path),
                    parent=parent,
                    kind="directory",
                    label=f"{path}/",
                    path=path,
                )
            ).id
        return parent

    for file in sorted(set(file_by_name.values())):
        parent = ensure_dirs(file)
        parts = _hierarchy_path(file, source_root)
        label = "external" if file == EXTERNAL_FILE else (parts[-1] if parts else file)
        add(
            ContainmentNode(
                id=_hier_file_id(file),
                parent=parent,
                kind="file",
                label=label,
                path=file,
                file=file,
                external=file == EXTERNAL_FILE,
            )
        )

    symbol_parent: Dict[str, str] = {}
    for edge in g.es:
        if edge.attributes().get("type") != EDGE_TYPE_CONTAIN:
            continue
        src = symbol_vids.get(edge.source)
        tgt = symbol_vids.get(edge.target)
        if not src or not tgt or src == tgt:
            continue
        if file_by_name.get(src) != file_by_name.get(tgt):
            continue
        symbol_parent.setdefault(tgt, _hier_symbol_id(src))

    names_by_file: Dict[str, List[str]] = {}
    for name, file in file_by_name.items():
        names_by_file.setdefault(file, []).append(name)
    for names in names_by_file.values():
        names.sort(
            key=lambda n: (
                (
                    attrs_by_name[n].get("start_line")
                    if isinstance(attrs_by_name[n].get("start_line"), int)
                    else 10**9
                ),
                n,
            )
        )
    for name, attrs in attrs_by_name.items():
        if name in symbol_parent:
            continue
        parent_name = _infer_symbol_parent(
            name,
            attrs,
            names_by_file.get(file_by_name.get(name, ""), []),
            attrs_by_name,
        )
        if parent_name:
            symbol_parent[name] = _hier_symbol_id(parent_name)

    for name in sorted(
        attrs_by_name,
        key=lambda n: (
            file_by_name.get(n, ""),
            (
                attrs_by_name[n].get("start_line")
                if isinstance(attrs_by_name[n].get("start_line"), int)
                else 10**9
            ),
            n,
        ),
    ):
        attrs = attrs_by_name[name]
        file = file_by_name[name]
        parent = symbol_parent.get(name) or _hier_file_id(file)
        if parent not in by_id:
            parent = _hier_file_id(file)
        start = attrs.get("start_line")
        end = attrs.get("end_line")
        imp = importance.get(name, 0.0)
        add(
            ContainmentNode(
                id=_hier_symbol_id(name),
                parent=parent,
                kind="symbol",
                label=_symbol_label(attrs, name),
                path=name,
                file=file,
                node_id=name,
                line=(start + 1) if isinstance(start, int) else None,
                end_line=(end + 1) if isinstance(end, int) else None,
                importance=imp,
                doi=imp,
                external=file == EXTERNAL_FILE,
            )
        )

    dependencies_by_key: Dict[Tuple[str, str, str], DependencyEdge] = {}
    for edge in g.es:
        attrs = edge.attributes()
        kind = attrs.get("type")
        if kind != EDGE_TYPE_REFERENCE:
            continue
        src = symbol_vids.get(edge.source)
        tgt = symbol_vids.get(edge.target)
        if not src or not tgt or src == tgt:
            continue
        key = (src, tgt, kind)
        dep = dependencies_by_key.setdefault(
            key, DependencyEdge(source=src, target=tgt, kind=kind, weight=0)
        )
        dep.weight += 1
        anchor_file = attrs.get("anchor_file")
        anchor_line = attrs.get("anchor_line")
        if anchor_file:
            anchor = {
                "file": anchor_file,
                "line": (anchor_line + 1) if isinstance(anchor_line, int) else None,
            }
            if anchor not in dep.anchors:
                dep.anchors.append(anchor)

    _assign_depths(nodes)
    _recompute_counts(nodes)

    return HierarchicalCodeGraph(
        root=HIERARCHY_ROOT,
        containment=nodes,
        dependencies=list(dependencies_by_key.values()),
        source_root=source_root or "",
    )


def _node_doi(node: Dict[str, Any]) -> float:
    if node.get("is_root"):
        return 1.0
    importance = float(node.get("importance") or 0.0)
    distance = int(node.get("depth") or 0)
    return round(max(0.0, importance - 0.18 * distance), 4)


def hierarchy_for_view(
    hgraph: HierarchicalCodeGraph,
    view_nodes: Sequence[Dict[str, Any]],
    *,
    max_open_files: int = 4,
    max_open_symbols: int = 16,
) -> Dict[str, Any]:
    """Project the repo-level hierarchy onto a codemap/page subgraph view.

    ``view_nodes`` use short frontend ids (``n0``/``n1``). The projection remaps
    visible symbol hierarchy ids to those ids so existing CodeGraph clients can
    route edges without knowing the graph's canonical symbol names.
    """

    view_by_graph_name = {
        node["name"]: node
        for node in view_nodes
        if isinstance(node.get("name"), str) and isinstance(node.get("id"), str)
    }
    graph_to_view = {node["name"]: node["id"] for node in view_by_graph_name.values()}
    focus_hids = [
        hid
        for name, node in view_by_graph_name.items()
        if node.get("is_root")
        for hid in [hgraph.symbol_hierarchy_id(name)]
        if hid
    ]

    def tree_distance(hid: Optional[str]) -> int:
        if not hid or not focus_hids:
            return 0
        distances: List[int] = []
        for focus_hid in focus_hids:
            route, _ = hgraph.route(hid, focus_hid)
            distances.append(max(0, len(route) - 1))
        return min(distances) if distances else 0

    def projected_doi(graph_name: Optional[str], node: ContainmentNode) -> float:
        if not graph_name:
            return float(node.doi or 0.0)
        view_node = view_by_graph_name.get(graph_name)
        if not view_node:
            return float(node.doi or 0.0)
        if view_node.get("is_root"):
            return 1.0
        importance = float(view_node.get("importance") or node.importance or 0.0)
        ref_distance = int(view_node.get("depth") or 0)
        containment_distance = tree_distance(hgraph.symbol_hierarchy_id(graph_name))
        return round(
            max(0.0, importance - 0.12 * ref_distance - 0.04 * containment_distance),
            4,
        )

    focus_files = {
        node.get("file")
        for node in view_nodes
        if node.get("is_root") and isinstance(node.get("file"), str)
    }
    visible_graph_names = set(graph_to_view)

    def remap_id(hid: str) -> str:
        if hid.startswith("hier::symbol::"):
            graph_name = hid[len("hier::symbol::") :]
            return f"hier::symbol::{graph_to_view.get(graph_name, graph_name)}"
        return hid

    needed: set[str] = {hgraph.root}
    for graph_name in visible_graph_names:
        hid = hgraph.symbol_hierarchy_id(graph_name)
        while hid:
            needed.add(hid)
            hid = (
                hgraph.nodes_by_id.get(hid).parent
                if hid in hgraph.nodes_by_id
                else None
            )

    projected: List[ContainmentNode] = []
    for node in hgraph.containment:
        if node.id not in needed:
            continue
        graph_name = node.node_id
        projected.append(
            ContainmentNode(
                id=remap_id(node.id),
                parent=remap_id(node.parent) if node.parent else None,
                kind=node.kind,
                label=node.label,
                path=node.path,
                depth=node.depth,
                file=node.file,
                node_id=(
                    graph_to_view.get(graph_name, graph_name) if graph_name else None
                ),
                line=node.line,
                end_line=node.end_line,
                importance=node.importance,
                doi=projected_doi(graph_name, node),
                external=node.external,
                open_by_default=node.open_by_default,
            )
        )

    _assign_depths(projected)
    _recompute_counts(projected)

    by_id = {node.id: node for node in projected}
    file_nodes = [
        node for node in projected if node.kind == "file" and not node.external
    ]
    file_nodes.sort(
        key=lambda n: (
            1 if n.file in focus_files else 0,
            float(n.doi or 0.0),
            -int(n.symbol_count or 0),
            n.path,
        ),
        reverse=True,
    )
    open_files: List[str] = []
    open_symbols = 0
    for node in file_nodes:
        if len(open_files) >= max_open_files or open_symbols >= max_open_symbols:
            break
        if not open_files or float(node.doi or 0.0) >= 0.25:
            node.open_by_default = True
            if node.file:
                open_files.append(node.file)
            open_symbols += int(node.symbol_count or 0)
            parent = node.parent
            while parent and parent in by_id:
                by_id[parent].open_by_default = True
                parent = by_id[parent].parent

    return {
        "root": hgraph.root,
        "nodes": [node.to_dict() for node in projected],
        "open_files": open_files,
        "source_root": hgraph.source_root,
    }


def hierarchy_from_view_nodes(nodes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Compatibility fallback when a repo-level hierarchy is unavailable."""

    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        file = node.get("file")
        key = (
            EXTERNAL_FILE
            if node.get("external") or not isinstance(file, str) or not file
            else file.replace("\\", "/")
        )
        by_file.setdefault(key, []).append(node)

    source_root = _common_source_root(list(by_file.keys()))
    hnodes: List[ContainmentNode] = [
        ContainmentNode(
            id=HIERARCHY_ROOT,
            parent=None,
            kind="root",
            label="root",
            open_by_default=True,
        )
    ]
    by_id = {HIERARCHY_ROOT: hnodes[0]}

    def add(node: ContainmentNode) -> ContainmentNode:
        existing = by_id.get(node.id)
        if existing is not None:
            return existing
        by_id[node.id] = node
        hnodes.append(node)
        return node

    for file in sorted(by_file):
        if file == EXTERNAL_FILE:
            parent = add(
                ContainmentNode(
                    id=_hier_dir_id(EXTERNAL_FILE),
                    parent=HIERARCHY_ROOT,
                    kind="directory",
                    label="external",
                    path=EXTERNAL_FILE,
                    external=True,
                )
            ).id
            rel_parts = [file]
        else:
            rel_parts = _hierarchy_path(file, source_root)
            parent = HIERARCHY_ROOT
            accum: List[str] = []
            for part in rel_parts[:-1]:
                accum.append(part)
                path = "/".join(accum)
                parent = add(
                    ContainmentNode(
                        id=_hier_dir_id(path),
                        parent=parent,
                        kind="directory",
                        label=f"{path}/",
                        path=path,
                    )
                ).id

        file_id = _hier_file_id(file)
        add(
            ContainmentNode(
                id=file_id,
                parent=parent,
                kind="file",
                label=rel_parts[-1] if rel_parts else file.rsplit("/", 1)[-1],
                path=file,
                file=file,
                doi=max((_node_doi(node) for node in by_file[file]), default=0.0),
                external=file == EXTERNAL_FILE,
            )
        )
        for node in sorted(
            by_file[file],
            key=lambda n: (0 if n.get("is_root") else 1, n.get("line") or 0, n["id"]),
        ):
            add(
                ContainmentNode(
                    id=f"hier::symbol::{node['id']}",
                    parent=file_id,
                    kind="symbol",
                    label=node.get("short") or node.get("label") or node["id"],
                    node_id=node["id"],
                    path=node.get("name") or node["id"],
                    file=file,
                    doi=_node_doi(node),
                    external=bool(node.get("external")),
                )
            )

    _assign_depths(hnodes)
    _recompute_counts(hnodes)

    focus_files = {
        node.get("file")
        for node in nodes
        if node.get("is_root") and isinstance(node.get("file"), str)
    }
    file_nodes = [node for node in hnodes if node.kind == "file" and not node.external]
    file_nodes.sort(
        key=lambda n: (
            1 if n.file in focus_files else 0,
            float(n.doi or 0.0),
            -int(n.symbol_count or 0),
            n.path,
        ),
        reverse=True,
    )
    open_files: List[str] = []
    open_symbols = 0
    for node in file_nodes:
        if len(open_files) >= 4 or open_symbols >= 16:
            break
        if not open_files or float(node.doi or 0.0) >= 0.25:
            node.open_by_default = True
            if node.file:
                open_files.append(node.file)
            open_symbols += int(node.symbol_count or 0)
            parent = node.parent
            while parent and parent in by_id:
                by_id[parent].open_by_default = True
                parent = by_id[parent].parent

    return {
        "root": HIERARCHY_ROOT,
        "nodes": [node.to_dict() for node in hnodes],
        "open_files": open_files,
        "source_root": source_root or "",
    }


def attach_edge_routes(
    edges: List[Dict[str, Any]], hierarchy: Dict[str, Any]
) -> List[Dict[str, Any]]:
    parent_by_id = {
        node["id"]: node.get("parent") for node in hierarchy.get("nodes", [])
    }
    kind_by_id = {node["id"]: node.get("kind") for node in hierarchy.get("nodes", [])}
    file_by_symbol = {
        node["node_id"]: node.get("file")
        for node in hierarchy.get("nodes", [])
        if node.get("kind") == "symbol" and node.get("node_id")
    }

    def chain(node_id: str) -> List[str]:
        out: List[str] = []
        cur: Optional[str] = node_id
        while cur:
            out.append(cur)
            cur = parent_by_id.get(cur)
        return out

    def route(source_id: str, target_id: str) -> Tuple[List[str], str]:
        src_chain = chain(source_id)
        tgt_chain = chain(target_id)
        tgt_index = {node_id: i for i, node_id in enumerate(tgt_chain)}
        for i, node_id in enumerate(src_chain):
            if node_id in tgt_index:
                return (
                    src_chain[: i + 1]
                    + list(reversed(tgt_chain[: tgt_index[node_id]])),
                    node_id,
                )
        return [source_id, target_id], ""

    for edge in edges:
        source_hid = f"hier::symbol::{edge['source']}"
        target_hid = f"hier::symbol::{edge['target']}"
        bundle_path, lca = route(source_hid, target_hid)
        edge["source_hierarchy"] = source_hid
        edge["target_hierarchy"] = target_hid
        edge["bundle_path"] = bundle_path
        edge["bundle_lca"] = lca
        edge["bundle_lca_kind"] = kind_by_id.get(lca) or ""
        edge["cross_file"] = file_by_symbol.get(edge["source"]) != file_by_symbol.get(
            edge["target"]
        )
    return edges
