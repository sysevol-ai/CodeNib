#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable embedding builders for hierarchical pipelines."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...code_chunker import CodeChunker, RepoChunkingConfig
from ...log_utils import get_logger
from ...profiler import Profiler
from .vector_store import CodeVectorStore

if TYPE_CHECKING:
    from ...native_index_authorization import NativeIndexAuthorization

logger = get_logger(__name__)


def _remove_unrequested_model_levels(
    store_path: Path,
    embedding_model: str,
    build_levels: List[str],
) -> None:
    model_suffix = embedding_model.replace("/", "__")
    requested = frozenset(build_levels)
    for level in ("l0", "l2"):
        if level in requested:
            continue
        level_path = store_path / level
        for name in (
            f"config_{model_suffix}.json",
            f"index_{model_suffix}.faiss",
            f"documents_{model_suffix}.pkl",
            f"index_{model_suffix}.pkl",
        ):
            (level_path / name).unlink(missing_ok=True)
        try:
            level_path.rmdir()
        except OSError:
            pass


def _profiler_section(profiler, label, metadata=None):
    """Return an active profiler section context if profiling is enabled."""
    if profiler is None:
        return nullcontext()
    return profiler.section(label, metadata)


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
    embedding: Optional[object] = None,
    index_metric: str = "ip",
    index_type: str = "flat",
    ivf_nlist: int = 100,
    ivf_nprobe: int = 8,
    profiler: Optional[Profiler] = None,
    force_rebuild: bool = False,
    strict_chunking: bool = False,
    artifact_metadata: Optional[Dict[str, Any]] = None,
    native_index_authorization: NativeIndexAuthorization | None = None,
) -> CodeVectorStore:
    """Build (or load) a hierarchical vector store (L0/L2) for a repository.

    When ``force_rebuild=False`` (the default) and a saved index for the
    requested ``embedding_model`` already exists at ``index_path``, the store
    is loaded from disk instead of re-chunking and re-embedding the repository.
    Pass ``force_rebuild=True`` to unconditionally rebuild.
    """
    store_path = Path(index_path)
    if plan_name:
        store_path = store_path / plan_name

    normalized_levels = [level.lower() for level in (build_levels or ["l0", "l2"])]
    model_suffix = embedding_model.replace("/", "__")
    config_path = store_path / f"config_{model_suffix}.json"
    cache_without_authority = (
        not force_rebuild
        and config_path.exists()
        and native_index_authorization is None
    )
    effective_force_rebuild = force_rebuild or cache_without_authority
    if effective_force_rebuild and not repo_path:
        raise ValueError(
            "repo_path is required to rebuild a vector index when no authorized "
            "cache can be loaded"
        )
    if effective_force_rebuild:
        _remove_unrequested_model_levels(
            store_path,
            embedding_model,
            normalized_levels,
        )

    if not effective_force_rebuild:
        if config_path.exists():
            logger.info(
                "Pre-built index found at %s — loading instead of rebuilding "
                "(pass force_rebuild=True to override).",
                store_path,
            )
            vector_store = CodeVectorStore(
                embedding_model=embedding_model,
                embedding_provider=embedding_provider,
                dimension=embedding_dimension,
                index_metric=index_metric,
                index_type=index_type,
                ivf_nlist=ivf_nlist,
                ivf_nprobe=ivf_nprobe,
                store_path=str(store_path),
                profiler=profiler,
                embedding=embedding,
                artifact_metadata=artifact_metadata,
                **(embedding_kwargs or {}),
            )
            vector_store.load(
                str(store_path),
                native_index_authorization=native_index_authorization,
            )
            return vector_store
    elif cache_without_authority:
        logger.warning(
            "Pre-built index found at %s but no external native-index "
            "authorization was supplied; rebuilding from repository source.",
            store_path,
        )

    if not repo_path:
        raise ValueError(
            "repo_path is required to build a vector index when no authorized "
            "cache can be loaded"
        )

    languages = languages or ["python"]
    build_levels = normalized_levels
    repo_cfg = RepoChunkingConfig(languages=languages)

    chunks_by_level = {}
    level_configs = {
        "l0": dict(
            chunker_kwargs=dict(
                language=languages[0],
                repo_config=repo_cfg,
                max_lines_per_chunk=max_lines_per_chunk,
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
        with _profiler_section(
            profiler,
            f"chunking_{level}",
            {"level": level, "language": languages[0]},
        ):
            chunks_by_level[level] = chunker.chunk_repository(
                repo_path=repo_path,
                strict=strict_chunking,
            )
        logger.info(
            f"Chunked {len(chunks_by_level[level])} {level} chunks "
            f"(lang={languages[0]})"
        )

    l0_chunks = chunks_by_level.get("l0", [])
    l2_chunks = chunks_by_level.get("l2", [])

    if not l0_chunks and not l2_chunks:
        raise ValueError("No code chunks generated from repository.")

    # Compute chunk / LOC statistics by counting actual file lines on disk.
    repo = Path(repo_path)
    unique_files = {chunk.file for chunk in (l0_chunks + l2_chunks)}
    loc_by_file: Dict[str, int] = {}
    for rel_path in unique_files:
        try:
            loc_by_file[rel_path] = len(
                (repo / rel_path).read_text(errors="replace").splitlines()
            )
        except OSError:
            pass
    total_loc = sum(loc_by_file.values())
    total_chunks = len(l0_chunks) + len(l2_chunks)
    chunk_stats = {
        "l0_chunks": len(l0_chunks),
        "l2_chunks": len(l2_chunks),
        "total_files": len(loc_by_file),
        "total_loc": total_loc,
        "avg_chunk_loc": round(total_loc / max(total_chunks, 1), 1),
    }

    store_path.mkdir(parents=True, exist_ok=True)
    vector_store = CodeVectorStore(
        embedding_model=embedding_model,
        embedding_provider=embedding_provider,
        dimension=embedding_dimension,
        index_metric=index_metric,
        index_type=index_type,
        ivf_nlist=ivf_nlist,
        ivf_nprobe=ivf_nprobe,
        store_path=str(store_path),
        profiler=profiler,
        embedding=embedding,
        artifact_metadata=artifact_metadata,
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
    vector_store.chunk_stats = chunk_stats
    return vector_store


class VectorStoreBuilder:
    """Builder class wrapping common vector store build operations."""

    def __init__(self, profiler: Optional[Profiler] = None) -> None:
        self.profiler = profiler

    def hierarchical(self, **kwargs) -> CodeVectorStore:
        return build_hierarchical_vector_store(profiler=self.profiler, **kwargs)
