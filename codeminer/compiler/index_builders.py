# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Index builder protocol, registry, and concrete implementations.

The compiler can invoke builders to create or update indexes
when ``ResourcePlan`` indicates they are missing or stale.

Concrete builders wrap existing index infrastructure:
  - ``BM25IndexBuilder``   → ``BM25CodeIndexer``
  - ``VectorIndexBuilder`` → ``build_hierarchical_vector_store``
  - ``SymbolGraphBuilder`` → ``SCIPPythonIndexer``
  / ``SCIPRustIndexer`` / ``SCIPTypeScriptIndexer``
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from .resources import IndexState, IndexStatus

logger = logging.getLogger(__name__)


@runtime_checkable
class IndexBuilder(Protocol):
    """Protocol for index build tools the compiler can invoke."""

    def build(self, scope: str, **kwargs: Any) -> IndexStatus: ...
    def incremental_update(self, scope: str, **kwargs: Any) -> IndexStatus: ...


class IndexBuilderRegistry:
    """Maps index_type names to their builder implementations."""

    def __init__(self) -> None:
        self._builders: Dict[str, IndexBuilder] = {}

    def register(self, index_type: str, builder: IndexBuilder) -> None:
        self._builders[index_type] = builder

    def get(self, index_type: str) -> Optional[IndexBuilder]:
        return self._builders.get(index_type)

    def has(self, index_type: str) -> bool:
        return index_type in self._builders


# ---------------------------------------------------------------------------
# Concrete builders
# ---------------------------------------------------------------------------


@dataclass
class BM25IndexBuilder:
    """Build a BM25 sparse index by wrapping ``BM25CodeIndexer``."""

    languages: List[str] = field(default_factory=lambda: ["python"])
    max_k: int = 128
    max_lines_per_chunk: int = 300

    def build(self, scope: str, **kwargs: Any) -> IndexStatus:
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]

        from ..code_chunker import CodeChunker, RepoChunkingConfig
        from ..index.sparse_idx.bm25_index import BM25CodeIndexer

        primary = self.languages[0] if self.languages else "python"
        chunker = CodeChunker(
            language=primary,
            repo_config=RepoChunkingConfig(languages=self.languages),
            max_lines_per_chunk=self.max_lines_per_chunk,
        )
        chunks = chunker.chunk_repository(repo_path=repo_path)

        indexer = BM25CodeIndexer(chunks=chunks, max_k=self.max_k)
        os.makedirs(output_dir, exist_ok=True)
        indexer.save_index(output_dir)

        return IndexStatus(
            index_type="bm25",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                "file_count": len(chunks),
                "max_k": self.max_k,
                "languages": list(self.languages),
            },
        )

    def incremental_update(self, scope: str, **kwargs: Any) -> IndexStatus:
        return self.build(scope, **kwargs)


