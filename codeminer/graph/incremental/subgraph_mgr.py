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

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

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
from .lsp_client import LSPClient, uri_to_relpath

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

# Symbol types worth querying for cross-file incoming references.
# Variables, fields, and parameters are file-local — querying references
# for them causes LSP servers to scan the entire workspace, which is slow.
_INCOMING_REF_TYPES = frozenset(
    {
        NODE_TYPE_CLASS,
        NODE_TYPE_FUNCTION,
        NODE_TYPE_METHOD,
    }
)


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
        self._semantic_tokens_cache: dict[str, list[dict] | None] = {}

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
        for v in g.graph.vs:
            un = v.attributes().get("unified_name")
            if un:
                g.unified_name_to_vertex.setdefault(un, []).append(v.index)

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
            vid = self.code_graph.name_to_vertex.get(vname)
            if vid is not None:
                node_type = self.code_graph.graph.vs[vid].attributes().get("type", "")
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
                ref_file = uri_to_relpath(loc.get("uri", ""), str(self.project_root))
                if ref_file is None:
                    continue
                ref_line = loc.get("range", {}).get("start", {}).get("line", 0)
                ref_scope = self.match_location_to_scope(ref_file, ref_line)
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

    def reconnect_outgoing(
        self,
        file_path: str,
        vertex_names: list[str],
        stats: dict,
        line_ranges: list[tuple[int, int]] | None = None,
        batch: "EdgeBatch | None" = None,
    ):
        """Use semanticTokens + definition to find outgoing refs."""
        if not self.lsp_client:
            return
        abs_file = str(self.project_root / file_path)

        ref_tokens = self._get_semantic_tokens(
            abs_file,
            file_path,
            line_ranges=line_ranges,
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

        # Deduplicate by text for definition lookup
        seen_text = {}
        for t in ref_tokens:
            key = t["text"]
            if key not in seen_text:
                seen_text[key] = self.lsp_client.definition(
                    abs_file, t["line"], t["character"]
                )

        # Retry empty definitions once with NO sleep — null is a legitimate
        # "no definition here" answer per LSP spec, but on cold servers a
        # second pass occasionally resolves more. The previous 3s sleep
        # added per-file overhead with ~zero real benefit (most empties
        # remain empty on retry).
        empty_keys = [k for k, v in seen_text.items() if v == []]
        if empty_keys:
            text_to_token = {}
            for t in ref_tokens:
                if t["text"] not in text_to_token:
                    text_to_token[t["text"]] = t
            resolved = 0
            for key in empty_keys:
                tok = text_to_token.get(key)
                if tok:
                    result = self.lsp_client.definition(
                        abs_file, tok["line"], tok["character"]
                    )
                    if result:
                        seen_text[key] = result
                        resolved += 1
            if resolved:
                logger.debug(
                    f"Retry resolved {resolved}/{len(empty_keys)} " "empty definitions"
                )

        # Build edges. Each call site becomes its own anchored edge so the
        # multi-edge schema preserves call-site identity for range queries.
        # _add_edge dedups on (src, tgt, type, anchor_file, anchor_line),
        # so identical anchors collapse but distinct call sites stay distinct.
        for ref_token in ref_tokens:
            defn_list = seen_text.get(ref_token["text"])
            if not defn_list:
                continue

            defn = defn_list[0]
            target_uri = defn.get("targetUri", defn.get("uri", ""))
            target_file = uri_to_relpath(target_uri, str(self.project_root))
            if target_file is None:
                continue

            target_range = defn.get("targetSelectionRange", defn.get("range", {}))
            target_line = target_range.get("start", {}).get("line", 0)

            scope = None
            if range_to_vertex:
                for (start, end), vname in range_to_vertex.items():
                    if start <= ref_token["line"] <= end:
                        scope = vname
                        break
            if scope is None:
                scope = self.match_location_to_scope(file_path, ref_token["line"])

            target_vertex = self.match_location_to_vertex(target_file, target_line)

            if scope and target_vertex:
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
            else:
                stats["unmatched"] += 1

    def _get_semantic_tokens(
        self,
        abs_file: str,
        file_path: str,
        line_ranges: list[tuple[int, int]] | None = None,
    ) -> list[dict] | None:
        """Get filtered semantic tokens, with caching.

        Prefers semanticTokens/range when available, falls back to full.
        """
        if file_path in self._semantic_tokens_cache:
            cached = self._semantic_tokens_cache[file_path]
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
            self._semantic_tokens_cache[file_path] = None
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
        self._semantic_tokens_cache[file_path] = filtered
        return list(filtered)

    # ═══════════════════════════════════════════════════════════
    # Location matching
    # ═══════════════════════════════════════════════════════════

    def match_location_to_vertex(self, file_path: str, line: int) -> Optional[str]:
        """Match an LSP location to an existing graph vertex.

        Level 1: Exact (file, start_line) match.
        Level 2: Innermost enclosing scope.

        Uses ``file_to_vertices`` to constrain the scan to one file's
        vertices instead of the whole graph (5985 calls × 31k vertices
        was a 500s+ hot loop on ruff).
        """
        g = self.code_graph
        vids = getattr(g, "file_to_vertices", {}).get(file_path, ())
        candidates = []
        for vid in vids:
            vname = g.graph.vs[vid]["name"]
            sr = g.symbol_ranges.get(vname)
            if sr and sr[0] == line:
                candidates.append(vname)
        if len(candidates) == 1:
            return candidates[0]
        return self.match_location_to_scope(file_path, line)

    def match_location_to_scope(self, file_path: str, line: int) -> Optional[str]:
        """Find the innermost scope vertex containing (file, line)."""
        g = self.code_graph
        vids = getattr(g, "file_to_vertices", {}).get(file_path, ())
        candidates = []
        for vid in vids:
            vname = g.graph.vs[vid]["name"]
            sr = g.symbol_ranges.get(vname)
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
