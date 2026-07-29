# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import bisect
import pickle
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import igraph as ig

from .. import compat_pickle
from ..types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    GRAPH_LAYER_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_DIRECTORY,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
    edge_types_for_graph_layer,
    is_symbol_node,
)

# Bump when the persisted graph schema changes (vertex/edge attributes,
# top-level pickle keys). load_graph() refuses mismatching pickles so stale
# caches surface as a loud error instead of silent attribute drift.
#
# v2 (main): adds file_to_vertices index + new pickle keys
# v3 (this branch): edges carry anchor_file / anchor_line; range indexes
#                   (file_nodes / file_edge_anchors) pickled with graph;
#                   unified_to_names aux dict (display -> identity names)
# v4: symbol vertices persist the exact SCIP declaration occurrence as
#     selection_line, independently from the symbol scope range.
_SCHEMA_VERSION = 4


@dataclass(slots=True)
class NodeRef:
    """Resolved vertex record returned by range/symbol queries.

    Carries identity (`name`), display (`unified_name`), span, and kind so
    consumers don't poke `graph.vs[vid][...]` directly.
    """

    vid: int
    name: str
    unified_name: str
    file: str
    start_line: int
    end_line: int
    kind: str


@dataclass(slots=True)
class EdgeRef:
    """Resolved edge record returned by range queries.

    `anchor_file`/`anchor_line` are populated only for REFERENCE edges
    (see anchor invariants in `query_range` docstring).
    """

    eid: int
    source_vid: int
    target_vid: int
    edge_kind: str
    anchor_file: Optional[str] = None
    anchor_line: Optional[int] = None


@dataclass(slots=True)
class RangeQueryResult:
    """Result of CodeGraph.query_range — typed records, not raw ids.

    Anchor invariants (also enforced in `query_range`'s docstring):
        (i)   anchor_file == source vertex's file (anchor lives at the call
              site, never the target).
        (ii)  anchor_file/anchor_line are meaningful only on REFERENCE edges;
              CONTAIN edges leave both fields None.
        (iii) outgoing ∩ incoming = ∅ — query_range filters self-edges
              (source == target) out of `incoming` so a recursive call
              site appears once, in `outgoing`.
    """

    defined: List[NodeRef] = field(default_factory=list)
    outgoing: List[EdgeRef] = field(default_factory=list)
    incoming: List[EdgeRef] = field(default_factory=list)


