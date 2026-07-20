# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SubgraphMgr: manages incremental subgraph operations on CodeGraph.

Handles:
- Subgraph deletion (file-level, vertex-level)
- Subgraph construction from LSP documentSymbol
- Cross-file reference edge reconnection (incoming + outgoing)
- Vertex renaming and index maintenance

Uses composition — holds a reference to CodeGraph and LSPClient.
Language-specific behavior is delegated via abstract methods that
patcher_lang subclasses must implement.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

from ...log_utils import get_logger
from ...types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NODE_TYPE_SYMBOL,
)
from ..code_graph import CodeGraph
from .lsp_client import LSPClient, is_lsp_document_unavailable_error, uri_to_relpath

logger = get_logger(__name__)

# LSP SymbolKind integer → NODE_TYPE_* default mapping
LSP_KIND_TO_NODE_TYPE = {
    2: NODE_TYPE_CLASS,  # Module
    3: NODE_TYPE_CLASS,  # Namespace
    5: NODE_TYPE_CLASS,  # Class
    6: NODE_TYPE_METHOD,  # Method
    7: NODE_TYPE_FIELD,  # Property
    8: NODE_TYPE_FIELD,  # Field
    9: NODE_TYPE_METHOD,  # Constructor
    10: NODE_TYPE_CLASS,  # Enum
    11: NODE_TYPE_CLASS,  # Interface
    12: NODE_TYPE_FUNCTION,  # Function
    13: NODE_TYPE_FIELD,  # Variable
    14: NODE_TYPE_FIELD,  # Constant
    22: NODE_TYPE_CLASS,  # Enum (alt)
    23: NODE_TYPE_CLASS,  # Struct
    25: NODE_TYPE_FUNCTION,  # Operator
}

# Symbol types whose uses may survive outside a changed range. Fields and
# package/module variables are included: even when language-local, a newly
# added definition can be referenced by unchanged lines elsewhere in its file.
# Parameters and local variables are filtered before becoming graph vertices.
_INCOMING_REF_TYPES = frozenset(
    {
        NODE_TYPE_CLASS,
        NODE_TYPE_FIELD,
        NODE_TYPE_FUNCTION,
        NODE_TYPE_METHOD,
    }
)

_LEXICAL_IDENTIFIER_RE = re.compile(r"(?:[^\W\d]|[$_])(?:\w|[$])*", re.UNICODE)
_MAX_REVERSE_REFERENCE_CANDIDATES = 64


class EdgeBatch:
    """Stage edges across many reconnect calls, flush in one igraph op.

    Per-edge ``CodeGraph._add_edge`` does an O(out-degree) dedup walk plus a
    Python↔C round-trip per edge — at thousands of edges per patch this is
    the second-largest non-LSP cost. The batch:

      1. Builds an index of existing edges keyed by
         ``(src_id, tgt_id, type, anchor_file, anchor_line)`` once.
      2. ``stage(src_name, tgt_name, type, anchor_file, anchor_line)`` resolves
         names to ids, dedups against existing + already-staged, queues into
         a list.
      3. ``flush()`` issues a single ``graph.add_edges(pairs, attributes=...)``
         call and clears the queue.

    Falls back to ``CodeGraph._add_edge`` when staging would be wrong (vertex
    creation needed) — keeps the contract simple: callers pass *names* of
    vertices they expect to already exist.
    """

    def __init__(self, code_graph):
        self.cg = code_graph
        self.pairs: list[tuple[int, int]] = []
        self.types: list[str] = []
        self.anchor_files: list = []
        self.anchor_lines: list = []
        self._staged_keys: set = set()
        self._existing: dict = self._build_existing_index()

    def _build_existing_index(self) -> dict:
        idx = {}
        es = self.cg.graph.es
        for eid in range(self.cg.graph.ecount()):
            e = es[eid]
            attrs = e.attributes()
            key = (
                e.source,
                e.target,
                attrs.get("type"),
                attrs.get("anchor_file"),
                attrs.get("anchor_line"),
            )
            idx[key] = eid
        return idx

    def stage(
        self,
        source_name: str,
        target_name: str,
        edge_type: str,
        anchor_file=None,
        anchor_line=None,
    ) -> bool:
        """Stage an edge. Returns True if newly staged, False if dedup hit.

        Caller guarantees both vertices already exist (no implicit creation).
        """
        cg = self.cg
        src = cg.name_to_vertex.get(source_name)
        tgt = cg.name_to_vertex.get(target_name)
        if src is None or tgt is None:
            # Caller-violated contract — fall back to the safe path that
            # creates vertices on demand.
            cg._add_edge(
                source_name,
                target_name,
                edge_type,
                anchor_file=anchor_file,
                anchor_line=anchor_line,
            )
            return True
        key = (src, tgt, edge_type, anchor_file, anchor_line)
        if key in self._existing or key in self._staged_keys:
            return False
        self._staged_keys.add(key)
        self.pairs.append((src, tgt))
        self.types.append(edge_type)
        self.anchor_files.append(anchor_file)
        self.anchor_lines.append(anchor_line)
        return True

    def flush(self) -> int:
        n = len(self.pairs)
        if not n:
            return 0
        old_ecount = self.cg.graph.ecount()
        self.cg.graph.add_edges(self.pairs)
        # Bulk attribute set: one Python-level loop, but no add_edges calls.
        for i in range(n):
            eid = old_ecount + i
            self.cg.graph.es[eid]["type"] = self.types[i]
            af = self.anchor_files[i]
            if af is not None:
                self.cg.graph.es[eid]["anchor_file"] = af
            al = self.anchor_lines[i]
            if al is not None:
                self.cg.graph.es[eid]["anchor_line"] = al
        self.pairs.clear()
        self.types.clear()
        self.anchor_files.clear()
        self.anchor_lines.clear()
        self._staged_keys.clear()
        # Bulk add bypassed CodeGraph._add_edge, so any cached edge index is
        # now missing the just-flushed edges. Drop it; the next _add_edge
        # will rebuild lazily and see the batched edges.
        self.cg._invalidate_edge_index()
        return n


