#!/usr/bin/env python3
"""
Reusable embedding builders for hierarchical and hybrid pipelines.
"""

from pathlib import Path
from typing import Dict, List, Optional

from ...code_chunker import CodeChunker, HybridChunkingConfig, RepoChunkingConfig
from ...log_utils import get_logger
from ...profiler import Profiler
from .vector_store import CodeVectorStore

logger = get_logger(__name__)


def build_hybrid_vector_stores(
    *,
    repo_path: str,
    index_path: str,
    languages: Optional[List[str]] = None,
    chunk_depth: int = 2,
    max_lines_per_chunk: Optional[int] = 100,
    hybrid_config: Optional[HybridChunkingConfig] = None,
    primary_embedding_model: str,
    primary_embedding_provider: str = "openai",
    primary_embedding_dimension: Optional[int] = 1536,
    primary_embedding_kwargs: Optional[Dict[str, object]] = None,
    secondary_embedding_model: str = "text-embedding-3-small",
    secondary_embedding_provider: str = "openai",
    secondary_embedding_dimension: Optional[int] = 1536,
    secondary_embedding_kwargs: Optional[Dict[str, object]] = None,
    index_metric: str = "ip",
    profiler: Optional[Profiler] = None,
) -> Dict[str, CodeVectorStore]:
    """
    Build two vector stores by routing "primary" chunks to a stronger embedding
    model and "secondary" chunks to a cheaper model using HybridChunkingConfig.

    Returns:
        Mapping ``{\"primary\": CodeVectorStore, \"secondary\": CodeVectorStore}``.
    """
    repo_path = str(Path(repo_path).resolve())
    languages = languages or ["python"]
    chunker = CodeChunker(
        language=languages[0],
        repo_config=RepoChunkingConfig(languages=languages),
        max_lines_per_chunk=max_lines_per_chunk,
        chunk_depth=chunk_depth,
        l2_level_exclusive=True,
        skeleton_mode=False,
    )
    hybrid_cfg = hybrid_config or HybridChunkingConfig()
    hybrid_result = chunker.hybrid_chunk_repository(repo_path, hybrid_config=hybrid_cfg)

    primary_path = Path(index_path) / "primary"
    secondary_path = Path(index_path) / "secondary"
    primary_path.mkdir(parents=True, exist_ok=True)
    secondary_path.mkdir(parents=True, exist_ok=True)

    primary_kwargs = primary_embedding_kwargs or {}
    primary_store = CodeVectorStore(
        embedding_model=primary_embedding_model,
        embedding_provider=primary_embedding_provider,
        dimension=primary_embedding_dimension,
        index_metric=index_metric,
        store_path=str(primary_path),
        profiler=profiler,
        **primary_kwargs,
    )
    secondary_kwargs = secondary_embedding_kwargs or {}
    secondary_store = CodeVectorStore(
        embedding_model=secondary_embedding_model,
        embedding_provider=secondary_embedding_provider,
        dimension=secondary_embedding_dimension,
        index_metric=index_metric,
        store_path=str(secondary_path),
        profiler=profiler,
        **secondary_kwargs,
    )

    if hybrid_result.primary_chunks:
        primary_store.add_code_chunks(hybrid_result.primary_chunks, level="l2")
        primary_store.save(str(primary_path))
    else:
        logger.warning("Hybrid chunker produced no primary chunks.")

    if hybrid_result.secondary_chunks:
        secondary_store.add_code_chunks(hybrid_result.secondary_chunks, level="l2")
        secondary_store.save(str(secondary_path))
    else:
        logger.warning("Hybrid chunker produced no secondary chunks.")

    logger.info(
        "Built hybrid vector stores",
        extra={
            "primary_chunks": len(hybrid_result.primary_chunks),
            "secondary_chunks": len(hybrid_result.secondary_chunks),
            "index_path": str(index_path),
        },
    )

    return {"primary": primary_store, "secondary": secondary_store}
