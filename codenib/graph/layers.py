# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Layered graph views over a single :class:`CodeGraph`.

The persisted ``CodeGraph`` stays backward-compatible: vertices and edges still
live in one igraph instance. This module promotes relation layers such as
``containment`` and ``dependency`` to first-class query views on top of that
base graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from ..types import (
    GRAPH_LAYER_ALL,
    GRAPH_LAYER_CONTAINMENT,
    GRAPH_LAYER_DEPENDENCY,
    GRAPH_LAYER_EDGE_TYPES,
    GRAPH_LAYER_IMPORT,
    GRAPH_LAYER_REFERENCE,
    GRAPH_LAYER_TYPE_USE,
    edge_types_for_graph_layer,
)
from .code_graph import CodeGraph


@dataclass(frozen=True)
class GraphLayerSpec:
    """Definition of a named edge layer."""

    name: str
    edge_types: Optional[frozenset[str]]
    description: str = ""

    def matches(self, edge_type: Optional[str]) -> bool:
        return self.edge_types is None or edge_type in self.edge_types


@dataclass(frozen=True)
class LayerEdgeRef:
    """Edge record resolved through a layer view."""

    eid: int
    source_vid: int
    target_vid: int
    source_name: str
    target_name: str
    edge_kind: str
    anchor_file: Optional[str] = None
    anchor_line: Optional[int] = None


@dataclass(frozen=True)
class GraphLayerSummary:
    """Small summary for dashboards and tests."""

    name: str
    node_count: int
    edge_count: int
    edge_types: Optional[frozenset[str]]

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "edge_types": None if self.edge_types is None else sorted(self.edge_types),
        }


DEFAULT_LAYER_SPECS: Dict[str, GraphLayerSpec] = {
    GRAPH_LAYER_ALL: GraphLayerSpec(
        GRAPH_LAYER_ALL,
        GRAPH_LAYER_EDGE_TYPES[GRAPH_LAYER_ALL],
        "All edges in the base CodeGraph.",
    ),
    GRAPH_LAYER_CONTAINMENT: GraphLayerSpec(
        GRAPH_LAYER_CONTAINMENT,
        GRAPH_LAYER_EDGE_TYPES[GRAPH_LAYER_CONTAINMENT],
        "Structural directory, file, and symbol containment.",
    ),
    GRAPH_LAYER_DEPENDENCY: GraphLayerSpec(
        GRAPH_LAYER_DEPENDENCY,
        GRAPH_LAYER_EDGE_TYPES[GRAPH_LAYER_DEPENDENCY],
        "Semantic dependency overlay: references, imports, and type-use edges.",
    ),
    GRAPH_LAYER_REFERENCE: GraphLayerSpec(
        GRAPH_LAYER_REFERENCE,
        GRAPH_LAYER_EDGE_TYPES[GRAPH_LAYER_REFERENCE],
        "Call/use reference edges with optional source anchors.",
    ),
    GRAPH_LAYER_IMPORT: GraphLayerSpec(
        GRAPH_LAYER_IMPORT,
        GRAPH_LAYER_EDGE_TYPES[GRAPH_LAYER_IMPORT],
        "Import edges when a decoder emits them separately.",
    ),
    GRAPH_LAYER_TYPE_USE: GraphLayerSpec(
        GRAPH_LAYER_TYPE_USE,
        GRAPH_LAYER_EDGE_TYPES[GRAPH_LAYER_TYPE_USE],
        "Type-use edges when a decoder emits them separately.",
    ),
}


class LayerGraphView:
    """A read-only view of one layer in a :class:`MultiGraphIndex`."""

    def __init__(self, index: "MultiGraphIndex", spec: GraphLayerSpec):
        self._index = index
        self.spec = spec

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def edge_ids(self) -> List[int]:
        return list(self._index.edge_ids(self.name))

    @property
    def node_ids(self) -> List[int]:
        nodes = set()
        graph = self._index.code_graph.graph
        for eid in self._index.edge_ids(self.name):
            edge = graph.es[eid]
            nodes.add(edge.source)
            nodes.add(edge.target)
        return sorted(nodes)

    def iter_edges(self) -> Iterable[LayerEdgeRef]:
        graph = self._index.code_graph.graph
        for eid in self._index.edge_ids(self.name):
            yield _edge_ref(self._index.code_graph, eid, graph)

    def neighbor_edges(
        self, node_name: str, direction: str = "forward"
    ) -> List[LayerEdgeRef]:
        """Return layer edges incident to ``node_name``.

        ``direction`` accepts ``forward``, ``backward``, or ``all``.
        """
        vertex_id = self._index.code_graph.name_to_vertex.get(node_name)
        if vertex_id is None:
            return []
        if direction not in {"forward", "backward", "all"}:
            raise ValueError("direction must be 'forward', 'backward', or 'all'")

        edge_ids = set(self._index.edge_ids(self.name))
        graph = self._index.code_graph.graph
        out: List[LayerEdgeRef] = []
        for eid in edge_ids:
            edge = graph.es[eid]
            if direction == "forward" and edge.source != vertex_id:
                continue
            if direction == "backward" and edge.target != vertex_id:
                continue
            if direction == "all" and vertex_id not in {edge.source, edge.target}:
                continue
            out.append(_edge_ref(self._index.code_graph, eid, graph))
        out.sort(key=lambda ref: ref.eid)
        return out

    def to_igraph(self, delete_vertices: bool = True):
        """Materialize this layer as an igraph subgraph."""
        return self._index.code_graph.graph.subgraph_edges(
            self.edge_ids, delete_vertices=delete_vertices
        )

    def summary(self) -> GraphLayerSummary:
        return GraphLayerSummary(
            name=self.name,
            node_count=len(self.node_ids),
            edge_count=len(self.edge_ids),
            edge_types=self.spec.edge_types,
        )


