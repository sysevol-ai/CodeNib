# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import List, Optional

from ..index.embedding import CodeVectorStore, build_hierarchical_vector_store
from ..log_utils import get_logger
from ..types import QueriedNode

logger = get_logger(__name__)


class EmbeddingRetrievePipeline:
    """Embedding-only retrieval pipeline.

    Typical single-instance usage (loads or builds index immediately)::

        pipeline = EmbeddingRetrievePipeline(
            repo_path=repo_path,
            index_path=index_path,
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
            embedding_dimension=1024,
        )
        results = pipeline.query(problem_statement)
        pipeline.close()

    Multi-instance / hot-swap usage (load model once, swap index per instance)::

        pipeline = EmbeddingRetrievePipeline(
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
            embedding_dimension=1024,
        )
        for instance in dataset:
            pipeline.load_index(index_path_for_instance)
            results = pipeline.query(instance["problem_statement"])
        pipeline.close()

    Args:
        repo_path: Repository root to index. Only required when
            ``force_rebuild=True``; ignored when loading a pre-built index.
        index_path: Directory used for vector index caches. When ``None``,
            the pipeline is initialised with an empty index so that
            :meth:`load_index` can be called later (hot-swap pattern).
        embedding_model: Embedding model name.
        embedding_provider: Embedding provider (``"huggingface"`` or
            ``"openai"``).
        embedding_dimension: Embedding vector dimension.
        embedding_revision: Optional Hugging Face model revision.
        trust_remote_code: Whether to execute Hugging Face model repository
            code. Remote models require an immutable commit revision.
        languages: Languages to chunk for indexing (default: ``["python"]``).
        max_lines_per_chunk: Maximum lines per chunk.
        top_k: Default number of results to retrieve.
        embedding_kwargs: Extra kwargs forwarded to the embedding wrapper.
        force_rebuild: Re-chunk and re-embed even if a cached index exists.
    """

    def __init__(
        self,
        repo_path: Optional[str] = None,
        index_path: Optional[str] = None,
        *,
        embedding_model: str = "nomic-ai/CodeRankEmbed",
        embedding_provider: str = "huggingface",
        embedding_dimension: int = 768,
        embedding_revision: Optional[str] = None,
        trust_remote_code: Optional[bool] = None,
        languages: Optional[List[str]] = None,
        max_lines_per_chunk: int = 300,
        top_k: int = 50,
        embedding_kwargs: Optional[dict] = None,
        force_rebuild: bool = False,
        index_type: str = "flat",
        ivf_nlist: int = 100,
        ivf_nprobe: int = 8,
    ) -> None:
        self.top_k = top_k
        _embedding_kwargs = dict(embedding_kwargs or {})
        if embedding_provider.lower() == "huggingface":
            if embedding_revision is not None:
                _embedding_kwargs["revision"] = embedding_revision
            if trust_remote_code is not None:
                _embedding_kwargs["trust_remote_code"] = trust_remote_code
        elif embedding_revision is not None or trust_remote_code is not None:
            raise ValueError(
                "embedding_revision and trust_remote_code require the "
                "huggingface embedding provider"
            )

        if index_path is None:
            # Model-only initialisation — no index loaded yet.
            # Callers should follow up with load_index().
            self.vector_store = CodeVectorStore(
                embedding_model=embedding_model,
                embedding_provider=embedding_provider,
                dimension=embedding_dimension,
                index_metric="ip",
                index_type=index_type,
                ivf_nlist=ivf_nlist,
                ivf_nprobe=ivf_nprobe,
                store_path=None,
                **_embedding_kwargs,
            )
        else:
            self.vector_store: CodeVectorStore = build_hierarchical_vector_store(
                repo_path=repo_path or "",
                index_path=index_path,
                plan_name=None,
                languages=languages or ["python"],
                max_lines_per_chunk=max_lines_per_chunk,
                build_levels=["l2"],
                embedding_model=embedding_model,
                embedding_provider=embedding_provider,
                embedding_dimension=embedding_dimension,
                embedding_kwargs=_embedding_kwargs,
                index_metric="ip",
                index_type=index_type,
                ivf_nlist=ivf_nlist,
                ivf_nprobe=ivf_nprobe,
                force_rebuild=force_rebuild,
            )

    def load_index(self, index_path: str) -> None:
        """Hot-swap the FAISS index without reloading the embedding model.

        Validates the pre-built index at *index_path* before releasing the
        current one. The embedding model stays in memory, so this is much
        faster than creating a new :class:`EmbeddingRetrievePipeline` for each
        instance, and a failed replacement leaves the current index available.
        """
        self.vector_store.swap_index(index_path)

    def query(self, query: str, top_k: Optional[int] = None) -> List[QueriedNode]:
        """Retrieve top-K nodes using embedding similarity search.

        Args:
            query: The search query (e.g., problem statement).
            top_k: Number of results; overrides the instance default if provided.

        Returns:
            List of QueriedNode sorted by similarity score.
        """
        k = top_k if top_k is not None else self.top_k
        search_results = self.vector_store.search_with_content(query, top_k=k)
        return [
            QueriedNode(
                node_name=n.node_name,
                type=n.type,
                file=n.file,
                node_id=n.node_id,
                start_line=n.start_line,
                end_line=n.end_line,
                score=n.score,
                content=n.content,
            )
            for n in search_results
        ]

    def close(self) -> None:
        """Release all resources including the embedding model."""
        if self.vector_store is not None:
            self.vector_store.close()
            self.vector_store = None