@dataclass
class VectorIndexBuilder:
    """Build a hierarchical embedding index (L0/L2)."""

    languages: List[str] = field(default_factory=lambda: ["python"])
    embedding_model: str = "nomic-ai/CodeRankEmbed"
    embedding_provider: str = "huggingface"
    embedding_dimension: int = 768
    embedding_kwargs: Dict[str, Any] = field(default_factory=dict)
    build_levels: List[str] = field(default_factory=lambda: ["l0", "l2"])
    max_lines_per_chunk: int = 300
    index_metric: str = "ip"

    def build(self, scope: str, **kwargs: Any) -> IndexStatus:
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]

        from pathlib import Path

        from ..index.embedding.builders import build_hierarchical_vector_store
        from ..index.incremental import (
            EmbeddingsCache,
            IncrementalChunkStore,
            IncrementalState,
        )

        os.makedirs(output_dir, exist_ok=True)
        vs = build_hierarchical_vector_store(
            repo_path=repo_path,
            index_path=output_dir,
            plan_name=None,
            languages=self.languages,
            max_lines_per_chunk=self.max_lines_per_chunk,
            build_levels=self.build_levels,
            embedding_model=self.embedding_model,
            embedding_provider=self.embedding_provider,
            embedding_dimension=self.embedding_dimension,
            embedding_kwargs=self.embedding_kwargs,
            index_metric=self.index_metric,
        )

        doc_count = {}
        if hasattr(vs, "l0_documents") and vs.l0_documents:
            doc_count["l0"] = len(vs.l0_documents)
        if hasattr(vs, "l2_documents") and vs.l2_documents:
            doc_count["l2"] = len(vs.l2_documents)

        # Seed the incremental state so future incremental_update() calls work.
        # We need the current HEAD commit and the L2 chunks that were just built.
        try:
            import subprocess

            result_git = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
            head_commit = result_git.stdout.strip()
        except Exception:
            head_commit = ""

        # Rebuild IncrementalChunkStore from documents that were just embedded
        from ..code_chunking.base import CodeChunk

        def _docs_to_chunks(docs):
            chunks = []
            for doc in docs:
                if not hasattr(doc, "page_content"):
                    continue
                chunks.append(
                    CodeChunk(
                        content=doc.page_content,
                        start_line=doc.metadata.get("start_line", 0),
                        end_line=doc.metadata.get("end_line", 0),
                        chunk_type=doc.metadata.get("chunk_type", "unknown"),
                        name=doc.metadata.get("name", ""),
                        file=doc.metadata.get("file", ""),
                        node_id=doc.metadata.get("node_id", ""),
                    )
                )
            return chunks

        chunk_store = IncrementalChunkStore()
        emb_cache = EmbeddingsCache()

        if vs.l2_documents:
            l2_chunks = _docs_to_chunks(vs.l2_documents)
            chunk_store = IncrementalChunkStore.from_chunks(
                l2_chunks, head_commit, level="l2"
            )
            # Seed L2 embeddings cache
            hash_to_vec = vs.get_embeddings_by_content_hash(level="l2")
            for content_hash, vec in hash_to_vec.items():
                emb_cache.put(content_hash, vec)

        if vs.l0_documents:
            l0_chunks = _docs_to_chunks(vs.l0_documents)
            chunk_store.add_chunks(l0_chunks, head_commit, level="l0")
            # Seed L0 embeddings cache
            hash_to_vec = vs.get_embeddings_by_content_hash(level="l0")
            for content_hash, vec in hash_to_vec.items():
                emb_cache.put(content_hash, vec)

        chunk_store.save(Path(output_dir) / "chunk_store.pkl")
        logger.info(
            "Seeded embeddings cache with %d vectors from initial build.",
            emb_cache.size(),
        )
        emb_cache.save(Path(output_dir) / "embeddings_cache.pkl")

        # Persist incremental state so callers don't need to track last_commit
        inc_state = IncrementalState(
            last_commit=head_commit,
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path=output_dir,
            build_levels=list(self.build_levels),
        )
        inc_state.save(Path(output_dir))

        return IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                "embedding_model": self.embedding_model,
                "levels": list(self.build_levels),
                "document_count": doc_count,
                "last_commit": head_commit,
            },
        )

    def incremental_update(self, scope: str, **kwargs: Any) -> IndexStatus:
        """
        Update the vector index incrementally using git diff detection.

        Required kwargs
        ---------------
        repo_path : str
            Absolute path to the repository root.
        output_dir : str
            Directory where the vector index (and incremental state) live.
        last_commit : str
            The git SHA recorded when the index was last fully built.
            Pass an empty string to force a full rebuild.

        The method loads the existing ``CodeVectorStore``, ``IncrementalChunkStore``,
        and ``EmbeddingsCache`` from *output_dir*, runs the incremental update
        pipeline, then saves all state back to disk.

        Falls back to a full ``build()`` call when incremental state files are
        missing (i.e. on first run or after a manual cache wipe).
        """
        from pathlib import Path

        from ..code_chunker import CodeChunker, RepoChunkingConfig
        from ..index.embedding.vector_store import CodeVectorStore
        from ..index.incremental import (
            EmbeddingsCache,
            GitDiffDetector,
            IncrementalChunkStore,
            IncrementalIndexUpdater,
            IncrementalState,
        )

        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]
        last_commit: str = kwargs.get("last_commit", "")

        # Load persisted state — auto-resolve last_commit if not provided
        inc_state = IncrementalState.load(Path(output_dir))
        if not last_commit and inc_state is not None:
            last_commit = inc_state.last_commit

        chunk_store_path = Path(output_dir) / "chunk_store.pkl"
        embeddings_cache_path = Path(output_dir) / "embeddings_cache.pkl"

        # Check both JSON and pickle formats (JSON+NPZ is the new default)
        chunk_store_json = chunk_store_path.with_suffix(".json")
        emb_cache_json = embeddings_cache_path.with_suffix(".json")
        emb_cache_npz = embeddings_cache_path.with_suffix(".npz")

        has_chunk_store = chunk_store_json.exists() or chunk_store_path.exists()
        has_emb_cache = (
            emb_cache_json.exists() and emb_cache_npz.exists()
        ) or embeddings_cache_path.exists()

        # Fall back to full build when incremental state is missing
        if not has_chunk_store or not has_emb_cache:
            logger.info(
                "Incremental state not found in %s; falling back to full build.",
                output_dir,
            )
            return self.build(scope, **kwargs)

        # Load existing artifacts
        vector_store = CodeVectorStore(
            embedding_model=self.embedding_model,
            embedding_provider=self.embedding_provider,
            dimension=self.embedding_dimension,
            index_metric=self.index_metric,
            store_path=output_dir,
            **self.embedding_kwargs,
        )
        vector_store.load(output_dir)

        chunk_store = IncrementalChunkStore.load(chunk_store_path)
        embeddings_cache = EmbeddingsCache.load(embeddings_cache_path)

        # Build chunkers matching the original build config
        primary = self.languages[0] if self.languages else "python"
        repo_cfg = RepoChunkingConfig(languages=self.languages)
        chunker = CodeChunker(
            language=primary,
            repo_config=repo_cfg,
            max_lines_per_chunk=self.max_lines_per_chunk,
        )

        # L0 chunker for file skeletons (only if L0 was part of the build)
        l0_chunker = None
        if "l0" in self.build_levels:
            l0_chunker = CodeChunker(
                language=primary,
                repo_config=repo_cfg,
                chunk_depth=0,
                skeleton_mode=True,
            )

        diff_detector = GitDiffDetector()
        updater = IncrementalIndexUpdater(
            chunker=chunker,
            embedding_model=vector_store.embedding,
            diff_detector=diff_detector,
            l0_chunker=l0_chunker,
        )

        result = updater.update(
            repo_path=repo_path,
            vector_store=vector_store,
            chunk_store=chunk_store,
            embeddings_cache=embeddings_cache,
            last_commit=last_commit,
        )

        # Persist updated state
        vector_store.save(output_dir)
        chunk_store.save(chunk_store_path)
        embeddings_cache.save(embeddings_cache_path)

        # Update incremental state with the new commit
        new_state = IncrementalState(
            last_commit=result.new_commit,
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path=output_dir,
            build_levels=list(self.build_levels),
        )
        new_state.save(Path(output_dir))

        doc_count = {}
        if vector_store.l0_documents:
            doc_count["l0"] = len(vector_store.l0_documents)
        if vector_store.l2_documents:
            doc_count["l2"] = len(vector_store.l2_documents)

        return IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                "embedding_model": self.embedding_model,
                "levels": list(self.build_levels),
                "document_count": doc_count,
                "chunks_reembedded": result.chunks_reembedded,
                "chunks_from_cache": result.chunks_from_cache,
                "cache_hit_rate": result.cache_hit_rate,
                "new_commit": result.new_commit,
            },
        )


