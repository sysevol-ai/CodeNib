# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional

from rank_bm25 import BM25Okapi

from ..._contained_source import read_repository_file
from ...code_chunker import CodeChunk
from ...log_utils import get_logger
from ...types import NODE_TYPE_DIRECTORY, NODE_TYPE_FILE, NodeInfo, is_symbol_node
from ...utils import is_test_file, wrap_code_snippet

if TYPE_CHECKING:
    from ...graph.code_graph import CodeGraph

logger = get_logger(__name__)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_MAX_RUNTIME_SOURCE_BYTES = 64 * 1024 * 1024
_SECURE_SOURCE_DIRECTORY_FDS = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_NONBLOCK")
    and os.open in getattr(os, "supports_dir_fd", ())
)


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    """Return whether metadata represents a link-like filesystem entry."""
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _path_components(path: str) -> Optional[tuple[str, ...]]:
    """Split a native path without normalizing away parent traversal."""
    normalized = path
    if os.altsep:
        normalized = normalized.replace(os.altsep, os.sep)
    _drive, tail = os.path.splitdrive(normalized)
    parts = tuple(part for part in tail.split(os.sep) if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        return None
    return parts


def _contained_source_path(
    project_root: object,
    file_path: object,
) -> Optional[tuple[str, str, tuple[str, ...]]]:
    """Resolve a lexical source path beneath a canonical repository root."""
    try:
        root_value = os.fspath(project_root)
        source_value = os.fspath(file_path)
    except TypeError:
        return None
    if (
        not isinstance(root_value, str)
        or not isinstance(source_value, str)
        or not source_value
        or "\x00" in root_value
        or "\x00" in source_value
        or _path_components(source_value) is None
    ):
        return None

    try:
        # The configured root is trusted and may itself be reached through a
        # stable developer-facing alias. Canonicalize that anchor once, but do
        # not resolve source components: links below the root must be rejected.
        root = os.path.realpath(os.path.abspath(root_value))
        if os.path.isabs(source_value):
            candidate = os.path.abspath(source_value)
        else:
            drive, _tail = os.path.splitdrive(source_value)
            if drive:
                # ``C:relative.py`` is drive-relative on Windows and therefore
                # cannot be safely anchored beneath the configured root.
                return None
            candidate = os.path.abspath(os.path.join(root, source_value))

        common = os.path.commonpath((root, candidate))
        if os.path.normcase(common) != os.path.normcase(root):
            return None
        relative = os.path.relpath(candidate, root)
        parts = _path_components(relative)
        if parts is None:
            return None
        return root, os.path.join(root, *parts), parts
    except (OSError, ValueError):
        # Different Windows drives, malformed paths, and unavailable roots are
        # all unreadable through the contained source boundary.
        return None


def _regular_source_flags(*, nofollow: bool) -> int:
    flags = os.O_RDONLY
    for name in ("O_NONBLOCK", "O_CLOEXEC", "O_BINARY"):
        flags |= getattr(os, name, 0)
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _open_regular_file_beneath(root: str, parts: tuple[str, ...]) -> Optional[int]:
    """Open one regular file through descriptor-relative POSIX traversal."""
    directory_fd: Optional[int] = None
    source_fd: Optional[int] = None
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        directory_fd = os.open(root, directory_flags)
        root_metadata = os.fstat(directory_fd)
        if _is_link_or_reparse(root_metadata) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            return None
        for part in parts[:-1]:
            child_fd: Optional[int] = None
            try:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                child_metadata = os.fstat(child_fd)
                if _is_link_or_reparse(child_metadata) or not stat.S_ISDIR(
                    child_metadata.st_mode
                ):
                    return None
                previous_fd = directory_fd
                directory_fd = child_fd
                child_fd = None
                os.close(previous_fd)
            finally:
                if child_fd is not None:
                    os.close(child_fd)

        source_fd = os.open(
            parts[-1],
            _regular_source_flags(nofollow=True),
            dir_fd=directory_fd,
        )
        opened = os.fstat(source_fd)
        if _is_link_or_reparse(opened) or not stat.S_ISREG(opened.st_mode):
            return None
        descriptor = source_fd
        source_fd = None
        return descriptor
    except (NotImplementedError, OSError, ValueError):
        return None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _static_regular_source(
    root: str,
    parts: tuple[str, ...],
) -> Optional[tuple[str, os.stat_result, tuple[tuple[int, int, int], ...]]]:
    """Validate a contained, link-free path on platforms without safe dir FDs."""
    try:
        metadata = os.lstat(root)
        identity = _file_identity(metadata)
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or identity is None
        ):
            return None
        identities = [identity]
        current = root
        for part in parts[:-1]:
            current = os.path.join(current, part)
            metadata = os.lstat(current)
            identity = _file_identity(metadata)
            if (
                _is_link_or_reparse(metadata)
                or not stat.S_ISDIR(metadata.st_mode)
                or identity is None
            ):
                return None
            identities.append(identity)
        source = os.path.join(current, parts[-1])
        metadata = os.lstat(source)
        identity = _file_identity(metadata)
        if (
            _is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or identity is None
        ):
            return None
        identities.append(identity)
        return source, metadata, tuple(identities)
    except (OSError, ValueError):
        return None


