#!/usr/bin/env python3
"""Reusable embedding builders for hierarchical pipelines."""

from pathlib import Path
from typing import Dict, List, Optional

from ...code_chunker import CodeChunker, RepoChunkingConfig
from ...log_utils import get_logger
from ...profiler import Profiler
from .vector_store import CodeVectorStore

logger = get_logger(__name__)


def build_hierarchical_vector_store(
    *,
    repo_path: str,
    index_path: str,
    plan_name: Optional[str] = None,
    languages: Optional[List[str]] = None,
    max_lines_per_chunk: Optional[int] = None,
    build_levels: Optional[List[str]] = None,
    embedding_model: str,
    embedding_provider: str,
    embedding_dimension: Optional[int],
    embedding_kwargs: Optional[Dict[str, object]] = None,
    index_metric: str = "ip",
    profiler: Optional[Profiler] = None,
) -> CodeVectorStore:
    """Build a hierarchical vector store (L0/L2) for a repository."""
    languages = languages or ["python"]
    build_levels = [level.lower() for level in (build_levels or ["l0", "l2"])]
    repo_cfg = RepoChunkingConfig(languages=languages)

    chunks_by_level = {}
    level_configs = {
        "l0": dict(
            chunker_kwargs=dict(
                language=languages[0],
                repo_config=repo_cfg,
                max_lines_per_chunk=None,
                chunk_depth=0,
                skeleton_mode=True,
            )
        ),
        "l2": dict(
            chunker_kwargs=dict(
                language=languages[0],
                repo_config=repo_cfg,
                max_lines_per_chunk=max_lines_per_chunk,
                chunk_depth=2,
                l2_level_exclusive=True,
                skeleton_mode=False,
            )
        ),
    }

    for level in build_levels:
        cfg = level_configs.get(level)
        if not cfg:
            continue
        chunker = CodeChunker(**cfg["chunker_kwargs"])
        chunks_by_level[level] = chunker.chunk_repository(repo_path=repo_path)

    l0_chunks = chunks_by_level.get("l0", [])
    l2_chunks = chunks_by_level.get("l2", [])

    if not l0_chunks and not l2_chunks:
        raise ValueError("No code chunks generated from repository.")

    store_path = Path(index_path)
    if plan_name:
        store_path = store_path / plan_name
    store_path.mkdir(parents=True, exist_ok=True)
    vector_store = CodeVectorStore(
        embedding_model=embedding_model,
        embedding_provider=embedding_provider,
        dimension=embedding_dimension,
        index_metric=index_metric,
        store_path=str(store_path),
        profiler=profiler,
        **(embedding_kwargs or {}),
    )

    if l0_chunks:
        vector_store.add_code_chunks(
            [chunk._asdict() for chunk in l0_chunks], level="l0"
        )
    if l2_chunks:
        vector_store.add_code_chunks(
            [chunk._asdict() for chunk in l2_chunks], level="l2"
        )

    vector_store.save(str(store_path))
    return vector_store


class VectorStoreBuilder:
    """Builder class wrapping common vector store build operations."""

    def __init__(self, profiler: Optional[Profiler] = None) -> None:
        self.profiler = profiler

    def hierarchical(self, **kwargs) -> CodeVectorStore:
        return build_hierarchical_vector_store(profiler=self.profiler, **kwargs)
