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
    max_lines_per_chunk: Optional[int] = 100,
    hybrid_config: Optional[HybridChunkingConfig] = None,
    hybrid_levels: Optional[List[str]] = None,
    shared_levels: Optional[List[str]] = None,
    plan_name: str = "hybrid",
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

    Hybrid chunking can be limited to specific levels (e.g., only L2), while
    other levels (e.g., L0 file skeletons) can be added to the primary store.

    Returns:
        Mapping ``{\"primary\": CodeVectorStore, \"secondary\": CodeVectorStore}``.
    """
    repo_path = str(Path(repo_path).resolve())
    languages = languages or ["python"]
    hybrid_levels = [lvl.lower() for lvl in (hybrid_levels or ["l2"])]
    shared_levels = [lvl.lower() for lvl in (shared_levels or [])]

    base_path = Path(index_path)
    if plan_name:
        base_path = base_path / plan_name

    level_configs = {
        "l0": dict(
            chunker_kwargs=dict(
                language=languages[0],
                repo_config=RepoChunkingConfig(languages=languages),
                max_lines_per_chunk=None,
                chunk_depth=0,
                skeleton_mode=True,
            )
        ),
        "l2": dict(
            chunker_kwargs=dict(
                language=languages[0],
                repo_config=RepoChunkingConfig(languages=languages),
                max_lines_per_chunk=max_lines_per_chunk,
                chunk_depth=2,
                l2_level_exclusive=True,
                skeleton_mode=False,
            )
        ),
    }

    hybrid_cfg = hybrid_config or HybridChunkingConfig()

    if not hybrid_levels:
        raise ValueError("hybrid_levels must include at least one level (e.g., ['l2'])")

    primary_path = base_path / "primary"
    secondary_path = base_path / "secondary"
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

    hybrid_counts = {"primary": 0, "secondary": 0}

    for level in hybrid_levels:
        if level not in level_configs:
            logger.warning("Unsupported hybrid level %s; skipping", level)
            continue
        chunker_cfg = level_configs[level]["chunker_kwargs"]
        chunker = CodeChunker(**chunker_cfg)
        hybrid_result = chunker.hybrid_chunk_repository(
            repo_path, hybrid_config=hybrid_cfg
        )

        if hybrid_result.primary_chunks:
            primary_store.add_code_chunks(hybrid_result.primary_chunks, level=level)
            hybrid_counts["primary"] += len(hybrid_result.primary_chunks)
        else:
            logger.warning(
                "Hybrid chunker produced no primary chunks at level %s", level
            )

        if hybrid_result.secondary_chunks:
            secondary_store.add_code_chunks(hybrid_result.secondary_chunks, level=level)
            hybrid_counts["secondary"] += len(hybrid_result.secondary_chunks)
        else:
            logger.warning(
                "Hybrid chunker produced no secondary chunks at level %s", level
            )

    # Add any shared (non-hybrid) levels to the primary store
    for level in shared_levels:
        if level not in level_configs:
            logger.warning("Unsupported shared level %s; skipping", level)
            continue
        chunker_cfg = level_configs[level]["chunker_kwargs"]
        chunker = CodeChunker(**chunker_cfg)
        chunks = chunker.chunk_repository(repo_path=repo_path)
        if not chunks:
            logger.warning("No chunks generated for shared level %s", level)
            continue
        primary_store.add_code_chunks(
            [chunk._asdict() for chunk in chunks], level=level
        )

    primary_store.save(str(primary_path))
    secondary_store.save(str(secondary_path))

    logger.info(
        "Built hybrid vector stores",
        extra={
            "primary_chunks": hybrid_counts["primary"],
            "secondary_chunks": hybrid_counts["secondary"],
            "shared_levels": shared_levels,
            "hybrid_levels": hybrid_levels,
            "index_path": str(index_path),
        },
    )

    return {"primary": primary_store, "secondary": secondary_store}


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
    """
    Build a hierarchical vector store (L0/L2) for a repository.
    """
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

    def hybrid(self, **kwargs) -> Dict[str, CodeVectorStore]:
        return build_hybrid_vector_stores(profiler=self.profiler, **kwargs)