def _file_identity(metadata: os.stat_result) -> Optional[tuple[int, int, int]]:
    """Return a usable path identity, or ``None`` for inode-less metadata."""
    device = getattr(metadata, "st_dev", None)
    inode = getattr(metadata, "st_ino", None)
    if (
        not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode <= 0
    ):
        return None
    return device, inode, stat.S_IFMT(metadata.st_mode)


def _read_open_descriptor(descriptor: int) -> Optional[str]:
    try:
        with os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
            errors="replace",
        ) as source:
            descriptor = -1
            return source.read()
    except (OSError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_static_contained_source(
    root: str,
    parts: tuple[str, ...],
) -> Optional[str]:
    """Read after a portable component check and bind the opened file identity."""
    validated = _static_regular_source(root, parts)
    if validated is None:
        return None
    source_path, expected, expected_path_identities = validated
    expected_identity = _file_identity(expected)
    if expected_identity is None:
        return None

    descriptor: Optional[int] = None
    try:
        descriptor = os.open(source_path, _regular_source_flags(nofollow=True))
        opened = os.fstat(descriptor)
        opened_identity = _file_identity(opened)
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened_identity is None
            or opened_identity != expected_identity
        ):
            return None
        owned_descriptor = descriptor
        descriptor = None
        content = _read_open_descriptor(owned_descriptor)
    except (NotImplementedError, OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)

    # Windows and other fallback platforms cannot make every component lookup
    # descriptor-relative. Recheck the static path after reading so a final or
    # intermediate link swap fails closed instead of returning source bytes.
    current = _static_regular_source(root, parts)
    if content is None or current is None or current[2] != expected_path_identities:
        return None
    return content