class MultiGraphIndex:
    """Layer index for a :class:`CodeGraph`.

    The index stores edge-id lists per layer. Layers can overlap; today every
    ``reference`` edge also belongs to ``dependency`` and ``all``.
    """

    def __init__(
        self,
        code_graph: CodeGraph,
        specs: Optional[Mapping[str, GraphLayerSpec]] = None,
        *,
        use_core: bool = True,
    ):
        self.code_graph = code_graph
        self.specs = _normalize_specs(specs or DEFAULT_LAYER_SPECS)
        self._edge_ids_by_layer = classify_edge_layers(
            _edge_types(code_graph),
            self.specs,
            use_core=use_core,
        )

    def layer_names(self) -> List[str]:
        return list(self.specs)

    def edge_ids(self, layer: str) -> List[int]:
        name = _normalize_layer_name(layer)
        if name not in self.specs:
            supported = ", ".join(sorted(self.specs))
            raise ValueError(f"Unknown graph layer {layer!r}; supported: {supported}")
        return self._edge_ids_by_layer.get(name, [])

    def get(self, layer: str) -> LayerGraphView:
        name = _normalize_layer_name(layer)
        if name not in self.specs:
            supported = ", ".join(sorted(self.specs))
            raise ValueError(f"Unknown graph layer {layer!r}; supported: {supported}")
        return LayerGraphView(self, self.specs[name])

    def summary(self) -> Dict[str, GraphLayerSummary]:
        return {name: self.get(name).summary() for name in self.specs}


def build_graph_layers(
    code_graph: CodeGraph,
    specs: Optional[Mapping[str, GraphLayerSpec]] = None,
    *,
    use_core: bool = True,
) -> MultiGraphIndex:
    """Build a layer index for ``code_graph``."""
    return MultiGraphIndex(code_graph, specs, use_core=use_core)


def classify_edge_layers(
    edge_types: Sequence[Optional[str]],
    specs: Optional[Mapping[str, GraphLayerSpec]] = None,
    *,
    use_core: bool = True,
) -> Dict[str, List[int]]:
    """Classify edge ids into overlapping layer buckets."""
    specs = _normalize_specs(specs or DEFAULT_LAYER_SPECS)
    if use_core and _is_default_specs(specs):
        accelerated = _classify_edge_layers_cpp(edge_types)
        if accelerated is not None:
            return accelerated
    return _classify_edge_layers_python(edge_types, specs)


def _normalize_specs(
    specs: Mapping[str, GraphLayerSpec],
) -> Dict[str, GraphLayerSpec]:
    normalized: Dict[str, GraphLayerSpec] = {}
    for name, spec in specs.items():
        layer = _normalize_layer_name(name)
        if layer in GRAPH_LAYER_EDGE_TYPES:
            edge_types = edge_types_for_graph_layer(layer)
        else:
            edge_types = spec.edge_types
        if spec.name != layer or spec.edge_types != edge_types:
            spec = GraphLayerSpec(layer, edge_types, spec.description)
        normalized[layer] = spec
    return normalized


def _normalize_layer_name(layer: str) -> str:
    normalized = (layer or "").strip().lower()
    if not normalized:
        raise ValueError("Graph layer name must be non-empty")
    return normalized


def _is_default_specs(specs: Mapping[str, GraphLayerSpec]) -> bool:
    return set(specs) == set(DEFAULT_LAYER_SPECS) and all(
        specs[name].edge_types == DEFAULT_LAYER_SPECS[name].edge_types
        for name in DEFAULT_LAYER_SPECS
    )


def _edge_types(code_graph: CodeGraph) -> List[Optional[str]]:
    return [edge.attributes().get("type") for edge in code_graph.graph.es]


def _classify_edge_layers_python(
    edge_types: Sequence[Optional[str]],
    specs: Mapping[str, GraphLayerSpec],
) -> Dict[str, List[int]]:
    out = {name: [] for name in specs}
    for eid, edge_type in enumerate(edge_types):
        for name, spec in specs.items():
            if spec.matches(edge_type):
                out[name].append(eid)
    return out


def _classify_edge_layers_cpp(
    edge_types: Sequence[Optional[str]],
) -> Optional[Dict[str, List[int]]]:
    try:
        import codenib_core as _cpp  # type: ignore[import-not-found]
    except ImportError:
        return None
    classify = getattr(_cpp, "classify_edge_layers", None)
    if classify is None:
        return None
    result = classify([edge_type or "" for edge_type in edge_types])
    return {name: list(result.get(name, [])) for name in DEFAULT_LAYER_SPECS}


def _edge_ref(code_graph: CodeGraph, eid: int, graph=None) -> LayerEdgeRef:
    graph = graph or code_graph.graph
    edge = graph.es[eid]
    attrs = edge.attributes()
    source = graph.vs[edge.source]
    target = graph.vs[edge.target]
    return LayerEdgeRef(
        eid=eid,
        source_vid=edge.source,
        target_vid=edge.target,
        source_name=source["name"],
        target_name=target["name"],
        edge_kind=attrs.get("type", ""),
        anchor_file=attrs.get("anchor_file"),
        anchor_line=attrs.get("anchor_line"),
    )
