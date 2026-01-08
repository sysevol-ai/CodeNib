"""
Vector Store implementation using FAISS and LangChain for code embeddings.
This module provides functionality to create, store, and query vector embeddings
of code chunks for semantic similarity search.
"""

import json
import pickle
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set

import faiss
from langchain_community.docstore import InMemoryDocstore
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import OpenAIEmbeddings

from ...log_utils import get_logger
from ...profiler import Profiler
from ...types import NodeInfo

logger = get_logger(__name__)

Level = Literal["l0", "l2"]


class CodeVectorStore:
    """
    Vector store for code embeddings using FAISS and LangChain.
    Provides semantic search capabilities over code chunks.

    Supports hierarchical indexing:
    - L0: File-level skeletons
    - L2: Function/method-level chunks for fine-grained retrieval (default)
    """

    def __init__(
        self,
        embedding_model: str = "text-embedding-ada-002",
        embedding_provider: str = "openai",
        dimension: int = 1536,
        index_type: str = "flat",
        index_metric: str = "ip",
        store_path: Optional[str] = None,
        profiler: Optional[Profiler] = None,
        **embedding_kwargs,
    ):
        """
        Initialize the CodeVectorStore.

        Args:
            embedding_model: Name of the embedding model to use
            embedding_provider: Provider for embeddings ("openai", "huggingface")
            dimension: Dimension of the embedding vectors
            index_type: Type of FAISS index (only "flat" is supported)
            index_metric: Distance metric ("ip" for inner product, "l2" for L2 distance)
            store_path: Path to store/load the vector store
            profiler: Optional profiler instance to capture detailed timings
            **embedding_kwargs: Additional arguments for embedding model
        """
        self.embedding_model = embedding_model
        self.embedding_provider = embedding_provider
        self.dimension = dimension
        self.index_type = index_type.lower()
        if self.index_type != "flat":
            raise ValueError(
                f"Unsupported index_type: {index_type}. Only 'flat' is supported."
            )
        self.index_metric = index_metric.lower()
        if self.index_metric not in ["ip", "l2"]:
            raise ValueError(
                f"Unsupported index_metric: {index_metric}. Must be 'ip' or 'l2'."
            )
        self.store_path = Path(store_path) if store_path else None
        self.profiler = profiler

        # Initialize embedding model
        self.embedding = self._initialize_embedding_model(**embedding_kwargs)
        self.dimension = self._infer_embedding_dimension(dimension)

        # Initialize L0 vector store (file-level skeletons)
        self.l0_index = self._build_faiss_index()
        self.l0_vector_store = FAISS(
            embedding_function=self.embedding,
            index=self.l0_index,
            docstore=InMemoryDocstore(),
            index_to_docstore_id={},
        )
        self.l0_documents: List[Document] = []

        # Initialize L2 vector store (function/method-level) - default
        self.l2_index = self._build_faiss_index()
        self.l2_vector_store = FAISS(
            embedding_function=self.embedding,
            index=self.l2_index,
            docstore=InMemoryDocstore(),
            index_to_docstore_id={},
        )
        self.l2_documents: List[Document] = []
        logger.info(
            f"Initialized CodeVectorStore with {embedding_provider}:{embedding_model}"
        )

    def _get_store_and_docs(self, level: Level) -> tuple[FAISS, List[Document]]:
        """Get the vector store and documents list for the specified level."""
        if level == "l0":
            return self.l0_vector_store, self.l0_documents
        elif level == "l2":
            return self.l2_vector_store, self.l2_documents
        else:
            raise ValueError(f"Invalid level: {level}. Must be 'l0' or 'l2'.")

    def _initialize_embedding_model(self, **kwargs) -> Embeddings:
        """Initialize the embedding model based on provider."""
        if self.embedding_provider.lower() == "openai":
            return OpenAIEmbeddings(model=self.embedding_model, **kwargs)
        elif self.embedding_provider.lower() == "huggingface":
            model_name = self.embedding_model
            model_kwargs = kwargs.pop("model_kwargs", {})
            encode_kwargs = kwargs.pop("encode_kwargs", {})

            return HuggingFaceEmbeddings(
                model_name=model_name,
                model_kwargs=model_kwargs,
                encode_kwargs=encode_kwargs,
                **kwargs,
            )
        else:
            raise ValueError(
                f"Unsupported embedding provider: {self.embedding_provider}"
            )

    def _infer_embedding_dimension(self, expected: Optional[int]) -> int:
        """Probe the embedding model to determine vector dimensionality."""
        probe_text = "codeminer-dimension-probe"
        vector = self.embedding.embed_query(probe_text)
        if not vector:
            raise ValueError("Failed to infer embedding dimension from model output")

        actual_dim = len(vector)
        if expected is not None and actual_dim != expected:
            logger.warning(
                "Embedding dimension mismatch: expected %s, got %s",
                expected,
                actual_dim,
            )
        return actual_dim

    def _profile_section(self, label: str, metadata: Optional[Dict[str, Any]] = None):
        """Return an active profiler section context if profiling is enabled."""
        if self.profiler is None:
            return nullcontext()
        return self.profiler.section(label, metadata)

    def _should_filter_by_threshold(self, score: float, threshold: float) -> bool:
        """
        Determine if a result should be filtered based on score threshold.

        For inner product (ip): higher scores are better (similarity),
        filter if score < threshold
        For L2 distance (l2): lower scores are better (distance),
        filter if score > threshold
        """
        if self.index_metric == "ip":
            # Inner product: higher is better (similarity score)
            return score < threshold
        elif self.index_metric == "l2":  # l2
            # L2 distance: lower is better (distance)
            return score > threshold
        else:
            raise ValueError(
                f"Unsupported index_metric: {self.index_metric}. Must be 'ip' or 'l2'."
            )

    def _build_faiss_index(self) -> faiss.Index:
        """Create a flat FAISS index with the configured metric."""
        if self.index_metric == "ip":
            return faiss.IndexFlatIP(self.dimension)
        elif self.index_metric == "l2":  # l2
            return faiss.IndexFlatL2(self.dimension)
        else:
            raise ValueError(
                f"Unsupported index_metric: {self.index_metric}. Must be 'ip' or 'l2'."
            )

    def add_code_chunks(
        self, code_chunks: List[Dict[str, Any]], level: Level = "l2"
    ) -> None:
        """
        Add code chunks to the vector store.

        Args:
            code_chunks: List of code chunk dictionaries with content and metadata
            level: Index level to add chunks to
            ("l0" for file skeletons, "l2" for functions/methods)
        """
        if not code_chunks:
            logger.warning("No code chunks provided")
            return

        vector_store, documents_list = self._get_store_and_docs(level)
        logger.info(f"Adding {len(code_chunks)} code chunks to {level} vector store")

        # Convert chunks to Document objects
        documents = []
        for i, chunk in enumerate(code_chunks):
            # Extract content and metadata
            content = chunk.get("content", "")
            metadata = {
                "chunk_id": len(documents_list) + i,
                "chunk_type": chunk.get("chunk_type", "unknown"),
                "name": chunk.get("name", f"chunk_{i}"),
                "file": chunk.get("file", ""),
                "start_line": chunk.get("start_line", 0),
                "end_line": chunk.get("end_line", 0),
                "node_id": chunk.get("node_id", ""),
                "level": level,  # Track which level this chunk belongs to
            }

            # Add any additional metadata
            for key, value in chunk.items():
                if key not in ["content"] and key not in metadata:
                    metadata[key] = value

            # Create Document
            document = Document(page_content=content, metadata=metadata)
            documents.append(document)

        # Store documents
        documents_list.extend(documents)

        with self._profile_section(
            f"vector_store_add_documents_{level}",
            {"num_documents": len(documents), "level": level},
        ):
            vector_store.add_documents(
                documents
            )  # this part will majorly blocked by embedding time

        logger.info(
            f"Successfully added {len(documents)} documents to {level} vector store"
        )

    def add_nodes_with_content(
        self, nodes: List[NodeInfo], level: Level = "l2"
    ) -> None:
        """
        Add NodeInfo objects (with content) to the vector store.

        Args:
            nodes: List of NodeInfo objects
            level: Index level to add nodes to ("l0" or "l2")
        """
        chunks = []
        for node in nodes:
            chunk = {
                "content": node.content,
                "chunk_type": node.type,
                "name": node.node_name,
                "file": node.file,
                "start_line": node.start_line,
                "end_line": node.end_line,
            }
            chunks.append(chunk)

        self.add_code_chunks(chunks, level=level)

    def search(
        self,
        query: str,
        top_k: int = 10,
        score_threshold: Optional[float] = None,
        level: Level = "l2",
        mask_node_ids: Optional[Set[str]] = None,
    ) -> List[NodeInfo]:
        """
        Search for similar code chunks using semantic similarity.

        Args:
            query: Search query text
            top_k: Number of top results to return
            score_threshold: Minimum similarity score threshold
            level: Index level to search ("l0" for file skeletons, "l2" for functions/methods)
            mask_node_ids: Optional set of CodeChunk.node_id values to filter results.

        Returns:
            List of NodeInfo objects with scores populated
        """
        vector_store, _ = self._get_store_and_docs(level)

        if vector_store is None:
            logger.warning(f"No {level} vector store available. Add code chunks first.")
            return []

        logger.debug(f"Searching {level} for: {query[:100]}...")

        docs_with_scores = vector_store.similarity_search_with_score(query, k=top_k)

        # Convert to NodeInfo objects
        results = []
        for doc, score in docs_with_scores:
            metadata = doc.metadata

            # Apply score threshold based on index metric
            # ip (inner product): higher score = more similar, filter if score < threshold
            # l2 (distance): lower score = more similar, filter if score > threshold
            if score_threshold is not None and self._should_filter_by_threshold(
                score, score_threshold
            ):
                continue

            node_with_score = NodeInfo(
                node_name=metadata.get("name", "unknown"),
                type=metadata.get("chunk_type", "unknown"),
                file=metadata.get("file", ""),
                node_id=metadata.get("node_id", ""),
                start_line=metadata.get("start_line", 0),
                end_line=metadata.get("end_line", 0),
                score=float(score),
            )
            results.append(node_with_score)

        if mask_node_ids:
            results = [r for r in results if r.node_id in mask_node_ids]
        if top_k:
            results = results[:top_k]

        logger.debug(
            f"Found {len(results)} results in {level} (masked={bool(mask_node_ids)})"
        )
        return results

    def search_with_content(
        self,
        query: str,
        top_k: int = 10,
        score_threshold: Optional[float] = None,
        level: Level = "l2",
        mask_node_ids: Optional[Set[str]] = None,
    ) -> List[NodeInfo]:
        """
        Search and return results with content included.

        Args:
            query: Search query text
            top_k: Number of top results to return
            score_threshold: Minimum similarity score threshold
            level: Index level to search ("l0" for file skeletons, "l2" for functions/methods)
            mask_node_ids: Optional set of CodeChunk.node_id values to filter results.

        Returns:
            List of NodeInfo objects with content populated
        """
        vector_store, _ = self._get_store_and_docs(level)

        if vector_store is None:
            logger.warning(f"No {level} vector store available. Add code chunks first.")
            return []

        docs_with_scores = vector_store.similarity_search_with_score(query, k=top_k)

        # Convert to NodeInfo objects
        results = []
        for doc, score in docs_with_scores:
            metadata = doc.metadata

            # Apply score threshold based on index metric
            # ip (inner product): higher score = more similar, filter if score < threshold
            # l2 (distance): lower score = more similar, filter if score > threshold
            if score_threshold is not None and self._should_filter_by_threshold(
                score, score_threshold
            ):
                continue

            node_with_content = NodeInfo(
                node_name=metadata.get("name", "unknown"),
                type=metadata.get("chunk_type", "unknown"),
                file=metadata.get("file", ""),
                node_id=metadata.get("node_id", ""),
                start_line=metadata.get("start_line", 0),
                end_line=metadata.get("end_line", 0),
                score=float(score),
                content=doc.page_content,
            )
            results.append(node_with_content)

        if mask_node_ids:
            results = [r for r in results if r.node_id in mask_node_ids]
        if top_k:
            results = results[:top_k]

        logger.debug(
            f"Found {len(results)} results with content in {level} "
            f"(masked={bool(mask_node_ids)})"
        )
        return results

    def hierarchical_search(
        self,
        query: str,
        l0_top_k: int = 5,
        l2_top_k: int = 10,
        l0_score_threshold: Optional[float] = None,
        l2_score_threshold: Optional[float] = None,
        filter_l2_by_l0: bool = True,
    ) -> Dict[str, List[NodeInfo]]:
        """
        Note: This method is implemented by Claude and is just for future reference.
        Perform hierarchical search: first L0 (files), then L2 (functions).

        This implements a coarse-to-fine retrieval strategy:
        1. Search L0 to find relevant files based on their skeletons
        2. Search L2 for specific functions/methods
        3. Optionally filter L2 results to only include those from L0 files

        Args:
            query: Search query text
            l0_top_k: Number of top L0 results (files)
            l2_top_k: Number of top L2 results (functions/methods)
            l0_score_threshold: Score threshold for L0 results
            l2_score_threshold: Score threshold for L2 results
            filter_l2_by_l0: If True, only return L2 results from files found in L0

        Returns:
            Dict with 'l0' and 'l2' keys containing search results
        """
        # Step 1: Search L0 to find relevant files
        l0_results = self.search(
            query, top_k=l0_top_k, score_threshold=l0_score_threshold, level="l0"
        )

        # Step 2: Search L2 for functions/methods (fetch more if filtering)
        l2_fetch_k = l2_top_k * 3 if filter_l2_by_l0 else l2_top_k
        l2_results = self.search(
            query, top_k=l2_fetch_k, score_threshold=l2_score_threshold, level="l2"
        )

        # Step 3: Optionally filter L2 results by L0 files
        if filter_l2_by_l0 and l0_results:
            l0_files = {result.file for result in l0_results}
            l2_results = [r for r in l2_results if r.file in l0_files]

        # Limit L2 results to top_k
        l2_results = l2_results[:l2_top_k]

        return {
            "l0": l0_results,
            "l2": l2_results,
        }

    def save(self, path: Optional[str] = None) -> None:
        """
        Save the vector store to disk.

        Args:
            path: Path to save the store (uses self.store_path if not provided)
        """
        save_path = Path(path) if path else self.store_path
        if save_path is None:
            raise ValueError("No save path provided")

        save_path = Path(save_path)
        save_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"Saving vector store to {save_path}")

        model_suffix = self.embedding_model.replace("/", "__")

        # Save L0 vector store
        l0_path = save_path / "l0"
        if self.l0_vector_store is not None and self.l0_documents:
            l0_path.mkdir(parents=True, exist_ok=True)
            index_name = f"index_{model_suffix}"
            self.l0_vector_store.save_local(str(l0_path), index_name=index_name)
            # Save L0 documents
            docs_path = l0_path / f"documents_{model_suffix}.pkl"
            with open(docs_path, "wb") as f:
                pickle.dump(self.l0_documents, f)
            # Save L0 config
            config_path = l0_path / f"config_{model_suffix}.json"
            config = {
                "embedding_model": self.embedding_model,
                "embedding_provider": self.embedding_provider,
                "dimension": self.dimension,
                "index_type": self.index_type,
                "index_metric": self.index_metric,
                "level": "l0",
                "num_documents": len(self.l0_documents),
            }
            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)
            logger.info(f"Saved L0 store with {len(self.l0_documents)} documents")

        # Save L2 vector store
        l2_path = save_path / "l2"
        if self.l2_vector_store is not None and self.l2_documents:
            l2_path.mkdir(parents=True, exist_ok=True)
            index_name = f"index_{model_suffix}"
            self.l2_vector_store.save_local(str(l2_path), index_name=index_name)
            # Save L2 documents
            docs_path = l2_path / f"documents_{model_suffix}.pkl"
            with open(docs_path, "wb") as f:
                pickle.dump(self.l2_documents, f)
            # Save L2 config
            config_path = l2_path / f"config_{model_suffix}.json"
            config = {
                "embedding_model": self.embedding_model,
                "embedding_provider": self.embedding_provider,
                "dimension": self.dimension,
                "index_type": self.index_type,
                "index_metric": self.index_metric,
                "level": "l2",
                "num_documents": len(self.l2_documents),
            }
            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)
            logger.info(f"Saved L2 store with {len(self.l2_documents)} documents")

        # Save top-level configuration
        config_path = save_path / f"config_{model_suffix}.json"
        config = {
            "embedding_model": self.embedding_model,
            "embedding_provider": self.embedding_provider,
            "dimension": self.dimension,
            "index_type": self.index_type,
            "index_metric": self.index_metric,
            "l0_documents": len(self.l0_documents),
            "l2_documents": len(self.l2_documents),
        }
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

        logger.info("Vector store saved successfully")

    def load(self, path: Optional[str] = None) -> None:
        """
        Load the vector store from disk.

        Args:
            path: Path to load the store from (uses self.store_path if not provided)
        """
        load_path = Path(path) if path else self.store_path
        if load_path is None:
            raise ValueError("No load path provided")

        load_path = Path(load_path)
        if not load_path.exists():
            raise FileNotFoundError(f"Vector store not found at {load_path}")

        logger.info(f"Loading vector store from {load_path}")

        model_suffix = self.embedding_model.replace("/", "__")

        # Load top-level configuration
        config_path = load_path / f"config_{model_suffix}.json"
        if not config_path.exists():
            config_path = load_path / "config.json"

        if config_path.exists():
            with open(config_path, "r") as f:
                config = json.load(f)

            # Verify configuration matches
            if config.get("dimension") != self.dimension:
                logger.warning(
                    f"Dimension mismatch: expected {self.dimension}, "
                    f"got {config.get('dimension')}"
                )
            saved_metric = config.get("index_metric")
            if saved_metric and saved_metric != self.index_metric:
                logger.warning(
                    "Index metric mismatch: expected %s, got %s",
                    self.index_metric,
                    saved_metric,
                )
                self.index_metric = saved_metric

        # Load L0 vector store
        l0_path = load_path / "l0"
        if l0_path.exists():
            try:
                index_name = f"index_{model_suffix}"
                self.l0_vector_store = FAISS.load_local(
                    str(l0_path),
                    self.embedding,
                    index_name=index_name,
                    allow_dangerous_deserialization=True,
                )
                # Load L0 documents
                docs_path = l0_path / f"documents_{model_suffix}.pkl"
                if docs_path.exists():
                    with open(docs_path, "rb") as f:
                        self.l0_documents = pickle.load(f)
                logger.info(f"Loaded L0 store with {len(self.l0_documents)} documents")
            except Exception as e:
                logger.warning(f"Could not load L0 vector store: {e}")

        # Load L2 vector store
        l2_path = load_path / "l2"
        if l2_path.exists():
            try:
                index_name = f"index_{model_suffix}"
                self.l2_vector_store = FAISS.load_local(
                    str(l2_path),
                    self.embedding,
                    index_name=index_name,
                    allow_dangerous_deserialization=True,
                )
                # Load L2 documents
                docs_path = l2_path / f"documents_{model_suffix}.pkl"
                if docs_path.exists():
                    with open(docs_path, "rb") as f:
                        self.l2_documents = pickle.load(f)
                logger.info(f"Loaded L2 store with {len(self.l2_documents)} documents")
            except Exception as e:
                logger.warning(f"Could not load L2 vector store: {e}")

        total_docs = len(self.l0_documents) + len(self.l2_documents)
        logger.info(
            f"Vector store loaded successfully with {total_docs} total documents "
            f"(L0: {len(self.l0_documents)}, L2: {len(self.l2_documents)})"
        )

    def get_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the vector store.

        Returns:
            Dictionary with store statistics
        """
        stats = {
            "embedding_model": self.embedding_model,
            "embedding_provider": self.embedding_provider,
            "dimension": self.dimension,
            "index_type": self.index_type,
            "index_metric": self.index_metric,
            "l0_documents": len(self.l0_documents),
            "l2_documents": len(self.l2_documents),
            "total_documents": len(self.l0_documents) + len(self.l2_documents),
        }

        # Analyze L0 chunk types
        if self.l0_documents:
            l0_chunk_types = {}
            for doc in self.l0_documents:
                chunk_type = doc.metadata.get("chunk_type", "unknown")
                l0_chunk_types[chunk_type] = l0_chunk_types.get(chunk_type, 0) + 1
            stats["l0_chunk_types"] = l0_chunk_types

        # Analyze L2 chunk types
        if self.l2_documents:
            l2_chunk_types = {}
            for doc in self.l2_documents:
                chunk_type = doc.metadata.get("chunk_type", "unknown")
                l2_chunk_types[chunk_type] = l2_chunk_types.get(chunk_type, 0) + 1
            stats["l2_chunk_types"] = l2_chunk_types

        return stats

    def clear(self, level: Optional[Level] = None) -> None:
        """
        Clear data from the vector store.

        Args:
            level: If specified, only clear that level ("l0" or "l2").
                   If None, clear both levels.
        """
        if level is None or level == "l0":
            logger.info("Clearing L0 vector store")
            self.l0_index = self._build_faiss_index()
            self.l0_vector_store = FAISS(
                embedding_function=self.embedding,
                index=self.l0_index,
                docstore=InMemoryDocstore(),
                index_to_docstore_id={},
            )
            self.l0_documents = []

        if level is None or level == "l2":
            logger.info("Clearing L2 vector store")
            self.l2_index = self._build_faiss_index()
            self.l2_vector_store = FAISS(
                embedding_function=self.embedding,
                index=self.l2_index,
                docstore=InMemoryDocstore(),
                index_to_docstore_id={},
            )
            self.l2_documents = []

        logger.info("Vector store cleared")


def create_code_vector_store(
    embedding_model: str = "text-embedding-ada-002",
    embedding_provider: str = "openai",
    store_path: Optional[str] = None,
    **kwargs,
) -> CodeVectorStore:
    """
    Factory function to create a CodeVectorStore.

    Args:
        embedding_model: Name of the embedding model
        embedding_provider: Provider for embeddings
        store_path: Path to store/load the vector store
        **kwargs: Additional arguments for CodeVectorStore

    Returns:
        CodeVectorStore instance
    """
    return CodeVectorStore(
        embedding_model=embedding_model,
        embedding_provider=embedding_provider,
        store_path=store_path,
        **kwargs,
    )