class CodeGraph:
    """
    A class to represent and manipulate a code graph using igraph.
    start_line uses 0-based index.
    """

    def __init__(self, project_root=None):
        # Create a directed graph
        self.graph = ig.Graph(directed=True)
        self.current_file = None
        self.current_scope = None
        self.project_root = project_root
        self.scope_stack = []  # List of {symbol: [start_line, end_line]} dicts
        # Store line ranges for symbols
        self.symbol_ranges = {}
        # Map symbol names to vertex IDs
        self.name_to_vertex = {}

        # Range indexes — per-file, populated by build_range_indexes() after the
        # graph is fully built or after a patcher batch. Pickled with the graph.
        # _file_nodes[file] -> list of (start_line, end_line, vid), unsorted.
        # _file_edge_anchors[file] -> sorted list of (anchor_line, eid).
        self._file_nodes: Dict[str, List[Tuple[int, int, int]]] = {}
        self._file_edge_anchors: Dict[str, List[Tuple[int, int]]] = {}

        # Aux: unified_name (display, may collide) -> list of identity names.
        # Populated by build_range_indexes(). Pickled with the graph.
        # Consumers disambiguate via file context (this layer just lists candidates).
        self._unified_to_names: Dict[str, List[str]] = {}

        # Edge dedup index: (src_id, tgt_id, type, anchor_file, anchor_line) -> eid.
        # Populated lazily on first _add_edge / invalidated to None when callers
        # delete edges or vertices (igraph compacts eids on delete, so cached
        # eids would point at wrong edges). Lookup via _ensure_edge_index().
        # Not pickled — rebuilt on demand after load.
        self._edge_index: Optional[Dict[Tuple, int]] = None

        # Edge insertion buffer, active only inside a `batch_edges()` block.
        # igraph rebuilds its adjacency index on every `add_edges` call, so
        # inserting one edge at a time is O(E^2). Decoders wrap their build
        # loop in `batch_edges()` to defer insertion into a single add_edges
        # flush (O(E)). When None, `_add_edge` inserts immediately as before.
        self._edge_buffer: Optional[List[Tuple[int, int]]] = None
        self._edge_attr_buffer: Optional[List[Tuple]] = None

    def add_file_node(self, file_path):
        """
        Add a file node to the graph and set it as the current file and scope.

        Args:
            file_path: Path of the file to add
        """
        self.current_file = file_path

        # Add vertex for file
        self._add_vertex(file_path, {"type": NODE_TYPE_FILE})

        self.current_scope = file_path
        # File scope has no range (special case)
        self.scope_stack = [{file_path: None}]

    def add_symbol_node(
        self, symbol, line, scope_start_line=None, scope_end_line=None, symbol_type=None
    ):
        """
        Add a symbol node to the graph.

        Args:
            symbol: Symbol name
            line: Line number of the symbol
            scope_start_line: Start line of the symbol's scope (optional)
            scope_end_line: End line of the symbol's scope (optional)
            symbol_type: Type of symbol
                (NODE_TYPE_CLASS, NODE_TYPE_METHOD, NODE_TYPE_FUNCTION) (optional)
        """
        # Use specific symbol type if provided, otherwise default to generic symbol
        node_type = symbol_type if symbol_type else NODE_TYPE_SYMBOL

        if scope_start_line is not None and scope_end_line is not None:
            # Store symbol range
            self.symbol_ranges[symbol] = (scope_start_line, scope_end_line)

            # Add symbol vertex with scope range
            self._add_vertex(
                symbol,
                {
                    "type": node_type,
                    "file": self.current_file,
                    "start_line": scope_start_line,
                    "end_line": scope_end_line,
                    "selection_line": line,
                },
            )
        else:
            # Add symbol vertex without scope range
            self._add_vertex(
                symbol,
                {
                    "type": node_type,
                    "file": self.current_file,
                    "start_line": line,
                    "end_line": line,
                    "selection_line": line,
                },
            )

    def add_symbol_reference(
        self,
        symbol,
        module_path=None,
        symbol_type=None,
        anchor_file: Optional[str] = None,
        anchor_line: Optional[int] = None,
    ):
        """
        Add a reference to a symbol.

        Args:
            symbol: Symbol being referenced
            module_path: Path of the module containing the symbol (optional)
            symbol_type: Type of symbol
                (NODE_TYPE_CLASS, NODE_TYPE_METHOD, NODE_TYPE_FUNCTION) (optional)
            anchor_file: File where the reference site is located (optional)
            anchor_line: 0-based line of the reference site (optional)
        """
        # If the symbol doesn't exist, create it without range info
        if symbol not in self.name_to_vertex:
            file_attr = module_path if module_path else None
            node_type = symbol_type if symbol_type else NODE_TYPE_SYMBOL
            self._add_vertex(symbol, {"type": node_type, "file": file_attr})

        # Add reference edge with anchor info (defaults: current file)
        if anchor_file is None:
            anchor_file = self.current_file
        self._add_edge(
            self.current_scope,
            symbol,
            EDGE_TYPE_REFERENCE,
            anchor_file=anchor_file,
            anchor_line=anchor_line,
        )

    def update_current_scope(self, symbol, start_line=None, end_line=None):
        """
        Update the current scope to the given symbol with its range.

        Args:
            symbol: Symbol to set as current scope
            start_line: Start line of the symbol's scope
            end_line: End line of the symbol's scope
        """
        self.current_scope = symbol
        # Add symbol with its range to scope stack
        scope_range = (
            [start_line, end_line]
            if start_line is not None and end_line is not None
            else None
        )
        self.scope_stack.append({symbol: scope_range})

    def exit_scopes_by_line(self, current_line):
        """
        Exit scopes that have ended based on current line number.
        File scope (with None range) is never popped.

        Args:
            current_line: Current line number being processed
        """
        # Pop scopes whose range has ended
        while len(self.scope_stack) > 1:  # Keep at least the file scope
            top_scope_dict = self.scope_stack[-1]
            scope_symbol = list(top_scope_dict.keys())[0]
            scope_range = top_scope_dict[scope_symbol]

            # If scope has no range (file scope), don't pop
            if scope_range is None:
                break

            # If current line is beyond scope's end, pop it
            start_line, end_line = scope_range
            if current_line > end_line:
                self.scope_stack.pop()
                # Update current_scope to new top of stack
                if self.scope_stack:
                    new_top = self.scope_stack[-1]
                    self.current_scope = list(new_top.keys())[0]
                else:
                    self.current_scope = self.current_file
            else:
                # Scope is still active
                break

    def add_containment_edge(self, target_symbol):
        """
        Add a containment edge from current scope to a symbol.

        CONTAIN edges deliberately carry no anchor — containment is a
        structural relation, not a call/reference site. (See anchor invariant
        ii on `query_range`.)

        Args:
            target_symbol: Symbol being contained.
        """
        # Use the current scope directly (not parent scope)
        self._add_edge(self.current_scope, target_symbol, EDGE_TYPE_CONTAIN)

    def _add_vertex(self, name, attributes=None):
        """
        Add a vertex to the graph if it doesn't exist.

        Args:
            name: Name of the vertex
            attributes: Dictionary of vertex attributes

        Returns:
            Vertex ID
        """
        if name in self.name_to_vertex:
            vertex_id = self.name_to_vertex[name]
            # Update attributes if provided
            if attributes:
                for key, value in attributes.items():
                    self.graph.vs[vertex_id][key] = value
            return vertex_id

        # Add a new vertex
        self.graph.add_vertices(1)
        vertex_id = self.graph.vcount() - 1
        self.name_to_vertex[name] = vertex_id

        # Set the name attribute
        self.graph.vs[vertex_id]["name"] = name

        # Set other attributes if provided
        if attributes:
            for key, value in attributes.items():
                self.graph.vs[vertex_id][key] = value

        return vertex_id

    def _add_edge(
        self,
        source_name,
        target_name,
        edge_type,
        anchor_file: Optional[str] = None,
        anchor_line: Optional[int] = None,
    ):
        """
        Add an edge between two vertices.

        Anchored edges (those carrying ``anchor_file`` / ``anchor_line``) are
        intentionally multi-edge — each call site keeps its own edge so range
        queries can address them individually. Pass ``anchor_file`` /
        ``anchor_line`` to record where the reference originated (e.g., the
        line of a call site); these feed ``build_range_indexes()``.

        Structural edges WITHOUT anchor info (CONTAIN edges, file/dir
        containment, etc.) are deduped by ``(source, target)`` — repeating
        them adds no information and would inflate the graph for languages
        like Rust where SCIP records the same containment site twice (struct
        decl + impl block). The C++ ``batch_add_edges`` does the same dedup
        for parity.

        Args:
            source_name: Name of the source vertex
            target_name: Name of the target vertex
            edge_type: Type of the edge (e.g., "reference", "contain")
            anchor_file: File containing the call/reference site (optional)
            anchor_line: 0-based line number of the call/reference site (optional)

        Returns:
            Edge ID of the newly created (or already-existing) edge.
        """
        # Make sure both vertices exist
        source_id = (
            self._add_vertex(source_name)
            if source_name not in self.name_to_vertex
            else self.name_to_vertex[source_name]
        )
        target_id = (
            self._add_vertex(target_name)
            if target_name not in self.name_to_vertex
            else self.name_to_vertex[target_name]
        )

        # Dedup by (src, tgt, type, anchor_file, anchor_line) via O(1) hash
        # lookup. Structural edges (no anchor) collapse on (src, tgt, type).
        # Mirrors C++ add_edge / batch_add_edges.
        if self._edge_index is None:
            self._rebuild_edge_index()
        key = (source_id, target_id, edge_type, anchor_file, anchor_line)
        existing = self._edge_index.get(key)
        if existing is not None:
            return existing

        # Inside a batch_edges() block: buffer the insertion instead of calling
        # igraph per edge (which is O(E^2)). The provisional eid is the position
        # the edge will occupy after flush — current ecount is unchanged while
        # buffering, so it equals ecount + buffer length. Decoders don't use the
        # returned eid, and dedup stays correct because the index is updated now.
        if self._edge_buffer is not None:
            edge_id = self.graph.ecount() + len(self._edge_buffer)
            self._edge_buffer.append((source_id, target_id))
            self._edge_attr_buffer.append((edge_type, anchor_file, anchor_line))
            self._edge_index[key] = edge_id
            return edge_id

        self.graph.add_edges([(source_id, target_id)])
        edge_id = self.graph.ecount() - 1

        # Set edge attributes
        self.graph.es[edge_id]["type"] = edge_type
        if anchor_file is not None:
            self.graph.es[edge_id]["anchor_file"] = anchor_file
        if anchor_line is not None:
            self.graph.es[edge_id]["anchor_line"] = anchor_line

        self._edge_index[key] = edge_id
        return edge_id

    @contextmanager
    def batch_edges(self):
        """Defer igraph edge insertion until the block exits, flushing all
        buffered edges in a single ``add_edges`` call.

        igraph rebuilds its internal adjacency index on every ``add_edges``
        call, so inserting one edge at a time (as bare ``_add_edge`` does) is
        O(E^2) in the number of edges — the dominant cost when decoding a large
        repo. Wrapping a build loop in this context collapses that to O(E):

            with graph.batch_edges():
                # ... many add_symbol_reference / _add_edge calls ...

        Edge dedup is unaffected (the dedup index is updated as edges are
        buffered). Edge *ids* returned by ``_add_edge`` while buffering are
        provisional and only become real after flush; no caller relies on them
        mid-build. Outside this block ``_add_edge`` inserts immediately, so the
        incremental patcher and all other callers are unchanged. Nesting is a
        no-op (the outermost block owns the flush)."""
        if self._edge_buffer is not None:
            # Already batching — let the outer block own the buffer/flush.
            yield
            return
        if self._edge_index is None:
            self._rebuild_edge_index()
        self._edge_buffer = []
        self._edge_attr_buffer = []
        try:
            yield
        finally:
            self._flush_edge_buffer()

    def _flush_edge_buffer(self) -> None:
        """Insert all buffered edges in one ``add_edges`` call, then assign
        per-edge attributes. Attribute assignment mirrors the immediate path
        exactly (anchor_* set only when not None), and does not trigger the
        adjacency rebuild that makes per-edge ``add_edges`` quadratic."""
        pairs = self._edge_buffer
        attrs = self._edge_attr_buffer
        self._edge_buffer = None
        self._edge_attr_buffer = None
        if not pairs:
            return
        base = self.graph.ecount()
        self.graph.add_edges(pairs)
        es = self.graph.es
        for offset, (edge_type, anchor_file, anchor_line) in enumerate(attrs):
            eid = base + offset
            es[eid]["type"] = edge_type
            if anchor_file is not None:
                es[eid]["anchor_file"] = anchor_file
            if anchor_line is not None:
                es[eid]["anchor_line"] = anchor_line

    def _rebuild_edge_index(self) -> None:
        """Rebuild the edge dedup index by walking all current edges. O(E).
        Called lazily after construct/load and after any operation that
        invalidates eids (delete_edges / delete_vertices)."""
        idx: Dict[Tuple, int] = {}
        for e in self.graph.es:
            attrs = e.attributes()
            key = (
                e.source,
                e.target,
                attrs.get("type"),
                attrs.get("anchor_file"),
                attrs.get("anchor_line"),
            )
            # Earlier eid wins on duplicate keys; igraph batch paths can
            # produce parallel edges with identical keys before this index
            # existed, so first-write semantics keeps the index stable.
            idx.setdefault(key, e.index)
        self._edge_index = idx

    def _invalidate_edge_index(self) -> None:
        """Mark the edge index stale. Next _add_edge will rebuild it.
        Call after any igraph delete_edges/delete_vertices — eids shift on
        delete and cached values would point at wrong edges."""
        self._edge_index = None

    # ------------------------------------------------------------------
    # Range indexes (LSP-aligned line-range queries)
    # ------------------------------------------------------------------

    def build_range_indexes(self) -> None:
        """Rebuild per-file line-range indexes from current graph state.

        Call this after the graph is fully constructed by a decoder, and
        again after any incremental patcher batch. The two indexes are
        pickled with the graph by `save_graph`, so subsequent `load_graph`
        calls do not need to rebuild.

        Cost: O(V + E) — one linear pass over vertices and edges.
        """
        nodes: Dict[str, List[Tuple[int, int, int]]] = defaultdict(list)
        for vid in range(self.graph.vcount()):
            v = self.graph.vs[vid]
            attrs = v.attributes()
            f = attrs.get("file")
            s = attrs.get("start_line")
            e = attrs.get("end_line")
            if f and s is not None and e is not None:
                nodes[f].append((s, e, vid))
        self._file_nodes = dict(nodes)

        edges: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        for eid in range(self.graph.ecount()):
            edge = self.graph.es[eid]
            attrs = edge.attributes()
            f = attrs.get("anchor_file")
            line = attrs.get("anchor_line")
            if f and line is not None:
                edges[f].append((line, eid))
        for f in edges:
            edges[f].sort()
        self._file_edge_anchors = dict(edges)

        unified: Dict[str, List[str]] = defaultdict(list)
        for vid in range(self.graph.vcount()):
            v = self.graph.vs[vid]
            uname = v.attributes().get("unified_name")
            if uname:
                unified[uname].append(v["name"])
        self._unified_to_names = dict(unified)
        self._unified_index_cache = None

    def merge_from(self, other: "CodeGraph") -> None:
        """Merge another ``CodeGraph`` into this one.

        Vertices are keyed by their canonical ``name`` attribute. Existing
        vertices receive any attributes from ``other``; missing vertices are
        inserted. Edges are copied through ``_add_edge`` so structural edge
        deduplication and anchored reference multi-edges keep the same
        semantics as decoder-built graphs.
        """
        for vertex in other.graph.vs:
            attrs = vertex.attributes()
            name = attrs.get("name")
            if not name:
                continue
            copied = {key: value for key, value in attrs.items() if key != "name"}
            self._add_vertex(name, copied)

        with self.batch_edges():
            for edge in other.graph.es:
                attrs = edge.attributes()
                edge_type = attrs.get("type")
                if edge_type is None:
                    continue
                source_name = other.graph.vs[edge.source]["name"]
                target_name = other.graph.vs[edge.target]["name"]
                self._add_edge(
                    source_name,
                    target_name,
                    edge_type,
                    anchor_file=attrs.get("anchor_file"),
                    anchor_line=attrs.get("anchor_line"),
                )

        self.symbol_ranges.update(other.symbol_ranges)
        self.build_range_indexes()

    def query_range(
        self,
        file: str,
        start_line: int,
        end_line: int,
        kinds: Optional[set] = None,
        depth: int = 1,
        *,
        layer: Optional[str] = None,
    ) -> RangeQueryResult:
        """Query the graph by source-file line range (LSP-aligned).

        Args:
            file: source file (graph-relative path).
            start_line, end_line: inclusive 0-based line span.
            kinds: edge kinds to include in `outgoing` AND `incoming`.
                Defaults to `{EDGE_TYPE_REFERENCE}` — CONTAIN edges have no
                meaningful anchor and are excluded by default. Pass an
                explicit set (e.g. `{EDGE_TYPE_REFERENCE, EDGE_TYPE_CONTAIN}`)
                to opt in.
            depth: reserved for future multi-hop expansion. Only `depth=1`
                is supported in v1; raises NotImplementedError otherwise.
            layer: optional named graph layer (`reference`, `dependency`,
                `containment`, `all`, ...). Mutually exclusive with ``kinds``.

        Returns: RangeQueryResult with typed `NodeRef`/`EdgeRef` records.

        Anchor invariants:
            (i)   anchor_file == source.file (anchor lives at the call site,
                  never the target).
            (ii)  anchor_* is meaningful only on EDGE_TYPE_REFERENCE; CONTAIN
                  edges leave both fields None.
            (iii) outgoing ∩ incoming = ∅ for any single query (call site
                  and target are in distinct scopes by definition).
        """
        if depth != 1:
            raise NotImplementedError(
                f"query_range supports only depth=1 in v1; got depth={depth}"
            )
        if layer is not None:
            if kinds is not None:
                raise ValueError("query_range accepts either kinds or layer, not both")
            layer_kinds = edge_types_for_graph_layer(layer)
            if layer_kinds is None:
                kinds = {edge.attributes().get("type") for edge in self.graph.es}
            else:
                kinds = set(layer_kinds)
        if kinds is None:
            kinds = {EDGE_TYPE_REFERENCE}

        # defined: brute-force overlap on per-file node list.
        nodes = self._file_nodes.get(file, [])
        defined_vids = [vid for s, e, vid in nodes if s <= end_line and e >= start_line]
        defined = [self._build_node_ref(vid) for vid in defined_vids]

        # outgoing: bisect on sorted (anchor_line, eid) list, then filter by kind.
        # Use bisect_left for the upper bound: bisect_right would include
        # `(end_line+1, 0)` (eid==0 entries at the line just past the query),
        # leaking an off-anchor edge into the slice.
        arr = self._file_edge_anchors.get(file, [])
        lo = bisect.bisect_left(arr, (start_line, -1))
        hi = bisect.bisect_left(arr, (end_line + 1, 0))
        outgoing: List[EdgeRef] = []
        for _, eid in arr[lo:hi]:
            edge = self.graph.es[eid]
            if edge["type"] in kinds:
                outgoing.append(self._build_edge_ref(eid))

        # incoming: walk igraph adjacency directly; carry target_vid so the
        # consumer can map the inbound edge back to which defined node it hit.
        # Self-edges (recursion) are emitted by `outgoing` already, so skip
        # them here to keep outgoing ∩ incoming = ∅ (invariant iii).
        incoming: List[EdgeRef] = []
        for vid in defined_vids:
            for eid in self.graph.incident(vid, mode="in"):
                edge = self.graph.es[eid]
                if edge.source == edge.target:
                    continue
                if edge["type"] in kinds:
                    incoming.append(self._build_edge_ref(eid))

        return RangeQueryResult(defined=defined, outgoing=outgoing, incoming=incoming)

    def _build_node_ref(self, vid: int) -> NodeRef:
        v = self.graph.vs[vid]
        attrs = v.attributes()
        return NodeRef(
            vid=vid,
            name=v["name"],
            unified_name=attrs.get("unified_name", ""),
            file=attrs.get("file", ""),
            start_line=attrs.get("start_line", 0),
            end_line=attrs.get("end_line", 0),
            kind=attrs.get("type", ""),
        )

    def _build_edge_ref(self, eid: int) -> EdgeRef:
        edge = self.graph.es[eid]
        attrs = edge.attributes()
        return EdgeRef(
            eid=eid,
            source_vid=edge.source,
            target_vid=edge.target,
            edge_kind=edge["type"],
            anchor_file=attrs.get("anchor_file"),
            anchor_line=attrs.get("anchor_line"),
        )

    def query_range_by_symbol(
        self,
        name: str,
        kinds: Optional[set] = None,
        depth: int = 1,
        *,
        layer: Optional[str] = None,
    ) -> RangeQueryResult:
        """Range-query by symbol identity (`name`).

        Resolves `name` -> vid -> `(file, start_line, end_line)` and forwards
        to `query_range`. `name` is the globally-unique identity attribute
        (semi-raw SCIP symbol), not the `unified_name` display.

        Returns an empty RangeQueryResult if the symbol is unknown or has
        no associated file/range (e.g. a SCIP reference-only vertex).
        """
        vid = self.name_to_vertex.get(name)
        if vid is None:
            return RangeQueryResult()
        attrs = self.graph.vs[vid].attributes()
        f = attrs.get("file")
        s = attrs.get("start_line")
        e = attrs.get("end_line")
        if f is None or s is None or e is None:
            return RangeQueryResult()
        return self.query_range(f, s, e, kinds=kinds, depth=depth, layer=layer)

    def layers(self, *, use_core: bool = True):
        """Build a multi-graph layer index over this graph."""
        from .layers import build_graph_layers

        return build_graph_layers(self, use_core=use_core)

    def layer(self, name: str = GRAPH_LAYER_REFERENCE, *, use_core: bool = True):
        """Return one named graph layer view."""
        return self.layers(use_core=use_core).get(name)

    # ------------------------------------------------------------------

    def add_root_node(self, project_root):
        """Add the root node to the graph"""
        self._add_vertex(project_root, {"type": "root"})

    def add_directory_node(self, dir_path):
        """Add a directory node to the graph"""
        self._add_vertex(dir_path, {"type": NODE_TYPE_DIRECTORY})

    def save_graph(self, output_path):
        """
        Save the graph to a pickle file for fast serialization.

        Args:
            output_path: Path to save the pickle file
        """
        # Prepare data for pickling
        data = {
            "schema_version": _SCHEMA_VERSION,
            "project_root": str(self.project_root) if self.project_root else None,
            "graph": self.graph,  # igraph objects are picklable
            "symbol_ranges": self.symbol_ranges,
            "name_to_vertex": self.name_to_vertex,
            "file_nodes": self._file_nodes,
            "file_edge_anchors": self._file_edge_anchors,
            "unified_to_names": self._unified_to_names,
        }

        with open(output_path, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def load_graph(cls, input_path):
        """
        Load a graph from a pickle file.

        Args:
            input_path: Path to the pickle file

        Returns:
            CodeGraph: Loaded graph instance
        """
        with open(input_path, "rb") as f:
            data = compat_pickle.load(f)

        on_disk = data.get("schema_version")
        if on_disk != _SCHEMA_VERSION:
            raise ValueError(
                f"graph.pkl at {input_path} has schema_version={on_disk!r}, "
                f"expected {_SCHEMA_VERSION}. Delete the cached pickle and "
                "regenerate via the indexing pipeline (e.g. integration-serial)."
            )

        # Create new CodeGraph instance
        graph_instance = cls(project_root=data.get("project_root"))

        # Restore the igraph object and internal state
        graph_instance.graph = data["graph"]
        graph_instance.symbol_ranges = data.get("symbol_ranges", {})
        graph_instance.name_to_vertex = data.get("name_to_vertex", {})
        graph_instance._file_nodes = data.get("file_nodes", {})
        graph_instance._file_edge_anchors = data.get("file_edge_anchors", {})
        graph_instance._unified_to_names = data.get("unified_to_names", {})

        return graph_instance

    def get_graph(self):
        """
        Get the igraph Graph object.

        Returns:
            The igraph Graph instance
        """
        return self.graph

    # ------------------------------------------------------------------
    # Readable-name resolution
    #
    # The canonical vertex ``name`` is the identity key — for clang/SCIP
    # indexed C/C++ it is a content hash (e.g. ``8ee8d723c4a8f670``). The
    # readable label lives in the ``unified_name`` attribute (e.g.
    # ``src/t_list.c:popGenericCommand()``). These helpers map a readable /
    # bare symbol the *caller* knows back to the canonical name graph ops
    # (get_successors / get_predecessors / incident) need.
    # ------------------------------------------------------------------

    @staticmethod
    def _bare_symbol(s: str) -> str:
        """Strip path/qualifier prefix and a trailing ``()`` to a bare token."""
        return s.split(":")[-1].split(".")[-1].rstrip("()").strip()

    def unified_index(self):
        """Readable ``unified_name`` (full + bare) -> [canonical names], cached.

        Rebuilds from vertex attributes when the persisted ``_unified_to_names``
        is empty (older prebuilt graphs ship it empty even though every vertex
        carries a ``unified_name``).
        """
        cache = getattr(self, "_unified_index_cache", None)
        if cache is not None:
            return cache
        index = {}

        def _add(key, name):
            index.setdefault(key, []).append(name)

        pre = self._unified_to_names or {}
        if pre:
            for disp, names in pre.items():
                for nm in names:
                    _add(disp, nm)
                    _add(self._bare_symbol(disp), nm)
        else:
            for nm in self.name_to_vertex:
                info = self.get_node_info_by_name(nm) or {}
                u = info.get("unified_name")
                if u:
                    _add(u, nm)
                    _add(self._bare_symbol(u), nm)
        self._unified_index_cache = index
        return index

    def display_name(self, name):
        """Readable label for a canonical *name* (``unified_name`` or itself)."""
        info = self.get_node_info_by_name(name) or {}
        return info.get("unified_name") or name

    def resolve_symbol(self, symbol):
        """Map a readable/bare *symbol* to (canonical_name | None, candidates).

        Tries the canonical name first, then the readable ``unified_name``
        (full display, then bare token), then a substring match. ``None`` when
        ambiguous or missing; candidates are readable display names.
        """
        n2v = self.name_to_vertex or {}
        s = (symbol or "").strip().strip("`'\"")
        if not s:
            return None, []
        if s in n2v:
            return s, [self.display_name(s)]

        uni = self.unified_index()
        cands = []
        if uni:
            sbase = self._bare_symbol(s)
            for key in (s, s + "()", sbase):
                if key in uni:
                    cands = list(dict.fromkeys(uni[key]))
                    break
            if not cands:
                sub = []
                for disp, names in uni.items():
                    if sbase and sbase.lower() in disp.lower():
                        sub += names
                cands = list(dict.fromkeys(sub))
        if not cands:  # languages whose canonical name is already readable
            base = s.split(".")[-1].split(":")[-1]
            cands = [k for k in n2v if k.endswith("." + s) or k.split(".")[-1] == base]
        canonical = cands[0] if len(cands) == 1 else None
        return canonical, [self.display_name(c) for c in cands[:8]]

    def get_node_info_by_name(self, node_name):
        """
        Get information about a node in the graph.

        Args:
            node_name: Name of the node

        Returns:
            Dictionary with vertex attributes or None if not found
        """
        if isinstance(node_name, str):
            vertex = self.name_to_vertex.get(node_name)
            if vertex is None:
                return None

        return self.graph.vs[vertex].attributes()

    def get_node_info_by_id(self, node_id):
        """
        Get information about a node in the graph.

        Args:
            node_id: ID of the node

        Returns:
            Dictionary with vertex attributes or None if not found
        """
        if isinstance(node_id, int):
            vertex = self.graph.vs[node_id]
            return vertex.attributes()

        return None

    def get_node_content(self, node_id):
        """
        Get the content of a node in the graph.
        Read the file from start_line to end_line.
        If the node is a file, return the file content.

        Args:
            node_id: ID of the node

        Returns:
            Content of the node or None if not found
        """
        node = self.graph.vs[node_id]
        node_type = node["type"] if "type" in node.attributes() else "unknown"
        if node_type == NODE_TYPE_FILE:
            # file path need to add self.project_root
            file_path = node["name"]
            if self.project_root:
                file_path = f"{self.project_root}/{file_path}"
            try:
                with open(file_path, "r") as f:
                    content = f.read()
                return content
            except FileNotFoundError:
                print(f"File not found: {file_path}")
                return None
        elif is_symbol_node(node_type):
            # Get the start and end lines
            start_line = node["start_line"]
            end_line = node["end_line"]

            # Get the file path
            file_path = node["file"] if "file" in node.attributes() else None
            if self.project_root and file_path:
                file_path = f"{self.project_root}/{file_path}"

            # Read the file content
            if file_path:
                try:
                    with open(file_path, "r") as f:
                        lines = f.readlines()
                    return "".join(lines[start_line - 1 : end_line])
                except FileNotFoundError:
                    print(f"File not found: {file_path}")
                    return None
        return None

    def get_neighbors(self, node_name):
        """
        Get the neighbors of a node in the graph.

        Args:
            node_name: Name of the node

        Returns:
            List of neighbor vertex IDs
        """
        if isinstance(node_name, str):
            vertex = self.name_to_vertex.get(node_name)
            if vertex is None:
                return []

        return self.graph.neighbors(vertex)

    def get_successors(self, node_name, edge_types=None):
        """
        Get the successors of a node_name (outgoing edges).

        Args:
            node_name: Name of the node
            edge_types: Optional edge types to retain. Typed traversal returns
                unique neighbors even when the graph contains multi-edges.

        Returns:
            List of successor vertex IDs
        """
        if isinstance(node_name, str):
            vertex = self.name_to_vertex.get(node_name)
            if vertex is None:
                return []

        if edge_types is None:
            return self.graph.successors(vertex)
        return self._get_typed_adjacent(vertex, edge_types, mode="out")

    def get_predecessors(self, node_name, edge_types=None):
        """
        Get the predecessors of a node_name (incoming edges).

        Args:
            node_name: Name of the node
            edge_types: Optional edge types to retain. Typed traversal returns
                unique neighbors even when the graph contains multi-edges.

        Returns:
            List of predecessor vertex IDs
        """
        if isinstance(node_name, str):
            vertex = self.name_to_vertex.get(node_name)
            if vertex is None:
                return []

        if edge_types is None:
            return self.graph.predecessors(vertex)
        return self._get_typed_adjacent(vertex, edge_types, mode="in")

    def _get_typed_adjacent(self, vertex, edge_types, *, mode):
        """Return unique adjacent vertices connected by selected edge types."""
        selected = set(edge_types)
        seen = set()
        neighbors = []
        for edge_id in self.graph.incident(vertex, mode=mode):
            edge = self.graph.es[edge_id]
            if edge.attributes().get("type") not in selected:
                continue
            neighbor = edge.target if mode == "out" else edge.source
            if neighbor in seen:
                continue
            seen.add(neighbor)
            neighbors.append(neighbor)
        return neighbors

    def print_graph_basic_info(self):
        """
        Print basic information about the graph.
        """
        print("Graph Summary:")
        print(f"  Number of vertices: {self.graph.vcount()}")
        print(f"  Number of edges: {self.graph.ecount()}")
        print(f"  Directed: {self.graph.is_directed()}")

        # Print vertex types
        vertex_types = self.graph.vs["type"]
        unique_vertex_types = set(vertex_types)
        print(f"  Unique vertex types: {unique_vertex_types}")

        # Print edge types
        edge_types = self.graph.es["type"]
        unique_edge_types = set(edge_types)
        print(f"  Unique edge types: {unique_edge_types}")

    def visualize_graph(
        self, output_path=None, width=800, height=600, layout="fruchterman_reingold"
    ):
        """
        Visualize the code graph with different colors for different node and edge types.

        Args:
            output_path: Path to save the visualization (optional)
            width: Width of the plot in pixels
            height: Height of the plot in pixels
            layout: Layout algorithm to use ('fruchterman_reingold', 'kk', 'grid', etc.)

        Returns:
            A matplotlib figure object if output_path is not provided
        """
        if self.graph.vcount() == 0:
            print("Graph is empty. Nothing to visualize.")
            return None

        try:
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise RuntimeError(
                "static graph rendering requires matplotlib; install it separately"
            ) from exc

        # Define color schemes
        node_type_colors = {
            NODE_TYPE_FILE: "skyblue",
            NODE_TYPE_SYMBOL: "lightgreen",
            NODE_TYPE_CLASS: "gold",
            NODE_TYPE_FUNCTION: "lightgreen",
            NODE_TYPE_METHOD: "lightcoral",
            NODE_TYPE_FIELD: "plum",
            NODE_TYPE_DIRECTORY: "orange",  # Add directory node color
            "root": "lightgrey",  # Root node color
            # Add more node types and colors as needed
        }

        edge_type_colors = {
            EDGE_TYPE_REFERENCE: "red",
            EDGE_TYPE_CONTAIN: "blue",
            # Add more edge types and colors as needed
        }

        # Define visual style
        visual_style = {}

        # Set vertex colors based on type
        vertex_colors = []
        for vertex in self.graph.vs:
            node_type = vertex["type"] if "type" in vertex.attributes() else "unknown"
            vertex_colors.append(node_type_colors.get(node_type, "grey"))
        visual_style["vertex_color"] = vertex_colors

        # Set vertex labels
        visual_style["vertex_label"] = [
            v["name"].split("/")[-1] if "/" in v["name"] else v["name"]
            for v in self.graph.vs
        ]
        visual_style["vertex_label_size"] = 8

        # Set vertex sizes (files can be larger than symbols)
        vertex_sizes = []
        for vertex in self.graph.vs:
            if "type" in vertex.attributes() and vertex["type"] == NODE_TYPE_FILE:
                vertex_sizes.append(20)
            else:
                vertex_sizes.append(10)
        visual_style["vertex_size"] = vertex_sizes

        # Set edge colors based on type
        edge_colors = []
        for edge in self.graph.es:
            edge_type = edge["type"] if "type" in edge.attributes() else "unknown"
            edge_colors.append(edge_type_colors.get(edge_type, "grey"))
        visual_style["edge_color"] = edge_colors

        # Set edge width
        visual_style["edge_width"] = 1.0

        # Calculate layout
        if layout == "fruchterman_reingold":
            visual_style["layout"] = self.graph.layout_fruchterman_reingold()
        elif layout == "kk":
            visual_style["layout"] = self.graph.layout_kamada_kawai()
        elif layout == "grid":
            visual_style["layout"] = self.graph.layout_grid()
        else:
            visual_style["layout"] = self.graph.layout(layout)

        # Adjust dimensions
        visual_style["bbox"] = (width, height)
        visual_style["margin"] = 40

        # Create legend data
        legend_data = {
            "Node Types": [
                (color, node_type) for node_type, color in node_type_colors.items()
            ],
            "Edge Types": [
                (color, edge_type) for edge_type, color in edge_type_colors.items()
            ],
        }

        # Create the plot
        fig, ax = plt.subplots(figsize=(width / 100, height / 100))

        # Plot the graph
        ig.plot(self.graph, target=ax, **visual_style)

        # Add legend for node types
        node_legend_patches = []
        for color, label in legend_data["Node Types"]:
            node_legend_patches.append(
                plt.Line2D(
                    [0],
                    [0],
                    marker="o",
                    color="w",
                    markerfacecolor=color,
                    markersize=10,
                    label=label,
                )
            )

        # Add legend for edge types
        edge_legend_patches = []
        for color, label in legend_data["Edge Types"]:
            edge_legend_patches.append(
                plt.Line2D([0], [0], color=color, lw=2, label=label)
            )

        # Add legends to plot
        ax.legend(
            handles=node_legend_patches + edge_legend_patches,
            loc="upper right",
            title="Legend",
            frameon=True,
        )

        # Save or show the plot
        if output_path:
            plt.savefig(output_path, dpi=100, bbox_inches="tight")
            print(f"Graph visualization saved to {output_path}")
            plt.close(fig)
            return None
        else:
            plt.tight_layout()
            return fig
