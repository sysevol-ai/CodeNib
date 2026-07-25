# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional

from rank_bm25 import BM25Okapi

from ...code_chunker import CodeChunk
from ...log_utils import get_logger
from ...types import NODE_TYPE_DIRECTORY, NODE_TYPE_FILE, NodeInfo, is_symbol_node
from ...utils import is_test_file, wrap_code_snippet

if TYPE_CHECKING:
    from ...graph.code_graph import CodeGraph

logger = get_logger(__name__)


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
    ):
        """
        Initialize the BM25CodeIndexer and optionally build the index immediately.

        Args:
            code_graph: CodeGraph instance containing nodes to index. If provided,
                       the index will be built immediately.
            code_chunker: CodeChunker instance containing chunks to index. If provided,
                         the index will be built immediately.
            chunks: List of CodeChunk objects to index. If provided, the index will
                   be built immediately.
            max_k: Maximum number of results to return in searches
            language: Language for stopword removal
                      Default is "english" which works well for processing code tokens
                      as it treats special characters as separators
        """
        self.max_k = max_k
        self.language = language
        self.documents = []
        self.retriever = None
        self.code_graph: CodeGraph = None
        self.project_root: Optional[str] = None
        self.nodes: List[str] = []

        # Build the index immediately if a code_graph is provided
        if code_graph is not None:
            self.build_index_from_graph(code_graph)
        elif chunks is not None:
            self.build_index_from_chunks(chunks)

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

    def build_index_from_chunks(self, chunks: List[CodeChunk]) -> BM25Retriever:
        """
        Build a BM25 index from a list of CodeChunk objects.

        Args:
            chunks: List of CodeChunk objects (with node_id, chunk_type, name, file, etc.)

        Returns:
            BM25Retriever instance
        """
        # Reset the index
        self.documents = []
        self.nodes = []
        self.code_graph = None
        self.project_root = None

        # Extract unique node IDs from chunks
        unique_node_ids = set()
        for chunk in chunks:
            if hasattr(chunk, "node_id") and chunk.node_id:
                unique_node_ids.add(chunk.node_id)

        # Convert unique node IDs to documents
        for node_id in unique_node_ids:
            doc = self._convert_node_id_to_document(node_id)
            if doc is not None:
                self.documents.append(doc)
                self.nodes.append(node_id)

        # Create BM25Retriever with LangChain format
        self.retriever = BM25Retriever.from_documents(self.documents, k=self.max_k)

        return self.retriever

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

            # Split on code-specific delimiters: '/', ':', '.', '_'
            # Handles paths ("test/test_bas.py") and qualified method names
            # like "sample/core.py:get_hmm".
            tokens = re.split(r"[/:._ ]+", text)

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
                # Construct full file path using project_root if available
                full_file_path = file_path
                if self.project_root and not os.path.isabs(file_path):
                    full_file_path = os.path.join(self.project_root, file_path)

                try:
                    with open(
                        full_file_path, "r", encoding="utf-8", errors="replace"
                    ) as f:
                        if node_type == NODE_TYPE_FILE:
                            # For file nodes, return entire file content
                            code_content = f.read()
                            if wrap_with_ln:
                                lines = code_content.split("\n")
                                content = wrap_code_snippet(code_content, 1, len(lines))
                            else:
                                content = code_content
                        else:
                            # For symbol nodes, extract specific lines
                            if start_line is not None and end_line is not None:
                                lines = f.readlines()
                                # start_line and end_line are already 0-based and inclusive
                                start_idx = max(0, start_line)
                                end_idx = min(
                                    len(lines), end_line + 1
                                )  # +1 for slice end exclusivity

                                extracted_lines = lines[start_idx:end_idx]

                                # Strip trailing blank lines to avoid extra whitespace.
                                original_end_idx = len(extracted_lines)
                                while (
                                    extracted_lines
                                    and extracted_lines[-1].strip() == ""
                                ):
                                    extracted_lines.pop()

                                code_content = "".join(extracted_lines)

                                if wrap_with_ln:
                                    # Calculate the actual end line after removing empty lines
                                    lines_removed = original_end_idx - len(
                                        extracted_lines
                                    )
                                    actual_end_line = end_line - lines_removed
                                    # Convert to 1-based for display purposes
                                    content = wrap_code_snippet(
                                        code_content,
                                        start_line + 1,
                                        actual_end_line + 1,
                                    )
                                else:
                                    content = code_content
                except (IOError, UnicodeDecodeError):
                    # If file reading fails, content remains None
                    pass

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
        for doc_data in documents_data:
            doc = Document(
                page_content=doc_data["page_content"], metadata=doc_data["metadata"]
            )
            self.documents.append(doc)
            node_name = doc.metadata.get("node_id") or doc.metadata.get("name")
            if node_name:
                self.nodes.append(node_name)

        # Recreate BM25Retriever
        self.retriever = BM25Retriever.from_documents(self.documents, k=self.max_k)

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
