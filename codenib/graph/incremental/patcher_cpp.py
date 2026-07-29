# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""C/C++-specific incremental patcher.

Uses clangd .idx files instead of LSP queries for incremental updates.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ...log_utils import get_logger
from ...types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)
from .patcher_base import PatcherBase

logger = get_logger(__name__)


class PatcherCpp(PatcherBase):
    """C/C++ incremental patcher using clangd .idx files."""

    REGISTRY_LANGUAGE = "cpp"

    def _get_crossfile_token_types(self):
        return {
            "type",
            "class",
            "struct",
            "enum",
            "function",
            "method",
            "namespace",
            "macro",
        }

    def _build_unified_name(
        self, file_path, name, parent_unified_part, kind, parent_kind: int = 0
    ):
        node_type = self._classify_symbol_type(kind)
        clean_name = name.replace("::", ".")

        if parent_unified_part:
            display = f"{parent_unified_part}.{clean_name}"
        else:
            display = clean_name

        if node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
            if not display.endswith("()"):
                display = f"{display}()"

        return f"{file_path}:{display}"

    def flatten_symbols(self, file_path, lsp_symbols):
        return self._flatten_symbols_default(file_path, lsp_symbols)

    # ═══════════════════════════════════════════════════════════
    # Override patch_files: C++ uses .idx path, not LSP
    # ═══════════════════════════════════════════════════════════

    def patch_files(self, changed_files, **kwargs):
        """Patch through clangd without exposing a partially mutated graph.

        Reindexing is prepared before any vertices are removed. Once mutation
        begins, the CodeGraph state is transactional: decoder, merge, or range
        index failures restore the caller's original graph before propagating
        the error.
        """
        all_changed = (
            changed_files.get("modified", [])
            + changed_files.get("added", [])
            + [new for _, new in changed_files.get("renamed", [])]
        )
        prepared_idx_dir = None
        if all_changed:
            with self.profiler.section("cpp.reindex"):
                prepared_idx_dir = self._reindex_changed_files(all_changed)
            if prepared_idx_dir is None:
                raise RuntimeError(
                    "clangd incremental indexing failed; graph was not modified"
                )

        graph_state = copy.deepcopy(self.code_graph.__dict__)
        try:
            return self._patch_files_mutating(
                changed_files,
                prepared_idx_dir=prepared_idx_dir,
                **kwargs,
            )
        except Exception:
            self.code_graph.__dict__.clear()
            self.code_graph.__dict__.update(graph_state)
            logger.exception(
                "C++ incremental patch failed; restored the original graph"
            )
            raise

    def _patch_files_mutating(
        self,
        changed_files,
        *,
        prepared_idx_dir: Path | None,
        **kwargs,
    ):
        """C++ incremental: delete old subgraphs, reindex via .idx, rebuild."""
        total_stats = {
            "files_deleted": 0,
            "files_modified": 0,
            "files_added": 0,
            "files_renamed": 0,
            "vertices_deleted": 0,
            "vertices_created": 0,
            "refs_incoming": 0,
            "refs_outgoing": 0,
            "refs_remapped": 0,
            "refs_unmatched": 0,
        }

        # Rebuild indexes for delete_file_subgraph
        self.build_indexes()

        nodes_before = self.code_graph.graph.vcount()
        edges_before = self.code_graph.graph.ecount()

        # Collect all changed file paths
        all_changed = (
            changed_files.get("modified", [])
            + changed_files.get("added", [])
            + [new for _, new in changed_files.get("renamed", [])]
        )

        # Delete old subgraphs (severed edges are discarded — cpp recovers
        # them by re-reading .idx data, see _apply_idx_data below).
        for path in changed_files.get("deleted", []):
            with self.profiler.section("delete_subgraph"):
                self.delete_file_subgraph(path)
            total_stats["files_deleted"] += 1

        for old_path, _ in changed_files.get("renamed", []):
            with self.profiler.section("delete_subgraph"):
                self.delete_file_subgraph(old_path)

        for path in changed_files.get("modified", []):
            with self.profiler.section("delete_subgraph"):
                self.delete_file_subgraph(path)

        for path in changed_files.get("added", []):
            with self.profiler.section("delete_subgraph"):
                self.delete_file_subgraph(path)

        # Reindex and rebuild via .idx
        if all_changed:
            stats = self._incremental_update_idx(
                all_changed,
                idx_dir=prepared_idx_dir,
            )
            total_stats["vertices_created"] = stats["vertices_created"]
            total_stats["refs_outgoing"] = stats["refs_added"]

        total_stats["files_modified"] = len(changed_files.get("modified", []))
        total_stats["files_added"] = len(changed_files.get("added", []))
        total_stats["files_renamed"] = len(changed_files.get("renamed", []))

        nodes_after = self.code_graph.graph.vcount()
        edges_after = self.code_graph.graph.ecount()
        total_stats["nodes_before"] = nodes_before
        total_stats["nodes_after"] = nodes_after
        total_stats["edges_before"] = edges_before
        total_stats["edges_after"] = edges_after

        earlier = kwargs.get("earlier_commit", "")
        later = kwargs.get("later_commit") or "HEAD"
        commit_info = ""
        if earlier and later:
            total_stats["commit_earlier"] = earlier
            total_stats["commit_later"] = later
            commit_info = f"commits {earlier[:12]}..{later[:12]}, "

        total_changed = sum(
            len(changed_files.get(k, []))
            for k in ("deleted", "added", "renamed", "modified")
        )
        logger.info(
            f"Incremental patch summary: {commit_info}"
            f"{total_changed} files changed, "
            f"nodes {nodes_before}→{nodes_after} "
            f"(Δ{nodes_after - nodes_before:+d}), "
            f"edges {edges_before}→{edges_after} "
            f"(Δ{edges_after - edges_before:+d})"
        )

        # Keep range queries coherent with the post-patch graph.  The generic
        # LSP patcher does this at the end of PatcherBase.patch_files(), but
        # C/C++ owns a separate .idx-based entry point and must uphold the
        # same public contract explicitly.
        with self.profiler.section("patch_files.build_range_indexes"):
            self.code_graph.build_range_indexes()

        report = self.profiler.report(reset=True)
        if report:
            total_time = sum(s.total for _, s in report)
            lines = [
                f"[graph_patcher_cpp] profiler summary: "
                f"{total_time:.3f}s total across {len(report)} section(s)"
            ]
            for label, s in report:
                avg = s.total / s.count if s.count else 0
                lines.append(
                    f"  {label:<40s} total={s.total:>7.3f}s "
                    f"count={s.count:>3d} avg={avg:>7.3f}s "
                    f"min={s.min_duration:>7.3f}s max={s.max_duration:>7.3f}s"
                )
            logger.info("\n".join(lines))

        return total_stats

    # ═══════════════════════════════════════════════════════════
    # .idx-based incremental update
    # ═══════════════════════════════════════════════════════════

    def _incremental_update_idx(
        self,
        changed_files: list[str],
        *,
        idx_dir: Path | None = None,
    ) -> dict:
        """Reindex via clangd .idx, then rebuild graph for changed files."""
        stats = {"vertices_created": 0, "refs_added": 0}

        if idx_dir is None:
            with self.profiler.section("cpp.reindex"):
                idx_dir = self._reindex_changed_files(changed_files)
        if idx_dir is None:
            raise RuntimeError("clangd incremental indexing failed")

        from codenib.ls_index.clangd_decode import ClangdGraphDecoder

        with self.profiler.section("cpp.parse_idx"):
            decoder = ClangdGraphDecoder(
                idx_directory=str(idx_dir),
                project_root=str(self.project_root),
            )
            decoder._collect_all_idx()
            decoder._build_id_to_display()

        decoder.code_graph = self.code_graph

        with self.profiler.section("cpp.rebuild_graph"):
            v_count, r_count = self._apply_idx_data(decoder, changed_files)
            stats["vertices_created"] = v_count
            stats["refs_added"] = r_count

        logger.info(
            f"C++ .idx incremental: {len(changed_files)} files, "
            f"+{v_count} vertices, {r_count} refs"
        )
        return stats

    def _reindex_changed_files(self, changed_files: list[str]) -> Path | None:
        """Run clangd background-index, preserving .idx cache."""
        clangd = shutil.which("clangd")
        if not clangd:
            return None

        comp_db = None
        for candidate in [
            Path(self.project_root) / "compile_commands.json",
            Path(self.project_root) / "build" / "compile_commands.json",
        ]:
            # Skip empty/trivial files (bare "[]" is ~3 bytes)
            if candidate.exists() and candidate.stat().st_size > 4:
                try:
                    data = json.loads(candidate.read_text())
                    if isinstance(data, list) and len(data) > 0:
                        comp_db = candidate
                        break
                except Exception as exc:
                    logger.debug(f"Failed to parse {candidate}: {exc}")
                    continue
        if comp_db is None:
            # Autotools / fresh-checkout case: compile_commands.json hasn't
            # been generated yet. Trigger ClangdIndexer's auto-generate
            # path (cmake/bear -- make) to produce one, then resume.
            logger.info(
                "No compile_commands.json — calling ClangdIndexer's "
                "auto-generate (cmake / bear -- make) to produce one"
            )
            from codenib.ls_index.clangd_indexer import ClangdIndexer

            indexer = ClangdIndexer(project_root=str(self.project_root))
            generated = indexer._auto_generate_compdb()
            if generated is None or not indexer._is_valid_compdb(generated):
                logger.warning("Auto-generate compile_commands failed")
                return None
            comp_db = generated
            logger.info(f"Auto-generated compile_commands at {comp_db}")

        candidates = [
            Path(self.project_root) / ".cache" / "clangd" / "index",
            comp_db.parent / ".cache" / "clangd" / "index",
        ]
        idx_dir = None
        for d in candidates:
            exists = d.exists()
            n_idx = len(list(d.glob("*.idx"))) if exists else 0
            logger.info(
                f"[patcher_cpp DEBUG] candidate {d}: exists={exists} idx_count={n_idx}"
            )
            if exists and n_idx > 0:
                idx_dir = d
                break

        if idx_dir is None:
            logger.info("No .idx cache, running full clangd index")
            from codenib.ls_index.clangd_indexer import ClangdIndexer

            cache_path = Path(self.project_root) / ".cache" / "clangd" / "index"
            n_before = len(list(cache_path.glob("*.idx"))) if cache_path.exists() else 0
            logger.info(
                f"[patcher_cpp DEBUG] before ClangdIndexer: {cache_path} has {n_before} .idx"
            )
            indexer = ClangdIndexer(project_root=str(self.project_root))
            success = indexer.generate_index(compdb_path=str(comp_db))
            n_after = len(list(cache_path.glob("*.idx"))) if cache_path.exists() else 0
            logger.info(
                "[patcher_cpp DEBUG] after ClangdIndexer: %s has %d .idx (success=%s)",
                cache_path,
                n_after,
                success,
            )
            return indexer.idx_directory if success else None

        pre_mtime = max(
            (f.stat().st_mtime for f in idx_dir.glob("*.idx")),
            default=0,
        )
        pre_count = len(list(idx_dir.glob("*.idx")))
        logger.info(
            f"C++ incremental: {pre_count} .idx in {idx_dir}, "
            f"didOpen {len(changed_files)} files"
        )

        # Force-invalidate .idx files for affected TUs so clangd's startup
        # background-index loader sees them as missing → MUST rebuild.
        #
        # Why this is needed: clangd 18.1.3 only verifies the *main file*
        # digest when loading existing shards. If a header file changes
        # but the .cc TU's source content didn't, clangd considers the
        # TU shard valid and skips reindex — leaving header symbol
        # changes invisible. By deleting .idx files we eliminate the
        # "shard valid" optimization and force a real reindex, matching
        # what `clear_indexer_cache` does for full rebuild but only for
        # the affected files.
        HEADER_EXTS = (".h", ".hpp", ".hxx", ".hh")
        TU_EXTS = (".c", ".cc", ".cpp", ".cxx")
        deleted = 0
        header_changed = False
        for changed_file in changed_files:
            name = Path(changed_file).name
            if name.endswith(HEADER_EXTS):
                header_changed = True
            for idx in idx_dir.glob(f"{name}.*.idx"):
                try:
                    idx.unlink()
                    deleted += 1
                except OSError:
                    pass  # stale .idx; unlink failure is harmless
        if header_changed:
            # Header changes can affect any TU. Delete all .idx files for
            # source-file TUs so clangd rebuilds them. Header-only .idx
            # are kept as-is — they'll be regenerated as a side effect of
            # indexing the TUs that include them.
            for idx in idx_dir.glob("*.idx"):
                # match files like "format.cc.HASH.idx" or "foo.c.HASH.idx"
                stem = idx.name
                # strip ".HASH.idx" tail
                base = stem.rsplit(".", 2)[0]  # "format.cc"
                if base.endswith(TU_EXTS):
                    try:
                        idx.unlink()
                        deleted += 1
                    except OSError:
                        pass  # stale .idx; unlink failure is harmless
        if deleted:
            logger.info(
                f"Pre-invalidated {deleted} .idx file(s) for affected TUs "
                f"(header-changed={header_changed})"
            )
            # Update pre_mtime/count for the polling logic below.
            pre_mtime = max(
                (f.stat().st_mtime for f in idx_dir.glob("*.idx")),
                default=0,
            )
            pre_count = len(list(idx_dir.glob("*.idx")))

        cmd = [
            clangd,
            "--background-index",
            f"--compile-commands-dir={comp_db.parent}",
            "--background-index-priority=normal",
            "--log=error",
        ]
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.project_root),
        )

        # Track BackgroundIndex progress via the LSP ``$/progress``
        # notifications clangd emits (see LLVM D73218). When we see
        # ``kind == "end"`` for the ``backgroundIndexProgress`` token,
        # clangd's queue has drained — much sharper signal than polling
        # .idx mtime with a 5-second idle timeout.
        progress_state = {
            "saw_begin": False,
            "active_tokens": set(),
            "lock": threading.Lock(),
        }

        def _stdout_loop():
            """Parse Content-Length framed JSON-RPC; track $/progress."""
            buf = b""
            stream = process.stdout
            try:
                while True:
                    # Read headers
                    while b"\r\n\r\n" not in buf:
                        chunk = (
                            stream.read1(65536)
                            if hasattr(stream, "read1")
                            else stream.read(4096)
                        )
                        if not chunk:
                            return
                        buf += chunk
                    header_blob, _, rest = buf.partition(b"\r\n\r\n")
                    buf = rest
                    cl = 0
                    for hline in header_blob.split(b"\r\n"):
                        if hline.lower().startswith(b"content-length:"):
                            try:
                                cl = int(hline.split(b":", 1)[1].strip())
                            except ValueError:
                                cl = 0
                    # Read body
                    while len(buf) < cl:
                        chunk = (
                            stream.read1(65536)
                            if hasattr(stream, "read1")
                            else stream.read(4096)
                        )
                        if not chunk:
                            return
                        buf += chunk
                    body, buf = buf[:cl], buf[cl:]
                    try:
                        msg = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("method") != "$/progress":
                        continue
                    params = msg.get("params") or {}
                    if params.get("token") != "backgroundIndexProgress":
                        continue
                    value = params.get("value") or {}
                    kind = value.get("kind")
                    with progress_state["lock"]:
                        if kind == "begin":
                            progress_state["saw_begin"] = True
                            progress_state["active_tokens"].add(params["token"])
                        elif kind == "end":
                            progress_state["active_tokens"].discard(params["token"])
            except Exception:
                # stdout reader dying is non-fatal: progress just stays
                # in whatever state it last reached. We have fallbacks
                # (process death + max_wait_s timeout) below.
                return

        def _drain_stderr(stream):
            try:
                while True:
                    chunk = (
                        stream.read1(65536)
                        if hasattr(stream, "read1")
                        else stream.read(4096)
                    )
                    if not chunk:
                        return
            except Exception:
                return

        threading.Thread(target=_stdout_loop, daemon=True).start()
        threading.Thread(
            target=_drain_stderr, args=(process.stderr,), daemon=True
        ).start()

        def lsp_send(msg):
            content = json.dumps(msg).encode()
            header = f"Content-Length: {len(content)}\r\n\r\n".encode()
            process.stdin.write(header + content)
            process.stdin.flush()

        try:
            # Capabilities: we want backgroundIndexProgress notifications.
            # ``workDoneProgress: True`` is the LSP-standard opt-in;
            # ``implicitWorkDoneProgressCreate: True`` is a clangd extension
            # that lets the server skip the ``window/workDoneProgress/create``
            # handshake — without it, clangd flips state to "Unsupported"
            # and never sends progress (verified on Ubuntu clangd 18.1.3).
            lsp_send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "processId": os.getpid(),
                        "rootUri": Path(self.project_root).as_uri(),
                        "capabilities": {
                            "window": {
                                "workDoneProgress": True,
                                "implicitWorkDoneProgressCreate": True,
                            },
                        },
                    },
                }
            )
            time.sleep(0.3)
            lsp_send(
                {
                    "jsonrpc": "2.0",
                    "method": "initialized",
                    "params": {},
                }
            )
            time.sleep(0.2)

            for fpath in changed_files:
                abs_path = Path(self.project_root) / fpath
                if not abs_path.is_file():
                    continue
                try:
                    text = abs_path.read_text(errors="replace")
                except Exception:
                    continue
                lsp_send(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didOpen",
                        "params": {
                            "textDocument": {
                                "uri": abs_path.as_uri(),
                                "languageId": "cpp",
                                "version": 1,
                                "text": text,
                            }
                        },
                    }
                )

            # Wait for clangd to finish indexing. Three exit conditions:
            #   (a) Progress: saw begin and queue is now empty → done.
            #   (b) No progress arrived AND .idx mtime quiet for 3s → done
            #       (fallback for clients/servers where $/progress fails,
            #       e.g. very old clangd or capability negotiation hiccup).
            #   (c) Clangd process died.
            # Hard cap at max_wait_s so a hung clangd doesn't stall the
            # bench forever.
            max_wait_s = 120.0
            no_progress_idle_s = 3.0
            poll_interval_s = 0.05
            deadline = time.monotonic() + max_wait_s
            settle_after_end_s = 0.5  # let trailing .idx writes flush
            end_seen_at: float | None = None
            last_mtime = pre_mtime
            last_mtime_change_t = time.monotonic()

            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break  # (c) clangd died

                with progress_state["lock"]:
                    saw_begin = progress_state["saw_begin"]
                    active = bool(progress_state["active_tokens"])

                if saw_begin and not active:
                    # (a) Progress reported done. Wait a short settle window
                    # so the very-last .idx file rename has time to land
                    # before we parse them.
                    if end_seen_at is None:
                        end_seen_at = time.monotonic()
                    elif time.monotonic() - end_seen_at >= settle_after_end_s:
                        break
                elif saw_begin:
                    # Indexing in flight — wait for the end event (do NOT
                    # consult mtime; a single long-running TU could
                    # falsely trip a stability check).
                    pass
                else:
                    # No $/progress yet — fall back to .idx mtime polling.
                    cur_mtime = max(
                        (f.stat().st_mtime for f in idx_dir.glob("*.idx")),
                        default=0,
                    )
                    if cur_mtime > last_mtime:
                        last_mtime = cur_mtime
                        last_mtime_change_t = time.monotonic()
                    if time.monotonic() - last_mtime_change_t >= no_progress_idle_s:
                        # (b) Nothing happening for 3s and no progress
                        # API in play — assume clangd is done (or never
                        # going to do anything for this set of files).
                        break

                time.sleep(poll_interval_s)

            post_count = len(list(idx_dir.glob("*.idx")))
            logger.info(
                f"C++ incremental done: {pre_count}→{post_count} .idx files "
                f"(progress_seen={progress_state['saw_begin']})"
            )

        finally:
            try:
                lsp_send(
                    {
                        "jsonrpc": "2.0",
                        "id": 99,
                        "method": "shutdown",
                        "params": None,
                    }
                )
                time.sleep(0.5)
                lsp_send(
                    {
                        "jsonrpc": "2.0",
                        "method": "exit",
                        "params": None,
                    }
                )
                process.wait(timeout=5)
            except Exception:
                process.kill()
                process.wait()

        return idx_dir

    def _apply_idx_data(
        self,
        decoder,
        changed_files: list[str],
    ) -> tuple[int, int]:
        """Add symbols and edges from .idx data for changed files.

        Edges go through ``EdgeBatch`` (one ``add_edges`` call instead of
        thousands of single-edge inserts). On redis-scale graphs (~95k
        edges) this drops graph rebuild from ~50s/step to seconds because
        each ``CodeGraph._add_edge`` does an O(out-degree) dedup walk plus
        a Python↔C round-trip — quadratic at thousands of edges per patch.
        """
        from codenib.ls_index.clangd_decode import (
            KIND_MACRO,
            REF_KIND_DEFINITION,
            REF_KIND_REFERENCE,
            ZERO_SYMBOL_ID,
        )

        from .subgraph_mgr import EdgeBatch

        changed_set = set(changed_files)
        new_vertices = 0
        new_refs = 0

        changed_sym_ids = set()
        for sym_id, sym in decoder._symbols.items():
            if sym.get("kind", 0) == KIND_MACRO:
                continue
            def_info = decoder._resolve_definition(sym_id)
            if not def_info["file"]:
                continue
            rel_file = decoder._file_uri_to_relative(def_info["file"])
            if rel_file in changed_set:
                changed_sym_ids.add(sym_id)

        for sym_id in changed_sym_ids:
            sym = decoder._symbols[sym_id]
            def_info = decoder._resolve_definition(sym_id)
            rel_file = decoder._file_uri_to_relative(def_info["file"])
            decoder._ensure_file_hierarchy(rel_file)

            node_type = decoder._kind_to_node_type(sym.get("kind", 0))
            qualified_name = decoder._sym_id_to_display_name(sym_id)
            line = def_info["line"]
            scope_start, scope_end = decoder._find_range(rel_file, line)

            self.code_graph.add_symbol_node(
                sym_id,
                line,
                scope_start_line=scope_start,
                scope_end_line=scope_end,
                symbol_type=node_type,
            )

            if sym_id in self.code_graph.name_to_vertex:
                vid = self.code_graph.name_to_vertex[sym_id]
                unified_display = qualified_name.replace("::", ".")
                if node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
                    unified_display = f"{unified_display}()"
                self.code_graph.graph.vs[vid][
                    "unified_name"
                ] = f"{rel_file}:{unified_display}"
                self.code_graph.graph.vs[vid]["file"] = rel_file
                new_vertices += 1

        # All edge inserts go through one batch. EdgeBatch.stage rejects
        # duplicates; on caller contract violation (vertex missing) it
        # transparently falls back to _add_edge.
        batch = EdgeBatch(self.code_graph)

        contained = set()
        for sym_id in changed_sym_ids:
            if sym_id not in self.code_graph.name_to_vertex:
                continue
            for ref in decoder._refs.get(sym_id, []):
                if ref["kind"] & REF_KIND_DEFINITION:
                    container_id = ref.get("container", "")
                    if (
                        container_id
                        and container_id != ZERO_SYMBOL_ID
                        and container_id in self.code_graph.name_to_vertex
                    ):
                        batch.stage(container_id, sym_id, EDGE_TYPE_CONTAIN)
                        contained.add(sym_id)
                        break

        for sym_id in changed_sym_ids:
            if sym_id in contained:
                continue
            if sym_id not in self.code_graph.name_to_vertex:
                continue
            vid = self.code_graph.name_to_vertex[sym_id]
            file_path = self.code_graph.graph.vs[vid].attributes().get("file")
            if file_path and file_path in self.code_graph.name_to_vertex:
                batch.stage(file_path, sym_id, EDGE_TYPE_CONTAIN)

        for sym_id, ref_list in decoder._refs.items():
            if sym_id not in self.code_graph.name_to_vertex:
                continue
            for ref in ref_list:
                if not (ref["kind"] & REF_KIND_REFERENCE):
                    continue
                container_id = ref.get("container", "")
                if not container_id or container_id == ZERO_SYMBOL_ID:
                    continue
                if container_id not in self.code_graph.name_to_vertex:
                    continue
                if sym_id in changed_sym_ids or container_id in changed_sym_ids:
                    # Carry call-site anchor metadata through (matches
                    # what clangd_decode does on full rebuild) — otherwise
                    # the patcher's reference edges land with anchor_*=None
                    # and miss range-query indexes.
                    loc = ref.get("location") or {}
                    anchor_uri = loc.get("file")
                    anchor_file = (
                        decoder._file_uri_to_relative(anchor_uri)
                        if anchor_uri
                        else None
                    )
                    start = loc.get("start")
                    anchor_line = start[0] if start else None
                    batch.stage(
                        container_id,
                        sym_id,
                        EDGE_TYPE_REFERENCE,
                        anchor_file=anchor_file,
                        anchor_line=anchor_line,
                    )
                    new_refs += 1

        with self.profiler.section("cpp.edge_batch_flush"):
            batch.flush()

        return new_vertices, new_refs
