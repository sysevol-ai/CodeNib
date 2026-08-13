#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Indexer for C/C++ projects using clangd.
Uses clangd's background indexer to generate .idx files, then
ClangdGraphDecoder (idx_decode + idx_parser) to build a CodeGraph
directly from the binary .idx format.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Optional, Union

from ..graph.code_graph import CodeGraph
from ..log_utils import get_logger
from ..paths import CLANGD_INDEX_DIRNAME, temp_state_dir
from ..profiler import Profiler
from .clangd_contract import clangd_background_index_command
from .index_quality import (
    IndexQualityPolicy,
    assess_index_quality,
    compilation_database_entry_count,
    compilation_database_stats,
    discover_compilation_database,
    prepare_compilation_database_for_indexing,
)

logger = get_logger("clangd_indexer")


class ClangdIndexer:
    """
    Indexer for C/C++ projects using clangd.

    Pipeline:
      1. generate_index  — run clangd to produce .idx files
      2. decode_index    — no-op (clangd .idx parsed directly)
      3. process_index   — ClangdGraphDecoder reads .idx → CodeGraph
    """

    # ==================================================================
    # Init
    # ==================================================================

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
    ):
        self.project_root = Path(project_root).absolute()
        self.language = "clang"

        if output_dir:
            self.output_dir = Path(output_dir).absolute()
        else:
            self.output_dir = temp_state_dir() / self.project_root.name

        os.makedirs(self.output_dir, exist_ok=True)

        self.index_file = None  # clangd generates .idx files in idx_directory
        self.decoded_file = None  # no protoc decode step for clangd
        self.graph_file = self.output_dir / "graph.pkl"
        self.exclude_patterns = exclude_patterns if exclude_patterns else []
        self.profiler = profiler or Profiler("clangd_indexer")
        self.compdb_path: Optional[Path] = None
        self.compdb_warning_rewrite_count = 0
        self.index_quality_report: Optional[dict] = None
        self._quality_policy = IndexQualityPolicy()

        # clangd decides its own output location (project-local
        # .cache/clangd/index/).  _run_clangd_indexer overwrites this
        # after indexing to point at the directory that actually received
        # new .idx files.
        self.idx_directory = self.project_root / ".cache" / "clangd" / "index"

    # ==================================================================
    # Helpers for Setting up
    # ==================================================================

    def _find_clangd(self) -> Optional[str]:
        """Find the clangd executable."""
        clangd = shutil.which("clangd")
        if clangd:
            return clangd

        local = self.project_root / "clangd"
        if local.exists() and local.is_file():
            return str(local.absolute())

        return None

    def _check_indexer_available(self) -> bool:
        """Check if clangd is available."""
        clangd_path = self._find_clangd()
        if not clangd_path:
            logger.error(
                "clangd not found. On Ubuntu run make active-system-deps-ubuntu "
                "then make clangd-tool; on macOS install llvm and run make clangd-tool."
            )
            return False

        try:
            result = subprocess.run(
                [clangd_path, "--version"],
                capture_output=True,
                text=True,
            )
            version_info = result.stdout.strip() or result.stderr.strip()
            logger.info(f"clangd version: {version_info}")
            self._clangd_path = clangd_path
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.error(f"Error running clangd at {clangd_path}: {e}")
            return False

    def _build_index_command(self, comp_db: Path) -> List[str]:
        """Build the full clangd background-index command."""
        clangd_cmd = getattr(self, "_clangd_path", "clangd")
        compile_commands_dir = str(comp_db.parent)
        return list(clangd_background_index_command(clangd_cmd, compile_commands_dir))

    def _get_decoder_class(self):
        """Return ClangdGraphDecoder."""
        from .clangd_decode import ClangdGraphDecoder

        return ClangdGraphDecoder

    # ==================================================================
    # Pipeline methods
    # ==================================================================

    def generate_index(
        self,
        compdb_path: Optional[str] = None,
        show_compiler_diagnostics: bool = False,
    ) -> bool:
        """
        Generate .idx files by running clangd with background indexing.

        Args:
            compdb_path: Path to compile_commands.json
            show_compiler_diagnostics: (unused, kept for API compatibility)

        Returns:
            True if .idx files were created successfully
        """
        if not self._check_indexer_available():
            return False

        # Resolve compilation database
        if compdb_path:
            comp_db = Path(compdb_path)
        else:
            comp_db = self.project_root / "compile_commands.json"
            if not comp_db.exists():
                comp_db = self.project_root / "build" / "compile_commands.json"

        if not self._is_valid_compdb(comp_db):
            logger.error(
                f"Compilation database missing or invalid. Tried:\n"
                f"  - {self.project_root / 'compile_commands.json'}\n"
                f"  - {self.project_root / 'build' / 'compile_commands.json'}\n\n"
                f"Please generate a compilation database first:\n"
                f"  cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON\n"
                f"  bear -- make"
            )
            return False

        logger.info(f"Using compilation database: {comp_db}")

        with self.profiler.section("generate_index") as section:
            success = self._run_clangd_indexer(comp_db)
        duration = section.duration

        if success:
            idx_count = len(list(self.idx_directory.glob("*.idx")))
            logger.info(
                f"Successfully generated {idx_count} .idx files "
                f"in {self.idx_directory} ({duration:.2f}s)"
            )
            return True
        else:
            logger.error(f"Index generation failed after {duration:.2f}s")
            return False

    def decode_index(self) -> bool:
        """
        No-op: clangd .idx files are parsed directly by ClangdGraphDecoder.

        There is no protoc decode step for the clangd binary format.
        """
        logger.info("Skipping protoc decode (clangd .idx files are parsed directly)")
        return True

    def process_index(
        self, output_file: Optional[str] = None
    ) -> Union[CodeGraph, None]:
        """
        Build a CodeGraph from clangd .idx files.

        Args:
            output_file: Path to save the serialised graph (.pkl)

        Returns:
            CodeGraph or None on failure
        """
        if not self.idx_directory.exists():
            logger.error(f"Index directory not found: {self.idx_directory}")
            return None

        idx_files = list(self.idx_directory.glob("*.idx"))
        if not idx_files:
            logger.error(f"No .idx files found in {self.idx_directory}")
            return None

        logger.info(f"Processing {len(idx_files)} .idx files from {self.idx_directory}")

        try:
            decoder_class = self._get_decoder_class()

            with self.profiler.section("process_index.decode") as section:
                decoder = decoder_class(
                    idx_directory=str(self.idx_directory),
                    project_root=str(self.project_root),
                )
                graph: CodeGraph = decoder.decode()
            duration = section.duration

            # Build line-range indexes once the graph is fully assembled.
            # Must happen before save_graph so the indexes are persisted.
            with self.profiler.section("process_index.build_range_indexes"):
                graph.build_range_indexes()

            if output_file:
                with self.profiler.section("process_index.save_graph") as save_section:
                    graph.save_graph(output_file)
                save_duration = save_section.duration
                logger.info(f"Saved graph to {output_file}")
                logger.info(f"Graph saving took: {save_duration:.2f}s")

            logger.info(f"Index processing took: {duration:.2f}s")
            return graph

        except Exception as e:
            logger.error(f"Error processing .idx files: {e}")
            return None

    def process_query_index(self):
        """Build the gated symbol-query view from an existing clangd index.

        This capability-scoped entry point leaves process_index and graph
        persistence unchanged. It never invokes clangd index generation.
        """

        self._select_existing_idx_directory()
        if not self.idx_directory.exists() or not any(self.idx_directory.glob("*.idx")):
            raise FileNotFoundError(
                f"No clangd .idx files found in {self.idx_directory}"
            )
        decoder_class = self._get_decoder_class()
        with self.profiler.section("process_query_index.decode"):
            decoder = decoder_class(
                idx_directory=str(self.idx_directory),
                project_root=str(self.project_root),
            )
            return decoder.decode_query_index()

    def process_query_provider(self, *, require_native: bool = False):
        """Return native symbol queries with lazy complete-graph fallback."""

        self._select_existing_idx_directory()
        if not self.idx_directory.exists() or not any(self.idx_directory.glob("*.idx")):
            raise FileNotFoundError(
                f"No clangd .idx files found in {self.idx_directory}"
            )
        decoder_class = self._get_decoder_class()
        decoder = decoder_class(
            idx_directory=str(self.idx_directory),
            project_root=str(self.project_root),
        )
        if require_native:
            return decoder.decode_native_query_provider()
        return decoder.decode_query_provider()

    def _select_existing_idx_directory(self) -> Optional[Path]:
        """Select a project-local clangd generation without building one."""

        candidates = [
            self.idx_directory,
            self.project_root / ".cache" / "clangd" / "index",
            self.project_root / "build" / ".cache" / "clangd" / "index",
        ]
        comp_db = discover_compilation_database(
            self.project_root,
            extra_candidates=(self.output_dir / "compile_commands.json",),
        )
        if comp_db is not None:
            candidates.extend(self._build_candidate_idx_dirs(comp_db))

        observed = set()
        for candidate in candidates:
            normalized = candidate.expanduser().absolute()
            key = str(normalized)
            if key in observed:
                continue
            observed.add(key)
            if normalized.is_dir() and any(normalized.glob("*.idx")):
                self.idx_directory = normalized
                return normalized
        return None

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        enforce_quality: bool = True,
        quality_policy: Optional[IndexQualityPolicy] = None,
        quality_baseline_graph: Optional[CodeGraph] = None,
        **kwargs,
    ):
        """
        Run clangd C/C++ pipeline: generate .idx → build CodeGraph.
        Skip levels:
          'graph'  — load cached graph.pkl if it exists
          'decode' / 'raw' — skip generation if .idx files already exist
          None     — full pipeline
        """
        # Drop Python-specific kwargs that may be forwarded by callers
        kwargs.pop("project_name", None)
        kwargs.pop("target_dir", None)
        kwargs.pop("cwd", None)
        allow_project_preparation = kwargs.pop("allow_project_preparation", True)
        if type(allow_project_preparation) is not bool:
            raise ValueError("allow_project_preparation must be a boolean")

        if output_file is None:
            output_file = str(self.graph_file)
        self._quality_policy = quality_policy or IndexQualityPolicy()

        requested_compdb = kwargs.get("compdb_path")
        if requested_compdb and self._is_valid_compdb(Path(requested_compdb)):
            self.compdb_path = Path(requested_compdb).resolve()
        else:
            self.compdb_path = discover_compilation_database(
                self.project_root,
                extra_candidates=(self.output_dir / "compile_commands.json",),
            )

        if (
            quality_baseline_graph is None
            and skip_level != "graph"
            and self.graph_file.exists()
        ):
            try:
                quality_baseline_graph = CodeGraph.load_graph(str(self.graph_file))
                logger.info("Using existing graph.pkl as the quality baseline")
            except Exception as exc:
                logger.warning("Could not load graph quality baseline: %s", exc)

        # Check graph cache
        if skip_level == "graph" and self.graph_file.exists():
            logger.info(f"Loading cached graph from {self.graph_file}")
            try:
                graph = CodeGraph.load_graph(str(self.graph_file))
                logger.info(
                    f"Loaded cached graph "
                    f"({len(graph.graph.vs)} nodes, {len(graph.graph.es)} edges)"
                )
                if (
                    self._record_index_quality(
                        graph,
                        baseline_graph=quality_baseline_graph,
                        policy=quality_policy,
                    )
                    or not enforce_quality
                ):
                    return graph
                logger.warning(
                    "Cached graph failed index quality checks; rebuilding: %s",
                    ", ".join(self.index_quality_report["failure_names"]),
                )
                quality_baseline_graph = graph
                self._quarantine_graph_artifact(self.graph_file)
                skip_level = None
            except Exception as e:
                logger.warning(f"Failed to load cached graph: {e}. Continuing ...")

        # Determine whether we already have .idx files
        has_idx_files = self.idx_directory.exists() and any(
            self.idx_directory.glob("*.idx")
        )

        if skip_level in ("graph", "decode", "raw") and has_idx_files:
            logger.info(
                f"Found existing .idx files in {self.idx_directory}, "
                f"skipping index generation"
            )
            should_generate = False
        else:
            should_generate = True

        # Auto-discover / generate compile_commands.json when needed
        if should_generate:
            should_regenerate_compdb = self.compdb_path is None or (
                not requested_compdb
                and self.compdb_path is not None
                and not self._is_preferred_compdb(self.compdb_path)
            )
            if should_regenerate_compdb and allow_project_preparation:
                existing = self.compdb_path
                if existing is not None:
                    existing = self._snapshot_compdb(existing, "discovered")
                generated = self._auto_generate_compdb()
                if generated is not None and self._is_valid_compdb(generated):
                    self.compdb_path = self._better_compdb(
                        existing, generated.resolve()
                    )
                elif generated is not None:
                    logger.warning(
                        "Auto-generated compilation database is invalid: %s", generated
                    )
            elif should_regenerate_compdb:
                logger.info(
                    "Skipping compilation database generation for read-only "
                    "CodeGraph onboarding"
                )
            if self.compdb_path is not None:
                prepared_path = self.output_dir / "compile_commands.json"
                if prepared_path.resolve() == self.compdb_path.resolve():
                    prepared_path = (
                        self.output_dir / CLANGD_INDEX_DIRNAME / "compile_commands.json"
                    )
                prepared, rewrite_count = prepare_compilation_database_for_indexing(
                    self.compdb_path,
                    prepared_path,
                )
                self.compdb_path = prepared
                self.compdb_warning_rewrite_count = rewrite_count
                kwargs["compdb_path"] = str(prepared)
                if rewrite_count:
                    logger.info(
                        "Neutralized %d warning-as-error flags in clangd compile DB",
                        rewrite_count,
                    )

        if reset_profiler:
            self.profiler.reset()

        try:
            # Step 1: generate .idx files (if needed)
            if should_generate:
                logger.info("Generating clangd index (.idx files)")
                if not self.generate_index(**kwargs):
                    # Check if .idx files appeared despite the failure
                    has_idx_files = self.idx_directory.exists() and any(
                        self.idx_directory.glob("*.idx")
                    )
                    if not has_idx_files:
                        return None
                    logger.warning(
                        "generate_index returned failure but .idx files exist; continuing."
                    )

            # Step 2: decode — no-op for clangd

            # Step 3: build CodeGraph from .idx files
            graph = self.process_index(output_file)

            if graph:
                logger.info(
                    f"Graph created successfully "
                    f"({len(graph.graph.vs)} nodes, {len(graph.graph.es)} edges)"
                )
                quality_passed = self._record_index_quality(
                    graph,
                    baseline_graph=quality_baseline_graph,
                    policy=quality_policy,
                )
                if enforce_quality and not quality_passed:
                    logger.error(
                        "Rejecting graph that failed index quality checks: %s",
                        ", ".join(self.index_quality_report["failure_names"]),
                    )
                    self._quarantine_graph_artifact(Path(output_file))
                    return None

            return graph
        finally:
            if report_profile:
                self.profiler.report(reset=reset_profiler)

    def clear_cache(self, level: str = "all") -> bool:
        """Clear cache files at different levels.

        For clangd the "raw" index is the idx_directory (many .idx files),
        and there is no separate "decoded" stage.

        Args:
            level: Preserve cache up to this pipeline stage, remove above.
                Pipeline: raw (.idx files) → graph (graph.pkl)
                - 'graph'  — keep everything (idx + graph)
                - 'decode' — same as 'raw' (clangd has no separate decode step)
                - 'raw'    — keep idx_directory, remove graph.pkl
                - 'all'    — remove everything
        """
        if level not in ("graph", "decode", "raw", "all"):
            logger.error(f"Invalid cache level: {level}")
            return False

        remove_idx = level in ("all",)
        remove_graph = level in ("decode", "raw", "all")

        if remove_idx and self.idx_directory.exists():
            shutil.rmtree(self.idx_directory)
            logger.info(f"Removed {self.idx_directory}")

        if remove_graph and self.graph_file.exists():
            self.graph_file.unlink()
            logger.info(f"Removed {self.graph_file}")

        return True

    # ==================================================================
    # Private helpers: LSP
    # ==================================================================

    @staticmethod
    def _lsp_send(process, message: dict):
        """Send an LSP JSON-RPC message to the process stdin."""
        content = json.dumps(message)
        content_bytes = content.encode("utf-8")
        header = f"Content-Length: {len(content_bytes)}\r\n\r\n"
        process.stdin.write(header.encode("utf-8") + content_bytes)
        process.stdin.flush()

    # ==================================================================
    # Private helpers: index generation
    # ==================================================================

    def _run_clangd_indexer(self, comp_db: Path) -> bool:
        """Start clangd, trigger background indexing, wait for completion.

        Uses fire-and-forget LSP messages (no response reading) — clangd
        stdout is left unread while we poll the .idx directory.
        """
        cmd = self._build_index_command(comp_db)
        logger.info(f"Starting clangd: {' '.join(cmd)}")

        # Clear stale .idx and graph files before re-indexing
        self.clear_cache(level="all")

        # Build candidate .idx directories before indexing starts,
        # snapshot mtime so we can detect changes (not just new files).
        candidate_dirs = self._build_candidate_idx_dirs(comp_db)
        pre_mtimes = {}
        for d in candidate_dirs:
            pre_mtimes[d] = self._get_max_mtime(d)
        logger.info(f"Candidate .idx directories: {[str(d) for d in candidate_dirs]}")
        for d in candidate_dirs:
            if d.exists():
                count = len(list(d.glob("*.idx")))
                if count:
                    logger.info(f"  {d}: {count} pre-existing .idx files")

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.project_root),
        )

        # Track BackgroundIndex progress via the LSP $/progress
        # notifications clangd emits (LLVM D73218). When clangd has
        # processed the whole queue we get a ``kind == "end"`` event for
        # the ``backgroundIndexProgress`` token — a sharp completion
        # signal so we don't need to poll .idx mtime for 10s of
        # stability. Falls back to the legacy mtime poll if no progress
        # arrives (very old clangd or capability hiccup).
        progress_state = {
            "saw_begin": False,
            "active_tokens": set(),
            "lock": threading.Lock(),
        }
        threading.Thread(
            target=_clangd_progress_reader,
            args=(process.stdout, progress_state),
            daemon=True,
        ).start()
        # Stderr needs draining too — `--log=error` is small but pipe can
        # still fill on bigger errors.
        threading.Thread(
            target=_clangd_stream_drain,
            args=(process.stderr,),
            daemon=True,
        ).start()

        try:
            # LSP initialize. ``window.workDoneProgress`` enables the
            # protocol; ``implicitWorkDoneProgressCreate`` is a clangd
            # extension that lets the server skip the
            # ``window/workDoneProgress/create`` handshake — without
            # it, clangd waits for a client reply we don't send and
            # silently flips progress to "Unsupported" mode.
            self._lsp_send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "processId": os.getpid(),
                        "rootUri": self.project_root.as_uri(),
                        "capabilities": {
                            "window": {
                                "workDoneProgress": True,
                                "implicitWorkDoneProgressCreate": True,
                            },
                        },
                    },
                },
            )
            time.sleep(0.5)

            # LSP initialized — triggers BackgroundIndex
            self._lsp_send(
                process,
                {
                    "jsonrpc": "2.0",
                    "method": "initialized",
                    "params": {},
                },
            )
            time.sleep(0.3)

            # Open source files listed in compile_commands.json to nudge
            # the background indexer (same approach as test_codegraph_clangd)
            self._open_source_files(process, comp_db)

            # Wait for indexing — progress-aware, with mtime fallback.
            self._wait_for_indexing(
                process,
                candidate_dirs,
                pre_mtimes,
                progress_state=progress_state,
            )

        except Exception as e:
            logger.error(f"Error during clangd indexing: {e}")
        finally:
            # Graceful shutdown (best-effort)
            try:
                self._lsp_send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "id": 99,
                        "method": "shutdown",
                        "params": None,
                    },
                )
                time.sleep(0.5)
                self._lsp_send(
                    process,
                    {
                        "jsonrpc": "2.0",
                        "method": "exit",
                        "params": None,
                    },
                )
                process.wait(timeout=5)
            except Exception:
                process.kill()
                process.wait()

        # Find which candidate directory has .idx files (prefer one with
        # changed mtimes, then any with files)
        for d in candidate_dirs:
            if d.exists() and self._get_max_mtime(d) > pre_mtimes.get(d, 0):
                self.idx_directory = d
                count = len(list(d.glob("*.idx")))
                logger.info(
                    f"Using .idx directory (mtime changed): {d} ({count} files)"
                )
                return True

        # Fallback: pick first candidate with any .idx files
        for d in candidate_dirs:
            if d.exists():
                idx_files = list(d.glob("*.idx"))
                if idx_files:
                    self.idx_directory = d
                    logger.info(
                        f"Using .idx directory (fallback): {d} ({len(idx_files)} files)"
                    )
                    return True

        return False

    def _build_candidate_idx_dirs(self, comp_db: Path) -> List[Path]:
        """Build a list of candidate directories where clangd may write .idx files,
        since clangd doesn't provide a way to specify the .idx output directory.

        Note: the global ~/.cache/clangd/index is intentionally excluded to
        avoid picking up .idx files from unrelated projects.
        """
        candidates = []
        # 1. Project-local .cache/clangd/index
        local_idx = self.project_root / ".cache" / "clangd" / "index"
        candidates.append(local_idx)
        # 2. Relative to compile_commands.json directory
        compdb_idx = comp_db.parent / ".cache" / "clangd" / "index"
        if compdb_idx.resolve() != local_idx.resolve():
            candidates.append(compdb_idx)
        return candidates

    @staticmethod
    def _get_max_mtime(directory: Path) -> float:
        """Get the maximum mtime of .idx files in a directory (0 if none)."""
        max_mtime = 0.0
        if directory.exists():
            for f in directory.glob("*.idx"):
                try:
                    mt = f.stat().st_mtime
                    if mt > max_mtime:
                        max_mtime = mt
                except OSError:
                    pass
        return max_mtime

    def _open_source_files(self, process, comp_db: Path):
        """Send textDocument/didOpen for every source file in compile_commands.json."""
        try:
            with open(comp_db, "r", encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            return

        source_files = []
        for entry in entries:
            source = entry.get("file")
            if not source:
                continue
            source_path = Path(str(source))
            if not source_path.is_absolute():
                directory = Path(str(entry.get("directory") or self.project_root))
                if not directory.is_absolute():
                    directory = self.project_root / directory
                source_path = directory / source_path
            source_files.append(source_path.resolve())
        logger.info(f"Opening {len(source_files)} source files to trigger indexing")

        for src_file in source_files:
            if not src_file.is_file():
                continue
            try:
                text = src_file.read_text(errors="replace")
            except Exception:
                continue

            self._lsp_send(
                process,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": src_file.as_uri(),
                            "languageId": "cpp",
                            "version": 1,
                            "text": text,
                        }
                    },
                },
            )

    def _wait_for_indexing(
        self,
        process,
        candidate_dirs: List[Path],
        pre_mtimes: dict,
        timeout: int = 1200,
        stable_seconds: int = 10,
        idle_timeout: int = 30,
        progress_state: Optional[dict] = None,
    ):
        """Wait for background indexing to complete.

        Prefers clangd's LSP ``$/progress`` ``backgroundIndexProgress``
        ``end`` event (delivered via ``progress_state``); falls back to
        polling .idx mtime when no progress signal arrives in time.
        """
        logger.info("Waiting for clangd background indexing to complete ...")
        logger.info(f"Monitoring directories: {[str(d) for d in candidate_dirs]}")
        start = time.time()
        last_change = time.time()
        last_max_mtime = max(pre_mtimes.values()) if pre_mtimes else 0
        activity_detected = False
        has_pre_existing = any(mt > 0 for mt in pre_mtimes.values())
        settle_after_end_s = 0.5  # let trailing .idx writes flush
        end_seen_at: Optional[float] = None
        total_files = 0

        while time.time() - start < timeout:
            time.sleep(0.5 if progress_state is not None else 2)

            # Process death is fatal in any state — check once per loop.
            if process.poll() is not None:
                logger.warning("clangd process exited during indexing")
                return

            # ── Path A: progress-aware (preferred) ────────────────────
            if progress_state is not None:
                with progress_state["lock"]:
                    saw_begin = progress_state["saw_begin"]
                    active = bool(progress_state["active_tokens"])
                if saw_begin and not active:
                    if end_seen_at is None:
                        end_seen_at = time.time()
                    elif time.time() - end_seen_at >= settle_after_end_s:
                        total_files = sum(
                            len(list(d.glob("*.idx")))
                            for d in candidate_dirs
                            if d.exists()
                        )
                        elapsed = time.time() - start
                        logger.info(
                            "Background indexing complete (progress.end): "
                            f"{total_files} .idx files ({elapsed:.0f}s)"
                        )
                        return
                    continue
                if saw_begin and active:
                    # Indexing in flight — wait for the end event. Do NOT
                    # drop into mtime fallback: a single long-running TU
                    # (>stable_seconds with no other writes) would falsely
                    # trip the stability check.
                    continue
                # not saw_begin yet
                if (time.time() - start) < idle_timeout:
                    continue
                # Progress never started AND we've waited idle_timeout —
                # drop into mtime fallback (old clangd, capability hiccup).

            # ── Path B: mtime fallback (no progress signal) ───────────
            current_max_mtime = 0.0
            total_files = 0
            for d in candidate_dirs:
                mt = self._get_max_mtime(d)
                if mt > current_max_mtime:
                    current_max_mtime = mt
                if d.exists():
                    total_files += len(list(d.glob("*.idx")))

            if current_max_mtime > last_max_mtime:
                last_max_mtime = current_max_mtime
                last_change = time.time()
                activity_detected = True
                elapsed = time.time() - start
                logger.info(
                    f"Indexing in progress: {total_files} .idx files "
                    f"({elapsed:.0f}s elapsed)"
                )
            elif activity_detected and (time.time() - last_change) >= stable_seconds:
                elapsed = time.time() - start
                logger.info(
                    f"Background indexing complete: {total_files} .idx files "
                    f"({elapsed:.0f}s)"
                )
                return
            elif (
                not activity_detected
                and has_pre_existing
                and (time.time() - start) >= idle_timeout
            ):
                logger.info(
                    f"No new indexing activity detected after {idle_timeout}s, "
                    f"using {total_files} pre-existing .idx files"
                )
                return

            # If clangd crashed, stop waiting
            if process.poll() is not None:
                logger.warning("clangd process exited during indexing")
                return

        logger.warning(
            f"Indexing timed out after {timeout}s ({total_files} .idx files)"
        )

    # ==================================================================
    # Private helpers: compile_commands.json
    # ==================================================================

    def _record_index_quality(
        self,
        graph: CodeGraph,
        *,
        baseline_graph: Optional[CodeGraph],
        policy: Optional[IndexQualityPolicy],
    ) -> bool:
        self.index_quality_report = assess_index_quality(
            graph,
            project_root=self.project_root,
            language="cpp",
            compdb_path=self.compdb_path,
            baseline_graph=baseline_graph,
            policy=policy,
        )
        compdb = self.index_quality_report.get("compile_db")
        if compdb is not None:
            compdb["warning_as_error_rewrites"] = self.compdb_warning_rewrite_count
        report_path = self.output_dir / "index_quality.json"
        report_path.write_text(
            json.dumps(self.index_quality_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "Index quality %s; report: %s",
            "passed" if self.index_quality_report["passed"] else "failed",
            report_path,
        )
        return bool(self.index_quality_report["passed"])

    @staticmethod
    def _quarantine_graph_artifact(graph_path: Path) -> Optional[Path]:
        if not graph_path.exists():
            return None
        rejected = graph_path.with_name(
            f"{graph_path.stem}.rejected{graph_path.suffix}"
        )
        os.replace(graph_path, rejected)
        logger.warning("Quarantined rejected graph artifact at %s", rejected)
        return rejected

    def _is_valid_compdb(self, compdb: Path) -> bool:
        """Check compile_commands.json is parseable and non-empty."""
        return compilation_database_entry_count(compdb) > 0

    def _auto_generate_compdb(self) -> Optional[Path]:
        """Try generating compile_commands.json with common build flows."""
        compdb = self._auto_generate_compdb_cmake()
        if compdb is not None and self._is_preferred_compdb(compdb):
            return compdb
        if compdb is not None:
            compdb = self._snapshot_compdb(compdb, "cmake")
        # If CMake fails or captures only a suspiciously small target, compare
        # it with Bear/Make candidates instead of accepting first success.
        bear_compdb = self._auto_generate_compdb_bear()
        return self._better_compdb(compdb, bear_compdb)

    def _auto_generate_compdb_cmake(self) -> Optional[Path]:
        cmake_lists = self.project_root / "CMakeLists.txt"
        if not cmake_lists.exists() or not shutil.which("cmake"):
            return None

        build_dir = self.project_root / "build"
        build_dir.mkdir(exist_ok=True)

        cmd = [
            "cmake",
            "-S",
            str(self.project_root),
            "-B",
            str(build_dir),
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ]
        logger.info("Attempting CMake compilation DB generation: %s", " ".join(cmd))
        try:
            subprocess.run(
                cmd,
                check=True,
                cwd=self.project_root,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to generate compile_commands.json with CMake: %s", e)
            if e.stderr:
                logger.warning("cmake stderr: %s", e.stderr.strip())
            return None

        compdb = build_dir / "compile_commands.json"
        return compdb if compdb.exists() else None

    def _auto_generate_compdb_bear(self) -> Optional[Path]:
        if not shutil.which("bear"):
            logger.info("bear not found; skipping Make-based compilation DB generation")
            return None
        if not shutil.which("make"):
            logger.info("make not found; cannot run bear -- make")
            return None

        best: Optional[Path] = None

        # Strategy 1: No root Makefile at all — check if autotools can create one,
        # otherwise go directly to subdirectory search.
        if not self._has_root_makefile():
            if self._is_autotools_project():
                logger.info(
                    "Autotools project detected (no Makefile yet); bootstrapping first"
                )
                self._bootstrap_autotools()
                # After bootstrap, the Makefile should exist now
                if self._has_root_makefile():
                    compdb = self._bear_make()
                    best = self._better_compdb(best, compdb)
                    if compdb and self._is_preferred_compdb(compdb):
                        return compdb
            # No root Makefile (even after bootstrap) — try subdirectory builds
            logger.info("No root Makefile found; searching subdirectories")
            compdb = self._try_subdirectory_make()
            return self._better_compdb(best, compdb)

        # Strategy 2: Autotools project with existing Makefile — bootstrap
        # first so that ./configure regenerates a proper Makefile.  Without
        # this, a stale or stub Makefile causes bear to capture very few files.
        if self._is_autotools_project():
            logger.info("Autotools project detected; bootstrapping before bear")
            self._bootstrap_autotools()

        # Strategy 3: Run bear -- make (make clean is done inside _bear_make).
        logger.info("Attempting Make compilation DB generation with bear")
        compdb = self._bear_make()
        best = self._better_compdb(best, compdb)
        if compdb and self._is_preferred_compdb(compdb):
            return compdb

        # Strategy 4: Compare a suspiciously small root result with prioritized
        # subdirectory builds instead of accepting the first non-empty JSON file.
        compdb = self._try_subdirectory_make()
        return self._better_compdb(best, compdb)

    def _has_root_makefile(self) -> bool:
        """Check if the project root has a Makefile or GNUmakefile."""
        return any(
            (self.project_root / name).exists()
            for name in ("Makefile", "GNUmakefile", "makefile")
        )

    def _is_autotools_project(self) -> bool:
        """Check if the project uses autotools (configure.ac / autogen.sh)."""
        return (
            (self.project_root / "configure.ac").exists()
            or (self.project_root / "configure.in").exists()
            or (self.project_root / "autogen.sh").exists()
        )

    def _bear_make(self) -> Optional[Path]:
        """Run bear -- make and return compile_commands.json path if valid.

        bear may exit 0 even when make partially fails, producing an empty
        compile_commands.json.  We use ``-k`` so make keeps going after
        individual target failures, maximising the number of compilation
        commands bear can capture.

        A ``make clean`` is run first so that incremental builds do not cause
        bear to miss already-compiled translation units.
        """
        candidates = (
            self.project_root / "compile_commands.json",
            self.project_root / "build" / "compile_commands.json",
        )

        make_overrides = self._indexing_make_overrides(self.project_root)

        # Clean first so bear can observe all compiler invocations
        self._run_build_command(["make", "clean"])

        # Try parallel build first (-k = keep going on errors)
        self._clear_compdb_candidates(candidates)
        self._run_build_command(["bear", "--", "make", "-k", "-j", *make_overrides])
        best = self._snapshot_best_compdb(candidates, "root-parallel")
        if best and self._is_preferred_compdb(best):
            return best

        # Retry without -j (some Makefiles break under parallelism)
        self._clear_compdb_candidates(candidates)
        self._run_build_command(["bear", "--", "make", "-k", *make_overrides])
        serial = self._snapshot_best_compdb(candidates, "root-serial")
        return self._better_compdb(best, serial)

    def _try_subdirectory_make(self) -> Optional[Path]:
        """Search for Makefiles in immediate subdirectories and common paths."""
        # Prioritised well-known subdirectory patterns, then scan depth-1 dirs.
        well_known = ["ports/unix", "src", "lib"]
        candidates = []
        for rel in well_known:
            subdir = self.project_root / rel
            if (subdir / "Makefile").exists() or (subdir / "GNUmakefile").exists():
                candidates.append(subdir)
        # Also scan depth-1 subdirectories for any Makefile not already found.
        try:
            for entry in sorted(self.project_root.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if entry in candidates:
                    continue
                if (entry / "Makefile").exists() or (entry / "GNUmakefile").exists():
                    candidates.append(entry)
        except OSError:
            pass

        # Initialize git submodules if needed (some subdirectory builds need them).
        if candidates and (self.project_root / ".gitmodules").exists():
            self._run_build_command(["git", "submodule", "update", "--init"])

        best: Optional[Path] = None
        compdb_candidates = (
            self.project_root / "compile_commands.json",
            self.project_root / "build" / "compile_commands.json",
        )
        for subdir in candidates:
            logger.info(
                "Found Makefile in subdirectory %s, attempting bear -- make there",
                subdir,
            )
            self._run_build_command(["make", "-C", str(subdir), "clean"])
            make_overrides = self._indexing_make_overrides(subdir)
            self._clear_compdb_candidates(compdb_candidates)
            self._run_build_command(
                [
                    "bear",
                    "--",
                    "make",
                    "-k",
                    "-C",
                    str(subdir),
                    "-j",
                    *make_overrides,
                ]
            )
            label = f"{subdir.relative_to(self.project_root)}-parallel"
            generated = self._snapshot_best_compdb(compdb_candidates, label)
            best = self._better_compdb(best, generated)
            if generated and self._is_preferred_compdb(generated):
                return generated

            self._clear_compdb_candidates(compdb_candidates)
            self._run_build_command(
                [
                    "bear",
                    "--",
                    "make",
                    "-k",
                    "-C",
                    str(subdir),
                    *make_overrides,
                ]
            )
            label = f"{subdir.relative_to(self.project_root)}-serial"
            generated = self._snapshot_best_compdb(compdb_candidates, label)
            best = self._better_compdb(best, generated)
            if generated and self._is_preferred_compdb(generated):
                return generated
        return best

    def _indexing_make_overrides(self, directory: Path) -> List[str]:
        """Return non-fatal warning overrides supported by a Makefile."""

        makefile = next(
            (
                directory / name
                for name in ("Makefile", "GNUmakefile", "makefile")
                if (directory / name).exists()
            ),
            None,
        )
        if makefile is None:
            return []
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        overrides: list[str] = []
        for variable in ("CFLAGS_EXTRA", "CXXFLAGS_EXTRA", "CPPFLAGS_EXTRA"):
            if re.search(rf"\b{variable}\b", text):
                value = self._makefile_variable_value(text, variable)
                value = f"{value} -Wno-error".strip()
                overrides.append(f"{variable}={value}")
        if not overrides and re.search(r"(?m)^\s*CWARN\s*[?:+]?=", text):
            value = self._makefile_variable_value(text, "CWARN")
            value = re.sub(r"-Werror(?:=[A-Za-z0-9_-]+)?", "-Wno-error", value)
            if "-Wno-error" not in value:
                value = f"{value} -Wno-error".strip()
            overrides.append(f"CWARN={value}")
        for variable in ("WERROR", "WARNINGS_AS_ERRORS"):
            if re.search(rf"(?m)^\s*{variable}\s*[?:+]?=", text):
                overrides.append(f"{variable}=")
        return overrides

    @staticmethod
    def _makefile_variable_value(text: str, variable: str) -> str:
        """Approximate a simple Make variable value before overriding it."""

        value = os.environ.get(variable, "")
        logical_text = re.sub(r"\\\r?\n\s*", " ", text)
        pattern = re.compile(
            rf"(?m)^\s*{re.escape(variable)}\s*(\?=|:=|\+=|=)\s*(.*?)\s*$"
        )
        for operator, raw in pattern.findall(logical_text):
            item = raw.split("#", 1)[0].strip()
            if operator == "+=":
                value = f"{value} {item}".strip()
            elif operator == "?=":
                if not value:
                    value = item
            else:
                value = item
        return value

    @staticmethod
    def _clear_compdb_candidates(candidates) -> None:
        for candidate in candidates:
            try:
                Path(candidate).unlink(missing_ok=True)
            except OSError as exc:
                logger.debug(
                    "Ignoring compilation database cleanup failure for %s: %s",
                    candidate,
                    exc,
                )

    def _snapshot_best_compdb(self, candidates, label: str) -> Optional[Path]:
        valid = [Path(path) for path in candidates if self._is_valid_compdb(Path(path))]
        if not valid:
            return None
        source = max(valid, key=compilation_database_entry_count)
        return self._snapshot_compdb(source, label)

    def _snapshot_compdb(self, source: Path, label: str) -> Path:
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-")
        snapshot = self.output_dir / f"compile_commands.{safe_label}.json"
        shutil.copy2(source, snapshot)
        logger.info(
            "Captured compilation database candidate %s (%d entries)",
            snapshot,
            compilation_database_entry_count(snapshot),
        )
        return snapshot

    def _better_compdb(
        self, current: Optional[Path], candidate: Optional[Path]
    ) -> Optional[Path]:
        if candidate is None:
            return current
        if current is None:
            return candidate
        return (
            candidate
            if self._compdb_score(candidate) > self._compdb_score(current)
            else current
        )

    def _compdb_score(self, compdb: Path) -> tuple:
        stats = compilation_database_stats(compdb, project_root=self.project_root)
        return (
            self._compdb_meets_policy(stats),
            int(stats["resolved_translation_unit_count"]),
            float(stats.get("source_coverage") or 0.0),
            int(stats["entry_count"]),
        )

    def _compdb_meets_policy(self, stats: dict) -> bool:
        entry_count = int(stats["entry_count"])
        source_coverage = stats.get("source_coverage")
        resolved_ratio = stats.get("resolved_ratio")
        return bool(
            entry_count >= self._quality_policy.min_compile_db_entries
            and isinstance(resolved_ratio, (int, float))
            and resolved_ratio >= self._quality_policy.min_resolved_compile_db_ratio
            and isinstance(source_coverage, (int, float))
            and source_coverage >= self._quality_policy.min_source_coverage
        )

    def _is_preferred_compdb(self, compdb: Path) -> bool:
        stats = compilation_database_stats(compdb, project_root=self.project_root)
        entry_count = int(stats["entry_count"])
        source_coverage = stats.get("source_coverage")
        resolved_ratio = stats.get("resolved_ratio")
        preferred = self._compdb_meets_policy(stats)
        if not preferred:
            logger.warning(
                "Compilation database candidate does not meet quality policy: "
                "%d entries, resolved ratio=%s, source coverage=%s",
                entry_count,
                (
                    f"{resolved_ratio:.3f}"
                    if isinstance(resolved_ratio, (int, float))
                    else "unknown"
                ),
                (
                    f"{source_coverage:.3f}"
                    if isinstance(source_coverage, (int, float))
                    else "unknown"
                ),
            )
        return preferred

    def _bootstrap_autotools(self) -> None:
        autogen = self.project_root / "autogen.sh"
        configure = self.project_root / "configure"
        configure_ac = self.project_root / "configure.ac"

        gitmodules = self.project_root / ".gitmodules"
        if gitmodules.exists():
            logger.info("Initializing git submodules")
            self._run_build_command(["git", "submodule", "update", "--init"])

        if autogen.exists():
            logger.info("Running autogen.sh")
            self._run_build_command(["sh", str(autogen)])
        elif configure_ac.exists() and shutil.which("autoreconf"):
            logger.info("Running autoreconf -fi")
            self._run_build_command(["autoreconf", "-fi"])

        if configure.exists():
            configure_cmd = ["sh", str(configure)]
            # Detect optional dependencies that can be built from bundled sources,
            # so configure doesn't fail on missing system libraries.
            configure_cmd += self._detect_configure_flags()
            logger.info("Running configure command: %s", " ".join(configure_cmd))
            self._run_build_command(configure_cmd)

    def _detect_configure_flags(self) -> List[str]:
        """Detect useful configure flags by inspecting configure.ac / configure."""
        flags = []
        configure_ac = self.project_root / "configure.ac"
        if not configure_ac.exists():
            configure_ac = self.project_root / "configure.in"
        if not configure_ac.exists():
            return flags

        try:
            text = configure_ac.read_text(errors="replace")
        except OSError:
            return flags

        # Detect --with-<dep>=builtin patterns from AC_ARG_WITH declarations
        # that mention "builtin" as an option. Common in projects bundling deps.
        for m in re.finditer(r"AC_ARG_WITH\(\[?(\w+)\]?", text):
            dep_name = m.group(1)
            # Check if "builtin" is mentioned near this AC_ARG_WITH block
            start = m.start()
            snippet = text[start : start + 500]
            if "builtin" in snippet:
                flags.append(f"--with-{dep_name}=builtin")
                logger.info("Detected bundled dependency: --with-%s=builtin", dep_name)

        return flags

    def _run_build_command(self, cmd: List[str], timeout_sec: int = 1200) -> bool:
        logger.info("Running build command: %s", " ".join(cmd))
        try:
            subprocess.run(
                cmd,
                check=True,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            return True
        except subprocess.TimeoutExpired:
            logger.warning(
                "Command timed out after %ss: %s", timeout_sec, " ".join(cmd)
            )
            return False
        except subprocess.CalledProcessError as e:
            logger.warning("Command failed: %s", " ".join(cmd))
            if e.stderr:
                logger.warning("stderr: %s", e.stderr[:600].strip())
            return False


# ──────────────────────────────────────────────────────────────────────────
# Module-level helpers (kept outside ClangdIndexer so they can be reused
# by other clangd-driving code paths without becoming part of the class
# surface area).
# ──────────────────────────────────────────────────────────────────────────


def _clangd_progress_reader(stream, progress_state: dict) -> None:
    """Parse Content-Length framed JSON-RPC from clangd stdout; track
    ``backgroundIndexProgress`` ``begin``/``end`` events into
    ``progress_state`` (a dict with keys ``saw_begin: bool``,
    ``active_tokens: set``, ``lock: threading.Lock``)."""
    buf = b""
    try:
        while True:
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
            kind = (params.get("value") or {}).get("kind")
            with progress_state["lock"]:
                if kind == "begin":
                    progress_state["saw_begin"] = True
                    progress_state["active_tokens"].add(params["token"])
                elif kind == "end":
                    progress_state["active_tokens"].discard(params["token"])
    except Exception:
        # Reader exit is non-fatal — fallbacks (mtime poll, process death,
        # outer timeout) will still terminate the wait loop.
        return


def _clangd_stream_drain(stream) -> None:
    """Best-effort drain so clangd doesn't block on a full pipe buffer."""
    try:
        while True:
            chunk = (
                stream.read1(65536) if hasattr(stream, "read1") else stream.read(4096)
            )
            if not chunk:
                return
    except Exception:
        return
