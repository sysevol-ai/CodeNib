# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Index builder protocol, registry, and concrete implementations.

The compiler can invoke builders to create or update indexes
when ``ResourcePlan`` indicates they are missing or stale.

Concrete builders wrap existing index infrastructure:
  - ``BM25IndexBuilder``   → ``BM25CodeIndexer``
  - ``VectorIndexBuilder`` → ``build_hierarchical_vector_store``
  - ``SymbolGraphBuilder`` → ``LSIndexer`` graph registry
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from ..index.embedding.model_policy import (
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    resolve_embedding_load_policy,
)
from ..provider_routes import (
    InferenceRoute,
    normalize_endpoint,
    normalize_provider,
    resolve_inference_route,
    validate_embedding_runtime_options,
)
from ..repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    default_exclude_patterns,
)
from .resources import IndexState, IndexStatus
from .verification import NullVerifier, UpdateVerifier, VerificationResult

logger = logging.getLogger(__name__)


def _git_output(repo_path: str, *args: str) -> str:
    """Run git in *repo_path* and return stripped stdout ("" on any failure)."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - git absent or hung: treat as unknown
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


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

    def artifact_identity(self) -> Dict[str, Any]:
        return {
            # v8 refuses skipped files and retains non-symbol source context.
            "builder_schema": 8,
            "languages": list(self.languages),
            "max_k": self.max_k,
            "max_lines_per_chunk": self.max_lines_per_chunk,
            "chunking_failure_policy": "fail",
            "include_header_epilogue": True,
            "repository_filter_policy": REPOSITORY_FILTER_POLICY_VERSION,
        }

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
            include_header_epilogue=True,
        )
        chunks = chunker.chunk_repository(repo_path=repo_path, strict=True)

        indexer = BM25CodeIndexer(
            chunks=chunks,
            max_k=self.max_k,
            project_root=repo_path,
        )
        os.makedirs(output_dir, exist_ok=True)
        indexer.save_index(output_dir)
        from .artifact_fingerprints import bm25_artifact_file_fingerprints

        artifact_files = bm25_artifact_file_fingerprints(output_dir)

        return IndexStatus(
            index_type="bm25",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                **self.artifact_identity(),
                "artifact_file_fingerprints": artifact_files,
                "chunk_count": len(chunks),
                "source_file_count": len(
                    {chunk.file for chunk in chunks if getattr(chunk, "file", "")}
                ),
                # Retain the legacy field for existing manifest consumers.
                "file_count": len(chunks),
            },
        )

    def incremental_update(self, scope: str, **kwargs: Any) -> IndexStatus:
        return self.build(scope, **kwargs)


@dataclass
class VectorIndexBuilder:
    """Build a hierarchical embedding index (L0/L2)."""

    languages: List[str] = field(default_factory=lambda: ["python"])
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_provider: str = "huggingface"
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION
    embedding_kwargs: Dict[str, Any] = field(default_factory=dict)
    embedding_endpoint: Optional[str] = None
    embedding_credential_env: Optional[str] = None
    embedding_runtime_kwargs: Dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    build_levels: List[str] = field(default_factory=lambda: ["l0", "l2"])
    max_lines_per_chunk: int = 300
    index_metric: str = "ip"

    def artifact_identity(self) -> Dict[str, Any]:
        """Return the embedding contract required to reopen this artifact."""

        route = self._embedding_route()
        return {
            # v5 binds each manifest entry to one committed vector config.
            "builder_schema": 5,
            "embedding_model": route.model,
            "embedding_provider": route.provider,
            "embedding_dimension": self.embedding_dimension,
            "dimension": self.embedding_dimension,
            "embedding_endpoint": route.endpoint,
            "embedding_kwargs": route.compatibility_options,
            "embedding_route": route.public_identity(),
            "embedding_fingerprint": route.compatibility_fingerprint,
            "index_metric": self.index_metric,
            "languages": list(self.languages),
            "levels": list(self.build_levels),
            "max_lines_per_chunk": self.max_lines_per_chunk,
            "chunking_failure_policy": "fail",
            "repository_filter_policy": REPOSITORY_FILTER_POLICY_VERSION,
        }

    def _embedding_route(self) -> InferenceRoute:
        return resolve_inference_route(
            operation="embeddings",
            provider=self.embedding_provider,
            model=self.embedding_model,
            endpoint=self.embedding_endpoint,
            dimension=self.embedding_dimension,
            credential_env=self.embedding_credential_env,
            compatibility_options=self.embedding_kwargs,
        )

    def _embedding_call_kwargs(self) -> Dict[str, Any]:
        route = self._embedding_route()
        if route.provider == "huggingface":
            kwargs = dict(self.embedding_kwargs)
            inherited_runtime: Dict[str, Any] = {}
        else:
            kwargs = route.embedding_backend_kwargs()
            inherited_runtime = {
                key: value
                for key, value in self.embedding_kwargs.items()
                if key not in route.compatibility_options
            }
        inherited_runtime.update(self.embedding_runtime_kwargs)
        runtime_kwargs = validate_embedding_runtime_options(
            inherited_runtime,
            provider=route.provider,
        )
        for key, value in runtime_kwargs.items():
            if key in {"encode_kwargs", "model_kwargs"}:
                nested = dict(kwargs.get(key) or {})
                nested.update(value)
                kwargs[key] = nested
            else:
                kwargs[key] = value
        runtime_endpoint = normalize_endpoint(kwargs.get("base_url"))
        if runtime_endpoint is not None and runtime_endpoint != route.endpoint:
            raise ValueError(
                "embedding runtime base_url does not match the artifact endpoint"
            )
        if route.endpoint:
            kwargs["base_url"] = route.endpoint
        if not kwargs.get("api_key"):
            credential = route.credential()
            if credential:
                kwargs["api_key"] = credential
        return kwargs

    def _validate_vector_dimension(self, vector_store: Any) -> None:
        actual = getattr(vector_store, "dimension", None)
        if (
            isinstance(actual, int)
            and not isinstance(actual, bool)
            and actual != self.embedding_dimension
        ):
            raise ValueError(
                "embedding provider returned dimension "
                f"{actual}, expected {self.embedding_dimension}"
            )

    def _persistence_config_fingerprint(self, output_dir: str) -> Dict[str, Any]:
        from ..index.embedding.artifact_integrity import vector_config_artifact_record

        model_suffix = self._embedding_route().model.replace("/", "__")
        return vector_config_artifact_record(output_dir, model_suffix)

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
        artifact_identity = self.artifact_identity()
        vs = build_hierarchical_vector_store(
            repo_path=repo_path,
            index_path=output_dir,
            plan_name=None,
            languages=self.languages,
            max_lines_per_chunk=self.max_lines_per_chunk,
            build_levels=self.build_levels,
            embedding_model=artifact_identity["embedding_model"],
            embedding_provider=artifact_identity["embedding_provider"],
            embedding_dimension=self.embedding_dimension,
            embedding_kwargs=self._embedding_call_kwargs(),
            index_metric=self.index_metric,
            artifact_metadata=artifact_identity,
            # ``build`` is the compiler's full-materialization path. Reusing an
            # artifact merely because its model config exists would let stale
            # vectors be stamped with the current source fingerprint. Cross-
            # commit reuse belongs to ``incremental_update`` instead.
            force_rebuild=True,
            strict_chunking=True,
        )
        self._validate_vector_dimension(vs)

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
        persistence_config = self._persistence_config_fingerprint(output_dir)

        return IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                **artifact_identity,
                "persistence_config_fingerprint": persistence_config,
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
        artifact_identity = self.artifact_identity()
        vector_store = CodeVectorStore(
            embedding_model=artifact_identity["embedding_model"],
            embedding_provider=artifact_identity["embedding_provider"],
            dimension=self.embedding_dimension,
            index_metric=self.index_metric,
            store_path=output_dir,
            artifact_metadata=artifact_identity,
            **self._embedding_call_kwargs(),
        )
        self._validate_vector_dimension(vector_store)
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
                max_lines_per_chunk=self.max_lines_per_chunk,
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
        persistence_config = self._persistence_config_fingerprint(output_dir)

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
                **self.artifact_identity(),
                "persistence_config_fingerprint": persistence_config,
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
                "Run 'make zoekt-tool' and use 'make active-scip-env' for PATH, "
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
    """Build a registry-backed symbol graph.

    Supports LSP-assisted incremental repair between commits. Every failure
    path -- missing base graph, moved/dirty worktree, absent language server,
    patch error, or an update that verification will not admit -- falls back to
    a full rebuild, so the artifact is always correct and only the cost varies.
    """

    language: str = "python"
    languages: Optional[List[str]] = None
    graph_route: str = "active"
    exclude_patterns: List[str] = field(default_factory=default_exclude_patterns)
    allow_partial_languages: bool = False
    allow_partial_index: bool = False
    source_coverage_fallback: bool = False
    # Admission control for incremental updates. The default proves nothing and
    # says so, which combined with require_verification=True means the builder
    # behaves exactly like a full rebuild until a real verifier is configured.
    verifier: Optional[UpdateVerifier] = None
    # When True, an update that is not positively verified is discarded and the
    # graph is rebuilt. Callers that knowingly accept unverified incremental
    # results (e.g. a local demo) set this False; the manifest still records
    # that the result was never checked.
    require_verification: bool = True

    def artifact_identity(self) -> Dict[str, Any]:
        graph_languages = self.languages or [self.language]
        return {
            "builder_schema": 3,
            "languages": list(graph_languages),
            "graph_route": self.graph_route,
            "exclude_patterns": sorted(self.exclude_patterns),
            "allow_partial_languages": self.allow_partial_languages,
            "allow_partial_index": self.allow_partial_index,
            "source_coverage_fallback": self.source_coverage_fallback,
            "repository_filter_policy": REPOSITORY_FILTER_POLICY_VERSION,
        }

    def __post_init__(self) -> None:
        if self.allow_partial_index and not self.source_coverage_fallback:
            raise ValueError(
                "allow_partial_index requires source_coverage_fallback so a "
                "partial compiler artifact cannot be published as complete"
            )

    def build(self, scope: str, **kwargs: Any) -> IndexStatus:
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]

        from ..ls_router import build_graph_for_languages_with_report

        os.makedirs(output_dir, exist_ok=True)
        graph_languages = self.languages or [self.language]
        build_kwargs = {
            "languages": graph_languages,
            "project_name": os.path.basename(os.path.abspath(repo_path)),
            "skip_level": None,
            "exclude_patterns": self.exclude_patterns,
            "graph_route": self.graph_route,
        }
        if self.allow_partial_index:
            build_kwargs["allow_partial_index"] = True
        result = build_graph_for_languages_with_report(
            repo_path,
            output_dir,
            allow_partial=self.allow_partial_languages,
            **build_kwargs,
        )

        graph = result.graph
        compiler_graph_available = graph is not None and hasattr(graph, "graph")
        compiler_node_count = len(graph.graph.vs) if compiler_graph_available else 0
        compiler_edge_count = len(graph.graph.es) if compiler_graph_available else 0
        if not compiler_graph_available and not self.source_coverage_fallback:
            detail = "; ".join(
                f"{language}: {error}"
                for language, error in result.failed_languages.items()
            )
            suffix = f" ({detail})" if detail else ""
            raise RuntimeError(f"symbol graph builder returned no graph{suffix}")
        if not compiler_graph_available:
            from ..graph.code_graph import CodeGraph
            from ..types import ROOT_NODE

            graph = CodeGraph(repo_path)
            graph.add_root_node(ROOT_NODE)

        compiler_partial_languages = sorted(
            language
            for language, report in result.index_generation_reports.items()
            if report.get("partial") is True
        )
        compiler_index_complete = (
            False
            if not compiler_graph_available
            else (
                all(
                    result.index_generation_reports.get(language, {}).get("complete")
                    is True
                    for language in result.available_languages
                )
                if result.available_languages
                and all(
                    language in result.index_generation_reports
                    for language in result.available_languages
                )
                else None
            )
        )
        fallback_report = None
        if self.source_coverage_fallback:
            from ..git_snapshot import GitSourceSurface
            from ..graph.source_coverage import supplement_graph_source_coverage
            from ..languages import extensions_for_language
            from .artifact_quality import graph_file_paths

            surface = GitSourceSurface.load(repo_path)
            extensions = {
                extension
                for language in graph_languages
                for extension in extensions_for_language(language, "graph")
            }
            coverage_report = supplement_graph_source_coverage(
                graph,
                repo_root=repo_path,
                surface=surface,
                extensions=extensions,
                represented_paths=graph_file_paths(graph),
                exclude_patterns=self.exclude_patterns,
            )
            if coverage_report.get("coverage_after") != 1.0:
                raise RuntimeError(
                    "source coverage fallback did not cover the requested "
                    "repository surface"
                )
            fallback_report = {
                "compiler_graph_available": compiler_graph_available,
                "compiler_index_complete": compiler_index_complete,
                "compiler_partial_languages": compiler_partial_languages,
                "compiler_nodes": compiler_node_count,
                "compiler_edges": compiler_edge_count,
                **coverage_report,
            }
            graph.save_graph(os.path.join(output_dir, "graph.pkl"))

        node_count = len(graph.graph.vs)
        if node_count == 0:
            raise RuntimeError("symbol graph builder returned an empty graph")
        available_languages = (
            list(graph_languages)
            if fallback_report is not None
            else result.available_languages
        )

        return IndexStatus(
            index_type="symbol_graph",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                **self.artifact_identity(),
                "node_count": node_count,
                "language": available_languages[0],
                "available_languages": available_languages,
                "compiler_available_languages": result.available_languages,
                "index_generation_reports": result.index_generation_reports,
                "partial_index": bool(compiler_partial_languages),
                "failed_languages": result.failed_languages,
                "partial": result.partial,
                **(
                    {"source_coverage_report": fallback_report}
                    if fallback_report is not None
                    else {}
                ),
            },
        )

    def incremental_update(self, scope: str, **kwargs: Any) -> IndexStatus:
        """Repair the existing graph from ``last_commit`` to the repo's HEAD.

        Required kwargs
        ---------------
        repo_path : str
            Repository root. Must be checked out at the target commit; the
            patcher reads file bodies from disk.
        output_dir : str
            Directory holding ``graph.pkl``.
        last_commit : str
            Commit the existing graph was built from. Empty forces a rebuild.

        Falls back to :meth:`build` whenever the incremental path is
        unavailable or its result is not admitted.
        """
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]
        last_commit: str = kwargs.get("last_commit", "") or ""

        reason = self._incremental_blocker(repo_path, output_dir, last_commit)
        if reason:
            logger.info("symbol_graph: full rebuild (%s)", reason)
            return self.build(
                scope, **{k: v for k, v in kwargs.items() if k != "last_commit"}
            )

        try:
            status = self._patch_graph(scope, repo_path, output_dir, last_commit)
        except Exception as exc:  # noqa: BLE001 - any patch failure means rebuild
            logger.warning("symbol_graph: patch failed (%s); rebuilding", exc)
            return self.build(
                scope, **{k: v for k, v in kwargs.items() if k != "last_commit"}
            )

        if status is None:
            return self.build(
                scope, **{k: v for k, v in kwargs.items() if k != "last_commit"}
            )
        return status

    # -- incremental helpers ------------------------------------------------

    def _incremental_blocker(
        self, repo_path: str, output_dir: str, last_commit: str
    ) -> Optional[str]:
        """Return why the incremental path cannot run, or None if it can."""
        if self.source_coverage_fallback:
            return "source coverage fallback requires a provenance-complete rebuild"
        if not last_commit:
            return "no previously indexed commit"
        if not os.path.isfile(os.path.join(output_dir, "graph.pkl")):
            return "no existing graph.pkl to patch"
        head = _git_output(repo_path, "rev-parse", "HEAD")
        if not head:
            return "repository HEAD could not be resolved"
        if head == last_commit:
            return "already at the indexed commit"
        # The patcher reads bodies from the working tree, so a *modified tracked*
        # file would be silently indexed as though it were part of the target
        # commit. Rebuilding is correct and simpler than reasoning about that.
        #
        # Untracked files are deliberately ignored: the index output directory
        # normally lives inside the repo (.codenib_cache/), so counting
        # untracked paths here would report every repo as dirty and disable the
        # incremental path entirely. Untracked files also never appear in the
        # commit-to-commit diff the patcher works from.
        if _git_output(repo_path, "status", "--porcelain", "--untracked-files=no"):
            return "working tree has uncommitted changes to tracked files"
        return None

    def _patch_graph(
        self, scope: str, repo_path: str, output_dir: str, last_commit: str
    ) -> Optional[IndexStatus]:
        """Run the LSP patcher over every configured language. None = rebuild."""
        from ..graph.code_graph import CodeGraph
        from ..graph.incremental.graph_patcher import LANGUAGE_EXTENSIONS, GraphPatcher
        from ..graph.incremental.patcher_base import IncrementalPatchRebuildRequired

        graph_path = os.path.join(output_dir, "graph.pkl")
        graph = CodeGraph.load_graph(graph_path)
        graph_languages = self.languages or [self.language]
        head = _git_output(repo_path, "rev-parse", "HEAD")

        started: List[Any] = []
        changed_total = 0
        per_language: Dict[str, Any] = {}
        start = time.monotonic()
        try:
            for lang in graph_languages:
                exts = LANGUAGE_EXTENSIONS.get(lang)
                if not exts:
                    continue
                changed = GraphPatcher.detect_changed_files(
                    repo_path, last_commit, head, extensions=exts
                )
                count = sum(len(paths) for paths in changed.values())
                if not count:
                    continue

                patcher = GraphPatcher(
                    project_root=repo_path, code_graph=graph, language=lang
                )
                # ``patch_files`` owns startup so language-specific contract
                # checks can request a rebuild before an LSP is launched or
                # graph state is mutated.
                started.append(patcher)
                try:
                    per_language[lang] = patcher.patch_files(
                        changed, earlier_commit=last_commit, later_commit=head
                    )
                except IncrementalPatchRebuildRequired as exc:
                    logger.info(
                        "symbol_graph: full rebuild requested for %s (%s)", lang, exc
                    )
                    return None
                changed_total += count
        finally:
            for patcher in started:
                try:
                    patcher.stop_lsp()
                except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                    logger.warning("symbol_graph: stop_lsp: %s", exc)

        if changed_total == 0:
            logger.info("symbol_graph: no source changes for configured languages")
            result = VerificationResult(
                verified=True,
                checked=True,
                reason="no changed source files for configured languages",
                details={"method": "empty-relevant-diff"},
            )
        else:
            verifier = self.verifier or NullVerifier()
            result = verifier.verify(
                index_type="symbol_graph",
                patched=graph,
                fresh=None,
                context={
                    "repo_path": repo_path,
                    "earlier_commit": last_commit,
                    "later_commit": head,
                    "languages": list(graph_languages),
                },
            )
        if self.require_verification and not result.verified:
            logger.info(
                "symbol_graph: update not admitted (%s); rebuilding", result.reason
            )
            return None

        graph.save_graph(graph_path)
        elapsed = time.monotonic() - start
        node_count = len(graph.graph.vs)
        return IndexStatus(
            index_type="symbol_graph",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                **self.artifact_identity(),
                "node_count": node_count,
                "language": graph_languages[0],
                "update_mode": "incremental",
                "changed_files": changed_total,
                "patch_seconds": round(elapsed, 3),
                "earlier_commit": last_commit,
                "later_commit": head,
                "patch_stats": per_language,
                **result.to_metadata(),
            },
        )


# ---------------------------------------------------------------------------
# Convenience registration
# ---------------------------------------------------------------------------


def register_default_builders(
    registry: IndexBuilderRegistry,
    *,
    languages: Optional[List[str]] = None,
    graph_route: str = "active",
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_provider: str = "huggingface",
    embedding_endpoint: Optional[str] = None,
    embedding_credential_env: Optional[str] = None,
    embedding_revision: Optional[str] = None,
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION,
    trust_remote_code: Optional[bool] = None,
    embedding_batch_size: Optional[int] = None,
    embedding_max_seq_length: Optional[int] = None,
    exclude_patterns: Optional[List[str]] = None,
    allow_partial_graph_languages: bool = False,
    allow_partial_graph_index: bool = False,
    graph_source_coverage_fallback: bool = False,
) -> None:
    """Register all standard index builders with sensible defaults."""
    langs = languages or ["python"]
    registry.register("bm25", BM25IndexBuilder(languages=langs))

    embedding_provider = normalize_provider(embedding_provider)
    embedding_kwargs: Dict[str, Any] = {}
    embedding_runtime_kwargs: Dict[str, Any] = {}
    if embedding_provider == "huggingface":
        load_policy = resolve_embedding_load_policy(
            embedding_model,
            revision=embedding_revision,
            trust_remote_code=trust_remote_code,
        )
        if load_policy.trust_remote_code:
            embedding_kwargs = {"model_kwargs": {"trust_remote_code": True}}
        if embedding_batch_size is not None:
            embedding_runtime_kwargs["default_batch_size"] = embedding_batch_size
        if embedding_max_seq_length is not None:
            embedding_kwargs["max_seq_length"] = embedding_max_seq_length
        if load_policy.revision is not None:
            embedding_kwargs["revision"] = load_policy.revision
    elif any(
        value is not None
        for value in (
            embedding_revision,
            trust_remote_code,
            embedding_batch_size,
            embedding_max_seq_length,
        )
    ):
        raise ValueError(
            "Hugging Face load options cannot be used with a remote embedding provider"
        )

    registry.register(
        "vector",
        VectorIndexBuilder(
            languages=langs,
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            embedding_dimension=embedding_dimension,
            embedding_kwargs=embedding_kwargs,
            embedding_endpoint=embedding_endpoint,
            embedding_credential_env=embedding_credential_env,
            embedding_runtime_kwargs=embedding_runtime_kwargs,
        ),
    )
    registry.register(
        "symbol_graph",
        SymbolGraphBuilder(
            language=langs[0],
            languages=list(langs),
            graph_route=graph_route,
            allow_partial_languages=allow_partial_graph_languages,
            allow_partial_index=allow_partial_graph_index,
            source_coverage_fallback=graph_source_coverage_fallback,
            exclude_patterns=(
                list(exclude_patterns)
                if exclude_patterns is not None
                else default_exclude_patterns()
            ),
        ),
    )
    # Zoekt is registered unconditionally; build() raises a clear error at
    # invocation time if the binary is unavailable so the IndexCompiler can
    # mark the entry as failed without aborting other index builds.
    registry.register("zoekt", ZoektIndexBuilder())