@dataclass
class ZoektIndexBuilder:
    """Build a Zoekt trigram index by shelling out to ``zoekt-git-index``.

    Zoekt is a Go-based code search engine (https://github.com/sourcegraph/zoekt)
    that indexes source files using a positional trigram index.  We treat it
    as an external tool: ``zoekt-git-index`` writes shard files into
    ``output_dir`` from the repository's tracked files, and the MCP server
    later spawns ``zoekt-webserver`` against that directory to answer
    queries.

    The builder is a *soft* dependency.  If the binary is missing, ``build()``
    raises :class:`RuntimeError` with installation guidance; the
    :class:`IndexCompiler` records the failure in the manifest, and other
    indexes continue building.
    """

    binary: str = "zoekt-git-index"
    extra_args: List[str] = field(default_factory=list)

    def build(self, scope: str, **kwargs: Any) -> IndexStatus:
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]

        binary_path = shutil.which(self.binary) or (
            self.binary if os.path.isfile(self.binary) else None
        )
        if binary_path is None:
            raise RuntimeError(
                f"Zoekt binary not found: {self.binary!r}. "
                "Install via 'go install github.com/sourcegraph/zoekt/cmd/...@latest' "
                "or use the official Docker image. "
                "Skipping zoekt index build."
            )

        os.makedirs(output_dir, exist_ok=True)
        cmd = [binary_path, "-index", output_dir, *self.extra_args, repo_path]
        logger.info("Building Zoekt index: %s", " ".join(cmd))
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"zoekt-git-index failed (rc={exc.returncode}): "
                f"{(exc.stderr or '').strip()}"
            ) from exc

        shard_count = sum(
            1 for entry in os.listdir(output_dir) if entry.endswith(".zoekt")
        )

        return IndexStatus(
            index_type="zoekt",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                "shard_count": shard_count,
                "binary": binary_path,
            },
        )

    def incremental_update(self, scope: str, **kwargs: Any) -> IndexStatus:
        return self.build(scope, **kwargs)


