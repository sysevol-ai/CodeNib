#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
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
from ..profiler import Profiler

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
            self.output_dir = Path("/tmp") / self.project_root.name

        os.makedirs(self.output_dir, exist_ok=True)

        self.index_file = None  # clangd generates .idx files in idx_directory
        self.decoded_file = None  # no protoc decode step for clangd
        self.graph_file = self.output_dir / "graph.pkl"
        self.exclude_patterns = exclude_patterns if exclude_patterns else []
        self.profiler = profiler or Profiler("clangd_indexer")

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
                "clangd not found. Please install clangd:\n"
                "  apt install clangd  (Debian/Ubuntu)\n"
                "  brew install llvm   (macOS)"
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
        return [
            clangd_cmd,
            "--background-index",
            f"--compile-commands-dir={compile_commands_dir}",
            "--background-index-priority=normal",
            "--log=error",
        ]

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

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
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

        if output_file is None:
            output_file = str(self.graph_file)

        # Check graph cache
        if skip_level == "graph" and self.graph_file.exists():
            logger.info(f"Loading cached graph from {self.graph_file}")
            try:
                graph = CodeGraph.load_graph(str(self.graph_file))
                logger.info(
                    f"Loaded cached graph "
                    f"({len(graph.graph.vs)} nodes, {len(graph.graph.es)} edges)"
                )
                return graph
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
        if should_generate and (
            "compdb_path" not in kwargs or not kwargs.get("compdb_path")
        ):
            for candidate in (
                self.project_root / "compile_commands.json",
                self.project_root / "build" / "compile_commands.json",
            ):
                if self._is_valid_compdb(candidate):
                    kwargs["compdb_path"] = str(candidate)
                    break
                if candidate.exists():
                    logger.warning(
                        "Ignoring invalid compilation database: %s", candidate
                    )
            else:
                generated = self._auto_generate_compdb()
                if generated is not None and self._is_valid_compdb(generated):
                    kwargs["compdb_path"] = str(generated)
                elif generated is not None:
                    logger.warning(
                        "Auto-generated compilation database is invalid: %s", generated
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
                        "rootUri": f"file://{self.project_root}",
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

        source_files = [e["file"] for e in entries if "file" in e]
        logger.info(f"Opening {len(source_files)} source files to trigger indexing")

        for src_file in source_files:
            if not Path(src_file).is_file():
                continue
            try:
                text = Path(src_file).read_text(errors="replace")
            except Exception:
                continue

            self._lsp_send(
                process,
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": f"file://{src_file}",
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
                    if process.poll() is not None:
                        logger.warning("clangd process exited during indexing")
                        return
                    continue
                # If progress never started AND we've waited long enough,
                # fall through to mtime polling — handles old clangd or
                # capability negotiation failure.
                if not saw_begin and (time.time() - start) < idle_timeout:
                    if process.poll() is not None:
                        logger.warning("clangd process exited during indexing")
                        return
                    continue
                # else: drop into mtime fallback below

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

    def _is_valid_compdb(self, compdb: Path) -> bool:
        """Check compile_commands.json is parseable and non-empty."""
        if not compdb.exists() or not compdb.is_file():
            return False
        try:
            if compdb.stat().st_size < 4:
                return False
            with compdb.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
            return isinstance(payload, list) and len(payload) > 0
        except Exception:
            return False

    def _auto_generate_compdb(self) -> Optional[Path]:
        """Try generating compile_commands.json with common build flows."""
        compdb = self._auto_generate_compdb_cmake()
        if compdb is not None:
            return compdb
        # If CMake approach fails, try bear + make with various heuristics to find a Makefile.
        compdb = self._auto_generate_compdb_bear()
        return compdb

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
                    if compdb:
                        return compdb
            # No root Makefile (even after bootstrap) — try subdirectory builds
            logger.info("No root Makefile found; searching subdirectories")
            compdb = self._try_subdirectory_make()
            if compdb:
                return compdb
            return None

        # Strategy 2: Autotools project with existing Makefile — bootstrap
        # first so that ./configure regenerates a proper Makefile.  Without
        # this, a stale or stub Makefile causes bear to capture very few files.
        if self._is_autotools_project():
            logger.info("Autotools project detected; bootstrapping before bear")
            self._bootstrap_autotools()

        # Strategy 3: Run bear -- make (make clean is done inside _bear_make).
        logger.info("Attempting Make compilation DB generation with bear")
        compdb = self._bear_make()
        if compdb:
            return compdb

        # Strategy 4: Fallback to subdirectory search
        compdb = self._try_subdirectory_make()
        if compdb:
            return compdb
        return None

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

        # Clean first so bear can observe all compiler invocations
        self._run_build_command(["make", "clean"])

        # Try parallel build first (-k = keep going on errors)
        self._run_build_command(["bear", "--", "make", "-k", "-j"])
        for candidate in candidates:
            if self._is_valid_compdb(candidate):
                return candidate

        # Retry without -j (some Makefiles break under parallelism)
        self._run_build_command(["bear", "--", "make", "-k"])
        for candidate in candidates:
            if self._is_valid_compdb(candidate):
                return candidate
        return None

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

        for subdir in candidates:
            logger.info(
                "Found Makefile in subdirectory %s, attempting bear -- make there",
                subdir,
            )
            self._run_build_command(["make", "-C", str(subdir), "clean"])
            if self._run_build_command(
                ["bear", "--", "make", "-k", "-C", str(subdir), "-j"]
            ) or self._run_build_command(
                ["bear", "--", "make", "-k", "-C", str(subdir)]
            ):
                compdb = self.project_root / "compile_commands.json"
                if compdb.exists():
                    try:
                        with open(compdb) as f:
                            entries = json.load(f)
                        if entries:
                            logger.info(
                                "Generated compile_commands.json from %s (%d entries)",
                                subdir,
                                len(entries),
                            )
                            return compdb
                    except (json.JSONDecodeError, OSError):
                        pass
        return None

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
