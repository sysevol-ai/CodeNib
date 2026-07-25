# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""PatcherBase: incremental orchestration for CodeGraph updates.

Handles the high-level patch flow (classify, remap, patch) while
delegating subgraph operations to SubgraphMgr and language-specific
behavior to patcher_lang subclasses.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Optional

from ...languages import lsp_command_for_language, lsp_language_id_for_language
from ...log_utils import get_logger
from ...profiler import Profiler
from ...types import EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE
from ..code_graph import CodeGraph
from . import change_mgr
from .lsp_client import LSPClient
from .subgraph_mgr import SubgraphMgr

logger = get_logger(__name__)


class PatcherBase(SubgraphMgr):
    """Base class for language-specific incremental patchers.

    Inherits SubgraphMgr for subgraph operations. Adds:
    - patch_files: top-level entry point
    - _rebuild_prepare_vertices / _rebuild_connect_edges: full rebuild for added/renamed
    - _incremental_prepare_vertices / _incremental_connect_edges: symbol-level diff
    - _classify_symbols, _remap_severed_edges, etc.

    Subclasses must implement:
    - _build_unified_name: construct unified_name from LSP symbol info
    - _get_crossfile_token_types: which semanticToken types to query

    Subclasses may override:
    - get_lsp_command: custom LSP server command
    - _language_id: custom LSP language id
    - flatten_symbols: convert LSP documentSymbol to flat dict (default provided)
    - get_old_symbols: extract old symbols from graph (default provided)
    - GRAPH_SYMBOL_KINDS: which SymbolKinds to process (default provided)
    """

    # Subclasses override: which LSP SymbolKinds to include in classification.
    # Default covers most languages. C++ overrides completely.
    GRAPH_SYMBOL_KINDS = frozenset(
        {
            2,  # Module
            5,  # Class
            6,  # Method
            8,  # Field
            9,  # Constructor
            10,  # Enum
            11,  # Interface
            12,  # Function
            13,  # Variable
            14,  # Constant
            22,  # Enum (alt)
            23,  # Struct
            25,  # Operator
        }
    )
    REGISTRY_LANGUAGE: str | None = None

    def __init__(
        self,
        project_root: str,
        code_graph: CodeGraph,
        lsp_client: Optional[LSPClient] = None,
        mode: str = "symbol",
    ):
        """``mode`` is one of:

        - ``"symbol"`` (default): the actual symbol-level incremental patcher.
        - ``"naive"``: file-granularity baseline used by the sequential
          benchmark (issue #129). For modified files, every old symbol is
          classified as deleted and every new symbol as added — no shifted /
          affected / unchanged / invisible buckets. Untouched files are still
          left alone. Only the classifier behaviour changes; the rest of the
          pipeline (LSP queries, edge reconnection) is identical to symbol
          mode, which is exactly what makes it the right A/B comparison.
        """
        super().__init__(project_root, code_graph, lsp_client)
        if mode not in ("symbol", "naive"):
            raise ValueError(f"unknown patcher mode: {mode!r}")
        self.mode = mode
        self.profiler = Profiler()

    # ═══════════════════════════════════════════════════════════
    # Abstract methods — language-specific
    # ═══════════════════════════════════════════════════════════

    def get_lsp_command(self) -> list[str]:
        """Return the LSP server command for this language."""
        if not self.REGISTRY_LANGUAGE:
            raise NotImplementedError(
                f"{self.__class__.__name__} must define REGISTRY_LANGUAGE "
                "or override get_lsp_command()."
            )
        command = lsp_command_for_language(self.REGISTRY_LANGUAGE)
        if command is None:
            raise ValueError(
                f"No LSP command registered for {self.REGISTRY_LANGUAGE!r}"
            )
        return command

    @abstractmethod
    def flatten_symbols(
        self, file_path: str, lsp_symbols: list[dict]
    ) -> dict[str, dict]:
        """Convert LSP documentSymbol to flat {unified_name: metadata} dict.

        Must filter by GRAPH_SYMBOL_KINDS and handle language-specific
        naming (e.g. Rust impl blocks, Go pointer receivers).
        """

    def get_old_symbols(self, file_path: str) -> dict[str, dict]:
        """Extract existing definition vertices from the graph for a file.

        Default implementation: returns symbols with valid start_line
        (skips SCIP reference-only vertices).
        Override for language-specific filtering.
        """
        g = self.code_graph
        result = {}
        vids = getattr(g, "file_to_vertices", {}).get(file_path, set())
        for vid in vids:
            v = g.graph.vs[vid]
            attrs = v.attributes()
            uname = attrs.get("unified_name")
            if not uname:
                continue
            sl = attrs.get("start_line")
            if sl is None:
                continue  # SCIP reference vertex, not a definition
            result[uname] = {
                "vertex_name": v["name"],
                "start_line": sl,
                "end_line": attrs.get("end_line", sl),
            }
        return result

    def _flatten_symbols_default(
        self,
        file_path: str,
        symbols: list[dict],
        parent_uname: str = "",
    ) -> dict[str, dict]:
        """Default flatten logic shared by most languages.

        Recursively walks documentSymbol tree, computing unified_name
        for each symbol. Filters by GRAPH_SYMBOL_KINDS and skips
        parameters/local variables.

        Language patchers call this from their flatten_symbols() method.
        """
        result = {}
        for sym in symbols or []:
            name = sym.get("name", "")
            kind = sym.get("kind", 0)

            # Variable/Constant inside functions are params/locals — skip
            if kind in (13, 14) and parent_uname and parent_uname.endswith("()"):
                continue

            # For kinds not tracked (e.g. kind=19 impl blocks), skip the
            # entry but still recurse into children (impl methods are tracked).
            if kind not in self.GRAPH_SYMBOL_KINDS:
                if kind == 2:
                    child_parent = parent_uname
                else:
                    uname = self._build_unified_name(
                        file_path, name, parent_uname, kind
                    )
                    child_parent = uname.split(":", 1)[1] if ":" in uname else name
                for child in sym.get("children", []):
                    child_result = self._flatten_symbols_default(
                        file_path, [child], child_parent
                    )
                    result.update(child_result)
                continue

            range_data = sym.get("range", {})
            sel_range = sym.get("selectionRange", range_data)
            start = range_data.get("start", {}).get("line", 0)
            end = range_data.get("end", {}).get("line", start)

            uname = self._build_unified_name(file_path, name, parent_uname, kind)
            entry = {
                "kind": kind,
                "start_line": start,
                "end_line": end,
                "sel_range": sel_range,
                "parent_uname": parent_uname or None,
            }
            # Dedup: prefer struct/class/enum over impl blocks
            if uname in result:
                existing = result[uname]
                if existing["kind"] in (5, 10, 23) and kind not in (5, 10, 23):
                    pass  # keep existing definition
                elif kind in (5, 10, 23) and existing["kind"] not in (5, 10, 23):
                    result[uname] = entry
                else:
                    if (end - start) > (existing["end_line"] - existing["start_line"]):
                        result[uname] = entry
            else:
                result[uname] = entry

            # Recurse into children.
            # Module (kind=2, e.g. Rust `mod tests`) is transparent — SCIP
            # doesn't include module name in the symbol path.
            if kind == 2:
                child_parent = parent_uname  # skip module
            else:
                child_parent = uname.split(":", 1)[1] if ":" in uname else name
            for child in sym.get("children", []):
                child_result = self._flatten_symbols_default(
                    file_path, [child], child_parent
                )
                result.update(child_result)

        return result

    # ═══════════════════════════════════════════════════════════
    # LSP lifecycle
    # ═══════════════════════════════════════════════════════════

    def start_lsp(self, skip_probe: bool = False):
        """Start the LSP server, resolving binary path."""
        from .lsp_client import resolve_lsp_binary

        cmd = self.get_lsp_command()
        resolved = resolve_lsp_binary(cmd[0])
        if resolved:
            cmd = [resolved] + cmd[1:]

        self.lsp_client = LSPClient(cmd, str(self.project_root), self._language_id())
        self.lsp_client.start(skip_probe=skip_probe)

    def stop_lsp(self):
        """Stop the LSP server."""
        if self.lsp_client:
            self.lsp_client.shutdown()
            self.lsp_client = None

    def _language_id(self) -> str:
        """Return the LSP language ID. Override if needed."""
        if not self.REGISTRY_LANGUAGE:
            return "unknown"
        return lsp_language_id_for_language(self.REGISTRY_LANGUAGE) or "unknown"

    # ═══════════════════════════════════════════════════════════
    # Top-level patch entry point
    # ═══════════════════════════════════════════════════════════

    def _should_skip_path(self, path: str) -> bool:
        """Override to exclude language-specific files (e.g. _test.go) so
        the patcher mirrors the SCIP/clangd decoder's file scope. Default
        keeps every file."""
        return False

    def _filter_changed_files(self, changed_files: dict) -> dict:
        """Apply ``_should_skip_path`` to every entry in ``changed_files``."""
        skip = self._should_skip_path
        return {
            "modified": [p for p in changed_files.get("modified", []) if not skip(p)],
            "added": [p for p in changed_files.get("added", []) if not skip(p)],
            "deleted": [p for p in changed_files.get("deleted", []) if not skip(p)],
            "renamed": [
                (o, n)
                for o, n in changed_files.get("renamed", [])
                if not skip(n) and not skip(o)
            ],
        }

    def patch_files(
        self,
        changed_files: dict,
        earlier_commit: str = None,
        later_commit: str = None,
    ) -> dict:
        """Apply incremental patch for all changed files.

        Args:
            changed_files: dict with modified/added/deleted/renamed lists.
            earlier_commit: base commit (required for modified files).
            later_commit: target commit.

        Returns:
            Stats dict.
        """
        # Drop files the language excludes from indexing (e.g. _test.go for
        # Go, where SCIP-go skips test files by design — see
        # scip_decode_go.py).
        changed_files = self._filter_changed_files(changed_files)

        # Auto-start LSP if not already running
        if self.lsp_client is None:
            self.start_lsp()

        # Rebuild indexes (vertex IDs may have changed from previous patch)
        self.build_indexes()

        total_stats = {
            "files_deleted": 0,
            "files_modified": 0,
            "files_added": 0,
            "files_renamed": 0,
            "vertices_deleted": 0,
            "vertices_created": 0,
            "vertices_shifted": 0,
            "refs_incoming": 0,
            "refs_outgoing": 0,
            "refs_remapped": 0,
            "refs_unmatched": 0,
        }

        # Clear stale caches from previous patch_files calls
        self._semantic_tokens_cache.clear()

        nodes_before = self.code_graph.graph.vcount()
        edges_before = self.code_graph.graph.ecount()

        # ── Round 1: Build all vertices ──────────────────────────
        # Process deletions, then build vertices for added/renamed/modified.
        # No edge connections yet — ensures all vertices exist first.

        # 1a. Deletions
        for path in changed_files.get("deleted", []):
            with self.profiler.section("delete_subgraph"):
                self.delete_file_subgraph(path)
            total_stats["files_deleted"] += 1

        # 1b. Renames: delete old
        for old_path, _ in changed_files.get("renamed", []):
            with self.profiler.section("delete_subgraph"):
                self.delete_file_subgraph(old_path)

        # 1c. Added files: build vertices
        add_contexts = []
        for path in changed_files.get("added", []):
            ctx = self._rebuild_prepare_vertices(path, is_new=True)
            add_contexts.append((path, ctx))
            self._merge_stats(total_stats, ctx["file_stats"])
            total_stats["files_added"] += 1

        # 1d. Renamed files: build vertices for new path
        rename_contexts = []
        for _, new_path in changed_files.get("renamed", []):
            ctx = self._rebuild_prepare_vertices(new_path, is_new=True)
            rename_contexts.append((new_path, ctx))
            self._merge_stats(total_stats, ctx["file_stats"])
            total_stats["files_renamed"] += 1

        # 1e. Modified files: classify + build vertices
        modified = changed_files.get("modified", [])
        if modified and not earlier_commit:
            raise ValueError("earlier_commit required for modified files")
        mod_contexts = []
        for path in modified:
            ctx = self._incremental_prepare_vertices(path, earlier_commit)
            if ctx is not None:
                mod_contexts.append((path, ctx))
                self._merge_stats(total_stats, ctx["file_stats"])
            total_stats["files_modified"] += 1

        # ── Round 2: Connect all edges ───────────────────────────
        # All vertices from all files now exist.

        for path, ctx in add_contexts:
            edge_stats = self._rebuild_connect_edges(path, ctx)
            self._merge_stats(total_stats, edge_stats)

        for path, ctx in rename_contexts:
            edge_stats = self._rebuild_connect_edges(path, ctx)
            self._merge_stats(total_stats, edge_stats)

        for path, ctx in mod_contexts:
            edge_stats = self._incremental_connect_edges(path, ctx)
            self._merge_stats(total_stats, edge_stats)

        nodes_after = self.code_graph.graph.vcount()
        edges_after = self.code_graph.graph.ecount()
        total_stats["nodes_before"] = nodes_before
        total_stats["nodes_after"] = nodes_after
        total_stats["edges_before"] = edges_before
        total_stats["edges_after"] = edges_after

        commit_info = ""
        if earlier_commit and later_commit:
            total_stats["commit_earlier"] = earlier_commit
            total_stats["commit_later"] = later_commit
            commit_info = f"commits {earlier_commit[:12]}..{later_commit[:12]}, "

        total_changed = sum(
            len(changed_files.get(k, []))
            for k in ("deleted", "added", "renamed", "modified")
        )
        logger.info(
            f"Incremental patch summary: {commit_info}"
            f"{total_changed} files changed, "
            f"nodes {nodes_before}→{nodes_after} (Δ{nodes_after - nodes_before:+d}), "
            f"edges {edges_before}→{edges_after} (Δ{edges_after - edges_before:+d})"
        )

        # Rebuild line-range indexes after each patcher batch so range
        # queries reflect the post-patch graph state. Cost is O(V+E),
        # negligible relative to the patch itself.
        with self.profiler.section("patch_files.build_range_indexes"):
            self.code_graph.build_range_indexes()

        # Profiler summary — also stash structured timings on the patcher so
        # benchmarks can read them without parsing logs.
        report = self.profiler.report(reset=True)
        total_stats["profile"] = {
            label: {"total_s": st.total, "count": st.count} for label, st in report
        }

        return total_stats

    # ═══════════════════════════════════════════════════════════
    # Single file: full rebuild (added/renamed)
    # ═══════════════════════════════════════════════════════════

    def _rebuild_prepare_vertices(
        self,
        file_path: str,
        is_new: bool = False,
        line_ranges: list[tuple[int, int]] | None = None,
    ) -> dict:
        """Round 1 for full rebuild: delete old + build new vertices + remap."""
        file_stats = {
            "vertices_deleted": 0,
            "vertices_created": 0,
            "refs_incoming": 0,
            "refs_outgoing": 0,
            "refs_remapped": 0,
            "refs_unmatched": 0,
        }

        severed_in = []
        severed_out = []

        if not is_new:
            with self.profiler.section("delete_subgraph"):
                result = self.delete_file_subgraph(file_path)
                file_stats["vertices_deleted"] = len(result["deleted_vertex_names"])
                severed_in = result["severed_incoming_refs"]
                severed_out = result["severed_outgoing_refs"]

        with self.profiler.section("lsp_document_symbol"):
            abs_file = str(self.project_root / file_path)
            symbols = self.lsp_client.document_symbol(abs_file)
            new_vertices = self.rebuild_file_subgraph(file_path, symbols)
            file_stats["vertices_created"] = len(new_vertices)

        with self.profiler.section("remap_edges"):
            remapped = self._remap_severed_edges(
                new_vertices,
                severed_in,
                severed_out,
                changed_line_ranges=line_ranges,
            )
            file_stats["refs_remapped"] = remapped

        return {
            "file_stats": file_stats,
            "new_vertices": new_vertices,
            "severed_in": severed_in,
            "severed_out": severed_out,
        }

    def _rebuild_connect_edges(
        self,
        file_path: str,
        ctx: dict,
        line_ranges: list[tuple[int, int]] | None = None,
    ) -> dict:
        """Round 2 for full rebuild: connect edges via LSP."""
        edge_stats = {
            "refs_incoming": 0,
            "refs_outgoing": 0,
            "refs_unmatched": 0,
        }
        new_vertices = ctx["new_vertices"]

        remapped_unames = self._get_remapped_unames(
            new_vertices,
            ctx["severed_in"],
            ctx["severed_out"],
        )
        new_only = [v for v in new_vertices if v not in remapped_unames]

        ref_stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}
        if new_only:
            with self.profiler.section("lsp_incoming_refs"):
                self.reconnect_incoming(file_path, new_only, ref_stats)

        if line_ranges:
            outgoing_ranges = self._compute_outgoing_ranges(
                new_vertices,
                line_ranges,
            )
        else:
            outgoing_ranges = []
            for vname in new_vertices:
                sr = self.code_graph.symbol_ranges.get(vname)
                if sr:
                    outgoing_ranges.append(sr)
            if not outgoing_ranges:
                outgoing_ranges = None

        with self.profiler.section("lsp_outgoing_refs"):
            self.reconnect_outgoing(
                file_path,
                new_vertices,
                ref_stats,
                line_ranges=outgoing_ranges,
            )

        edge_stats["refs_incoming"] = ref_stats["incoming_added"]
        edge_stats["refs_outgoing"] = ref_stats["outgoing_added"]
        edge_stats["refs_unmatched"] = ref_stats["unmatched"]
        return edge_stats

    # ═══════════════════════════════════════════════════════════
    # Single file: symbol-level incremental (modified)
    # ═══════════════════════════════════════════════════════════

    def _incremental_prepare_vertices(
        self, file_path: str, base_commit: str
    ) -> Optional[dict]:
        """Round 1: classify symbols and build/delete/shift vertices.

        Returns a context dict for Round 2 (edge connection), or None
        if no hunks were found (file unchanged at line level).
        """
        file_stats = {
            "vertices_deleted": 0,
            "vertices_created": 0,
            "vertices_shifted": 0,
            "refs_incoming": 0,
            "refs_outgoing": 0,
            "refs_remapped": 0,
            "refs_unmatched": 0,
        }

        with self.profiler.section("git_diff"):
            hunks = change_mgr.get_changed_line_ranges(
                str(self.project_root), file_path, base_commit
            )
        if not hunks:
            return None

        abs_file = str(self.project_root / file_path)
        with self.profiler.section("lsp_document_symbol"):
            raw_symbols = self.lsp_client.document_symbol(abs_file)
            new_symbols = self.flatten_symbols(file_path, raw_symbols)
        old_symbols = self.get_old_symbols(file_path)

        if not old_symbols:
            # No old symbols (SCIP didn't index this file) — fall back to
            # full rebuild but still in two rounds so cross-file edges work.
            ctx = self._rebuild_prepare_vertices(file_path, is_new=False)
            ctx["fallback_rebuild"] = True
            return ctx

        with self.profiler.section("classify_symbols"):
            classified = self._classify_symbols(old_symbols, new_symbols, hunks)

        # DELETED
        for uname in classified["deleted"]:
            old = old_symbols[uname]
            self.delete_vertices_by_name([old["vertex_name"]])
            file_stats["vertices_deleted"] += 1

        # SHIFTED
        for uname in classified["shifted"]:
            self._process_shifted(
                uname=uname,
                old=old_symbols[uname],
                new=new_symbols[uname],
                file_path=file_path,
                file_stats=file_stats,
            )

        # AFFECTED — fast (preserved) / slow (rebuilt) split per uname
        affected_vnames = []
        affected_changed_ranges = []
        for uname in classified["affected"]:
            new_vname, changed = self._process_affected(
                uname=uname,
                old=old_symbols[uname],
                new=new_symbols[uname],
                hunks=hunks,
                file_path=file_path,
                file_stats=file_stats,
            )
            affected_vnames.append(new_vname)
            affected_changed_ranges.append(changed)

        # ADDED (create vertices, collect for Round 2)
        added_vnames = []
        for uname in classified["added"]:
            new = new_symbols[uname]
            vname = f"{uname}:{new['start_line']}"
            node_type = self._classify_symbol_type(new["kind"])

            self.code_graph._add_vertex(
                vname,
                {
                    "type": node_type,
                    "file": file_path,
                    "start_line": new["start_line"],
                    "end_line": new["end_line"],
                    "unified_name": uname,
                },
            )
            self.code_graph.symbol_ranges[vname] = (new["start_line"], new["end_line"])

            parent_uname = new.get("parent_uname")
            parent_vname = (
                self._find_vertex_by_unified_name(parent_uname, file_path)
                if parent_uname
                else file_path
            )
            if parent_vname:
                self.code_graph._add_edge(parent_vname, vname, EDGE_TYPE_CONTAIN)

            self._store_selection_range(vname, new)
            added_vnames.append(vname)
            file_stats["vertices_created"] += 1

        logger.info(
            f"Incremental patch {file_path}: "
            f"del={len(classified['deleted'])} "
            f"add={len(classified['added'])} "
            f"affected={len(classified['affected'])} "
            f"shifted={len(classified['shifted'])} "
            f"unchanged={len(classified['unchanged'])} "
            f"invisible={len(classified.get('invisible', []))}"
        )

        return {
            "file_stats": file_stats,
            "classified": classified,
            "affected_vnames": affected_vnames,
            "affected_changed_ranges": affected_changed_ranges,
            "added_vnames": added_vnames,
            "hunks": hunks,
        }

    def _incremental_connect_edges(self, file_path: str, ctx: dict) -> dict:
        """Round 2: connect reference edges using LSP queries.

        Called after all files' vertices have been prepared.
        """
        edge_stats = {
            "refs_incoming": 0,
            "refs_outgoing": 0,
            "refs_unmatched": 0,
        }

        if ctx.get("fallback_rebuild"):
            # Fallback: old_symbols was empty, used _rebuild_prepare_vertices.
            # Now connect edges via _rebuild_connect_edges.
            return self._rebuild_connect_edges(file_path, ctx)

        affected_vnames = ctx["affected_vnames"]
        affected_changed_ranges = ctx["affected_changed_ranges"]
        added_vnames = ctx["added_vnames"]

        # Stage all reference edges through one EdgeBatch and flush at the
        # end of this file's processing. Avoids ~5000 single-edge
        # ``add_edges`` calls per patch (Python↔C round-trip dominated).
        from .subgraph_mgr import EdgeBatch

        batch = EdgeBatch(self.code_graph)

        # Affected: outgoing refs for changed lines
        if affected_vnames:
            flat_vnames = []
            flat_ranges = []
            for vname, ranges in zip(
                affected_vnames, affected_changed_ranges, strict=False
            ):
                for r in ranges:
                    flat_vnames.append(vname)
                    flat_ranges.append(r)

            ref_stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}
            with self.profiler.section("lsp_outgoing_refs"):
                self.reconnect_outgoing(
                    file_path,
                    flat_vnames,
                    ref_stats,
                    line_ranges=flat_ranges,
                    batch=batch,
                )
            edge_stats["refs_outgoing"] = ref_stats["outgoing_added"]

        # Added: incoming + outgoing refs
        if added_vnames:
            added_ranges = []
            for vname in added_vnames:
                sr = self.code_graph.symbol_ranges.get(vname)
                if sr:
                    added_ranges.append(sr)
                else:
                    vid = self.code_graph.name_to_vertex.get(vname)
                    if vid is not None:
                        v = self.code_graph.graph.vs[vid]
                        sl = v.attributes().get("start_line", 0)
                        el = v.attributes().get("end_line", sl)
                        added_ranges.append((sl, el))

            ref_stats = {"incoming_added": 0, "outgoing_added": 0, "unmatched": 0}
            with self.profiler.section("lsp_incoming_refs"):
                self.reconnect_incoming(file_path, added_vnames, ref_stats, batch=batch)
            with self.profiler.section("lsp_outgoing_refs"):
                effective_ranges = (
                    added_ranges if len(added_ranges) == len(added_vnames) else None
                )
                self.reconnect_outgoing(
                    file_path,
                    added_vnames,
                    ref_stats,
                    line_ranges=effective_ranges,
                    batch=batch,
                )
            edge_stats["refs_incoming"] = ref_stats["incoming_added"]
            edge_stats["refs_outgoing"] += ref_stats["outgoing_added"]

        with self.profiler.section("edge_batch_flush"):
            batch.flush()

        return edge_stats

    # ═══════════════════════════════════════════════════════════
    # Symbol classification
    # ═══════════════════════════════════════════════════════════

    def _classify_symbols(
        self,
        old_symbols: dict[str, dict],
        new_symbols: dict[str, dict],
        hunks: list[tuple[int, int, int, int]],
    ) -> dict[str, list[str]]:
        """Classify each symbol as deleted/added/affected/shifted/unchanged.

        ``hunks`` is a list of (old_start, old_end, new_start, new_end).
        Common-symbol overlap uses the new-file side; old-only-symbol
        overlap uses the old-file side.

        old_set − new_set is split into:
          - deleted: old uname whose old-side range overlaps a hunk
          - invisible: old uname outside every hunk. The base graph
            indexer (e.g. SCIP) saw this symbol but ``documentSymbol``
            cannot (typically macro-expanded enum variants). Since the
            location wasn't touched, leave the vertex untouched.
        """

        def overlaps_any_hunk(start, end):
            return any(
                start <= h_new_e and end >= h_new_s
                for (_old_s, _old_e, h_new_s, h_new_e) in hunks
            )

        def overlaps_any_hunk_old(start, end):
            return any(
                start <= h_old_e and end >= h_old_s
                for (h_old_s, h_old_e, _new_s, _new_e) in hunks
            )

        old_set = set(old_symbols.keys())
        new_set = set(new_symbols.keys())

        # Naive mode (issue #129 baseline): in a modified file, drop every
        # old symbol and rebuild every new one. No symbol-level diff. The
        # rest of the pipeline (reconnect_outgoing / reconnect_incoming) then
        # issues LSP queries for *every* new symbol — by design, since the
        # whole point of naive mode is to bound how much LSP traffic the
        # symbol-level diff is saving.
        if getattr(self, "mode", "symbol") == "naive":
            return {
                "deleted": sorted(old_set),
                "added": sorted(new_set),
                "affected": [],
                "shifted": [],
                "unchanged": [],
                "invisible": [],
            }

        deleted = []
        invisible = []
        for uname in sorted(old_set - new_set):
            old = old_symbols[uname]
            sl = old["start_line"]
            el = old.get("end_line", sl)
            if overlaps_any_hunk_old(sl, el):
                deleted.append(uname)
            else:
                invisible.append(uname)

        added = sorted(new_set - old_set)
        common = old_set & new_set

        affected = []
        shifted = []
        unchanged = []

        for uname in sorted(common):
            new = new_symbols[uname]
            if overlaps_any_hunk(new["start_line"], new["end_line"]):
                affected.append(uname)
            else:
                old = old_symbols[uname]
                if old["start_line"] != new["start_line"]:
                    shifted.append(uname)
                else:
                    unchanged.append(uname)

        # Leaf-priority: if a parent (struct) is affected only because
        # a child (method) is affected, demote parent to shifted.
        demote = []
        for uname in affected:
            children_in_affected = [
                u for u in affected if u != uname and u.startswith(uname + ".")
            ]
            if children_in_affected and not overlaps_any_hunk(
                new_symbols[uname]["start_line"],
                new_symbols[uname]["start_line"],
            ):
                demote.append(uname)
        for uname in demote:
            affected.remove(uname)
            shifted.append(uname)

        return {
            "deleted": deleted,
            "added": added,
            "affected": affected,
            "shifted": shifted,
            "unchanged": unchanged,
            "invisible": invisible,
        }

    # ═══════════════════════════════════════════════════════════
    # Per-classification processing helpers
    # ═══════════════════════════════════════════════════════════

    def _process_shifted(
        self,
        uname: str,
        old: dict,
        new: dict,
        file_path: str,
        file_stats: dict,
    ) -> None:
        """Apply a *shifted* symbol update: body unchanged, start_line moved.

        Steps (order matters):
          1. Rebase outgoing anchored REFERENCE edges from the OLD vname by
             ``shift = new.start_line - old.start_line`` so range queries
             keep matching the moved call sites.
          2. Rename the vertex to the new ``"{uname}:{new_start}"`` form
             and refresh ``start_line``/``end_line`` attrs.
          3. Update the selection range index for downstream LSP probes.
        """
        old_vname = old["vertex_name"]
        new_vname = f"{uname}:{new['start_line']}"

        shift = new["start_line"] - old["start_line"]
        if shift:
            self.shift_outgoing_anchor_lines(
                vertex_name=old_vname,
                anchor_file=file_path,
                old_start=old["start_line"],
                old_end=old["end_line"],
                shift=shift,
            )

        self.rename_vertex(
            old_vname,
            new_vname,
            {
                "start_line": new["start_line"],
                "end_line": new["end_line"],
            },
        )
        self._update_selection_range(old_vname, new_vname, new)
        file_stats["vertices_shifted"] += 1

    def _process_affected(
        self,
        uname: str,
        old: dict,
        new: dict,
        hunks: list[tuple[int, int, int, int]],
        file_path: str,
        file_stats: dict,
    ) -> tuple[str, list[tuple[int, int]]]:
        """Apply an *affected* symbol update — body has hunks overlapping it.

        Two paths, decided per-symbol:

        - **Fast (case 3, body length preserved)**: every hunk overlapping
          the body has equal old/new line counts. Surgical:
            1. Delete outgoing REFERENCE edges with anchors in the OLD
               changed regions (anchors there are stale).
            2. Uniform-shift remaining body anchors by ``delta = new.start
               - old.start`` (file-level shift).
            3. Rename / refresh attrs.
          Round 2 then reconnects only the changed regions.

        - **Slow (case 4, body length changed)**: at least one hunk's
          line count changed. Internal anchor positions are non-uniform,
          so we wipe ALL outgoing reference edges and let Round 2
          reconnect the full body.

        Returns ``(new_vname, ranges_for_round_2)`` where ranges are in
        NEW-file coordinates.
        """
        old_vname = old["vertex_name"]
        new_vname = f"{uname}:{new['start_line']}"
        old_start, old_end = old["start_line"], old["end_line"]
        new_start, new_end = new["start_line"], new["end_line"]

        overlapping_old: list[tuple[int, int]] = []
        overlapping_new: list[tuple[int, int]] = []
        all_preserving = True
        for h_old_s, h_old_e, h_new_s, h_new_e in hunks:
            body_overlap_old = h_old_s <= old_end and h_old_e >= old_start
            body_overlap_new = h_new_s <= new_end and h_new_e >= new_start
            if not body_overlap_old and not body_overlap_new:
                continue
            old_count = h_old_e - h_old_s + 1
            new_count = h_new_e - h_new_s + 1
            if old_count != new_count:
                all_preserving = False
            if body_overlap_old:
                overlapping_old.append((max(h_old_s, old_start), min(h_old_e, old_end)))
            if body_overlap_new:
                overlapping_new.append((max(h_new_s, new_start), min(h_new_e, new_end)))

        if all_preserving:
            # Fast path: surgical anchor cleanup + uniform shift.
            if overlapping_old:
                self.delete_outgoing_in_anchor_ranges(
                    vertex_name=old_vname,
                    anchor_file=file_path,
                    ranges=overlapping_old,
                )
            delta = new_start - old_start
            if delta:
                self.shift_outgoing_anchor_lines(
                    vertex_name=old_vname,
                    anchor_file=file_path,
                    old_start=old_start,
                    old_end=old_end,
                    shift=delta,
                )
            file_stats["vertices_affected_preserved"] = (
                file_stats.get("vertices_affected_preserved", 0) + 1
            )
        else:
            # Slow path: clear all outgoing refs; Round 2 rebuilds.
            self.delete_outgoing_reference_edges(old_vname)
            file_stats["vertices_affected_rebuilt"] = (
                file_stats.get("vertices_affected_rebuilt", 0) + 1
            )

        # Rename or refresh in place.
        if old_vname != new_vname:
            self.rename_vertex(
                old_vname,
                new_vname,
                {"start_line": new_start, "end_line": new_end},
            )
        else:
            vid = self.code_graph.name_to_vertex.get(new_vname)
            if vid is not None:
                self.code_graph.graph.vs[vid]["end_line"] = new_end
                self.code_graph.symbol_ranges[new_vname] = (new_start, new_end)

        self._update_selection_range(old_vname, new_vname, new)

        # Round 2 ranges in NEW coords. Slow path always does the full body.
        if all_preserving:
            ranges_for_r2 = (
                overlapping_new if overlapping_new else [(new_start, new_end)]
            )
        else:
            ranges_for_r2 = [(new_start, new_end)]

        return new_vname, ranges_for_r2

    # ═══════════════════════════════════════════════════════════
    # Remap severed edges
    # ═══════════════════════════════════════════════════════════

    _STRUCTURAL_NODE_TYPES = frozenset({"file", "directory"})

    @staticmethod
    def _rebase_anchor_line(
        anchor_file: Optional[str],
        anchor_line: Optional[int],
        anchor_shifts: Optional[dict],
    ) -> Optional[int]:
        """Apply per-file shift table to a recorded anchor line.

        ``anchor_shifts`` maps file → list of ``(old_start, old_end, delta)``
        triples. If a triple covers ``anchor_line`` the delta is added; the
        first matching triple wins. Returns the (possibly unchanged) line.
        """
        if anchor_line is None or not anchor_shifts or not anchor_file:
            return anchor_line
        shifts = anchor_shifts.get(anchor_file)
        if not shifts:
            return anchor_line
        for old_start, old_end, delta in shifts:
            if old_start <= anchor_line <= old_end:
                return anchor_line + delta
        return anchor_line

    def _remap_severed_edges(
        self,
        new_vertices: list[str],
        severed_incoming: list[tuple],
        severed_outgoing: list[tuple],
        changed_line_ranges: list[tuple[int, int]] | None = None,
        anchor_shifts: Optional[dict] = None,
    ) -> int:
        """Remap severed reference edges to new vertex names.

        ``anchor_shifts``: optional ``{file: [(old_start, old_end, delta)]}``
        table built from sibling-file patches that already shifted symbols
        by the time this remap runs. Severed entries' ``anchor_line`` is
        rebased through the table before the rebuilt edge is written, so
        the new edge lands on the post-patch line numbering.
        """
        uname_to_new = {}
        for vname in new_vertices:
            if vname in self.code_graph.name_to_vertex:
                v = self.code_graph.graph.vs[self.code_graph.name_to_vertex[vname]]
                uname = v.attributes().get("unified_name")
                if uname:
                    uname_to_new[uname] = vname

        # Global uname → current vname map covers cross-file renames that
        # happened earlier in this batch (e.g. caller's file was patched
        # before the current target's). Used as a fallback when the
        # severed entry's src_name is stale.
        global_uname_to_vname: dict[str, str] = {}
        g_vs = self.code_graph.graph.vs
        for vid in range(self.code_graph.graph.vcount()):
            un = g_vs[vid].attributes().get("unified_name")
            if un and un not in global_uname_to_vname:
                global_uname_to_vname[un] = g_vs[vid]["name"]

        changed_unames = set()
        if changed_line_ranges:
            for vname in new_vertices:
                sr = self.code_graph.symbol_ranges.get(vname)
                if sr is None:
                    continue
                sym_start, sym_end = sr
                for cl_start, cl_end in changed_line_ranges:
                    if sym_start <= cl_end and cl_start <= sym_end:
                        vid = self.code_graph.name_to_vertex.get(vname)
                        if vid is not None:
                            uname = (
                                self.code_graph.graph.vs[vid]
                                .attributes()
                                .get("unified_name")
                            )
                            if uname:
                                changed_unames.add(uname)
                        break

        remapped = 0

        # Incoming: always safe to remap. Severed entries are 6-tuples
        # `(src, tgt, src_uname, tgt_uname, anchor_file, anchor_line)` —
        # one entry per anchored call site. Anchor is threaded through to
        # _add_edge so the rebuilt edge carries the same anchor metadata.
        for entry in severed_incoming:
            src_name, _, src_uname, tgt_uname, anchor_file, anchor_line = entry
            if not tgt_uname:
                continue
            new_target = uname_to_new.get(tgt_uname)
            if not new_target:
                continue

            # Resolve src: prefer the recorded vname, fall back to the
            # uname index when the src has been renamed (R1).
            if src_name in self.code_graph.name_to_vertex:
                resolved_src = src_name
            elif src_uname:
                resolved_src = global_uname_to_vname.get(src_uname)
            else:
                resolved_src = None

            if resolved_src:
                rebased_line = self._rebase_anchor_line(
                    anchor_file, anchor_line, anchor_shifts
                )
                self.code_graph._add_edge(
                    resolved_src,
                    new_target,
                    EDGE_TYPE_REFERENCE,
                    anchor_file=anchor_file,
                    anchor_line=rebased_line,
                )
                remapped += 1

        # Outgoing: skip if source body changed.
        for entry in severed_outgoing:
            src_name, tgt_name, src_uname, tgt_uname, anchor_file, anchor_line = entry

            if src_uname:
                if src_uname in changed_unames:
                    continue
                new_src = uname_to_new.get(src_uname)
            else:
                new_src = (
                    src_name if src_name in self.code_graph.name_to_vertex else None
                )

            if tgt_name in self.code_graph.name_to_vertex:
                resolved_tgt = tgt_name
            elif tgt_uname:
                resolved_tgt = uname_to_new.get(tgt_uname)
            else:
                resolved_tgt = None

            if new_src and resolved_tgt:
                rebased_line = self._rebase_anchor_line(
                    anchor_file, anchor_line, anchor_shifts
                )
                self.code_graph._add_edge(
                    new_src,
                    resolved_tgt,
                    EDGE_TYPE_REFERENCE,
                    anchor_file=anchor_file,
                    anchor_line=rebased_line,
                )
                remapped += 1

        return remapped

    # ═══════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════

    def _compute_outgoing_ranges(
        self,
        new_vertices: list[str],
        changed_line_ranges: list[tuple[int, int]] | None,
    ) -> list[tuple[int, int]] | None:
        """Expand changed line ranges to cover affected function bodies."""
        if not changed_line_ranges:
            return changed_line_ranges

        expanded = list(changed_line_ranges)
        for vname in new_vertices:
            sr = self.code_graph.symbol_ranges.get(vname)
            if sr is None:
                continue
            sym_start, sym_end = sr
            overlaps = any(
                sym_start <= cl_end and cl_start <= sym_end
                for cl_start, cl_end in changed_line_ranges
            )
            if overlaps:
                expanded.append((sym_start, sym_end))

        if not expanded:
            return changed_line_ranges
        expanded.sort()
        merged = [expanded[0]]
        for start, end in expanded[1:]:
            if start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def _find_vertex_by_unified_name(self, uname: str, file_path: str) -> Optional[str]:
        """Find a vertex name by unified_name, preferring same file."""
        idx = getattr(self.code_graph, "unified_name_to_vertex", {})
        vids = idx.get(uname, [])
        if not vids:
            for vname, (_, _) in self.code_graph.symbol_ranges.items():
                vid = self.code_graph.name_to_vertex.get(vname)
                if vid is not None:
                    v = self.code_graph.graph.vs[vid]
                    if v.attributes().get("unified_name") == uname:
                        return vname
            return None

        for vid in vids:
            v = self.code_graph.graph.vs[vid]
            if v.attributes().get("file") == file_path:
                return v["name"]
        return self.code_graph.graph.vs[vids[0]]["name"]

    def _get_remapped_unames(
        self,
        new_vertices: list[str],
        severed_incoming: list[tuple],
        severed_outgoing: list[tuple],
    ) -> set[str]:
        """Get set of vertex names whose edges were remapped.

        Severed entries are 6-tuples since the multi-edge fix:
        `(src, tgt, src_uname, tgt_uname, anchor_file, anchor_line)`.
        """
        remapped_unames = set()
        for entry in severed_incoming:
            _, _, _, tgt_uname, _, _ = entry
            if tgt_uname:
                for vname in new_vertices:
                    vid = self.code_graph.name_to_vertex.get(vname)
                    if vid is not None:
                        u = (
                            self.code_graph.graph.vs[vid]
                            .attributes()
                            .get("unified_name")
                        )
                        if u == tgt_uname:
                            remapped_unames.add(vname)
        for entry in severed_outgoing:
            _, _, src_uname, _, _, _ = entry
            if src_uname:
                for vname in new_vertices:
                    vid = self.code_graph.name_to_vertex.get(vname)
                    if vid is not None:
                        u = (
                            self.code_graph.graph.vs[vid]
                            .attributes()
                            .get("unified_name")
                        )
                        if u == src_uname:
                            remapped_unames.add(vname)
        return remapped_unames

    def _update_selection_range(self, old_vname: str, new_vname: str, new_meta: dict):
        """Update symbol_selection_ranges for a shifted/affected symbol."""
        sel = new_meta["sel_range"]
        sel_start = sel.get("start", {})
        sel_end = sel.get("end", {})
        self.symbol_selection_ranges[new_vname] = (
            sel_start.get("line", new_meta["start_line"]),
            sel_start.get("character", 0),
            sel_end.get("line", new_meta["start_line"]),
            sel_end.get("character", 0),
        )
        if new_vname != old_vname and old_vname in self.symbol_selection_ranges:
            del self.symbol_selection_ranges[old_vname]

    def _store_selection_range(self, vname: str, meta: dict):
        """Store symbol_selection_ranges for a new vertex."""
        sel = meta["sel_range"]
        sel_start = sel.get("start", {})
        sel_end = sel.get("end", {})
        self.symbol_selection_ranges[vname] = (
            sel_start.get("line", meta["start_line"]),
            sel_start.get("character", 0),
            sel_end.get("line", meta["start_line"]),
            sel_end.get("character", 0),
        )

    @staticmethod
    def _merge_stats(total: dict, file_stats: dict):
        """Merge file-level stats into total stats."""
        for key in (
            "vertices_deleted",
            "vertices_created",
            "vertices_shifted",
            "refs_incoming",
            "refs_outgoing",
            "refs_remapped",
            "refs_unmatched",
        ):
            if key in file_stats:
                total[key] = total.get(key, 0) + file_stats[key]
