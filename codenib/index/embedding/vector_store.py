# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Vector Store implementation using FAISS and sentence-transformers for code embeddings.
This module provides functionality to create, store, and query vector embeddings
of code chunks for semantic similarity search.
"""

import hashlib
import json
import os
import pickle
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Set, Tuple

import faiss
import numpy as np

from ... import compat_pickle
from ...log_utils import get_logger
from ...profiler import Profiler
from ...provider_routes import normalize_provider
from ...types import NodeInfo
from .artifact_integrity import (
    VECTOR_PERSISTENCE_SCHEMA,
    validate_vector_config_artifact,
    validate_vector_level_artifacts,
    vector_level_artifact_records,
)

logger = get_logger(__name__)

Level = Literal["l0", "l2"]


def _atomic_replace(target: Path, writer: Callable[[Path], None]) -> None:
    """Write a sibling temporary file and atomically publish it."""

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        writer(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json_dump(target: Path, value: object) -> None:
    def _write(path: Path) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")

    _atomic_replace(target, _write)


def _atomic_pickle_dump(target: Path, value: object) -> None:
    def _write(path: Path) -> None:
        with path.open("wb") as handle:
            pickle.dump(value, handle)

    _atomic_replace(target, _write)


class _Document:
    """Lightweight document container replacing LangChain Document.

    Provides the same ``page_content`` / ``metadata`` interface so that
    callers accessing ``store.l0_documents`` or ``store.l2_documents``
    continue to work without changes.
    """

    __slots__ = ("page_content", "metadata")

    def __init__(self, page_content: str = "", metadata: Optional[Dict] = None):
        self.page_content = page_content
        self.metadata = metadata if metadata is not None else {}

    def __repr__(self) -> str:
        name = self.metadata.get("name", "")
        return f"_Document(name={name!r}, len={len(self.page_content)})"


class _HuggingFaceEmbeddingWrapper:
    """Wraps ``SentenceTransformer`` to expose ``embed_query`` / ``embed_documents``.

    The interface is intentionally compatible with the LangChain ``Embeddings``
    protocol so that external callers accessing ``store.embedding.embed_query``
    or ``store.embedding.embed_documents`` keep working.
    """

    def __init__(self, model_name: str, max_seq_length: Optional[int] = None, **kwargs):
        from sentence_transformers import SentenceTransformer

        from .prompt_registry import resolve_prompts

        model_kwargs = kwargs.pop("model_kwargs", {})
        self._encode_kwargs = kwargs.pop("encode_kwargs", {})
        self._default_batch_size: Optional[int] = kwargs.pop("default_batch_size", None)

        # Pop prompt-related kwargs so they aren't forwarded to
        # SentenceTransformer's __init__. Anything left as None falls back to
        # the per-model registry; anything set explicitly (including "")
        # overrides the registry.
        explicit = {
            "query_prompt_name": kwargs.pop("query_prompt_name", None),
            "query_prompt": kwargs.pop("query_prompt", None),
            "document_prompt_name": kwargs.pop("document_prompt_name", None),
            "document_prompt": kwargs.pop("document_prompt", None),
        }
        defaults = resolve_prompts(model_name)
        merged = {
            k: (v if v is not None else defaults.get(k)) for k, v in explicit.items()
        }
        self._query_prompt_name = merged["query_prompt_name"]
        self._query_prompt = merged["query_prompt"]
        self._document_prompt_name = merged["document_prompt_name"]
        self._document_prompt = merged["document_prompt"]

        # Build SentenceTransformer init kwargs
        st_kwargs: Dict[str, Any] = {}
        if kwargs.pop("trust_remote_code", False):
            st_kwargs["trust_remote_code"] = True
        # Forward remaining kwargs (e.g. device, cache_folder)
        st_kwargs.update(kwargs)
        st_kwargs.update(model_kwargs)

        self._model = SentenceTransformer(model_name, **st_kwargs)

        # Cap the effective sequence length to avoid CUDA OOM.
        self._apply_max_seq_length(model_name, max_seq_length)

        logger.info(
            "Embedding wrapper for %s: query_prompt_name=%r query_prompt=%r "
            "document_prompt_name=%r document_prompt=%r",
            model_name,
            self._query_prompt_name,
            (
                (self._query_prompt[:60] + "…")
                if isinstance(self._query_prompt, str) and len(self._query_prompt) > 60
                else self._query_prompt
            ),
            self._document_prompt_name,
            self._document_prompt,
        )

    # Expose the underlying SentenceTransformer so that callers that
    # previously reached through ``store.embedding._client`` (langchain-
    # huggingface >=0.1) or ``store.embedding.client`` (older) keep working.
    @property
    def _client(self):
        return self._model

    @property
    def client(self):
        return self._model

    def _apply_max_seq_length(
        self, model_name: str, max_seq_length: Optional[int]
    ) -> None:
        """Cap tokeniser / model sequence length to prevent OOM."""
        try:
            tok = self._model.tokenizer
            max_pos = getattr(
                self._model[0].auto_model.config,
                "max_position_embeddings",
                None,
            )
            effective_max = max_seq_length or max_pos
            if effective_max:
                if tok.model_max_length > effective_max:
                    tok.model_max_length = effective_max
                if self._model.max_seq_length > effective_max:
                    logger.info(
                        "Capping max_seq_length from %s to %s for model %s",
                        self._model.max_seq_length,
                        effective_max,
                        model_name,
                    )
                    self._model.max_seq_length = effective_max
        except Exception as e:
            if max_seq_length is not None:
                logger.warning(
                    "--max-seq-length %s was requested but could not be "
                    "applied to model %s: %s. CUDA OOM may occur.",
                    max_seq_length,
                    model_name,
                    e,
                )
            else:
                logger.debug("Could not check tokenizer max length: %s", e)

    def _build_encode_kwargs(
        self,
        prompt: Optional[str],
        prompt_name: Optional[str],
    ) -> Dict[str, Any]:
        """Merge per-call prompt args on top of self._encode_kwargs.

        ``prompt`` (raw string) wins over ``prompt_name``; either being a
        non-None value disables the other. Empty string is a valid prompt
        meaning "encode with empty prefix" — same as no-prefix.
        """
        kwargs = dict(self._encode_kwargs)
        if prompt is not None:
            kwargs["prompt"] = prompt
            kwargs.pop("prompt_name", None)
        elif prompt_name is not None:
            kwargs["prompt_name"] = prompt_name
            kwargs.pop("prompt", None)
        return kwargs

    def embed_query(self, text: str) -> List[float]:
        kwargs = self._build_encode_kwargs(self._query_prompt, self._query_prompt_name)
        vec = self._model.encode([text], **kwargs)
        return vec[0].tolist()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        kwargs = self._build_encode_kwargs(
            self._document_prompt, self._document_prompt_name
        )
        # Apply default batch size if configured (e.g., to avoid CUDA OOM)
        if self._default_batch_size is not None:
            kwargs.setdefault("batch_size", self._default_batch_size)
        vecs = self._model.encode(texts, **kwargs)
        return vecs.tolist()


class _OpenAIEmbeddingWrapper:
    """Wraps the OpenAI SDK to expose ``embed_query`` / ``embed_documents``."""

    def __init__(self, model: str, request_options: Optional[Dict] = None, **kwargs):
        from openai import OpenAI

        self._model = model
        self._request_options = dict(request_options or {})
        self._client = OpenAI(**kwargs)

    def embed_query(self, text: str) -> List[float]:
        resp = self._client.embeddings.create(
            input=[text],
            model=self._model,
            **self._request_options,
        )
        return resp.data[0].embedding

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        resp = self._client.embeddings.create(
            input=texts,
            model=self._model,
            **self._request_options,
        )
        return [d.embedding for d in sorted(resp.data, key=lambda x: x.index)]


def _to_document(obj: Any) -> _Document:
    """Convert any document-like object to ``_Document`` (duck-typed)."""
    if isinstance(obj, _Document):
        return obj
    return _Document(
        page_content=getattr(obj, "page_content", ""),
        metadata=getattr(obj, "metadata", {}),
    )


def _result_source_file(metadata: Dict[str, Any]) -> str:
    """Prefer the repository-relative file identity encoded in ``node_id``."""
    raw_file = str(metadata.get("file") or "").replace("\\", "/")
    node_id = str(metadata.get("node_id") or "").replace("\\", "/")
    if not raw_file or not node_id:
        return raw_file

    # ``chunk_file`` can be used outside a repository and then carries the
    # same absolute path in both fields. Preserve that direct-file contract.
    if node_id == raw_file or node_id.startswith(f"{raw_file}:"):
        return raw_file

    node_file = node_id.split(":", 1)[0].removeprefix("./")
    comparable_file = raw_file.removeprefix("./")
    if node_file and (
        comparable_file == node_file or comparable_file.endswith(f"/{node_file}")
    ):
        return node_file
    return raw_file


class CodeVectorStore:
    """
    Vector store for code embeddings using FAISS and sentence-transformers.
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
        ivf_nlist: int = 100,
        ivf_nprobe: int = 8,
        store_path: Optional[str] = None,
        profiler: Optional[Profiler] = None,
        embedding: Optional[Any] = None,
        artifact_metadata: Optional[Dict[str, Any]] = None,
        **embedding_kwargs,
    ):
        """
        Initialize the CodeVectorStore.

        Args:
            embedding_model: Name of the embedding model to use
            embedding_provider: Provider for embeddings ("openai" or "huggingface")
            dimension: Dimension of the embedding vectors
            index_type: FAISS index type — "flat" (exact brute force, default)
                or "ivf" (IVF inverted-file; approximate, faster at scale).
                IVF indices are trained lazily on the first batch of vectors.
            index_metric: Distance metric ("ip" for inner product, "l2" for L2 distance)
            ivf_nlist: IVF only — number of Voronoi cells (coarse centroids). On
                small corpora it is clamped down to the training-set size, since
                FAISS k-means needs at least ``nlist`` training points.
            ivf_nprobe: IVF only — cells probed per query; the recall/latency
                knob. Clamped to the effective ``nlist``.
            store_path: Path to store/load the vector store
            profiler: Optional profiler instance to capture detailed timings
            embedding: A pre-built embedding wrapper to reuse. When several
                stores share one model (e.g. one per repo), pass the same
                instance so the model is loaded onto the GPU only once.
            artifact_metadata: Optional immutable source/build identity persisted
                with the top-level configuration.
            **embedding_kwargs: Additional arguments for embedding model
        """
        self.embedding_model = embedding_model
        self.embedding_provider = embedding_provider
        self.dimension = dimension
        self.index_type = index_type.lower()
        if self.index_type not in ("flat", "ivf"):
            raise ValueError(
                f"Unsupported index_type: {index_type}. Must be 'flat' or 'ivf'."
            )
        self.index_metric = index_metric.lower()
        if self.index_metric not in ["ip", "l2"]:
            raise ValueError(
                f"Unsupported index_metric: {index_metric}. Must be 'ip' or 'l2'."
            )
        self.ivf_nlist = max(1, int(ivf_nlist))
        self.ivf_nprobe = max(1, int(ivf_nprobe))
        self.store_path = Path(store_path) if store_path else None
        self.profiler = profiler
        self.artifact_metadata = dict(artifact_metadata or {})

        # Initialize the embedding model — or reuse a shared one so the same
        # model isn't loaded onto the GPU once per store.
        self.embedding = (
            embedding
            if embedding is not None
            else self._initialize_embedding_model(**embedding_kwargs)
        )
        self._cached_query_text: Optional[str] = None
        self._cached_query_vector: Optional[np.ndarray] = None
        self._query_cache_depth = 0
        self.dimension = self._infer_embedding_dimension(dimension)

        # Initialize L0 (file-level skeletons)
        self.l0_index = self._build_faiss_index()
        self.l0_documents: List[_Document] = []

        # Initialize L2 (function/method-level) - default
        self.l2_index = self._build_faiss_index()
        self.l2_documents: List[_Document] = []

        logger.info(
            f"Initialized CodeVectorStore with {embedding_provider}:{embedding_model}"
        )

    def _get_index_and_docs(self, level: Level) -> tuple[faiss.Index, List[_Document]]:
        """Get the FAISS index and documents list for the specified level."""
        if level == "l0":
            return self.l0_index, self.l0_documents
        elif level == "l2":
            return self.l2_index, self.l2_documents
        else:
            raise ValueError(f"Invalid level: {level}. Must be 'l0' or 'l2'.")

    def _initialize_embedding_model(self, **kwargs):
        """Initialize the embedding model based on provider."""
        if self.embedding_provider.lower() == "openai":
            return _OpenAIEmbeddingWrapper(model=self.embedding_model, **kwargs)
        elif self.embedding_provider.lower() == "huggingface":
            return _HuggingFaceEmbeddingWrapper(
                model_name=self.embedding_model, **kwargs
            )
        else:
            raise ValueError(
                f"Unsupported embedding provider: {self.embedding_provider}"
            )

    def _infer_embedding_dimension(self, expected: Optional[int]) -> int:
        """Probe the embedding model to determine vector dimensionality."""
        probe_text = "codenib-dimension-probe"
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

    def _embed_query(self, query: str) -> np.ndarray:
        """Encode a query, reusing it only inside an explicit request scope."""
        cached_text = getattr(self, "_cached_query_text", None)
        cached_vector = getattr(self, "_cached_query_vector", None)
        cache_active = getattr(self, "_query_cache_depth", 0) > 0
        if cache_active and cached_text == query and cached_vector is not None:
            return cached_vector

        vector = np.asarray(
            self.embedding.embed_query(query), dtype=np.float32
        ).reshape(-1)
        if cache_active:
            self._cached_query_text = query
            self._cached_query_vector = vector
        return vector

    @contextmanager
    def reuse_query_embedding(self):
        """Reuse one query vector within a composed request, then discard it."""

        depth = getattr(self, "_query_cache_depth", 0)
        if depth == 0:
            self.clear_query_cache()
        self._query_cache_depth = depth + 1
        try:
            yield
        finally:
            self._query_cache_depth -= 1
            if self._query_cache_depth == 0:
                self.clear_query_cache()

    def clear_query_cache(self) -> None:
        """Clear the single-query embedding reused by consecutive search stages."""

        self._cached_query_text = None
        self._cached_query_vector = None

    def _should_filter_by_threshold(self, score: float, threshold: float) -> bool:
        """
        Determine if a result should be filtered based on score threshold.

        For inner product (ip): higher scores are better (similarity),
        filter if score < threshold
        For L2 distance (l2): lower scores are better (distance),
        filter if score > threshold
        """
        if self.index_metric == "ip":
            return score < threshold
        elif self.index_metric == "l2":
            return score > threshold
        else:
            raise ValueError(
                f"Unsupported index_metric: {self.index_metric}. Must be 'ip' or 'l2'."
            )

    def _faiss_metric(self) -> int:
        """Map the configured metric string to a FAISS metric constant."""
        if self.index_metric == "ip":
            return faiss.METRIC_INNER_PRODUCT
        elif self.index_metric == "l2":
            return faiss.METRIC_L2
        raise ValueError(
            f"Unsupported index_metric: {self.index_metric}. Must be 'ip' or 'l2'."
        )

    def _build_flat_index(self) -> faiss.Index:
        """Create a flat (exact) FAISS index with the configured metric."""
        if self.index_metric == "ip":
            return faiss.IndexFlatIP(self.dimension)
        elif self.index_metric == "l2":
            return faiss.IndexFlatL2(self.dimension)
        raise ValueError(
            f"Unsupported index_metric: {self.index_metric}. Must be 'ip' or 'l2'."
        )

    def _build_faiss_index(self, nlist: Optional[int] = None) -> faiss.Index:
        """Create an empty FAISS index of the configured type and metric.

        Flat indices are ready for ``add``; IVF indices are returned
        *untrained* and must be trained on a batch of vectors (see
        :meth:`_add_to_index`) before any vectors are added.
        """
        if self.index_type == "flat":
            return self._build_flat_index()
        # IVF: a flat quantizer assigns each vector to one of ``cells`` cells.
        cells = max(1, int(nlist if nlist is not None else self.ivf_nlist))
        quantizer = self._build_flat_index()
        index = faiss.IndexIVFFlat(
            quantizer, self.dimension, cells, self._faiss_metric()
        )
        index.nprobe = min(self.ivf_nprobe, cells)
        return index

    def _add_to_index(self, level: Level, vectors: np.ndarray) -> None:
        """Add pre-computed ``vectors`` to *level*'s FAISS index.

        Flat indices take a plain ``add``. An untrained IVF index is trained
        on this first batch — clamping ``nlist`` down to the batch size on
        small corpora (FAISS k-means requires at least ``nlist`` training
        points) — then the vectors are added; later batches add to the
        already-trained index.
        """
        if vectors is None or len(vectors) == 0:
            return
        index = self.l0_index if level == "l0" else self.l2_index
        if self.index_type == "ivf" and not index.is_trained:
            n = int(vectors.shape[0])
            effective_nlist = max(1, min(self.ivf_nlist, n))
            if effective_nlist != index.nlist:
                # nlist is fixed at construction, so rebuild at the size the
                # training set can actually support, then re-bind the slot.
                index = self._build_faiss_index(nlist=effective_nlist)
                if level == "l0":
                    self.l0_index = index
                else:
                    self.l2_index = index
            with self._profile_section(
                f"faiss_index_train_{level}",
                {"num_vectors": n, "nlist": effective_nlist, "level": level},
            ):
                index.train(vectors)
        index.add(vectors)

    def _search_index(
        self,
        query: str,
        index: faiss.Index,
        documents: List[_Document],
        top_k: int,
    ) -> List[tuple[_Document, float]]:
        """Encode *query* and search a raw FAISS index.

        Returns a list of ``(document, score)`` pairs sorted by relevance.
        """
        if index is None or index.ntotal == 0:
            return []

        query_vec = self._embed_query(query).reshape(1, -1)

        # FAISS search
        k = min(top_k, index.ntotal)
        distances, indices = index.search(query_vec, k)

        results: List[tuple[_Document, float]] = []
        for dist, idx in zip(distances[0], indices[0], strict=True):
            if idx < 0:
                continue  # FAISS sentinel for empty slots
            if idx < len(documents):
                results.append((documents[idx], float(dist)))
        return results

    def swap_index(self, path: str) -> None:
        """Hot-swap the FAISS index without reloading the embedding model.

        The replacement is fully loaded and validated before the current
        L0/L2 state is released. The embedding model is left intact so the
        caller can reuse the same model across many instances.
        """
        self.load(path)

    def close(self) -> None:
        """Release embeddings and FAISS resources to free memory."""
        for index in (self.l0_index, self.l2_index):
            if index is None:
                continue
            reset = getattr(index, "reset", None)
            if callable(reset):
                reset()

        self.l0_documents.clear()
        self.l2_documents.clear()
        self.l0_index = None
        self.l2_index = None

        self.embedding = None
        self._query_cache_depth = 0
        self._cached_query_text = None
        self._cached_query_vector = None

        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

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

        index, documents_list = self._get_index_and_docs(level)
        logger.info(f"Adding {len(code_chunks)} code chunks to {level} vector store")

        # Convert chunks to _Document objects
        documents: List[_Document] = []
        for i, chunk in enumerate(code_chunks):
            content = chunk.get("content", "")
            content_hash = hashlib.md5(
                content.encode("utf-8", errors="replace")
            ).hexdigest()
            metadata = {
                "chunk_id": len(documents_list) + i,
                "chunk_type": chunk.get("chunk_type", "unknown"),
                "name": chunk.get("name", f"chunk_{i}"),
                "file": chunk.get("file", ""),
                "start_line": chunk.get("start_line", 0),
                "end_line": chunk.get("end_line", 0),
                "node_id": chunk.get("node_id", ""),
                "level": level,
                "content_hash": content_hash,
            }
            for key, value in chunk.items():
                if key not in ["content"] and key not in metadata:
                    metadata[key] = value

            documents.append(_Document(page_content=content, metadata=metadata))

        # Store documents
        documents_list.extend(documents)

        texts = [doc.page_content for doc in documents]

        # Phase 1: Embed texts (typically the bottleneck)
        with self._profile_section(
            f"embedding_encode_{level}",
            {"num_documents": len(documents), "level": level},
        ):
            embeddings = self.embedding.embed_documents(texts)

        # Phase 2: Add pre-computed vectors to FAISS index
        with self._profile_section(
            f"faiss_index_add_{level}",
            {"num_vectors": len(embeddings), "level": level},
        ):
            vectors = np.array(embeddings, dtype=np.float32)
            self._add_to_index(level, vectors)

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
            level: Index level to search ("l0" for file skeletons, "l2" for
                functions/methods)
            mask_node_ids: Optional set of CodeChunk.node_id values to filter results.

        Returns:
            List of NodeInfo objects with scores populated
        """
        index, documents = self._get_index_and_docs(level)

        if index is None or index.ntotal == 0:
            logger.warning(f"No {level} vector store available. Add code chunks first.")
            return []

        logger.debug(f"Searching {level} for: {query[:100]}...")

        docs_with_scores = self._search_index(query, index, documents, top_k)

        results = []
        for doc, score in docs_with_scores:
            metadata = doc.metadata

            if score_threshold is not None and self._should_filter_by_threshold(
                score, score_threshold
            ):
                continue

            node_with_score = NodeInfo(
                node_name=metadata.get("name", "unknown"),
                type=metadata.get("chunk_type", "unknown"),
                file=_result_source_file(metadata),
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
            level: Index level to search ("l0" for file skeletons, "l2" for
                functions/methods)
            mask_node_ids: Optional set of CodeChunk.node_id values to filter results.

        Returns:
            List of NodeInfo objects with content populated
        """
        index, documents = self._get_index_and_docs(level)

        if index is None or index.ntotal == 0:
            logger.warning(f"No {level} vector store available. Add code chunks first.")
            return []

        docs_with_scores = self._search_index(query, index, documents, top_k)

        results = []
        for doc, score in docs_with_scores:
            metadata = doc.metadata

            if score_threshold is not None and self._should_filter_by_threshold(
                score, score_threshold
            ):
                continue

            node_with_content = NodeInfo(
                node_name=metadata.get("name", "unknown"),
                type=metadata.get("chunk_type", "unknown"),
                file=_result_source_file(metadata),
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

    def search_within_ids(
        self,
        query: str,
        mask_node_ids: Set[str],
        top_k: int = 10,
        level: Level = "l2",
    ) -> List[NodeInfo]:
        """Search only within a restricted set of node IDs.

        Instead of searching the full FAISS index globally and filtering
        afterwards, this method restricts the search space *before* computing
        similarity.  It reconstructs stored vectors for matching documents
        and computes similarity against the query embedding directly.

        Args:
            query: Search query text.
            mask_node_ids: Set of node_id / node_name values to restrict
                search to.
            top_k: Number of top results to return.
            level: Index level to search.

        Returns:
            List of NodeInfo objects sorted by similarity score.
        """
        index, documents = self._get_index_and_docs(level)
        if index is None or index.ntotal == 0:
            logger.warning(f"No {level} vector store available.")
            return []

        # Find documents whose node_id or name is in mask set
        matched: list[tuple[int, _Document]] = []
        for i, doc in enumerate(documents):
            meta = doc.metadata
            if (
                meta.get("node_id", "") in mask_node_ids
                or meta.get("name", "") in mask_node_ids
            ):
                matched.append((i, doc))

        if not matched:
            logger.debug("search_within_ids: no matching documents found")
            return []

        # Encode query
        query_vec = self._embed_query(query)

        # Reconstruct stored vectors and compute similarity
        results: list[NodeInfo] = []
        for faiss_idx, doc in matched:
            vec = index.reconstruct(faiss_idx)
            if self.index_metric == "ip":
                score = float(np.dot(query_vec, vec))
            else:  # l2 — lower is better
                score = float(np.sum((query_vec - vec) ** 2))

            metadata = doc.metadata
            results.append(
                NodeInfo(
                    node_name=metadata.get("name", "unknown"),
                    type=metadata.get("chunk_type", "unknown"),
                    file=_result_source_file(metadata),
                    node_id=metadata.get("node_id", ""),
                    start_line=metadata.get("start_line", 0),
                    end_line=metadata.get("end_line", 0),
                    score=score,
                    content=doc.page_content,
                )
            )

        best_by_node = {}
        for result in results:
            identity = result.node_id or (
                result.file,
                result.node_name,
                result.start_line,
                result.end_line,
            )
            existing = best_by_node.get(identity)
            if existing is None:
                best_by_node[identity] = result
                continue
            if self.index_metric == "ip":
                is_better = result.score > existing.score
            else:
                is_better = result.score < existing.score
            if is_better:
                best_by_node[identity] = result
        results = list(best_by_node.values())

        # Sort: ip → higher is better; l2 → lower is better
        results.sort(
            key=lambda r: r.score,
            reverse=(self.index_metric == "ip"),
        )

        logger.debug(
            "search_within_ids: %d matched, %d unique, returning top %d",
            len(matched),
            len(results),
            min(top_k, len(results)),
        )
        return results[:top_k]

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

        level_state = {}
        for level in ("l0", "l2"):
            index, documents = self._get_index_and_docs(level)
            if documents:
                if index is None or int(index.ntotal) != len(documents):
                    vector_count = 0 if index is None else int(index.ntotal)
                    raise ValueError(
                        f"Cannot save misaligned {level} vector store: "
                        f"{vector_count} vectors for {len(documents)} documents"
                    )
            level_state[level] = (index, documents)

        config_path = save_path / f"config_{model_suffix}.json"
        save_marker = save_path / f".{config_path.name}.save-in-progress"
        _atomic_json_dump(
            save_marker,
            {"persistence_schema": VECTOR_PERSISTENCE_SCHEMA},
        )
        try:
            level_artifacts: dict[str, dict[str, dict[str, Any]]] = {}
            for level, (_index, documents) in level_state.items():
                if documents:
                    level_artifacts[level] = self._save_level(
                        save_path, level, model_suffix
                    )
                else:
                    self._remove_level_files(save_path, level, model_suffix)

            config = {
                "embedding_model": self.embedding_model,
                "embedding_provider": self.embedding_provider,
                "dimension": self.dimension,
                "index_type": self.index_type,
                "index_metric": self.index_metric,
                "l0_documents": len(self.l0_documents),
                "l2_documents": len(self.l2_documents),
                "persistence_schema": VECTOR_PERSISTENCE_SCHEMA,
                "level_artifacts": level_artifacts,
            }
            if self.artifact_metadata:
                config["artifact"] = self.artifact_metadata
            # This config is the commit record for both levels. Publishing it
            # last makes an interrupted multi-file save detectable on load.
            _atomic_json_dump(config_path, config)
        except Exception:
            # Keep the marker so a later load cannot accept a partially
            # replaced legacy artifact. A successful retry removes it.
            raise
        else:
            save_marker.unlink()

        logger.info("Vector store saved successfully")

    @staticmethod
    def _remove_level_files(save_path: Path, level: str, model_suffix: str) -> None:
        """Remove persisted files when a vector level becomes empty."""

        level_path = save_path / level
        for name in (
            f"config_{model_suffix}.json",
            f"index_{model_suffix}.faiss",
            f"documents_{model_suffix}.pkl",
            f"documents_{model_suffix}.json",
            f"index_{model_suffix}.pkl",
        ):
            (level_path / name).unlink(missing_ok=True)
        try:
            level_path.rmdir()
        except OSError:
            pass

    def _save_level(
        self, save_path: Path, level: str, model_suffix: str
    ) -> dict[str, dict[str, Any]]:
        """Save a single level (l0 or l2) to disk."""
        index, documents = self._get_index_and_docs(level)

        level_path = save_path / level
        level_path.mkdir(parents=True, exist_ok=True)

        # Write raw FAISS index
        index_name = f"index_{model_suffix}"
        index_path = level_path / f"{index_name}.faiss"
        _atomic_replace(index_path, lambda path: faiss.write_index(index, str(path)))

        # Save documents (list of _Document)
        docs_path = level_path / f"documents_{model_suffix}.pkl"
        _atomic_pickle_dump(docs_path, documents)
        artifacts = vector_level_artifact_records(
            level_path,
            model_suffix,
            documents_file=docs_path.name,
        )

        # Save level config
        config_path = level_path / f"config_{model_suffix}.json"
        config = {
            "embedding_model": self.embedding_model,
            "embedding_provider": self.embedding_provider,
            "dimension": self.dimension,
            "index_type": self.index_type,
            "index_metric": self.index_metric,
            "level": level,
            "num_documents": len(documents),
        }
        _atomic_json_dump(config_path, config)

        logger.info(f"Saved {level.upper()} store with {len(documents)} documents")
        return artifacts

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
        save_marker = load_path / f".config_{model_suffix}.json.save-in-progress"
        if save_marker.exists():
            raise ValueError(
                f"Vector store has an interrupted save marker: {save_marker}"
            )

        expected_artifact = dict(self.artifact_metadata)
        expected_config = expected_artifact.get("persistence_config_fingerprint")
        if expected_config is not None:
            config_path = validate_vector_config_artifact(
                load_path,
                model_suffix,
                expected_config,
            )
        loaded_artifact = expected_artifact
        expected_counts: Dict[str, Optional[int]] = {"l0": None, "l2": None}
        committed_levels: Optional[dict[str, object]] = None
        if config_path.exists():
            with open(config_path, "r") as f:
                config = json.load(f)

            saved_model = config.get("embedding_model")
            if saved_model is not None and saved_model != self.embedding_model:
                raise ValueError(
                    f"Vector config model mismatch: expected {self.embedding_model!r}, "
                    f"found {saved_model!r}"
                )
            saved_provider = config.get("embedding_provider")
            if saved_provider is not None and normalize_provider(
                saved_provider
            ) != normalize_provider(self.embedding_provider):
                raise ValueError(
                    "Vector config provider mismatch: expected "
                    f"{self.embedding_provider!r}, found {saved_provider!r}"
                )
            saved_dimension = config.get("dimension")
            if saved_dimension is not None and saved_dimension != self.dimension:
                raise ValueError(
                    f"Vector config dimension mismatch: expected {self.dimension}, "
                    f"found {saved_dimension}"
                )
            saved_index_type = config.get("index_type")
            if saved_index_type and saved_index_type != self.index_type:
                raise ValueError(
                    f"Vector config index type mismatch: expected {self.index_type!r}, "
                    f"found {saved_index_type!r}"
                )
            saved_metric = config.get("index_metric")
            if saved_metric and saved_metric != self.index_metric:
                raise ValueError(
                    f"Vector config metric mismatch: expected {self.index_metric!r}, "
                    f"found {saved_metric!r}"
                )
            saved_artifact = config.get("artifact")
            if isinstance(saved_artifact, dict):
                expected_fingerprint = expected_artifact.get("embedding_fingerprint")
                saved_fingerprint = saved_artifact.get("embedding_fingerprint")
                if (
                    expected_fingerprint is not None
                    and saved_fingerprint != expected_fingerprint
                ):
                    raise ValueError(
                        "Vector artifact embedding fingerprint does not match manifest"
                    )
                loaded_artifact = dict(saved_artifact)
            elif expected_artifact.get("embedding_fingerprint") is not None:
                raise ValueError("Vector config is missing embedding artifact identity")

            for level in ("l0", "l2"):
                value = config.get(f"{level}_documents")
                if value is None:
                    continue
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(
                        f"Vector config has invalid {level} document count: {value!r}"
                    )
                expected_counts[level] = value

            persistence_schema = config.get("persistence_schema")
            raw_levels = config.get("level_artifacts")
            if persistence_schema is not None or raw_levels is not None:
                if persistence_schema != VECTOR_PERSISTENCE_SCHEMA:
                    raise ValueError(
                        "Vector config has unsupported persistence schema: "
                        f"{persistence_schema!r}"
                    )
                if not isinstance(raw_levels, dict) or not set(raw_levels) <= {
                    "l0",
                    "l2",
                }:
                    raise ValueError("Vector config has invalid committed levels")
                committed_levels = dict(raw_levels)
                if any(expected_counts[level] is None for level in ("l0", "l2")):
                    raise ValueError(
                        "Vector config with committed artifacts requires level counts"
                    )
        elif expected_artifact.get("embedding_fingerprint") is not None:
            raise ValueError("Vector store is missing its top-level configuration")

        loaded_levels = {}
        for level in ("l0", "l2"):
            expected_count = expected_counts[level]
            level_path = load_path / level
            faiss_path = level_path / f"index_{model_suffix}.faiss"
            committed_artifacts = (
                committed_levels.get(level) if committed_levels is not None else None
            )

            # A zero count in the top-level config is authoritative. Older
            # writers could leave stale level files behind after deletions.
            if expected_count == 0:
                if committed_artifacts is not None:
                    raise ValueError(
                        f"Vector config commits artifacts for empty {level} level"
                    )
                loaded_levels[level] = (self._build_faiss_index(), [])
                continue

            if (
                committed_levels is not None
                and expected_count is not None
                and expected_count > 0
                and committed_artifacts is None
            ):
                raise ValueError(
                    f"Vector config is missing committed artifacts for {level}"
                )

            if committed_artifacts is not None or faiss_path.exists():
                index, documents = self._load_level(
                    level_path,
                    model_suffix,
                    committed_artifacts=committed_artifacts,
                )
            elif expected_count is not None and expected_count > 0:
                raise FileNotFoundError(
                    f"Vector config expects {expected_count} {level} documents, "
                    f"but {faiss_path} is missing"
                )
            else:
                index, documents = self._build_faiss_index(), []

            if expected_count is not None and len(documents) != expected_count:
                raise ValueError(
                    f"{level} config expects {expected_count} documents, "
                    f"loaded {len(documents)}"
                )
            loaded_levels[level] = (index, documents)

        old_indices = (self.l0_index, self.l2_index)
        self.l0_index, self.l0_documents = loaded_levels["l0"]
        self.l2_index, self.l2_documents = loaded_levels["l2"]
        self.artifact_metadata = loaded_artifact
        self.store_path = load_path

        for old_index in old_indices:
            if (
                old_index is None
                or old_index is self.l0_index
                or old_index is self.l2_index
            ):
                continue
            reset = getattr(old_index, "reset", None)
            if callable(reset):
                try:
                    reset()
                except Exception as exc:
                    logger.debug("Could not release replaced FAISS index: %s", exc)

        for level, (_index, documents) in loaded_levels.items():
            if documents:
                logger.info(
                    "Loaded %s store with %d documents",
                    level.upper(),
                    len(documents),
                )

        total_docs = len(self.l0_documents) + len(self.l2_documents)
        logger.info(
            f"Vector store loaded successfully with {total_docs} total documents "
            f"(L0: {len(self.l0_documents)}, L2: {len(self.l2_documents)})"
        )

    def _load_level(
        self,
        level_path: Path,
        model_suffix: str,
        *,
        committed_artifacts: object = None,
    ) -> tuple[faiss.Index, List[_Document]]:
        """Load a single level from disk.

        Handles both the new format (raw FAISS + _Document list) and the
        legacy LangChain format (FAISS + docstore pkl) transparently.
        """
        index_name = f"index_{model_suffix}"
        committed_documents_path = None
        if committed_artifacts is not None:
            faiss_path, committed_documents_path = validate_vector_level_artifacts(
                level_path,
                model_suffix,
                committed_artifacts,
            )
        else:
            faiss_path = level_path / f"{index_name}.faiss"

        if not faiss_path.exists():
            raise FileNotFoundError(f"FAISS index not found at {faiss_path}")

        try:
            index = faiss.read_index(str(faiss_path))
        except Exception as e:
            raise ValueError(
                f"Could not load FAISS index from {faiss_path}: {e}"
            ) from e
        if int(index.d) != self.dimension:
            raise ValueError(
                f"FAISS dimension mismatch at {faiss_path}: "
                f"expected {self.dimension}, found {int(index.d)}"
            )

        # Portable artifacts use inert JSON so a downloaded document store is
        # never unpickled. Local indexes retain the pickle fallback for
        # compatibility with previously built artifacts.
        json_path = level_path / f"documents_{model_suffix}.json"
        if committed_documents_path is not None:
            if committed_documents_path.suffix == ".json":
                documents = self._load_documents_json(committed_documents_path)
            else:
                try:
                    with committed_documents_path.open("rb") as handle:
                        raw_docs = compat_pickle.load(handle)
                    documents = [_to_document(document) for document in raw_docs]
                except Exception as exc:
                    raise ValueError(
                        "Could not load committed vector documents from "
                        f"{committed_documents_path}: {exc}"
                    ) from exc
        elif json_path.exists():
            documents = self._load_documents_json(json_path)
        else:
            documents = None

            # Try loading the local documents pickle (works for both new
            # _Document and legacy LangChain Document objects).
            docs_path = level_path / f"documents_{model_suffix}.pkl"
            if docs_path.exists():
                try:
                    with open(docs_path, "rb") as f:
                        raw_docs = compat_pickle.load(f)
                    documents = [_to_document(d) for d in raw_docs]
                except Exception as exc:
                    logger.warning(
                        "Could not load documents from %s: %s", docs_path, exc
                    )

            # Fallback: LangChain stores use index_name.pkl for their docstore.
            lc_pkl_path = level_path / f"{index_name}.pkl"
            if documents is None and lc_pkl_path.exists():
                try:
                    documents = self._load_langchain_pkl(lc_pkl_path)
                    logger.info(
                        "Loaded %d documents from legacy LangChain format",
                        len(documents),
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not load legacy LangChain pkl from %s: %s",
                        lc_pkl_path,
                        exc,
                    )

            if documents is None:
                if int(index.ntotal) == 0:
                    documents = []
                else:
                    raise ValueError(
                        f"No readable document store found for {level_path}"
                    )

        if int(index.ntotal) != len(documents):
            raise ValueError(
                f"Misaligned vector level {level_path}: {int(index.ntotal)} vectors "
                f"for {len(documents)} documents"
            )
        return index, documents

    @staticmethod
    def _load_documents_json(path: Path) -> List[_Document]:
        """Load the non-executable portable vector document format."""

        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list):
            raise ValueError(f"vector documents must be a JSON list: {path}")

        documents: List[_Document] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ValueError(
                    f"vector document {index} must be a JSON object: {path}"
                )
            page_content = item.get("page_content")
            metadata = item.get("metadata")
            if not isinstance(page_content, str) or not isinstance(metadata, dict):
                raise ValueError(
                    f"vector document {index} has invalid content or metadata: {path}"
                )
            documents.append(
                _Document(page_content=page_content, metadata=dict(metadata))
            )
        return documents

    @staticmethod
    def _load_langchain_pkl(pkl_path: Path) -> List[_Document]:
        """Extract documents from a LangChain FAISS pkl file.

        The pkl file contains ``(InMemoryDocstore, index_to_docstore_id)``
        where ``index_to_docstore_id`` maps integer FAISS indices to
        docstore string IDs.
        """
        with open(pkl_path, "rb") as f:
            docstore, index_to_docstore_id = compat_pickle.load(f)

        documents: List[_Document] = []
        for i in sorted(index_to_docstore_id.keys()):
            doc_id = index_to_docstore_id[i]
            doc = docstore.search(doc_id)
            if doc and hasattr(doc, "page_content"):
                documents.append(_to_document(doc))
        return documents

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

    def get_embeddings_by_content_hash(
        self, level: Level = "l2"
    ) -> Dict[str, np.ndarray]:
        """
        Extract raw embedding vectors from the FAISS index, keyed by content hash.

        This is used to seed the ``EmbeddingsCache`` after a full build so that
        the first incremental update achieves ~100% cache hit rate for unchanged
        chunks.

        Each document's content is MD5-hashed to produce the key.  If the
        document metadata already contains a ``content_hash`` field it is used
        directly; otherwise the hash is computed on the fly.

        Returns:
            Dict mapping content_hash → np.ndarray (float32 vectors).
        """
        index, documents = self._get_index_and_docs(level)
        if not documents or index is None or index.ntotal == 0:
            return {}

        result: Dict[str, np.ndarray] = {}
        for i, doc in enumerate(documents):
            content_hash = doc.metadata.get("content_hash")
            if content_hash is None:
                content_hash = hashlib.md5(
                    doc.page_content.encode("utf-8", errors="replace")
                ).hexdigest()

            vec = index.reconstruct(i)
            result[content_hash] = np.asarray(vec, dtype=np.float32)

        logger.info(
            "Extracted %d embedding vectors from %s FAISS index for cache seeding.",
            len(result),
            level,
        )
        return result

    def rebuild_from_embeddings(
        self,
        documents: list,
        embeddings: List[np.ndarray],
        level: Level = "l2",
    ) -> None:
        """
        Clear *level* and rebuild its FAISS index from pre-computed embeddings.

        Used by the incremental update path: unchanged chunks contribute their
        cached vectors, so only genuinely new/modified chunks require model
        inference.  No embedding model calls are made by this method.

        Args:
            documents: Document-like objects with ``page_content`` and
                ``metadata`` attributes (``_Document`` or compatible).
            embeddings: Corresponding embedding vectors as ``np.ndarray``
                (shape ``[dim]``, dtype ``float32``).
            level: Which index level to rebuild (``"l0"`` or ``"l2"``).

        Raises:
            ValueError: If *documents* and *embeddings* have different lengths.
        """
        if len(documents) != len(embeddings):
            raise ValueError(
                f"documents ({len(documents)}) and embeddings ({len(embeddings)}) "
                "must have the same length."
            )

        # Wipe the existing index for this level
        self.clear(level)

        if not documents:
            logger.debug(
                "rebuild_from_embeddings: no documents; level %s cleared.", level
            )
            return

        # Convert to _Document if needed and add vectors to the raw FAISS index
        native_docs = [_to_document(d) for d in documents]
        vectors = np.array(
            [
                emb if isinstance(emb, np.ndarray) else np.asarray(emb)
                for emb in embeddings
            ],
            dtype=np.float32,
        )

        self._add_to_index(level, vectors)
        if level == "l0":
            self.l0_documents = native_docs
        else:
            self.l2_documents = native_docs

        logger.info(
            "rebuild_from_embeddings: %s index rebuilt with %d documents.",
            level,
            len(documents),
        )

    def delta_update(
        self,
        all_documents: list,
        all_embeddings: List[np.ndarray],
        changed_content_hashes: Set[str],
        level: Level = "l2",
        threshold: float = 0.1,
    ) -> None:
        """
        Patch the FAISS index in place when the change set is small.

        When the fraction of changed chunks is below *threshold*, this uses
        ``IndexFlat.remove_ids`` + ``add`` to modify only the affected rows,
        keeping unchanged vectors and their aligned documents untouched.  If
        the change ratio exceeds the threshold (or the index is empty), it
        falls back to a full rebuild via :meth:`rebuild_from_embeddings`.

        Args:
            all_documents: The complete desired set of documents for *level*
                after the update.  Must carry ``content_hash`` in metadata.
            all_embeddings: Corresponding embedding vectors, aligned with
                *all_documents*.
            changed_content_hashes: Content hashes of chunks that were
                added, removed, or modified in this update cycle.  Used both
                to decide between delta/rebuild and to identify stale rows.
            level: Which index level to update.
            threshold: Maximum change ratio (changed/total) for the delta
                path; above this a full rebuild is performed.
        """
        total = len(all_documents)

        if total == 0:
            self.clear(level)
            return

        index, current_docs = self._get_index_and_docs(level)
        change_ratio = len(changed_content_hashes) / total

        # Fall back to full rebuild when the delta path can't help.
        if (
            index is None
            or index.ntotal == 0
            or not current_docs
            or change_ratio > threshold
        ):
            logger.info(
                "delta_update: %d/%d changed (%.0f%%) → full rebuild of %s.",
                len(changed_content_hashes),
                total,
                change_ratio * 100,
                level,
            )
            self.rebuild_from_embeddings(all_documents, all_embeddings, level=level)
            return

        # --- Delta path: in-place patch -------------------------------
        # Use a list per hash so duplicate-content docs (same code in
        # different files) are all preserved.
        from collections import defaultdict

        target_by_hash: Dict[str, List[Tuple[object, np.ndarray]]] = defaultdict(list)
        for doc, emb in zip(all_documents, all_embeddings, strict=True):
            ch = doc.metadata.get("content_hash")
            if ch is None:
                # Can't align by hash → safest to rebuild.
                logger.warning(
                    "delta_update: target doc missing content_hash → full rebuild."
                )
                self.rebuild_from_embeddings(all_documents, all_embeddings, level=level)
                return
            target_by_hash[ch].append((doc, emb))

        current_hashes = [d.metadata.get("content_hash") for d in current_docs]

        # For each hash, allow at most target-count survivors (handles
        # both duplicate-content additions and removals correctly).
        target_avail: Dict[str, int] = {h: len(v) for h, v in target_by_hash.items()}
        rows_to_remove: List[int] = []
        for i, h in enumerate(current_hashes):
            if (
                h is None
                or h not in target_avail
                or h in changed_content_hashes
                or target_avail[h] <= 0
            ):
                rows_to_remove.append(i)
            else:
                target_avail[h] -= 1

        # Unclaimed target entries become additions.
        docs_to_add: List[Tuple[object, np.ndarray]] = []
        for h, entries in target_by_hash.items():
            claimed = len(entries) - target_avail.get(h, 0)
            docs_to_add.extend(entries[claimed:])

        if rows_to_remove:
            selector = faiss.IDSelectorBatch(np.array(rows_to_remove, dtype=np.int64))
            index.remove_ids(selector)

        # Survivors: prefer the fresh target doc (same content_hash) so that
        # pure metadata changes — file rename, start_line shift, name edit —
        # are reflected without requiring a full rebuild.  The vector is
        # identical because content_hash is identical, so no FAISS op needed.
        remove_set = set(rows_to_remove)
        survivor_idx: Dict[str, int] = {}
        new_docs_list: List[_Document] = []
        for i, d in enumerate(current_docs):
            if i in remove_set:
                continue
            h = current_hashes[i]
            idx = survivor_idx.get(h, 0)
            survivor_idx[h] = idx + 1
            entries = target_by_hash.get(h)
            if entries and idx < len(entries):
                new_docs_list.append(_to_document(entries[idx][0]))
            else:
                new_docs_list.append(d)

        if docs_to_add:
            add_vectors = np.array(
                [np.asarray(e, dtype=np.float32) for _, e in docs_to_add],
                dtype=np.float32,
            )
            self._add_to_index(level, add_vectors)
            new_docs_list.extend(_to_document(d) for d, _ in docs_to_add)

        if level == "l0":
            self.l0_documents = new_docs_list
        else:
            self.l2_documents = new_docs_list

        logger.info(
            "delta_update: %s patched in place — removed %d, added %d "
            "(ntotal=%d, %.0f%% changed).",
            level,
            len(rows_to_remove),
            len(docs_to_add),
            index.ntotal,
            change_ratio * 100,
        )

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
            self.l0_documents = []

        if level is None or level == "l2":
            logger.info("Clearing L2 vector store")
            self.l2_index = self._build_faiss_index()
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
