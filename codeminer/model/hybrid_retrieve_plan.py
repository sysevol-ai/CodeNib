#!/usr/bin/env python3
"""
Hybrid embedding pipeline that pairs hybrid chunking with two embedding models.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..code_chunker import HybridChunkingConfig
from ..index.embedding import CodeVectorStore, build_hybrid_vector_stores
from ..log_utils import get_logger

logger = get_logger(__name__)


@dataclass
class HybridEmbeddingConfig:
    """
    Configuration for hybrid embedding pipeline.
    """

    repo_path: str
    index_path: Path

    primary_embedding_model: str
    primary_embedding_provider: str = "openai"
    primary_embedding_dimension: Optional[int] = 1536
    primary_embedding_kwargs: Dict[str, object] = field(default_factory=dict)

    secondary_embedding_model: str = "text-embedding-3-small"
    secondary_embedding_provider: str = "openai"
    secondary_embedding_dimension: Optional[int] = 1536
    secondary_embedding_kwargs: Dict[str, object] = field(default_factory=dict)

    # Chunking
    max_lines_per_chunk: Optional[int] = 100
    chunk_depth: int = 2
    languages: Optional[List[str]] = None

    # Hybrid chunking params
    hybrid_chunking: HybridChunkingConfig = field(default_factory=HybridChunkingConfig)


def build_hybrid_embeddings(
    config: HybridEmbeddingConfig,
) -> Dict[str, CodeVectorStore]:
    """
    Materialize primary/secondary vector stores using hybrid chunking.
    """
    languages = config.languages or ["python"]
    logger.info(
        "Building hybrid embeddings",
        extra={
            "repo_path": config.repo_path,
            "index_path": str(config.index_path),
            "languages": languages,
            "primary_model": config.primary_embedding_model,
            "secondary_model": config.secondary_embedding_model,
        },
    )

    return build_hybrid_vector_stores(
        repo_path=config.repo_path,
        index_path=str(config.index_path),
        languages=languages,
        chunk_depth=config.chunk_depth,
        max_lines_per_chunk=config.max_lines_per_chunk,
        hybrid_config=config.hybrid_chunking,
        primary_embedding_model=config.primary_embedding_model,
        primary_embedding_provider=config.primary_embedding_provider,
        primary_embedding_dimension=config.primary_embedding_dimension,
        primary_embedding_kwargs=config.primary_embedding_kwargs,
        secondary_embedding_model=config.secondary_embedding_model,
        secondary_embedding_provider=config.secondary_embedding_provider,
        secondary_embedding_dimension=config.secondary_embedding_dimension,
        secondary_embedding_kwargs=config.secondary_embedding_kwargs,
    )