class SubgraphMgr(ABC):
    """Manages incremental subgraph operations on a CodeGraph.

    Abstract methods that language-specific patchers must implement:
        - _build_unified_name: construct unified_name matching SCIP format
        - _get_crossfile_token_types: which semantic token types to query
    """

    def __init__(
        self,
        project_root: str,
        code_graph: CodeGraph,
        lsp_client: Optional[LSPClient] = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.code_graph = code_graph
        self.lsp_client = lsp_client
        # selectionRange for each vertex (for incoming ref queries)
        self.symbol_selection_ranges: dict[str, tuple[int, int, int, int]] = {}
        # Cache semanticTokens per file (avoid duplicate LSP calls)
        self._semantic_tokens_cache: dict[
            tuple[str, tuple[tuple[int, int], ...]], list[dict] | None
        ] = {}
        self._reverse_reference_cache: dict[str, list[dict]] = {}
        self._touched_reference_targets: set[str] = set()
        self._symbol_leaf_to_vertices: dict[str, list[int]] = {}

    # ═══════════════════════════════════════════════════════════
    # Abstract methods — language-specific
    # ═══════════════════════════════════════════════════════════

    @abstractmethod
    def _build_unified_name(
        self,
        file_path: str,
        name: str,
        parent_unified_part: str,
        kind: int,
    ) -> str:
        """Construct unified_name matching the SCIP decoder's format.

        Args:
            file_path: Relative file path.
            name: Symbol name from LSP documentSymbol.
            parent_unified_part: Display part of parent's unified_name.
            kind: LSP SymbolKind integer.

        Returns:
            e.g. "src/lib.rs:Router.handle()"
        """

    @abstractmethod
    def _get_crossfile_token_types(self) -> set[str]:
        """Semantic token types that may reference cross-file symbols."""

    def _classify_symbol_type(self, kind: int) -> str:
        """Map LSP SymbolKind to NODE_TYPE_*. Override for language-specific."""
        return LSP_KIND_TO_NODE_TYPE.get(kind, NODE_TYPE_SYMBOL)

    # ═══════════════════════════════════════════════════════════
    # Index maintenance
    # ═══════════════════════════════════════════════════════════

    def build_indexes(self):
        """Build unified_name and file_to_vertices indexes."""
        g = self.code_graph
        g.unified_name_to_vertex = {}
        self._symbol_leaf_to_vertices = {}
        for v in g.graph.vs:
            un = v.attributes().get("unified_name")
            if un:
                g.unified_name_to_vertex.setdefault(un, []).append(v.index)
                leaf = self._unified_name_leaf(un)
                self._symbol_leaf_to_vertices.setdefault(leaf, []).append(v.index)

        g.file_to_vertices = {}
        for v in g.graph.vs:
            attrs = v.attributes()
            node_type = attrs.get("type")
            name = v["name"]
            if node_type == NODE_TYPE_FILE:
                g.file_to_vertices.setdefault(name, set()).add(v.index)
            else:
                f = attrs.get("file")
                if f:
                    g.file_to_vertices.setdefault(f, set()).add(v.index)

    # ═══════════════════════════════════════════════════════════
    # Subgraph deletion
    # ═══════════════════════════════════════════════════════════

    def delete_file_subgraph(self, file_path: str) -> dict:
        """Remove file vertex + all symbol vertices for a file.

        Records severed reference edges for later remap.

        Returns:
            dict with deleted_vertex_names, severed_incoming_refs,
            severed_outgoing_refs.
        """
        g = self.code_graph

        # Collect vertex IDs
        if hasattr(g, "file_to_vertices") and file_path in g.file_to_vertices:
            vids = set(g.file_to_vertices[file_path])
        else:
            from ...types import NODE_TYPE_FILE as NTF
            from ..code_graph import is_symbol_node

            vids = set()
            for v in g.graph.vs:
                attrs = v.attributes()
                if attrs.get("type") == NTF and v["name"] == file_path:
                    vids.add(v.index)
                elif attrs.get("file") == file_path and is_symbol_node(
                    attrs.get("type", "")
                ):
                    vids.add(v.index)

        if not vids:
            return {
                "deleted_vertex_names": [],
                "severed_incoming_refs": [],
                "severed_outgoing_refs": [],
            }

        severed_incoming = []
        severed_outgoing = []
        for vid in vids:
            v = g.graph.vs[vid]
            v_uname = v.attributes().get("unified_name") or ""
            # Outgoing: one entry per anchored call site (NO (src,tgt) dedup).
            # Multi-edge schema means (src, tgt) may have N edges, one per
            # call site; remap path needs each anchor preserved.
            for eid in g.graph.incident(vid, mode="out"):
                edge = g.graph.es[eid]
                if edge["type"] != EDGE_TYPE_REFERENCE:
                    continue
                if edge.target in vids:
                    continue
                tv = g.graph.vs[edge.target]
                eattrs = edge.attributes()
                severed_outgoing.append(
                    (
                        v["name"],
                        tv["name"],
                        v_uname,
                        tv.attributes().get("unified_name") or "",
                        eattrs.get("anchor_file"),
                        eattrs.get("anchor_line"),
                    )
                )
            # Incoming: similarly per-anchor.
            for eid in g.graph.incident(vid, mode="in"):
                edge = g.graph.es[eid]
                if edge["type"] != EDGE_TYPE_REFERENCE:
                    continue
                if edge.source in vids:
                    continue
                sv = g.graph.vs[edge.source]
                eattrs = edge.attributes()
                severed_incoming.append(
                    (
                        sv["name"],
                        v["name"],
                        sv.attributes().get("unified_name") or "",
                        v_uname,
                        eattrs.get("anchor_file"),
                        eattrs.get("anchor_line"),
                    )
                )

        deleted_names = [g.graph.vs[vid]["name"] for vid in vids]
        g.graph.delete_vertices(sorted(vids))
        g._invalidate_edge_index()
        self._rebuild_indexes()
        for n in deleted_names:
            g.symbol_ranges.pop(n, None)

        return {
            "deleted_vertex_names": deleted_names,
            "severed_incoming_refs": severed_incoming,
            "severed_outgoing_refs": severed_outgoing,
        }

    def delete_vertices_by_name(self, names: list) -> dict:
        """Delete specific vertices by name. Records severed edges."""
        g = self.code_graph
        vids = set()
        for n in names:
            vid = g.name_to_vertex.get(n)
            if vid is not None:
                vids.add(vid)

        if not vids:
            return {
                "deleted_vertex_names": [],
                "severed_incoming_refs": [],
                "severed_outgoing_refs": [],
            }

        severed_incoming = []
        severed_outgoing = []
        for vid in vids:
            v = g.graph.vs[vid]
            v_uname = v.attributes().get("unified_name") or ""
            # Outgoing: one entry per anchored call site (multi-edge aware).
            for eid in g.graph.incident(vid, mode="out"):
                edge = g.graph.es[eid]
                if edge["type"] != EDGE_TYPE_REFERENCE:
                    continue
                if edge.target in vids:
                    continue
                tv = g.graph.vs[edge.target]
                eattrs = edge.attributes()
                severed_outgoing.append(
                    (
                        v["name"],
                        tv["name"],
                        v_uname,
                        tv.attributes().get("unified_name") or "",
                        eattrs.get("anchor_file"),
                        eattrs.get("anchor_line"),
                    )
                )
            for eid in g.graph.incident(vid, mode="in"):
                edge = g.graph.es[eid]
                if edge["type"] != EDGE_TYPE_REFERENCE:
                    continue
                if edge.source in vids:
                    continue
                sv = g.graph.vs[edge.source]
                eattrs = edge.attributes()
                severed_incoming.append(
                    (
                        sv["name"],
                        v["name"],
                        sv.attributes().get("unified_name") or "",
                        v_uname,
                        eattrs.get("anchor_file"),
                        eattrs.get("anchor_line"),
                    )
                )

        deleted_names = [g.graph.vs[vid]["name"] for vid in vids]
        g.graph.delete_vertices(sorted(vids))
        g._invalidate_edge_index()
        self._rebuild_indexes()
        for n in deleted_names:
            g.symbol_ranges.pop(n, None)

        return {
            "deleted_vertex_names": deleted_names,
            "severed_incoming_refs": severed_incoming,
            "severed_outgoing_refs": severed_outgoing,
        }

    def delete_outgoing_reference_edges(self, vertex_name: str) -> int:
        """Delete only outgoing REFERENCE edges from a vertex."""
        g = self.code_graph
        vid = g.name_to_vertex.get(vertex_name)
        if vid is None:
            return 0
        to_delete = [
            eid
            for eid in g.graph.incident(vid, mode="out")
            if g.graph.es[eid]["type"] == EDGE_TYPE_REFERENCE
        ]
        if to_delete:
            g.graph.delete_edges(to_delete)
            g._invalidate_edge_index()
        return len(to_delete)

    def delete_edges_by_anchor(
        self,
        source_name: str,
        target_name: str,
        edge_type: str,
        anchor_file: str,
        anchor_line: int,
    ) -> int:
        """Delete edges matching `(src, tgt, type, anchor_file, anchor_line)`.

        Multi-edge aware: if the (src, tgt) pair has multiple edges (one per
        call site), only the edge whose anchor matches is removed. Returns
        the count of edges deleted.
        """
        g = self.code_graph
        src_vid = g.name_to_vertex.get(source_name)
        tgt_vid = g.name_to_vertex.get(target_name)
        if src_vid is None or tgt_vid is None:
            return 0

        to_delete = []
        for eid in g.graph.incident(src_vid, mode="out"):
            edge = g.graph.es[eid]
            if edge.target != tgt_vid:
                continue
            if edge["type"] != edge_type:
                continue
            attrs = edge.attributes()
            if (
                attrs.get("anchor_file") == anchor_file
                and attrs.get("anchor_line") == anchor_line
            ):
                to_delete.append(eid)
        if to_delete:
            g.graph.delete_edges(to_delete)
            g._invalidate_edge_index()
        return len(to_delete)

    def delete_outgoing_in_anchor_ranges(
        self,
        vertex_name: str,
        anchor_file: str,
        ranges: list[tuple[int, int]],
    ) -> int:
        """Delete outgoing REFERENCE edges from ``vertex_name`` whose
        ``(anchor_file, anchor_line)`` falls inside any of the given ranges.

        Used by the patcher's affected-symbol path to surgically remove only
        the outgoing edges whose call sites lie in the changed body region,
        keeping anchors in unchanged regions intact. CONTAIN edges (no
        anchor) are never touched. Edges with no ``anchor_line`` set are
        also skipped.

        Returns the count of edges deleted.
        """
        g = self.code_graph
        vid = g.name_to_vertex.get(vertex_name)
        if vid is None or not ranges:
            return 0

        to_delete = []
        for eid in g.graph.incident(vid, mode="out"):
            edge = g.graph.es[eid]
            if edge["type"] != EDGE_TYPE_REFERENCE:
                continue
            attrs = edge.attributes()
            if attrs.get("anchor_file") != anchor_file:
                continue
            line = attrs.get("anchor_line")
            if line is None:
                continue
            for start, end in ranges:
                if start <= line <= end:
                    to_delete.append(eid)
                    break
        if to_delete:
            g.graph.delete_edges(to_delete)
            g._invalidate_edge_index()
        return len(to_delete)

    def shift_outgoing_anchor_lines(
        self,
        vertex_name: str,
        anchor_file: str,
        old_start: int,
        old_end: int,
        shift: int,
    ) -> int:
        """Shift ``anchor_line`` on outgoing REFERENCE edges from
        ``vertex_name`` whose ``(anchor_file, anchor_line)`` falls inside
        ``[old_start, old_end]`` by ``shift``.

        Used by the patcher's shifted-symbol path: when a symbol moves up
        or down by N lines but its body is unmodified, every anchor in its
        old body region needs the same uniform shift to stay aligned with
        the new line numbering.

        Returns the count of edges modified. ``shift=0`` is a no-op.
        """
        if shift == 0:
            return 0
        g = self.code_graph
        vid = g.name_to_vertex.get(vertex_name)
        if vid is None:
            return 0

        moved = 0
        for eid in g.graph.incident(vid, mode="out"):
            edge = g.graph.es[eid]
            if edge["type"] != EDGE_TYPE_REFERENCE:
                continue
            attrs = edge.attributes()
            if attrs.get("anchor_file") != anchor_file:
                continue
            line = attrs.get("anchor_line")
            if line is None:
                continue
            if old_start <= line <= old_end:
                edge["anchor_line"] = line + shift
                moved += 1
        if moved:
            g._invalidate_edge_index()
        return moved

    def rebase_file_scope_reference_anchors(
        self,
        file_path: str,
        line_mapper: Callable[[int], int | None],
    ) -> tuple[int, int]:
        """Rebase references owned by a file node after line edits.

        Module-level references use the file vertex as their source. Symbol
        updates therefore do not visit them. ``line_mapper`` returns a new
        line for retained anchors and ``None`` for anchors invalidated by a
        changed old-file range.

        Returns ``(moved, removed)``.
        """

        g = self.code_graph
        file_vid = g.name_to_vertex.get(file_path)
        if file_vid is None:
            return 0, 0
        file_vertex = g.graph.vs[file_vid]
        if file_vertex.attributes().get("type") != NODE_TYPE_FILE:
            return 0, 0

        return self.rebase_outgoing_reference_anchors(
            file_path,
            file_path,
            line_mapper,
        )

    def rebase_reference_anchors_for_file(
        self,
        file_path: str,
        line_mapper: Callable[[int], int | None],
        *,
        reconcile_removed_targets: bool = False,
    ) -> tuple[int, int]:
        """Rebase every reference anchored in one changed source file.

        ``anchor_file`` is the source-location authority. The edge's source
        vertex is not: overloaded or duplicate backend identities such as Go
        ``init`` functions can make an edge appear to be owned by a vertex
        from another file. A single file-wide pass also prevents separate
        file-, shifted-, and affected-symbol paths from applying a line shift
        more than once.
        """

        graph = self.code_graph
        moved = 0
        to_delete = []
        for edge in graph.graph.es:
            attrs = edge.attributes()
            if edge["type"] != EDGE_TYPE_REFERENCE:
                continue
            if attrs.get("anchor_file") != file_path:
                continue
            old_line = attrs.get("anchor_line")
            if old_line is None:
                continue
            new_line = line_mapper(old_line)
            if new_line is None:
                if reconcile_removed_targets:
                    self._touched_reference_targets.add(
                        graph.graph.vs[edge.target]["name"]
                    )
                to_delete.append(edge.index)
            elif new_line != old_line:
                edge["anchor_line"] = new_line
                moved += 1

        if to_delete:
            graph.graph.delete_edges(to_delete)
        if moved or to_delete:
            graph._invalidate_edge_index()
        return moved, len(to_delete)

    def rebase_outgoing_reference_anchors(
        self,
        vertex_name: str,
        anchor_file: str,
        line_mapper: Callable[[int], int | None],
    ) -> tuple[int, int]:
        """Map retained outgoing anchors and remove anchors in edited lines.

        A zero-context Git hunk provides a lossless mapping for every
        unchanged source line, even when insertions make the shift inside a
        symbol non-uniform. Keeping those cold-build edges avoids replacing
        stable SCIP semantics with an LSP server's potentially different
        interpretation of unchanged code.
        """

        g = self.code_graph
        vertex_id = g.name_to_vertex.get(vertex_name)
        if vertex_id is None:
            return 0, 0

        moved = 0
        to_delete = []
        for eid in g.graph.incident(vertex_id, mode="out"):
            edge = g.graph.es[eid]
            if edge["type"] != EDGE_TYPE_REFERENCE:
                continue
            attrs = edge.attributes()
            if attrs.get("anchor_file") != anchor_file:
                continue
            old_line = attrs.get("anchor_line")
            if old_line is None:
                continue
            new_line = line_mapper(old_line)
            if new_line is None:
                to_delete.append(eid)
            elif new_line != old_line:
                edge["anchor_line"] = new_line
                moved += 1

        if to_delete:
            g.graph.delete_edges(to_delete)
        if moved or to_delete:
            g._invalidate_edge_index()
        return moved, len(to_delete)

    def rename_vertex(self, old_name: str, new_name: str, new_attrs: dict = None):
        """Rename a vertex without deleting it. All edges preserved."""
        g = self.code_graph
        if old_name == new_name and not new_attrs:
            return
        vid = g.name_to_vertex.get(old_name)
        if vid is None:
            return

        if old_name != new_name:
            del g.name_to_vertex[old_name]
            g.name_to_vertex[new_name] = vid
            g.graph.vs[vid]["name"] = new_name
            if old_name in g.symbol_ranges:
                g.symbol_ranges[new_name] = g.symbol_ranges.pop(old_name)

        if new_attrs:
            for key, value in new_attrs.items():
                g.graph.vs[vid][key] = value
            sl = new_attrs.get("start_line")
            el = new_attrs.get("end_line")
            if sl is not None and el is not None:
                g.symbol_ranges[new_name] = (sl, el)

    def _rebuild_indexes(self):
        """Rebuild all indexes after vertex deletion (IDs shift)."""
        g = self.code_graph
        g.name_to_vertex = {v["name"]: v.index for v in g.graph.vs}
        self.build_indexes()

    # ═══════════════════════════════════════════════════════════
    # Subgraph construction (from LSP documentSymbol)
    # ═══════════════════════════════════════════════════════════

    def rebuild_file_subgraph(self, file_path: str, symbols: list[dict]) -> list[str]:
        """Add file vertex + symbol vertices + containment edges.

        Also keeps ``file_to_vertices`` consistent for this file so callers
        can use ``match_location_to_*`` immediately without an explicit
        ``build_indexes()`` step. Other index dicts (``unified_name_to_vertex``)
        are still rebuilt by the caller via ``build_indexes()`` at batch end.

        Returns list of created vertex names.
        """
        g = self.code_graph
        g.add_file_node(file_path)

        parent_dir = str(Path(file_path).parent)
        if parent_dir != ".":
            if parent_dir not in g.name_to_vertex:
                g.add_directory_node(parent_dir)
            g._add_edge(parent_dir, file_path, EDGE_TYPE_CONTAIN)

        # Seed file_to_vertices for this file so match_location_to_* works
        # without a follow-up build_indexes(). Vertices created by
        # _process_symbol_tree below get appended via _track_file_vertex.
        if not hasattr(g, "file_to_vertices") or g.file_to_vertices is None:
            g.file_to_vertices = {}
        file_vid = g.name_to_vertex[file_path]
        g.file_to_vertices.setdefault(file_path, set()).add(file_vid)

        return self._process_symbol_tree(
            symbols,
            file_path,
            parent_vertex_name=file_path,
            parent_unified_part="",
        )

    def _process_symbol_tree(
        self,
        symbols: list[dict],
        file_path: str,
        parent_vertex_name: str,
        parent_unified_part: str,
    ) -> list[str]:
        """Recursively process documentSymbol children into graph vertices."""
        created = []
        if not symbols:
            return created

        g = self.code_graph
        for sym in symbols:
            name = sym.get("name", "")
            kind = sym.get("kind", 0)
            range_data = sym.get("range", {})
            sel_range = sym.get("selectionRange", range_data)
            start_line = range_data.get("start", {}).get("line", 0)
            end_line = range_data.get("end", {}).get("line", start_line)

            unified_name = self._build_unified_name(
                file_path, name, parent_unified_part, kind
            )
            vertex_name = f"{unified_name}:{start_line}"
            node_type = self._classify_symbol_type(kind)

            g._add_vertex(
                vertex_name,
                {
                    "type": node_type,
                    "file": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "selection_line": sel_range.get("start", {}).get(
                        "line", start_line
                    ),
                    "selection_character": sel_range.get("start", {}).get(
                        "character", 0
                    ),
                    "unified_name": unified_name,
                },
            )
            g.symbol_ranges[vertex_name] = (start_line, end_line)
            # Keep file_to_vertices consistent for live callers.
            if hasattr(g, "file_to_vertices") and g.file_to_vertices is not None:
                vid = g.name_to_vertex[vertex_name]
                g.file_to_vertices.setdefault(file_path, set()).add(vid)

            sel_start = sel_range.get("start", {})
            sel_end = sel_range.get("end", {})
            self.symbol_selection_ranges[vertex_name] = (
                sel_start.get("line", start_line),
                sel_start.get("character", 0),
                sel_end.get("line", start_line),
                sel_end.get("character", 0),
            )

            g._add_edge(parent_vertex_name, vertex_name, EDGE_TYPE_CONTAIN)
            created.append(vertex_name)

            # Module (kind=2, e.g. Rust `mod tests`) is transparent —
            # SCIP doesn't include module name in symbol path.
            if kind == 2:
                child_parent_part = parent_unified_part  # skip module
            else:
                parts = unified_name.split(":", 1)
                child_parent_part = parts[1] if len(parts) > 1 else name

            children = sym.get("children", [])
            if children:
                created.extend(
                    self._process_symbol_tree(
                        children,
                        file_path,
                        parent_vertex_name=vertex_name,
                        parent_unified_part=child_parent_part,
                    )
                )

        return created

    # ═══════════════════════════════════════════════════════════
    # Reference edge reconnection
    # ═══════════════════════════════════════════════════════════

    def reconnect_incoming(
        self,
        file_path: str,
        vertex_names: list[str],
        stats: dict,
        batch: "EdgeBatch | None" = None,
    ):
        """For each vertex, call LSP references to find external callers."""
        if not self.lsp_client:
            return
        abs_file = str(self.project_root / file_path)

        for vname in vertex_names:
            node_type = ""
            target_file = None
            target_start_line = None
            vid = self.code_graph.name_to_vertex.get(vname)
            if vid is not None:
                target_attrs = self.code_graph.graph.vs[vid].attributes()
                node_type = target_attrs.get("type", "")
                target_file = target_attrs.get("file")
                target_start_line = target_attrs.get("start_line")
                if node_type not in _INCOMING_REF_TYPES:
                    continue

            sel = self.symbol_selection_ranges.get(vname)
            if sel is None:
                continue
            sel_line, sel_char, _, _ = sel

            refs = self.lsp_client.references(
                abs_file, sel_line, sel_char, include_declaration=False
            )
            for loc in refs:
                # Some servers treat references to an interface method as
                # references to every implementation. Keep only call sites
                # whose own go-to-definition resolves to this concrete method.
                if (
                    getattr(self, "respect_backend_exclusions", True)
                    and node_type == NODE_TYPE_METHOD
                    and not self._reference_resolves_to(loc, vname)
                ):
                    continue
                ref_file = uri_to_relpath(loc.get("uri", ""), str(self.project_root))
                if ref_file is None:
                    continue
                ref_line = loc.get("range", {}).get("start", {}).get("line", 0)
                if ref_file == target_file and ref_line == target_start_line:
                    continue
                ref_scope = self._reference_scope_for_token(
                    ref_file,
                    {"line": ref_line},
                    self.match_location_to_scope(ref_file, ref_line),
                )
                if ref_scope:
                    if batch is not None:
                        if batch.stage(
                            ref_scope,
                            vname,
                            EDGE_TYPE_REFERENCE,
                            anchor_file=ref_file,
                            anchor_line=ref_line,
                        ):
                            stats["incoming_added"] += 1
                    else:
                        self.code_graph._add_edge(
                            ref_scope,
                            vname,
                            EDGE_TYPE_REFERENCE,
                            anchor_file=ref_file,
                            anchor_line=ref_line,
                        )
                        stats["incoming_added"] += 1
                else:
                    stats["unmatched"] += 1

    def _reference_resolves_to(self, location: dict, vertex_name: str) -> bool:
        """Check that a reference location resolves to one concrete vertex."""
        if not self.lsp_client:
            return False
        ref_file = uri_to_relpath(location.get("uri", ""), str(self.project_root))
        if ref_file is None:
            return False
        start = location.get("range", {}).get("start", {})
        line = start.get("line", 0)
        character = start.get("character", 0)
        definitions = self.lsp_client.definition(
            str(self.project_root / ref_file), line, character
        )
        for definition in definitions:
            target_uri = definition.get("targetUri", definition.get("uri", ""))
            target_file = uri_to_relpath(target_uri, str(self.project_root))
            if target_file is None:
                continue
            target_range = definition.get(
                "targetSelectionRange", definition.get("range", {})
            )
            target_line = target_range.get("start", {}).get("line", 0)
            matched = self.match_location_to_vertex(
                target_file,
                target_line,
                allow_scope_fallback=False,
                allow_name_fallback=False,
            )
            if matched == vertex_name:
                return True
        return False

    def reconnect_outgoing(
        self,
        file_path: str,
        vertex_names: list[str],
        stats: dict,
        line_ranges: list[tuple[int, int]] | None = None,
        batch: "EdgeBatch | None" = None,
        scope_filter: set[str] | None = None,
    ):
        """Use semanticTokens + definition to find outgoing refs."""
        if not self.lsp_client:
            return
        abs_file = str(self.project_root / file_path)

        semantic_tokens = (
            self._get_semantic_tokens(
                abs_file,
                file_path,
                line_ranges=line_ranges,
            )
            or []
        )
        ref_tokens = list(semantic_tokens)
        lexical_ranges = line_ranges
        if (
            lexical_ranges is None
            and scope_filter is not None
            and getattr(self, "strict_lsp_requests", False)
        ):
            # Whole-file materialization also owns imports, annotations, and
            # navigable documentation syntax outside declaration ranges.
            # Servers commonly omit those positions from semantic tokens, so
            # the strict path validates lexical candidates through the same
            # definition/reference oracle. Interactive updates keep the
            # bounded semantic-token policy.
            lexical_ranges = [(0, 2**31 - 1)]
        if lexical_ranges:
            semantic_positions = {
                (token["line"], token["character"]) for token in semantic_tokens
            }
            ref_tokens.extend(
                token
                for token in self._get_lexical_reference_tokens(
                    abs_file,
                    lexical_ranges,
                )
                if (token["line"], token["character"]) not in semantic_positions
            )
        if not ref_tokens:
            return

        # Map range → vertex for scope resolution
        range_to_vertex = {}
        if line_ranges and len(line_ranges) == len(vertex_names):
            for vname, (start, end) in zip(vertex_names, line_ranges, strict=False):
                range_to_vertex[(start, end)] = vname

        # Filter to line ranges if semanticTokens/range wasn't used
        if line_ranges:
            filtered = []
            for t in ref_tokens:
                for start, end in line_ranges:
                    if start <= t["line"] <= end:
                        filtered.append(t)
                        break
            ref_tokens = filtered
            if not ref_tokens:
                return

        scoped_tokens = []
        for token in ref_tokens:
            scope = None
            if range_to_vertex:
                for (start, end), vname in range_to_vertex.items():
                    if start <= token["line"] <= end:
                        scope = vname
                        break
            scope = self._reference_scope_for_token(file_path, token, scope)
            if scope_filter is not None and scope not in scope_filter:
                continue
            scoped_tokens.append((token, scope))
        if not scoped_tokens:
            return

        # Definition is position-sensitive: the same token text may be
        # shadowed or imported differently in separate scopes. Deduplicate
        # only repeated semantic-token rows for the same source position.
        definitions_by_position = {}
        for t, _scope in scoped_tokens:
            key = (t["line"], t["character"])
            if key not in definitions_by_position:
                definitions_by_position[key] = self.lsp_client.definition(
                    abs_file, t["line"], t["character"]
                )

        # Retry empty definitions once with NO sleep — null is a legitimate
        # "no definition here" answer per LSP spec, but on cold servers a
        # second pass occasionally resolves more. The previous 3s sleep
        # added per-file overhead with ~zero real benefit (most empties
        # remain empty on retry).
        empty_keys = [k for k, v in definitions_by_position.items() if v == []]
        if empty_keys:
            position_to_token = {}
            for t, _scope in scoped_tokens:
                position_to_token.setdefault((t["line"], t["character"]), t)
            resolved = 0
            for key in empty_keys:
                tok = position_to_token.get(key)
                if tok:
                    result = self.lsp_client.definition(
                        abs_file, tok["line"], tok["character"]
                    )
                    if result:
                        definitions_by_position[key] = result
                        resolved += 1
            if resolved:
                logger.debug(
                    f"Retry resolved {resolved}/{len(empty_keys)} " "empty definitions"
                )

        # Build edges. Each call site becomes its own anchored edge so the
        # multi-edge schema preserves call-site identity for range queries.
        # _add_edge dedups on (src, tgt, type, anchor_file, anchor_line),
        # so identical anchors collapse but distinct call sites stay distinct.
        for ref_token, scope in scoped_tokens:
            defn_list = definitions_by_position.get(
                (ref_token["line"], ref_token["character"])
            )
            target_vertices = []
            for defn in defn_list or []:
                target_uri = defn.get("targetUri", defn.get("uri", ""))
                target_file = uri_to_relpath(target_uri, str(self.project_root))
                if target_file is None:
                    continue
                target_range = defn.get("targetSelectionRange", defn.get("range", {}))
                target_line = target_range.get("start", {}).get("line", 0)
                target_vertex = self.match_location_to_vertex(
                    target_file,
                    target_line,
                    symbol_name=ref_token.get("text"),
                    allow_scope_fallback=False,
                    allow_name_fallback=ref_token.get("token_type")
                    not in {"variable", "parameter"},
                )
                if target_vertex:
                    target_vertices.append(target_vertex)

            if getattr(self, "mode", None) in {"symbol", "naive"}:
                # The full LSP graph is declaration-reference materialization.
                # Declaration-side references therefore validate direct
                # definition results. Exact-name candidates also recover
                # overloaded declarations omitted by definition.
                target_vertices = self._reverse_reference_targets(
                    file_path,
                    ref_token,
                    direct_targets=target_vertices,
                )

            matched = bool(scope and target_vertices)
            for target_vertex in dict.fromkeys(target_vertices):
                if not scope:
                    break
                target_id = self.code_graph.name_to_vertex.get(target_vertex)
                if target_id is not None:
                    target_attrs = self.code_graph.graph.vs[target_id].attributes()
                    if (
                        target_attrs.get("file") == file_path
                        and target_attrs.get("start_line") == ref_token["line"]
                    ):
                        # Match the full LSP decoder: declaration locations
                        # are not materialized as self-reference edges.
                        continue
                if batch is not None:
                    if batch.stage(
                        scope,
                        target_vertex,
                        EDGE_TYPE_REFERENCE,
                        anchor_file=file_path,
                        anchor_line=ref_token["line"],
                    ):
                        stats["outgoing_added"] += 1
                else:
                    self.code_graph._add_edge(
                        scope,
                        target_vertex,
                        EDGE_TYPE_REFERENCE,
                        anchor_file=file_path,
                        anchor_line=ref_token["line"],
                    )
                    stats["outgoing_added"] += 1
            if not matched:
                stats["unmatched"] += 1

    def _reverse_reference_targets(
        self,
        file_path: str,
        token: dict,
        direct_targets: list[str] | None = None,
    ) -> list[str]:
        """Resolve overloaded declarations by checking their reference sets.

        ``definition`` may return only one declaration from an overloaded or
        extended type while a full LSP graph contains an edge to every
        declaration whose ``references`` response includes the use site.
        Interactive updates retain a bounded latency policy. Strict
        materialization scans every exact-name candidate because truncation
        would make equivalence depend on overload fan-out.
        """

        if not self.lsp_client:
            return []
        symbol_name = str(token.get("text") or "")
        candidates = self._symbol_leaf_to_vertices.get(symbol_name, [])
        direct_target_names = set(direct_targets or [])
        candidate_names = list(dict.fromkeys(direct_targets or []))
        candidate_names.extend(
            name
            for name in self._constructor_targets(candidate_names)
            if name not in candidate_names
        )
        scan_all_candidates = bool(getattr(self, "strict_lsp_requests", False))
        if candidates and (
            scan_all_candidates or len(candidates) <= _MAX_REVERSE_REFERENCE_CANDIDATES
        ):
            candidate_names.extend(
                graph_name
                for vertex_id in candidates
                if (graph_name := self.code_graph.graph.vs[vertex_id]["name"])
                not in candidate_names
            )
        if not candidate_names:
            return []

        token_line = int(token["line"])
        token_character = int(token["character"])
        token_end = token_character + max(int(token.get("length") or 1), 1)
        matches = []
        graph = self.code_graph.graph
        uncached_names = []
        uncached_queries = []
        for vertex_name in candidate_names:
            vertex_id = self.code_graph.name_to_vertex.get(vertex_name)
            if vertex_id is None:
                continue
            vertex = graph.vs[vertex_id]
            attrs = vertex.attributes()
            target_file = attrs.get("file")
            target_line = attrs.get("selection_line")
            if not target_file or target_line is None:
                continue
            if vertex_name not in self._reverse_reference_cache:
                uncached_names.append(vertex_name)
                uncached_queries.append(
                    (
                        str(self.project_root / target_file),
                        int(target_line),
                        int(attrs.get("selection_character") or 0),
                    )
                )

        if uncached_queries:
            outcomes = self.lsp_client.references_batch(
                uncached_queries,
                include_declaration=False,
            )
            for vertex_name, (_locations, error) in zip(
                uncached_names, outcomes, strict=True
            ):
                if error and error.get("code") != -2:
                    if getattr(
                        self, "strict_lsp_requests", False
                    ) and not is_lsp_document_unavailable_error(error):
                        raise RuntimeError(
                            "reverse-reference oracle request failed for "
                            f"{vertex_name}: {error}"
                        )
                    continue
                self._reverse_reference_cache[vertex_name] = _locations

        for vertex_name in candidate_names:
            vertex_id = self.code_graph.name_to_vertex.get(vertex_name)
            if vertex_id is None:
                continue
            references = self._reverse_reference_cache.get(vertex_name)
            if references is None:
                continue
            location_matches = False
            for location in references:
                ref_file = uri_to_relpath(
                    location.get("uri", ""), str(self.project_root)
                )
                start = location.get("range", {}).get("start", {})
                end = location.get("range", {}).get("end", start)
                if ref_file != file_path or start.get("line") != token_line:
                    continue
                start_character = int(start.get("character") or 0)
                end_character = int(end.get("character") or start_character + 1)
                if start_character < token_end and token_character < end_character:
                    matches.append(vertex_name)
                    location_matches = True
                    break
            if vertex_name in direct_target_names or location_matches:
                self._touched_reference_targets.add(vertex_name)
        return matches

    def reconcile_touched_incoming(
        self,
        changed_files: set[str] | None = None,
    ) -> dict[str, int]:
        """Synchronize incoming sets for declarations touched by a diff.

        A source edit can change type resolution at an unchanged call site.
        Outgoing repair already fetched declaration-side references to validate
        changed tokens; reuse those responses without another LSP call. When
        ``changed_files`` is provided, only replace anchors owned by those
        files. This preserves materialized facts in untouched files when a
        long-lived language server later reports a broader reference set than
        the original full-build provider snapshot.
        """
        graph = self.code_graph.graph
        desired_by_target: dict[str, set[tuple[str, str, int]]] = {}
        reconciled = 0
        unmatched = 0
        for target_name in sorted(self._touched_reference_targets):
            references = self._reverse_reference_cache.get(target_name)
            target_id = self.code_graph.name_to_vertex.get(target_name)
            if target_id is None:
                continue
            target = graph.vs[target_id]
            target_attrs = target.attributes()
            if references is None and self.lsp_client:
                target_file = target_attrs.get("file")
                target_line = target_attrs.get("selection_line")
                if target_file and target_line is not None:
                    references = self.lsp_client.references(
                        str(self.project_root / target_file),
                        int(target_line),
                        int(target_attrs.get("selection_character") or 0),
                        include_declaration=False,
                    )
                    error = getattr(self.lsp_client, "last_error", None)
                    if error and error.get("code") != -2:
                        continue
                    self._reverse_reference_cache[target_name] = references
            if references is None:
                continue
            desired = desired_by_target.setdefault(target_name, set())
            for location in references:
                if (
                    getattr(self, "respect_backend_exclusions", True)
                    and target_attrs.get("type") == NODE_TYPE_METHOD
                    and not self._reference_resolves_to(location, target_name)
                ):
                    continue
                ref_file = uri_to_relpath(
                    location.get("uri", ""), str(self.project_root)
                )
                if ref_file is None:
                    continue
                if changed_files is not None and ref_file not in changed_files:
                    continue
                ref_line = int(
                    location.get("range", {}).get("start", {}).get("line", 0)
                )
                if ref_file == target_attrs.get(
                    "file"
                ) and ref_line == target_attrs.get("start_line"):
                    continue
                ref_scope = self._reference_scope_for_token(
                    ref_file,
                    {"line": ref_line},
                    self.match_location_to_scope(ref_file, ref_line),
                )
                if ref_scope:
                    desired.add((ref_scope, ref_file, ref_line))
                else:
                    unmatched += 1
            reconciled += 1

        remove_ids = []
        for target_name in desired_by_target:
            target_id = self.code_graph.name_to_vertex.get(target_name)
            if target_id is None:
                continue
            remove_ids.extend(
                edge_id
                for edge_id in graph.incident(target_id, mode="in")
                if graph.es[edge_id]["type"] == EDGE_TYPE_REFERENCE
                and (
                    changed_files is None
                    or graph.es[edge_id].attributes().get("anchor_file")
                    in changed_files
                )
            )
        if remove_ids:
            graph.delete_edges(sorted(set(remove_ids)))
            self.code_graph._invalidate_edge_index()

        batch = EdgeBatch(self.code_graph)
        added = 0
        for target_name, desired in desired_by_target.items():
            for source_name, anchor_file, anchor_line in desired:
                if batch.stage(
                    source_name,
                    target_name,
                    EDGE_TYPE_REFERENCE,
                    anchor_file=anchor_file,
                    anchor_line=anchor_line,
                ):
                    added += 1
        batch.flush()
        self._touched_reference_targets.clear()
        return {
            "targets_reconciled": reconciled,
            "incoming_removed": len(set(remove_ids)),
            "incoming_added": added,
            "unmatched": unmatched,
        }

    def _constructor_targets(self, direct_targets: list[str]) -> list[str]:
        """Expand a class definition to constructor declarations it contains.

        Some servers resolve ``Type(...)`` to the class for definition but
        report the same location as a reference to both the class and its
        constructor. The full graph materializes both declaration-side facts;
        incremental repair must consider the constructor before validating its
        reference response.
        """
        constructor_leaf = {
            "python": "__init__()",
            "ruby": "initialize()",
        }.get(self.REGISTRY_LANGUAGE, "constructor()")
        graph = self.code_graph.graph
        constructors = []
        for vertex_name in direct_targets:
            vertex_id = self.code_graph.name_to_vertex.get(vertex_name)
            if vertex_id is None or graph.vs[vertex_id]["type"] != NODE_TYPE_CLASS:
                continue
            for edge_id in graph.incident(vertex_id, mode="out"):
                edge = graph.es[edge_id]
                if edge["type"] != EDGE_TYPE_CONTAIN or edge.source != vertex_id:
                    continue
                child = graph.vs[edge.target]
                unified_name = str(child.attributes().get("unified_name") or "")
                display_name = unified_name.split(":", 1)[-1]
                if (
                    child["type"] == NODE_TYPE_METHOD
                    and display_name.rsplit(".", 1)[-1] == constructor_leaf
                ):
                    constructors.append(child["name"])
        return constructors

    def _get_semantic_tokens(
        self,
        abs_file: str,
        file_path: str,
        line_ranges: list[tuple[int, int]] | None = None,
    ) -> list[dict] | None:
        """Get filtered semantic tokens, with caching.

        Prefers semanticTokens/range when available, falls back to full.
        """
        cache_key = (file_path, tuple(line_ranges or ()))
        if cache_key in self._semantic_tokens_cache:
            cached = self._semantic_tokens_cache[cache_key]
            return list(cached) if cached is not None else None

        if not self.lsp_client:
            return None

        tokens_response = None

        # Try range first
        if line_ranges and self.lsp_client.supports_semantic_tokens_range:
            min_line = min(s for s, _ in line_ranges)
            max_line = max(e for _, e in line_ranges)
            tokens_response = self.lsp_client.semantic_tokens_range(
                abs_file,
                min_line,
                max_line,
            )

        # Fall back to full
        if tokens_response is None or not tokens_response.get("data"):
            if not line_ranges:
                reason = "no line_ranges"
            elif not self.lsp_client.supports_semantic_tokens_range:
                reason = "server does not support range"
            else:
                reason = "range returned empty"
            logger.debug(f"Using semanticTokens/full for {file_path} ({reason})")
            tokens_response = self.lsp_client.semantic_tokens_full(abs_file)

        # No retry: null/empty semanticTokens is a legitimate response per
        # LSP spec. On pathological files (e.g. basedpyright hanging on
        # sklearn's test_pls.py for 60s) a retry pays the full server
        # timeout twice. The cache entry below ensures repeat callers
        # within this patcher run skip immediately.
        if not tokens_response or not tokens_response.get("data"):
            logger.warning(
                f"semanticTokens failed for {file_path}, "
                "skipping outgoing reference discovery"
            )
            self._semantic_tokens_cache[cache_key] = None
            return None

        tokens = self.lsp_client.decode_semantic_tokens(tokens_response, abs_file)
        if not tokens:
            return None

        crossfile_types = self._get_crossfile_token_types()
        filtered = [
            t
            for t in tokens
            if "declaration" not in t["modifiers"]
            and "definition" not in t["modifiers"]
            and t["token_type"] in crossfile_types
        ]
        self._semantic_tokens_cache[cache_key] = filtered
        return list(filtered)

    @staticmethod
    def _get_lexical_reference_tokens(
        abs_file: str,
        line_ranges: list[tuple[int, int]],
    ) -> list[dict]:
        """Supplement incomplete semantic tokens inside changed line ranges.

        LSP servers may omit navigable syntax such as JSX element and property
        names from ``semanticTokens`` even though ``definition`` resolves at
        those positions. Lexical candidates remain provider-agnostic: keyword,
        comment, and local-variable positions are discarded naturally when
        definition lookup or graph matching returns no result.
        """

        try:
            lines = (
                Path(abs_file)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
        except OSError:
            return []

        selected_lines = set()
        for start, end in line_ranges:
            if end < 0 or start >= len(lines):
                continue
            selected_lines.update(range(max(start, 0), min(end, len(lines) - 1) + 1))

        tokens = []
        for line_number in sorted(selected_lines):
            line = lines[line_number]
            for match in _LEXICAL_IDENTIFIER_RE.finditer(line):
                prefix = line[: match.start()]
                text = match.group(0)
                tokens.append(
                    {
                        "line": line_number,
                        "character": len(prefix.encode("utf-16-le")) // 2,
                        "length": len(text.encode("utf-16-le")) // 2,
                        "token_type": "lexical",
                        "modifiers": [],
                        "text": text,
                    }
                )
        return tokens

    def _reference_scope_for_token(
        self,
        file_path: str,
        token: dict,
        preferred_scope: str | None,
    ) -> str | None:
        """Resolve the source vertex that owns one reference token.

        Language patchers may override this when evaluation scope differs
        from the innermost declaration range. Python decorators are one such
        case: their expressions execute in the decorated definition's parent
        scope.
        """

        return self.match_location_to_scope(file_path, token["line"]) or preferred_scope

    def _containment_parent(self, vertex_name: str) -> str | None:
        """Return a vertex's direct containment parent, when available."""

        graph = self.code_graph
        vertex_id = graph.name_to_vertex.get(vertex_name)
        if vertex_id is None:
            return None
        for edge_id in graph.graph.incident(vertex_id, mode="in"):
            edge = graph.graph.es[edge_id]
            if edge["type"] == EDGE_TYPE_CONTAIN and edge.target == vertex_id:
                return graph.graph.vs[edge.source]["name"]
        return None

    # ═══════════════════════════════════════════════════════════
    # Location matching
    # ═══════════════════════════════════════════════════════════

    def _vertex_line_range(self, vid: int) -> tuple[int, int] | None:
        """Return a vertex's source line range from indexes or attributes."""
        g = self.code_graph
        v = g.graph.vs[vid]
        sr = g.symbol_ranges.get(v["name"])
        if sr is not None:
            return sr

        attrs = v.attributes()
        start_line = attrs.get("start_line")
        if start_line is None:
            return None
        end_line = attrs.get("end_line", start_line)
        if end_line is None:
            end_line = start_line
        return start_line, end_line

    @staticmethod
    def _unified_name_leaf(unified_name: str) -> str:
        """Return a language-neutral final symbol component."""

        leaf = unified_name.rsplit(":", 1)[-1]
        leaf = leaf.rstrip("()")
        return leaf.rsplit(".", 1)[-1]

    def match_location_to_vertex(
        self,
        file_path: str,
        line: int,
        symbol_name: str | None = None,
        allow_scope_fallback: bool = True,
        allow_name_fallback: bool = True,
    ) -> Optional[str]:
        """Match an LSP location to an existing graph vertex.

        Level 1: Exact (file, start_line) match.
        Level 2: Unique symbol-name match within the target file, when
        ``allow_name_fallback`` is true. This handles LSPs that choose a
        different duplicate declaration than the static index used to build
        the graph. Variables disable it because an unindexed local ``X`` must
        not resolve to an unrelated module-level ``X``.
        Level 3: Innermost enclosing scope, when ``allow_scope_fallback`` is
        true. Outgoing definition resolution disables this fallback because
        mapping an unindexed local variable to its containing function creates
        a false self-reference edge.

        Uses ``file_to_vertices`` to constrain the scan to one file's
        vertices instead of the whole graph (5985 calls × 31k vertices
        was a 500s+ hot loop on ruff).
        """
        g = self.code_graph
        vids = getattr(g, "file_to_vertices", {}).get(file_path, ())
        candidates = []
        for vid in vids:
            vname = g.graph.vs[vid]["name"]
            sr = self._vertex_line_range(vid)
            if sr and sr[0] == line:
                candidates.append(vname)
        if symbol_name and len(candidates) > 1:
            named_candidates = [
                vname
                for vname in candidates
                if self._unified_name_leaf(
                    str(
                        g.graph.vs[g.name_to_vertex[vname]]
                        .attributes()
                        .get("unified_name")
                        or ""
                    )
                )
                == symbol_name
            ]
            if len(named_candidates) == 1:
                return named_candidates[0]
        if len(candidates) == 1:
            return candidates[0]

        if symbol_name and allow_name_fallback:
            named_candidates = []
            for vid in vids:
                attrs = g.graph.vs[vid].attributes()
                unified_name = str(attrs.get("unified_name") or "")
                if self._unified_name_leaf(unified_name) == symbol_name:
                    named_candidates.append(g.graph.vs[vid]["name"])
            if len(named_candidates) == 1:
                return named_candidates[0]
        if allow_scope_fallback:
            return self.match_location_to_scope(file_path, line)
        return None

    def match_location_to_scope(self, file_path: str, line: int) -> Optional[str]:
        """Find the innermost scope vertex containing (file, line)."""
        g = self.code_graph
        vids = getattr(g, "file_to_vertices", {}).get(file_path, ())
        candidates = []
        for vid in vids:
            vname = g.graph.vs[vid]["name"]
            sr = self._vertex_line_range(vid)
            if sr and sr[0] <= line <= sr[1]:
                candidates.append((vname, sr[1] - sr[0]))

        if not candidates:
            if file_path in g.name_to_vertex:
                return file_path
            return None

        return min(candidates, key=lambda x: x[1])[0]

    def clear_cache(self):
        """Clear semantic tokens cache between patch_files runs."""
        self._semantic_tokens_cache.clear()