def _read_legacy_source(file_path: object) -> Optional[str]:
    """Preserve direct-path reads for old indexes that have no project root.

    Without a trusted root there is no meaningful containment boundary. Older
    graph artifacts commonly store absolute source paths, so keep following a
    final link in this compatibility branch while still refusing non-files and
    using non-blocking flags where the platform provides them.
    """
    try:
        source_path = os.fspath(file_path)
    except TypeError:
        return None
    if not isinstance(source_path, str) or not source_path or "\x00" in source_path:
        return None

    descriptor: Optional[int] = None
    try:
        descriptor = os.open(source_path, _regular_source_flags(nofollow=False))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return None
        owned_descriptor = descriptor
        descriptor = None
        content = _read_open_descriptor(owned_descriptor)
        return content
    except (NotImplementedError, OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_source_text(project_root: object, file_path: object) -> Optional[str]:
    """Read source through containment when a repository root is available."""
    if project_root is None:
        return _read_legacy_source(file_path)

    contained = _contained_source_path(project_root, file_path)
    if contained is None:
        return None
    root, _source_path, parts = contained
    if _SECURE_SOURCE_DIRECTORY_FDS:
        try:
            payload = read_repository_file(
                root,
                "/".join(parts),
                max_bytes=_MAX_RUNTIME_SOURCE_BYTES,
            )
        except ValueError:
            return None
        return (
            payload.decode("utf-8", errors="replace")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
    return _read_static_contained_source(root, parts)


def _split_source_lines(source_text: str) -> List[str]:
    """Split only on normalized LF while retaining TextIO ``readlines`` shape."""
    if not source_text:
        return []
    segments = source_text.split("\n")
    lines = [segment + "\n" for segment in segments[:-1]]
    if segments[-1]:
        lines.append(segments[-1])
    return lines


@dataclass(slots=True)
class Document:
    """Minimal document shape shared with existing retrieval consumers."""

    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class BM25Retriever:
    """Small ``invoke``-compatible wrapper around :class:`BM25Okapi`."""

    def __init__(self, documents: List[Document], *, k: int) -> None:
        self.documents = list(documents)
        self.k = k
        tokenized = [document.page_content.split() for document in self.documents]
        self._index = BM25Okapi(tokenized) if tokenized else None

    @classmethod
    def from_documents(
        cls,
        documents: List[Document],
        *,
        k: int,
    ) -> BM25Retriever:
        return cls(documents, k=k)

    def invoke(self, query: str) -> List[Document]:
        if self._index is None:
            return []
        count = min(self.k, len(self.documents))
        return list(
            self._index.get_top_n(
                query.split(),
                self.documents,
                n=count,
            )
        )


class BM25CodeIndexer:
    """
    A class that builds a BM25 index from CodeGraph nodes and provides
    search functionality with stemming support.
    """

    def __init__(
        self,
        code_graph=None,
        chunks=None,
        max_k: int = 15,
        language: str = "english",
        project_root: Optional[str] = None,
    ):
        """
        Initialize the BM25CodeIndexer and optionally build the index immediately.

        Args:
            code_graph: CodeGraph instance containing nodes to index. If provided,
                       the index will be built immediately.
            chunks: List of CodeChunk objects to index. If provided, the index will
                   be built immediately.
            max_k: Maximum number of results to return in searches
            language: Language for stopword removal
                      Default is "english" which works well for processing code tokens
                      as it treats special characters as separators
            project_root: Repository root used to resolve relative source paths
        """
        self.max_k = max_k
        self.language = language
        self.documents = []
        self.retriever = None
        self.code_graph: CodeGraph = None
        self.project_root = project_root
        self.nodes: List[str] = []

        # Build the index immediately if a code_graph is provided
        if code_graph is not None:
            self.build_index_from_graph(code_graph)
        elif chunks is not None:
            self.build_index_from_chunks(chunks, project_root=project_root)

    def build_index_from_graph(self, code_graph: CodeGraph) -> BM25Retriever:
        """
        Build a BM25 index from a CodeGraph.

        Args:
            code_graph: CodeGraph instance containing nodes to index
        """
        # Reset the index
        self.documents = []
        self.nodes = []
        self.code_graph = code_graph
        self.project_root = code_graph.project_root

        # Convert graph nodes to documents
        for vertex in code_graph.graph.vs:
            doc = self._convert_vertex_to_document(vertex)
            if doc is not None:
                self.documents.append(doc)
                node_name = doc.metadata.get("node_id") or doc.metadata.get("name")
                if node_name:
                    self.nodes.append(node_name)

        # Create BM25Retriever with LangChain format
        self.retriever = BM25Retriever.from_documents(self.documents, k=self.max_k)

        return self.retriever

    def build_index_from_chunks(
        self,
        chunks: List[CodeChunk],
        *,
        project_root: Optional[str] = None,
    ) -> BM25Retriever:
        """
        Build a BM25 index from a list of CodeChunk objects.

        Args:
            chunks: List of CodeChunk objects (with node_id, chunk_type, name, file, etc.)
            project_root: Repository root used to resolve relative source paths

        Returns:
            BM25Retriever instance
        """
        # Reset the index
        self.documents = []
        self.nodes = []
        self.code_graph = None
        self.project_root = project_root

        # Keep each bounded source span independently searchable. Overloads and
        # split large definitions can share a node_id, but merging them widens
        # the returned location and defeats the chunk-size contract.
        documents_by_span: dict[tuple[str, int, int], Document] = {}
        for chunk in chunks:
            node_id = getattr(chunk, "node_id", None)
            if not node_id:
                continue
            doc = self._convert_chunk_to_document(chunk)
            if doc is not None:
                key = (
                    node_id,
                    int(doc.metadata["start_line"]),
                    int(doc.metadata["end_line"]),
                )
                documents_by_span.setdefault(key, doc)

        self.documents = list(documents_by_span.values())
        self.nodes = list(dict.fromkeys(key[0] for key in documents_by_span))

        # Create BM25Retriever with LangChain format
        self.retriever = BM25Retriever.from_documents(self.documents, k=self.max_k)

        return self.retriever

    def _convert_chunk_to_document(self, chunk: CodeChunk) -> Optional[Document]:
        """Convert a source chunk while preserving its repository location."""
        doc = self._convert_node_id_to_document(chunk.node_id)
        if doc is None:
            return None
        doc.metadata.update(
            {
                "chunk_type": chunk.chunk_type,
                "type": chunk.chunk_type,
                "name": chunk.name,
                "start_line": int(chunk.start_line),
                "end_line": int(chunk.end_line),
            }
        )
        # Sparse retrieval must search implementation text, not only the
        # ``file:symbol`` identity. Keep the identity in-band so exact symbol
        # queries remain strong while natural-language questions can match
        # docstrings, literals, calls, and control-flow vocabulary.
        doc.page_content = self._apply_stemming(
            f"{chunk.node_id} {chunk.content or ''}"
        )
        return doc

    def _convert_node_id_to_document(self, node_id: str):
        """
        Convert a node ID from CodeChunker format to a Document for indexing.

        Args:
            node_id: Node ID in format "file.py:SymbolName" or "dir/file.py:SymbolName()"

        Returns:
            Document object or None if the node couldn't be converted
        """
        metadata = {"node_id": node_id}

        if ":" not in node_id:
            # Interpret as a file or directory node
            _, ext = os.path.splitext(node_id)
            if ext:
                node_type = NODE_TYPE_FILE
                metadata["file"] = node_id
            else:
                node_type = NODE_TYPE_DIRECTORY
            metadata["type"] = node_type
            metadata["name"] = node_id
            content = node_id
        else:
            file_path, symbol_name = node_id.split(":", 1)

            # Determine node type based on symbol name
            if "()" in symbol_name:
                node_type = "function"
            else:
                node_type = "class"

            metadata.update(
                {
                    "type": node_type,
                    "name": symbol_name,
                    "file": file_path,
                }
            )

            # Use node_id as content for searching
            content = node_id

        content = self._apply_stemming(content)

        # Create a unique ID for the document
        doc_id = f"node_{node_id}"
        metadata["doc_id"] = doc_id

        return Document(page_content=content, metadata=metadata)

    def _convert_vertex_to_document(self, vertex):
        """
        Convert a graph vertex to a Document for indexing.

        Args:
            vertex: Graph vertex to convert
            code_graph: CodeGraph instance

        Returns:
            Document object or None if the vertex couldn't be converted
        """
        node_id = vertex.index
        node_type = vertex["type"] if "type" in vertex.attributes() else "unknown"
        node_name = vertex["name"]

        metadata = {
            "node_id": node_name,
            "type": node_type,
            "name": node_name,
        }

        if node_type == NODE_TYPE_FILE:
            # File node: add file path under "file" to align with graph/search consumers
            metadata["file"] = node_name
            content = node_name
        elif node_type == NODE_TYPE_DIRECTORY:
            # Directory node
            content = node_name
        elif is_symbol_node(node_type):
            # Symbol-like nodes: class/function/method/field/symbol
            file_path = vertex["file"] if "file" in vertex.attributes() else None
            start_line = (
                vertex["start_line"]
                if "start_line" in vertex.attributes()
                and vertex["start_line"] is not None
                else 0
            )
            end_line = (
                vertex["end_line"]
                if "end_line" in vertex.attributes() and vertex["end_line"] is not None
                else 0
            )

            if file_path:
                metadata["file"] = file_path
            # remove () if present at the end of function/method names for better matching
            metadata["name"] = node_name
            metadata["start_line"] = start_line
            metadata["end_line"] = end_line

            # Prefer `unified_name` for the indexed text. Raw `name` is
            # readable for Python but semi-raw SCIP for rust/ts/go and a USR
            # for clangd, which silently degrades BM25 recall on those
            # languages. `unified_name` is always `file_path:SymbolDisplay`
            # across decoders and tokenizes cleanly via `_apply_stemming`.
            # Identity (`node_id`, `name` metadata, returned `node_name`)
            # stays raw so the downstream `name_to_vertex` invariant used by
            # ROISubgraph / graph_expand is unchanged.
            unified = (
                vertex["unified_name"]
                if "unified_name" in vertex.attributes()
                else None
            )
            content = unified if unified else node_name
        else:
            # Unknown or root-like node types: index minimally
            content = node_name

        # Include additional attributes (except ones we already standardized)
        for key in vertex.attributes():
            if key not in [
                "name",
                "type",
                "file",
                "start_line",
                "end_line",
            ]:
                metadata[key] = vertex[key]

        # Apply custom text processing for code-specific tokenization
        content = self._apply_stemming(content)

        # Create a unique ID for the document
        doc_id = f"node_{node_id}"
        metadata["doc_id"] = doc_id
        return Document(page_content=content, metadata=metadata)

    def _apply_stemming(self, text: str) -> str:
        """
        Apply custom text processing for code-specific tokenization.
        Handles code-specific patterns like file paths, function calls, etc.

        Args:
            text: Input text to process

        Returns:
            Processed text with code-aware tokenization
        """
        if not text:
            return text

        try:
            # Remove parentheses from function calls first (e.g., "get_hmm()" -> "get_hmm")
            text = re.sub(r"\(\)", "", text)

            # Split on every non-alphanumeric delimiter. This covers paths,
            # qualified identifiers, punctuation, string literals, and source
            # newlines while preserving stable lowercase code tokens.
            tokens = re.split(r"[^A-Za-z0-9]+", text)

            processed_tokens = []
            for token in tokens:
                if token:
                    # Lowercase all tokens for better matching
                    processed_tokens.append(token.lower())

            # Join with spaces to create searchable terms
            return " ".join(filter(None, processed_tokens))
        except Exception as e:
            logger.warning(
                f"Error during text processing: {e}. Returning original text."
            )
            return text

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        return_code_content: bool = False,
        wrap_with_ln: bool = True,
        filter_test: bool = False,
    ) -> List[NodeInfo]:
        """
        Search the index for nodes matching the query.

        Args:
            query: Search query
            top_k: Number of top results to return (defaults to max_k if not specified)
            return_code_content: Whether to include code content in the results
            wrap_with_ln: Whether to wrap code content with line numbers
            filter_test: Whether to filter out test files from the results

        Returns:
            List of NodeInfo objects containing matched nodes with optional content
        """
        if self.retriever is None:
            raise ValueError(
                "Index has not been built. Call build_index_from_graph first."
            )

        # Apply custom text processing to query
        query = self._apply_stemming(query)

        if top_k is None:
            top_k = self.max_k

        logger.debug(f"BM25 search with k={top_k}, num_documents={len(self.documents)}")

        # Retrieve results and truncate to requested top_k
        results = self.retriever.invoke(query)
        logger.info(f"BM25 retrieval returned {len(results)} results")

        # Convert results to NodeInfo objects and apply filtering
        processed_results = []
        for doc in results:
            # Extract all metadata directly from the document
            metadata = doc.metadata
            node_name = metadata.get("node_id", "")

            # Filter out test files if requested
            if filter_test and is_test_file(node_name):
                continue

            # Get basic node info
            file_path = metadata.get("file")
            start_line = metadata.get("start_line")
            end_line = metadata.get("end_line")
            node_type = metadata.get("type", "unknown")

            # Handle code content if requested
            content = None
            if return_code_content and file_path:
                source_text = _read_source_text(self.project_root, file_path)
                if source_text is not None:
                    if node_type == NODE_TYPE_FILE:
                        # For file nodes, return entire file content.
                        code_content = source_text
                        if wrap_with_ln:
                            lines = code_content.split("\n")
                            content = wrap_code_snippet(code_content, 1, len(lines))
                        else:
                            content = code_content
                    elif start_line is not None and end_line is not None:
                        # Symbol ranges remain 0-based and inclusive.
                        lines = _split_source_lines(source_text)
                        start_idx = max(0, start_line)
                        end_idx = min(
                            len(lines), end_line + 1
                        )  # +1 for slice end exclusivity
                        extracted_lines = lines[start_idx:end_idx]

                        # Strip trailing blank lines to avoid extra whitespace.
                        original_end_idx = len(extracted_lines)
                        while extracted_lines and extracted_lines[-1].strip() == "":
                            extracted_lines.pop()

                        code_content = "".join(extracted_lines)
                        if wrap_with_ln:
                            # Calculate the actual end line after removing empty lines.
                            lines_removed = original_end_idx - len(extracted_lines)
                            actual_end_line = end_line - lines_removed
                            content = wrap_code_snippet(
                                code_content,
                                start_line + 1,
                                actual_end_line + 1,
                            )
                        else:
                            content = code_content

            # Create NodeInfo object (LangChain BM25Retriever doesn't provide scores)
            result = NodeInfo(
                score=0.0,  # LangChain BM25Retriever doesn't provide scores
                node_name=node_name,
                node_id=node_name,  # Set node_id to the same value as node_name
                type=node_type,
                file=file_path,
                start_line=start_line,
                end_line=end_line,
                content=content,
            )

            processed_results.append(result)

            # Stop once we have enough results
            if len(processed_results) >= top_k:
                break

        return processed_results

    def search_identifier_occurrences(
        self,
        identifier: str,
        *,
        context_query: Optional[str] = None,
        top_k: int = 20,
        wrap_with_ln: bool = True,
        filter_test: bool = False,
    ) -> List[NodeInfo]:
        """Find source chunks that reference an exact code identifier.

        BM25 ranks lexical relevance, but it does not guarantee exhaustive
        exact-symbol coverage. This complementary path scans persisted chunk
        ranges so a symbol query can return callers/usages without requiring a
        graph index.
        """
        symbol = str(identifier or "").strip().removesuffix("()")
        symbol = symbol.rsplit(".", 1)[-1]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
            raise ValueError("identifier must be a single code identifier")
        if top_k <= 0:
            return []

        occurrence = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])"
        )
        invocation = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}\s*\(")
        context_terms = set(self._apply_stemming(context_query or "").split())
        context_terms -= set(self._apply_stemming(symbol).split())
        context_terms -= {
            "call",
            "caller",
            "calls",
            "current",
            "site",
            "sites",
            "usage",
            "use",
            "used",
            "where",
        }
        files: dict[str, List[str]] = {}
        matches: List[tuple[int, int, int, NodeInfo]] = []

        for position, doc in enumerate(self.documents):
            metadata = doc.metadata
            file_path = metadata.get("file")
            start_line = metadata.get("start_line")
            end_line = metadata.get("end_line")
            if (
                not file_path
                or not isinstance(start_line, int)
                or not isinstance(end_line, int)
            ):
                continue
            node_name = str(metadata.get("node_id") or metadata.get("name") or "")
            if filter_test and is_test_file(node_name or file_path):
                continue

            declared_name = str(metadata.get("name") or "").removesuffix("()")
            if declared_name.rsplit(".", 1)[-1] == symbol:
                continue

            try:
                source_key = os.fspath(file_path)
            except TypeError:
                continue
            if not isinstance(source_key, str):
                continue
            if source_key not in files:
                source_text = _read_source_text(self.project_root, source_key)
                files[source_key] = (
                    _split_source_lines(source_text) if source_text is not None else []
                )
            lines = files[source_key]
            if not lines:
                continue

            start_idx = max(0, start_line)
            end_idx = min(len(lines), end_line + 1)
            selected = lines[start_idx:end_idx]
            code_content = "".join(selected)
            if not occurrence.search(code_content):
                continue

            call_count = len(invocation.findall(code_content))
            searchable = self._apply_stemming(f"{node_name} {file_path} {code_content}")
            context_score = sum(
                searchable.count(term) for term in context_terms if len(term) >= 3
            )
            content = (
                wrap_code_snippet(
                    code_content,
                    start_idx + 1,
                    start_idx + len(selected),
                )
                if wrap_with_ln
                else code_content
            )
            matches.append(
                (
                    -context_score,
                    -call_count,
                    position,
                    NodeInfo(
                        score=float(call_count or 1),
                        node_name=node_name,
                        node_id=node_name,
                        type=metadata.get("type", "unknown"),
                        file=file_path,
                        start_line=start_line,
                        end_line=end_line,
                        content=content,
                    ),
                )
            )

        matches.sort(key=lambda item: (item[0], item[1], item[2]))
        return [node for _, _, _, node in matches[:top_k]]

    def save_index(self, directory_path: str):
        """
        Save the index to a directory.

        Args:
            directory_path: Path to save the index to
        """
        if self.retriever is None:
            raise ValueError(
                "Index has not been built. Call build_index_from_graph first."
            )

        # Create directory if it doesn't exist
        os.makedirs(directory_path, exist_ok=True)

        # Save documents as JSON since LangChain BM25Retriever doesn't have persist method
        documents_data = []
        for doc in self.documents:
            documents_data.append(
                {"page_content": doc.page_content, "metadata": doc.metadata}
            )

        documents_file = os.path.join(directory_path, "documents.json")
        with open(documents_file, "w", encoding="utf-8") as f:
            json.dump(documents_data, f, indent=2)

        # Save additional metadata including project_root
        metadata = {
            "project_root": (
                str(self.project_root) if self.project_root is not None else None
            ),
            "max_k": self.max_k,
            "language": self.language,
        }
        metadata_file = os.path.join(directory_path, "bm25_metadata.json")
        with open(metadata_file, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

    def load_index(self, directory_path: str):
        """
        Load the index from a directory.

        Args:
            directory_path: Path to load the index from
        """
        if not os.path.exists(directory_path):
            raise ValueError(f"Directory {directory_path} does not exist.")

        # Load documents from JSON
        documents_file = os.path.join(directory_path, "documents.json")
        if not os.path.exists(documents_file):
            raise ValueError(f"Documents file not found: {documents_file}")

        with open(documents_file, "r", encoding="utf-8") as f:
            documents_data = json.load(f)

        # Reconstruct Document objects
        self.documents = []
        self.nodes = []
        seen_nodes = set()
        for doc_data in documents_data:
            doc = Document(
                page_content=doc_data["page_content"], metadata=doc_data["metadata"]
            )
            self.documents.append(doc)
            node_name = doc.metadata.get("node_id") or doc.metadata.get("name")
            if node_name and node_name not in seen_nodes:
                self.nodes.append(node_name)
                seen_nodes.add(node_name)

        # Load additional metadata including project_root
        metadata_file = os.path.join(directory_path, "bm25_metadata.json")
        if os.path.exists(metadata_file):
            with open(metadata_file, "r", encoding="utf-8") as f:
                metadata = json.load(f)
                self.project_root = metadata.get("project_root")
                self.max_k = metadata.get("max_k", 10)
                self.language = metadata.get("language", "english")
        else:
            # For backward compatibility with indices saved without metadata
            self.project_root = None

        # Recreate only after restoring ``max_k``. Reversing this order silently
        # capped loaded artifacts at the constructor default (15), even when
        # the persisted compiler contract requested 128 candidates.
        self.retriever = BM25Retriever.from_documents(self.documents, k=self.max_k)