@dataclass
class SymbolGraphBuilder:
    """Build a SCIP-based symbol graph."""

    language: str = "python"

    def build(self, scope: str, **kwargs: Any) -> IndexStatus:
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]

        from ..scip_interface import (
            SCIPPythonIndexer,
            SCIPRustIndexer,
            SCIPTypeScriptIndexer,
        )

        _INDEXER_MAP = {
            "python": SCIPPythonIndexer,
            "rust": SCIPRustIndexer,
            "typescript": SCIPTypeScriptIndexer,
            "javascript": SCIPTypeScriptIndexer,
        }

        indexer_cls = _INDEXER_MAP.get(self.language)
        if indexer_cls is None:
            raise ValueError(f"Unsupported language for symbol graph: {self.language}")

        os.makedirs(output_dir, exist_ok=True)
        indexer = indexer_cls(
            project_root=repo_path,
            output_dir=output_dir,
        )
        graph = indexer.run_pipeline(
            output_file=os.path.join(output_dir, "graph.pkl"),
            skip_level=None,
        )

        node_count = 0
        if graph is not None and hasattr(graph, "graph"):
            node_count = len(graph.graph.vs)

        return IndexStatus(
            index_type="symbol_graph",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                "node_count": node_count,
                "language": self.language,
            },
        )

    def incremental_update(self, scope: str, **kwargs: Any) -> IndexStatus:
        return self.build(scope, **kwargs)


# ---------------------------------------------------------------------------
# Convenience registration
# ---------------------------------------------------------------------------


def register_default_builders(
    registry: IndexBuilderRegistry,
    *,
    languages: Optional[List[str]] = None,
    embedding_model: str = "nomic-ai/CodeRankEmbed",
    embedding_dimension: int = 768,
    trust_remote_code: bool = False,
) -> None:
    """Register all standard index builders with sensible defaults."""
    langs = languages or ["python"]
    registry.register("bm25", BM25IndexBuilder(languages=langs))

    # Build embedding_kwargs with trust_remote_code if requested
    embedding_kwargs = {}
    if trust_remote_code:
        embedding_kwargs = {"model_kwargs": {"trust_remote_code": True}}

    registry.register(
        "vector",
        VectorIndexBuilder(
            languages=langs,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            embedding_kwargs=embedding_kwargs,
        ),
    )
    registry.register("symbol_graph", SymbolGraphBuilder(language=langs[0]))
    # Zoekt is registered unconditionally; build() raises a clear error at
    # invocation time if the binary is unavailable so the IndexCompiler can
    # mark the entry as failed without aborting other index builds.
    registry.register("zoekt", ZoektIndexBuilder())
