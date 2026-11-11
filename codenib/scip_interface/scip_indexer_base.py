#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Base class for SCIP indexers across different languages.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Union

from ..log_utils import get_logger
from ..paths import temp_state_dir
from ..profiler import Profiler
from ..types import NODE_TYPE_DIRECTORY, NODE_TYPE_FILE, ROOT_NODE, is_symbol_node
from .scip_filter import definition_path, normalize_scip_path, scip_path_is_allowed

if TYPE_CHECKING:
    from ..graph.code_graph import CodeGraph
    from .lsp_occurrence_index import SCIPOccurrenceIndex
    from .scip_pb2 import Index

logger = get_logger("scip_indexer_base")

_SCIP_BLOCK_MARKER = re.compile(r'[{}"]')
_SCIP_QUOTED_END = re.compile(r'[^"\\]*(?:\\[\s\S][^"\\]*)*"')
_SCIP_ESCAPE = re.compile(r"\\([\s\S])")
_SCIP_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def extract_symbol(text: str) -> Optional[str]:
    """Return the unescaped value of the first ``symbol: "..."`` field in a
    decoded SCIP occurrence/symbol_information block.

    Mirrors ``core/scip_decode_base.cpp::extract_symbol`` so Python and C++
    decoders see byte-identical symbol strings. A naive regex
    ``r'symbol:\\s*"([^"]+)"'`` stops at the first ``"`` byte, which cuts a
    SCIP symbol containing a literal ``"`` (emitted by scip-typescript as
    ``\\"`` for JS string-literal object keys like ``"version"``) in half
    and yields a trailing ``\\`` on the captured portion.

    The function reads the literal, honouring protobuf TextFormat escapes
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
        quoted = _SCIP_QUOTED_END.match(text, i + 1)
        if quoted is None:
            return None
        value = text[i + 1 : quoted.end() - 1]
        if "\\" not in value:
            return value
        return _SCIP_ESCAPE.sub(
            lambda match: _SCIP_ESCAPES.get(match[1], match[1]), value
        )
    return None


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
        matched = False
        i = start
        # Skip ordinary text in the regex engine. A quoted literal is one
        # token, so escaped quotes and literal braces never affect depth.
        while marker := _SCIP_BLOCK_MARKER.search(text, i):
            ch = marker[0]
            i = marker.end()
            if ch == '"':
                quoted = _SCIP_QUOTED_END.match(text, i)
                if quoted is None:
                    break
                i = quoted.end()
            elif ch == "{":
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    blocks.append(text[start : marker.start()])
                    pos = i
                    matched = True
                    break
        if not matched:
            break
    return blocks


def index_text(index) -> str:
    """Return ``index`` in protobuf TextFormat with UTF-8 strings.

    This is the ``index.decoded`` format. The upb backend's C encoder behind
    ``str()`` writes the same text as ``MessageToString(index, as_utf8=True)``
    several times faster; other backends' ``str()`` escape non-ASCII bytes, so
    they keep ``MessageToString``.
    """
    from google.protobuf.internal import api_implementation

    if api_implementation.Type() == "upb":
        return str(index)
    from google.protobuf.text_format import MessageToString

    return MessageToString(index, as_utf8=True)


def _bytes_fingerprint(payload: bytes) -> dict[str, object]:
    """``regular_file_fingerprint``'s shape for bytes already in memory."""

    return {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


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
        self.decoded_input_receipt: dict[str, object] | None = None
        self.graph_output_receipt: dict[str, object] | None = None
        # Fingerprints of the binary index and of the decoded text written from
        # it by this run's decode_index; see _occurrences_from_binary_index.
        self._decoded_from: dict[str, dict[str, object]] | None = None
        self._capture_artifact_receipts = False
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

    def _make_decoder(
        self, index_file: str, project_root: Union[str, Path], scip_index=None
    ):
        """Instantiate the decoder selected by ``self.decoder_backend``.

        ``scip_index`` is the parsed index ``index_file`` was written from; a
        serial decoder that accepts it reads it instead of the file.

        Returns an object with ``decode() -> CodeGraph`` and ``save_graph(path)``.
        """
        if self.decoder_backend == "core":
            from .scip_decode_core import SCIPDecoderCore

            return SCIPDecoderCore(
                index_file_path=index_file,
                project_root=str(project_root) if project_root else None,
                language=self.language,
            )
        decoder_class = self._get_decoder_class()
        if scip_index is not None and getattr(
            decoder_class, "accepts_scip_index", False
        ):
            return decoder_class(
                index_file, project_root=project_root, scip_index=scip_index
            )
        return decoder_class(index_file, project_root=project_root)

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

    def _index_environment(self) -> Optional[dict]:
        """Environment for the indexer process; None inherits CodeNib's."""

        return None

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
                env = self._index_environment()

                exec_cmd = list(cmd)
                if exec_cmd and not Path(exec_cmd[0]).is_file():
                    resolved_bin = shutil.which(exec_cmd[0])
                    if resolved_bin:
                        exec_cmd[0] = resolved_bin

                subprocess.run(
                    exec_cmd,
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
        Decode the SCIP index with the packaged protobuf descriptor.

        Returns:
            bool: True if decoding was successful, False otherwise
        """
        if not self.index_file.exists():
            logger.error(f"Index file not found at {self.index_file}")
            return False

        try:
            from google.protobuf.message import DecodeError

            from .scip_pb2 import Index
        except ImportError:
            logger.error("SCIP decoding requires protobuf. Install `codenib[graph]`.")
            return False

        self._decoded_from = None
        try:
            with self.profiler.section("decode_index") as section:
                payload = self.index_file.read_bytes()
                if not payload:
                    raise DecodeError("SCIP index is empty")
                index = Index()
                index.ParseFromString(payload)
                text = index_text(index)
                self.decoded_file.write_text(text, encoding="utf-8")
                from ..compiler.artifact_fingerprints import regular_file_fingerprint
                from .scip_decode_view import text_blocks_match

                self._decoded_from = {
                    "scip": _bytes_fingerprint(payload),
                    "decoded": regular_file_fingerprint(self.decoded_file),
                    # Whether graph decoders may read the index for the text.
                    "blocks_match": text_blocks_match(index, text),
                }
                del text
            duration = section.duration

            logger.info(f"Successfully decoded SCIP index to {self.decoded_file}")
            logger.info(f"⏱️  Index decoding took: {duration:.2f} seconds")
            return True
        except (OSError, DecodeError) as e:
            logger.error(f"Error decoding SCIP index: {e}")
            return False

    def _decoded_scip_index(
        self,
    ) -> tuple[Optional[Index], Optional[dict[str, object]]]:
        """The binary index this run decoded, and the fingerprint of its text.

        ``index.decoded`` was written from these bytes, so the parsed index
        stands for it. Its consumers are bound to that text by the returned
        fingerprint, which ``_process_decoded_index`` checks before and after
        them. Returns ``(None, None)``, and the caller reads the text, when
        this run did not decode the index or the binary changed since.
        """

        source = self._decoded_from
        if source is None:
            return None, None
        try:
            payload = self.index_file.read_bytes()
        except OSError:
            return None, None
        if _bytes_fingerprint(payload) != source["scip"]:
            return None, None
        from .scip_pb2 import Index

        with self.profiler.section("process_index.parse_binary_index"):
            index = Index()
            index.ParseFromString(payload)
        return index, dict(source["decoded"])

    def _graph_scip_index(self, index: Optional[Index]) -> Optional[Index]:
        """``index`` when a graph decoder may read it instead of the text."""

        source = self._decoded_from
        if index is None or source is None or not source.get("blocks_match"):
            return None
        return index

    def _occurrences_from_binary_index(
        self,
        index: Optional[Index] = None,
        fingerprint: Optional[dict[str, object]] = None,
    ) -> tuple[Optional[SCIPOccurrenceIndex], Optional[dict[str, object]]]:
        """Occurrences read from the binary index this run decoded.

        Parsing ``index.scip`` takes a fraction of a second; scanning its
        decoded text for the same occurrences took 8 to 17 s on a 1,000-file
        Python repository, with an identical result. ``index`` and
        ``fingerprint`` come from ``_decoded_scip_index``, which is called when
        they are not given. Returns ``(None, None)`` when there is no index.
        """

        if index is None:
            index, fingerprint = self._decoded_scip_index()
        if index is None:
            return None, None
        from .lsp_occurrence_index import SCIPOccurrenceIndex

        with self.profiler.section("process_index.occurrences_from_binary_index"):
            occurrences = SCIPOccurrenceIndex.from_scip_index(
                index,
                path_filter=(
                    self._path_allowed
                    if self._target_dir or self.exclude_patterns
                    else None
                ),
            )
        return occurrences, fingerprint

    def process_index(
        self, output_file: Optional[str] = None
    ) -> Union[CodeGraph, None]:
        """
        Process the decoded SCIP index into a more usable format.

        The graph and the occurrences are read from the binary index that this
        run decoded when there is one; see ``_decoded_scip_index``.

        Args:
            output_file: Path to write the processed data to

        Returns:
            CodeGraph: Processed graph object
        """
        index, fingerprint = self._decoded_scip_index()
        occurrence_index, fingerprint = self._occurrences_from_binary_index(
            index, fingerprint
        )
        return self._process_decoded_index(
            output_file,
            occurrence_index=occurrence_index,
            decoded_fingerprint=fingerprint,
            scip_index=self._graph_scip_index(index),
        )

    def _process_decoded_index(
        self,
        output_file: Optional[str] = None,
        *,
        occurrence_index: Optional[SCIPOccurrenceIndex] = None,
        decoded_fingerprint: Optional[dict[str, object]] = None,
        scip_index: Optional[Index] = None,
    ) -> Union[CodeGraph, None]:
        """Consume one decoded artifact and optional producer-built positions.

        Prepared positions, and ``scip_index``, the parsed index the artifact
        was written from, require its exact byte fingerprint. Verify that
        fingerprint before either consumer and after both, even when the
        caller does not request receipts. No graph is saved on a mismatch.
        """
        self.decoded_input_receipt = None
        self.graph_output_receipt = None
        if not self.decoded_file.exists():
            logger.error(f"Decoded index file not found at {self.decoded_file}")
            return None

        try:
            if (occurrence_index is None) != (decoded_fingerprint is None):
                raise ValueError("prepared positions require a decoded fingerprint")
            if scip_index is not None and decoded_fingerprint is None:
                raise ValueError("a parsed index requires a decoded fingerprint")
            verify_input = (
                self._capture_artifact_receipts or decoded_fingerprint is not None
            )
            if verify_input:
                from ..compiler.artifact_fingerprints import regular_file_fingerprint

                decoded_path = self.decoded_file.resolve(strict=True)
                decoded_before = regular_file_fingerprint(self.decoded_file)
                if (
                    decoded_fingerprint is not None
                    and decoded_before != decoded_fingerprint
                ):
                    raise RuntimeError(
                        "decoded SCIP input differs from prepared positions"
                    )
            logger.info(
                f"Starting SCIP index processing (backend={self.decoder_backend})..."
            )
            with self.profiler.section("process_index.decode") as section:
                decoder = self._make_decoder(
                    str(self.decoded_file),
                    project_root=self.project_root,
                    scip_index=scip_index,
                )
                graph: CodeGraph = decoder.decode()
            duration = section.duration
            graph = self._filter_project_graph(graph)

            from .lsp_occurrence_index import SCIPOccurrenceIndex

            with self.profiler.section("process_index.build_lsp_occurrence_index"):
                if occurrence_index is None:
                    occurrence_index = SCIPOccurrenceIndex.from_decoded_file(
                        self.decoded_file,
                        path_filter=(
                            self._path_allowed
                            if self._target_dir or self.exclude_patterns
                            else None
                        ),
                    )
            graph.lsp_occurrence_index = occurrence_index
            if verify_input:
                decoded_after = regular_file_fingerprint(self.decoded_file)
                if decoded_after != decoded_before:
                    raise RuntimeError(
                        "decoded SCIP input changed while graph consumers read it"
                    )
            if self._capture_artifact_receipts:
                self.decoded_input_receipt = {
                    "path": str(decoded_path),
                    **decoded_before,
                }

            # Build line-range indexes once the graph is fully assembled.
            # Must happen before save_graph so the indexes are persisted.
            with self.profiler.section("process_index.build_range_indexes"):
                graph.build_range_indexes()

            if output_file:
                with self.profiler.section("process_index.save_graph") as save_section:
                    output_path = Path(output_file)
                    graph.save_graph(str(output_path))
                    if self._capture_artifact_receipts:
                        from .query_surface import query_surface_sha256

                        graph_fingerprint = regular_file_fingerprint(output_path)
                        self.graph_output_receipt = {
                            "path": str(output_path.resolve(strict=True)),
                            **graph_fingerprint,
                            "query_surface_sha256": query_surface_sha256(graph),
                        }
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
            self.decoded_input_receipt = None
            self.graph_output_receipt = None
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
        self.decoded_input_receipt = None
        self.graph_output_receipt = None
        capture_artifact_receipts = kwargs.pop("capture_artifact_receipts", False)
        if type(capture_artifact_receipts) is not bool:
            raise ValueError("capture_artifact_receipts must be a boolean")
        self._capture_artifact_receipts = capture_artifact_receipts
        if output_file is None:
            output_file = str(self.graph_file)
        self._target_dir = normalize_scip_path(kwargs.pop("target_dir", None))

        # Check graph cache if skip_level is 'graph'
        if skip_level == "graph" and self.graph_file.exists():
            logger.info(f"Loading cached graph from {self.graph_file}")
            try:
                from ..graph.code_graph import CodeGraph

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
            self._capture_artifact_receipts = False
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

        # Thousands of symbols share each file path, and every check
        # re-normalizes all exclude patterns: remember each answer for this pass.
        answers: dict[tuple[str, bool], bool] = {}

        def path_allowed(path: str, allow_target_ancestor: bool = False) -> bool:
            key = (path, allow_target_ancestor)
            answer = answers.get(key)
            if answer is None:
                answer = answers[key] = self._path_allowed(
                    path, allow_target_ancestor=allow_target_ancestor
                )
            return answer

        keep = [
            vertex.index
            for vertex in graph.graph.vs
            if self._keep_vertex(vertex.attributes(), path_allowed)
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

    def _keep_vertex(
        self,
        attrs: dict,
        path_allowed: Optional[Callable[..., bool]] = None,
    ) -> bool:
        allowed = path_allowed or self._path_allowed
        node_type = attrs.get("type")
        name = attrs.get("name")
        if node_type == "root" or name == ROOT_NODE:
            return True
        if node_type in {NODE_TYPE_DIRECTORY, NODE_TYPE_FILE}:
            return allowed(str(name), allow_target_ancestor=True)
        if is_symbol_node(node_type):
            path = definition_path(attrs) or attrs.get("file")
            return bool(path and allowed(str(path)))
        return True

    def _path_allowed(self, path: str, *, allow_target_ancestor: bool = False) -> bool:
        return scip_path_is_allowed(
            path,
            target_dir=self._target_dir,
            exclude_patterns=self.exclude_patterns,
            allow_target_ancestor=allow_target_ancestor,
        )
