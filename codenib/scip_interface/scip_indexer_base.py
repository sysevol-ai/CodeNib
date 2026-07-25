#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Base class for SCIP indexers across different languages.
"""
import fnmatch
import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Union

from ..graph.code_graph import CodeGraph
from ..log_utils import get_logger
from ..paths import temp_state_dir
from ..profiler import Profiler
from ..types import NODE_TYPE_DIRECTORY, NODE_TYPE_FILE, ROOT_NODE, is_symbol_node

logger = get_logger("scip_indexer_base")


def extract_symbol(text: str) -> Optional[str]:
    """Return the unescaped value of the first ``symbol: "..."`` field in a
    decoded SCIP occurrence/symbol_information block.

    Mirrors ``core/scip_decode_base.cpp::extract_symbol`` so Python and C++
    decoders see byte-identical symbol strings. A naive regex
    ``r'symbol:\\s*"([^"]+)"'`` stops at the first ``"`` byte, which cuts a
    SCIP symbol containing a literal ``"`` (emitted by scip-typescript as
    ``\\"`` for JS string-literal object keys like ``"version"``) in half
    and yields a trailing ``\\`` on the captured portion.

    The function walks the literal, honouring protobuf TextFormat escapes
    (``\\"``, ``\\\\``, ``\\'``, ``\\n``, ``\\t``, ``\\r``), and returns the
    decoded string. Unknown ``\\x`` escapes fall through to the next char
    (matches protobuf's lenient TextFormat parser).
    """
    kw = "symbol:"
    pos = 0
    while pos < len(text):
        k = text.find(kw, pos)
        if k < 0:
            return None
        if k > 0:
            prev = text[k - 1]
            if prev.isalnum() or prev == "_":
                pos = k + len(kw)
                continue
        i = k + len(kw)
        while i < len(text) and text[i] in " \t":
            i += 1
        if i >= len(text) or text[i] != '"':
            pos = k + len(kw)
            continue
        i += 1
        buf: List[str] = []
        while i < len(text):
            ch = text[i]
            if ch == '"':
                return "".join(buf)
            if ch == "\\" and i + 1 < len(text):
                nxt = text[i + 1]
                if nxt == "n":
                    buf.append("\n")
                elif nxt == "t":
                    buf.append("\t")
                elif nxt == "r":
                    buf.append("\r")
                elif nxt == "\\":
                    buf.append("\\")
                elif nxt == '"':
                    buf.append('"')
                elif nxt == "'":
                    buf.append("'")
                else:
                    buf.append(nxt)
                i += 2
                continue
            buf.append(ch)
            i += 1
        return None
    return None


def _normalize_rel_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    normalized = Path(str(path).strip().replace("\\", "/")).as_posix().strip("/")
    return normalized or None


def _definition_path(attrs: dict) -> str | None:
    unified_name = attrs.get("unified_name")
    if isinstance(unified_name, str) and ":" in unified_name:
        return unified_name.split(":", 1)[0]
    return None


def _matches_any(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = _normalize_rel_path(pattern)
        if normalized is None:
            continue
        if fnmatch.fnmatch(path, normalized):
            return True
        if normalized.endswith("/**"):
            prefix = normalized[:-3]
            if path == prefix or path.startswith(f"{prefix}/"):
                return True
    return False


def extract_scip_blocks(text: str, keyword: str) -> List[str]:
    """Return the content of every top-level ``<keyword> { ... }`` block
    in the decoded SCIP text format, with proper brace counting.

    Mirrors ``core/scip_decode_base.cpp::extract_blocks`` so Python and
    C++ decoders segment the same ``documents`` / ``occurrences`` blocks.
    A naive regex ``r"<kw>\\s*{(.*?)}"`` truncates at the first ``}``
    which corrupts any SCIP symbol containing literal ``{``/``}`` (e.g.
    JS object keys like ``'obj{}'``).
    """
    blocks: List[str] = []
    pos = 0
    kw_len = len(keyword)
    while pos < len(text):
        k = text.find(keyword, pos)
        if k < 0:
            break
        if k > 0:
            prev = text[k - 1]
            if prev.isalnum() or prev == "_" or prev == "/":
                pos = k + kw_len
                continue
        b = k + kw_len
        while b < len(text) and text[b].isspace():
            b += 1
        if b >= len(text) or text[b] != "{":
            pos = k + kw_len
            continue
        start = b + 1
        depth = 1
        in_string = False
        escape = False
        matched = False
        i = start
        while i < len(text):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        blocks.append(text[start:i])
                        pos = i + 1
                        matched = True
                        break
            i += 1
        if not matched:
            break
    return blocks


class SCIPIndexerBase(ABC):
    """
    Abstract base class for SCIP indexers.

    This class provides common functionality for all SCIP indexers,
    while allowing language-specific implementations to customize
    the indexing process.
    """

    _VALID_BACKENDS = ("serial", "core")

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
        language: str = "unknown",
        decoder_backend: Optional[str] = None,
    ):
        """
        Initialize the SCIP indexer.

        Args:
            project_root: Root directory of the project to index
            output_dir: Directory to store output files
            exclude_patterns: List of patterns to exclude from indexing
            profiler: Profiler instance for performance tracking
            language: Language being indexed (for logging)
            decoder_backend: Which decoder to use when ``process_index`` runs.
                ``"serial"`` (default) uses the pure-Python per-language decoder;
                ``"core"`` uses the C++ pybind decoder from ``core/``.
        """
        self.project_root = Path(project_root).absolute()
        self.language = language
        self.decoder_backend = self._resolve_backend(decoder_backend)

        if output_dir:
            self.output_dir = Path(output_dir).absolute()
        else:
            self.output_dir = temp_state_dir() / self.project_root.name

        # Create output directory if it doesn't exist
        os.makedirs(self.output_dir, exist_ok=True)

        # Set paths for index files in the output directory
        self.index_file = self.output_dir / "index.scip"
        self.decoded_file = self.output_dir / "index.decoded"
        self.graph_file = self.output_dir / "graph.pkl"
        self.lsp_index_file = self.output_dir / "lsp_index.pkl"
        self.exclude_patterns = exclude_patterns if exclude_patterns else []
        self._target_dir: str | None = None
        self.profiler = profiler or Profiler(f"scip_{language}_indexer")

        # Path to the scip.proto file (shared across all indexers)
        self.module_dir = Path(__file__).parent
        self.proto_file = self.module_dir / "scip.proto"

    @classmethod
    def _resolve_backend(cls, backend: Optional[str]) -> str:
        if backend is None:
            return "serial"
        normalized = backend.lower()
        if normalized not in cls._VALID_BACKENDS:
            raise ValueError(
                f"Unknown decoder_backend {backend!r}. "
                f"Supported: {cls._VALID_BACKENDS}"
            )
        return normalized

    def _make_decoder(self, index_file: str, project_root: Union[str, Path]):
        """Instantiate the decoder selected by ``self.decoder_backend``.

        Returns an object with ``decode() -> CodeGraph`` and ``save_graph(path)``.
        """
        if self.decoder_backend == "core":
            from .scip_decode_core import SCIPDecoderCore

            return SCIPDecoderCore(
                index_file_path=index_file,
                project_root=str(project_root) if project_root else None,
                language=self.language,
            )
        return self._get_decoder_class()(index_file, project_root=project_root)

    @abstractmethod
    def _check_indexer_available(self) -> bool:
        """
        Check if the language-specific indexer tool is available.

        Returns:
            bool: True if indexer is available, False otherwise
        """
        pass

    @abstractmethod
    def _build_index_command(self, **kwargs) -> List[str]:
        """
        Build the command to generate the SCIP index.

        Args:
            **kwargs: Language-specific options

        Returns:
            List[str]: Command as list of strings
        """
        pass

    @abstractmethod
    def _get_decoder_class(self):
        """
        Get the decoder class for this language.

        Returns:
            The decoder class to use for processing the SCIP index
        """
        pass

    def generate_index(self, **kwargs) -> bool:
        """
        Generate SCIP index for the project.

        Args:
            **kwargs: Language-specific options

        Returns:
            bool: True if index generation was successful, False otherwise
        """
        if not self._check_indexer_available():
            return False

        # Build the command
        cmd = self._build_index_command(**kwargs)

        logger.debug(f"Running command: {' '.join(cmd)}")

        # Run the command
        with self.profiler.section("generate_index") as section:
            try:
                # For Rust projects, override the repository toolchain so SCIP
                # generation uses CodeNib's selected rust-analyzer version.
                env = None
                if self.language == "rust":
                    import os

                    from .rust_analyzer import rust_toolchain

                    env = os.environ.copy()
                    env["RUSTUP_TOOLCHAIN"] = rust_toolchain()

                subprocess.run(
                    cmd,
                    check=True,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                success = True
            except subprocess.CalledProcessError as e:
                logger.error(f"Error generating SCIP index: {e}")
                if e.stdout:
                    logger.error(f"stdout: {e.stdout}")
                if e.stderr:
                    logger.error(f"stderr: {e.stderr}")
                success = False

        duration = section.duration

        if success:
            logger.info(f"Successfully generated SCIP index at {self.index_file}")
            logger.info(f"⏱️  Index generation took: {duration:.2f} seconds")
            return True
        else:
            logger.error(f"❌ Index generation failed after {duration:.2f} seconds")
            return False

    def decode_index(self) -> bool:
        """
        Decode the SCIP index using protobuf to create a readable version.

        Returns:
            bool: True if decoding was successful, False otherwise
        """
        if not self.index_file.exists():
            logger.error(f"Index file not found at {self.index_file}")
            return False

        try:
            # Using protoc to decode the binary SCIP file
            cmd = [
                "protoc",
                "--decode=scip.Index",
                f"--proto_path={self.module_dir}",
                "scip.proto",
                f"< {self.index_file}",
                f"> {self.decoded_file}",
            ]

            # We need to use shell=True for the redirect operators
            cmd_str = " ".join(cmd)
            logger.info(f"Running command: {cmd_str}")

            with self.profiler.section("decode_index") as section:
                subprocess.run(cmd_str, shell=True, check=True, cwd=self.module_dir)
            duration = section.duration

            logger.info(f"Successfully decoded SCIP index to {self.decoded_file}")
            logger.info(f"⏱️  Index decoding took: {duration:.2f} seconds")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Error decoding SCIP index: {e}")
            return False

    def process_index(
        self, output_file: Optional[str] = None
    ) -> Union[CodeGraph, None]:
        """
        Process the decoded SCIP index into a more usable format.

        Args:
            output_file: Path to write the processed data to

        Returns:
            CodeGraph: Processed graph object
        """
        if not self.decoded_file.exists():
            logger.error(f"Decoded index file not found at {self.decoded_file}")
            return None

        try:
            logger.info(
                f"Starting SCIP index processing (backend={self.decoder_backend})..."
            )
            with self.profiler.section("process_index.decode") as section:
                decoder = self._make_decoder(
                    str(self.decoded_file), project_root=self.project_root
                )
                graph: CodeGraph = decoder.decode()
            duration = section.duration
            graph = self._filter_project_graph(graph)

            from .lsp_occurrence_index import SCIPOccurrenceIndex

            with self.profiler.section("process_index.build_lsp_occurrence_index"):
                occurrence_index = SCIPOccurrenceIndex.from_decoded_file(
                    self.decoded_file,
                    path_filter=(
                        self._path_allowed
                        if self._target_dir or self.exclude_patterns
                        else None
                    ),
                )
            graph.lsp_occurrence_index = occurrence_index

            # Build line-range indexes once the graph is fully assembled.
            # Must happen before save_graph so the indexes are persisted.
            with self.profiler.section("process_index.build_range_indexes"):
                graph.build_range_indexes()

            if output_file:
                with self.profiler.section("process_index.save_graph") as save_section:
                    output_path = Path(output_file)
                    graph.save_graph(str(output_path))
                    occurrence_index.save(output_path.with_name("lsp_index.pkl"))
                save_duration = save_section.duration
                logger.info(f"Saved processed SCIP index to {output_path}")
                logger.info(f"⏱️  Graph saving took: {save_duration:.2f} seconds")

            n_nodes = graph.graph.vcount()
            n_edges = graph.graph.ecount()
            logger.info(
                f"✅ Graph created successfully ({n_nodes} nodes, {n_edges} edges)"
            )
            if n_nodes <= 1:
                logger.warning(
                    "⚠️  Graph has no symbol nodes — the SCIP index may be empty. "
                    "Check that the indexer produced valid output."
                )
            logger.info(f"⏱️  Index processing took: {duration:.2f} seconds")
            return graph

        except Exception as e:
            logger.error(f"Error processing SCIP index: {e}")
            return None

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ) -> Union[CodeGraph, None]:
        """
        Run the complete SCIP indexing pipeline: generate, decode, and process.

        Args:
            output_file: Path to write the processed data to (if None, uses self.graph_file)
            skip_level: Cache/skip level - 'graph', 'decode', 'raw', or None
                - 'graph': Check if graph.pkl exists, load and return it if found
                - 'decode': Check if index.decoded exists, skip to processing if found
                - 'raw': Check if index.scip exists, skip to decoding if found
                - None: Run full pipeline from scratch (default)
            reset_profiler: Clear profiler stats before running the pipeline
            report_profile: Emit profiler summary automatically after the run
            **kwargs: Language-specific options passed to generate_index

        Returns:
            CodeGraph: Processed graph object
        """
        # Use default graph file if output_file not specified
        if output_file is None:
            output_file = str(self.graph_file)
        self._target_dir = _normalize_rel_path(kwargs.pop("target_dir", None))

        # Check graph cache if skip_level is 'graph'
        if skip_level == "graph" and self.graph_file.exists():
            logger.info(f"Loading cached graph from {self.graph_file}")
            try:
                graph = CodeGraph.load_graph(str(self.graph_file))
                if self.lsp_index_file.is_file():
                    from .lsp_occurrence_index import SCIPOccurrenceIndex

                    graph.lsp_occurrence_index = SCIPOccurrenceIndex.load(
                        self.lsp_index_file
                    )
                elif self.decoded_file.is_file():
                    from .lsp_occurrence_index import SCIPOccurrenceIndex

                    graph.lsp_occurrence_index = SCIPOccurrenceIndex.from_decoded_file(
                        self.decoded_file,
                        path_filter=(
                            self._path_allowed
                            if self._target_dir or self.exclude_patterns
                            else None
                        ),
                    )
                    graph.lsp_occurrence_index.save(self.lsp_index_file)
                if self._target_dir or self.exclude_patterns:
                    graph = self._filter_project_graph(graph)
                    graph.build_range_indexes()
                    if output_file:
                        graph.save_graph(output_file)
                        if hasattr(graph, "lsp_occurrence_index"):
                            graph.lsp_occurrence_index.save(
                                Path(output_file).with_name("lsp_index.pkl")
                            )
                logger.info(
                    "✅ Successfully loaded cached graph "
                    f"({len(graph.graph.vs)} nodes, "
                    f"{len(graph.graph.es)} edges)"
                )
                return graph
            except Exception as e:
                logger.warning(
                    f"Failed to load cached graph: {e}. Proceeding with pipeline..."
                )

        # Determine what needs to be generated based on what exists
        if skip_level in ("graph", "decode") and self.decoded_file.exists():
            logger.info(
                f"Found existing decoded file at "
                f"{self.decoded_file}, skipping generation and decode"
            )
            should_generate_index = False
            should_decode_index = False
        elif skip_level in ("graph", "decode", "raw") and self.index_file.exists():
            logger.info(
                f"Found existing raw index at {self.index_file}, skipping generation"
            )
            should_generate_index = False
            should_decode_index = True
        else:
            should_generate_index = True
            should_decode_index = True

        if reset_profiler:
            self.profiler.reset()

        try:
            # Generate the index if needed
            if should_generate_index:
                logger.info("Generating SCIP index")
                if not self.generate_index(**kwargs):
                    # The indexer reported failure but the index
                    # file may still have been written
                    # (e.g. scip-typescript crashes during cleanup after emitting index.scip).
                    # Continue if the file exists.
                    if not self.index_file.exists():
                        return None
                    logger.warning(
                        "Index generation returned failure but %s exists; continuing.",
                        self.index_file,
                    )

            # Decode the index if needed
            if should_decode_index:
                if not self.index_file.exists():
                    logger.error(
                        f"Index file not found at {self.index_file}, cannot decode"
                    )
                    return None
                logger.info("Decoding SCIP index")
                if not self.decode_index():
                    return None

            # Process the index and save graph
            graph = self.process_index(output_file)

            if graph:
                logger.info(
                    "✅ Graph created successfully "
                    f"({len(graph.graph.vs)} nodes, "
                    f"{len(graph.graph.es)} edges)"
                )

            return graph
        finally:
            if report_profile:
                self.profiler.report(reset=reset_profiler)

    def clear_cache(self, level: str = "all") -> bool:
        """
        Clear cache files at different levels.

        Args:
            level: Preserve cache up to this pipeline stage, remove above.
                Pipeline: raw (index.scip) → decode (index.decoded) → graph (graph.pkl)
                - 'graph': keep everything (raw + decoded + graph)
                - 'decode': keep raw + decoded, remove graph
                - 'raw': keep raw, remove decoded + graph
                - 'all': remove all cache files (default)

        Returns:
            bool: True if cache clearing was successful, False otherwise
        """
        try:
            files_to_remove = []

            if level == "graph":
                files_to_remove = []
                logger.info("Clearing cache: keeping up to graph (nothing to remove)")
            elif level == "decode":
                files_to_remove = [self.graph_file, self.lsp_index_file]
                logger.info("Clearing cache: keeping up to decode, removing graph")
            elif level == "raw":
                files_to_remove = [
                    self.decoded_file,
                    self.graph_file,
                    self.lsp_index_file,
                ]
                logger.info("Clearing cache: keeping raw, removing decoded + graph")
            elif level == "all":
                files_to_remove = [
                    self.index_file,
                    self.decoded_file,
                    self.graph_file,
                    self.lsp_index_file,
                ]
                logger.info("Clearing all cache files")
            else:
                logger.error(
                    f"Invalid cache level: {level}. Must be 'graph', 'decode', 'raw', or 'all'"
                )
                return False

            # Remove the specified files
            for file_path in files_to_remove:
                if file_path.exists():
                    file_path.unlink()
                    logger.info(f"Removed {file_path}")
                else:
                    logger.debug(f"File does not exist, skipping: {file_path}")

            logger.info("✅ Cache cleared successfully")
            return True

        except Exception as e:
            logger.error(f"Error clearing cache: {e}")
            return False

    def _filter_project_graph(self, graph: CodeGraph) -> CodeGraph:
        """Apply route-level path filters after SCIP decode.

        SCIP indexers often index more source sets than a route-level LSP
        baseline. Filtering here keeps backend-alignment gates comparing the
        same source surface and makes `target_dir` behave consistently across
        SCIP-backed languages.
        """
        if not self._target_dir and not self.exclude_patterns:
            return graph

        keep = [
            vertex.index
            for vertex in graph.graph.vs
            if self._keep_vertex(vertex.attributes())
        ]
        if len(keep) == graph.graph.vcount():
            return graph

        graph.graph = graph.graph.subgraph(keep)
        graph.name_to_vertex = {
            vertex["name"]: vertex.index
            for vertex in graph.graph.vs
            if "name" in vertex.attributes()
        }
        graph.symbol_ranges = {
            name: value
            for name, value in graph.symbol_ranges.items()
            if name in graph.name_to_vertex
        }
        graph._invalidate_edge_index()
        return graph

    def _keep_vertex(self, attrs: dict) -> bool:
        node_type = attrs.get("type")
        name = attrs.get("name")
        if node_type == "root" or name == ROOT_NODE:
            return True
        if node_type in {NODE_TYPE_DIRECTORY, NODE_TYPE_FILE}:
            return self._path_allowed(str(name), allow_target_ancestor=True)
        if is_symbol_node(node_type):
            path = _definition_path(attrs) or attrs.get("file")
            return bool(path and self._path_allowed(str(path)))
        return True

    def _path_allowed(self, path: str, *, allow_target_ancestor: bool = False) -> bool:
        rel_path = _normalize_rel_path(path)
        if not rel_path:
            return False
        if self._target_dir:
            in_target = rel_path == self._target_dir or rel_path.startswith(
                f"{self._target_dir}/"
            )
            is_target_ancestor = allow_target_ancestor and self._target_dir.startswith(
                f"{rel_path}/"
            )
            if not in_target and not is_target_ancestor:
                return False
        return not _matches_any(rel_path, self.exclude_patterns)
