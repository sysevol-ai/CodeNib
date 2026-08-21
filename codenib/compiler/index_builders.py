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

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from ..index.embedding.model_policy import (
    DEFAULT_EMBEDDING_DIMENSION,
    DEFAULT_EMBEDDING_MODEL,
    resolve_embedding_load_policy,
)
from ..index.embedding.text_policy import REMOTE_EMBEDDING_DOCUMENT_MAX_CHARS
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
    repository_path_is_visible,
)
from ..repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
    repository_relative_source_path,
)
from .resources import IndexState, IndexStatus
from .verification import NullVerifier, UpdateVerifier, VerificationResult

logger = logging.getLogger(__name__)


class _AtomicIncrementalRebuildRequired(RuntimeError):
    """Signal that vector deltas must use the complete-generation builder."""


def _snapshot_source_selection(
    source_selection: RepositorySourceSelection,
) -> RepositorySourceSelection:
    """Return a detached, exact-type snapshot for one build operation."""

    if type(source_selection) is not RepositorySourceSelection:
        raise TypeError("source_selection must be a RepositorySourceSelection")
    return RepositorySourceSelection(source_selection.exclude_subtrees)


def _source_selection_for_build(
    configured: RepositorySourceSelection,
    requested: object = None,
) -> RepositorySourceSelection:
    """Snapshot one builder selection and reject a mismatched runtime request."""

    selected = _snapshot_source_selection(configured)
    if requested is None:
        return selected
    runtime = _snapshot_source_selection(requested)
    if runtime != selected:
        raise RuntimeError(
            "builder source selection does not match the requested selection"
        )
    return selected


def _selection_exclude_hints(
    patterns: List[str],
    source_selection: RepositorySourceSelection,
) -> List[str]:
    """Add backend performance hints for exact excluded subtrees."""

    hints = list(patterns)
    for subtree in source_selection.exclude_subtrees:
        # Hints are not the correctness boundary. Avoid handing glob
        # metacharacters to backends that would broaden an exact literal path.
        if any(character in subtree for character in "*?["):
            continue
        for pattern in (subtree, f"{subtree}/**"):
            if pattern not in hints:
                hints.append(pattern)
    return hints


def _constrain_and_save_selected_graph(
    graph: Any,
    output_dir: str,
    source_selection: RepositorySourceSelection,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Apply the central graph gate, then publish graph and occurrence views."""

    from .artifact_fingerprints import regular_file_fingerprint
    from .artifact_quality import constrain_graph_to_source_selection

    occurrence_path = os.path.join(output_dir, "lsp_index.pkl")
    report = constrain_graph_to_source_selection(graph, source_selection)
    if report["excluded_paths_after"] or report["invalid_paths_after"]:
        raise RuntimeError(
            "source-selection graph gate could not prove a clean artifact"
        )

    graph.save_graph(os.path.join(output_dir, "graph.pkl"))
    occurrence_index = getattr(graph, "lsp_occurrence_index", None)
    lsp_occurrence_artifact = None
    if occurrence_index is not None:
        occurrence_index.save(occurrence_path)
        lsp_occurrence_artifact = {
            "relative_path": "lsp_index.pkl",
            **regular_file_fingerprint(occurrence_path),
        }
    elif os.path.lexists(occurrence_path):
        # A full rebuild must never adopt an occurrence sidecar left by an
        # earlier generation.  Backends that produced occurrences attach the
        # exact current object to ``graph``; absence therefore means this
        # generation has no publishable occurrence view.
        os.unlink(occurrence_path)
    return (
        {
            "source_selection_digest": report["source_selection_digest"],
            "nodes_before": report["nodes_before"],
            "nodes_after": report["nodes_after"],
            "edges_before": report["edges_before"],
            "edges_after": report["edges_after"],
            "removed_excluded_path_count": len(report["removed_excluded_paths"]),
            "excluded_path_count_after": len(report["excluded_paths_after"]),
            "invalid_path_count_before": len(report["invalid_paths_before"]),
            "invalid_path_count_after": len(report["invalid_paths_after"]),
        },
        lsp_occurrence_artifact,
    )


def _require_selected_paths(
    source_selection: RepositorySourceSelection,
    paths: Any,
    *,
    repository_root: str,
    subject: str,
) -> None:
    """Reject non-canonical or excluded repository-relative result paths."""

    for source_path in paths:
        try:
            relative_path = repository_relative_source_path(
                repository_root,
                source_path,
            )
            allowed = repository_path_is_visible(
                relative_path,
                selection=source_selection,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{subject} has a non-canonical repository path: {source_path!r}"
            ) from exc
        if not allowed:
            raise RuntimeError(
                f"{subject} leaked excluded repository path: {relative_path}"
            )


def _require_selected_vector_documents(
    vector_store: Any,
    source_selection: RepositorySourceSelection,
    *,
    repository_root: str,
) -> None:
    """Validate every materialized vector document against source selection."""

    for level in ("l0", "l2"):
        documents = getattr(vector_store, f"{level}_documents", ()) or ()
        _require_selected_documents(
            source_selection,
            documents,
            repository_root=repository_root,
            subject=f"vector {level} documents",
        )


def _require_selected_documents(
    source_selection: RepositorySourceSelection,
    documents: Any,
    *,
    repository_root: str,
    subject: str,
) -> None:
    """Validate the repository file metadata on materialized documents."""

    paths = []
    for document in documents:
        metadata = getattr(document, "metadata", None)
        paths.append(metadata.get("file") if isinstance(metadata, dict) else None)
    _require_selected_paths(
        source_selection,
        paths,
        repository_root=repository_root,
        subject=subject,
    )


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
    additional_ignore_dirs: List[str] = field(default_factory=list)
    source_selection: RepositorySourceSelection = field(
        default_factory=RepositorySourceSelection
    )

    def __post_init__(self) -> None:
        self.source_selection = _snapshot_source_selection(self.source_selection)

    def artifact_identity(self) -> Dict[str, Any]:
        source_selection = _snapshot_source_selection(self.source_selection)
        return self._artifact_identity_for_selection(source_selection)

    def _artifact_identity_for_selection(
        self,
        source_selection: RepositorySourceSelection,
    ) -> Dict[str, Any]:
        identity = {
            # v8 refuses skipped files and retains non-symbol source context.
            "builder_schema": 8,
            "languages": list(self.languages),
            "max_k": self.max_k,
            "max_lines_per_chunk": self.max_lines_per_chunk,
            "chunking_failure_policy": "fail",
            "include_header_epilogue": True,
            "repository_filter_policy": REPOSITORY_FILTER_POLICY_VERSION,
            "source_selection_digest": source_selection.digest,
        }
        if self.additional_ignore_dirs:
            identity["additional_ignore_dirs"] = sorted(
                set(self.additional_ignore_dirs)
            )
        return identity

    def build(self, scope: str, **kwargs: Any) -> IndexStatus:
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]
        source_selection = _source_selection_for_build(
            self.source_selection,
            kwargs.get("source_selection"),
        )
        artifact_identity = self._artifact_identity_for_selection(source_selection)

        from ..code_chunker import CodeChunker, RepoChunkingConfig
        from ..index.sparse_idx.bm25_index import BM25CodeIndexer

        primary = self.languages[0] if self.languages else "python"
        repo_config = RepoChunkingConfig(
            languages=self.languages,
            source_selection=source_selection,
        )
        repo_config.ignore_dirs.update(self.additional_ignore_dirs)
        chunker = CodeChunker(
            language=primary,
            repo_config=repo_config,
            max_lines_per_chunk=self.max_lines_per_chunk,
            include_header_epilogue=True,
        )
        chunks = chunker.chunk_repository(repo_path=repo_path, strict=True)
        _require_selected_paths(
            source_selection,
            (getattr(chunk, "file", None) for chunk in chunks),
            repository_root=repo_path,
            subject="BM25 chunks",
        )

        indexer = BM25CodeIndexer(
            chunks=chunks,
            max_k=self.max_k,
            project_root=repo_path,
        )
        _require_selected_documents(
            source_selection,
            getattr(indexer, "documents", ()),
            repository_root=repo_path,
            subject="BM25 documents",
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
                **artifact_identity,
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
    embedding_document_max_chars: int = REMOTE_EMBEDDING_DOCUMENT_MAX_CHARS
    additional_ignore_dirs: List[str] = field(default_factory=list)
    source_selection: RepositorySourceSelection = field(
        default_factory=RepositorySourceSelection
    )

    def __post_init__(self) -> None:
        self.source_selection = _snapshot_source_selection(self.source_selection)

    def artifact_identity(self) -> Dict[str, Any]:
        """Return the embedding contract required to reopen this artifact."""

        source_selection = _snapshot_source_selection(self.source_selection)
        return self._artifact_identity_for_selection(source_selection)

    def _artifact_identity_for_selection(
        self,
        source_selection: RepositorySourceSelection,
    ) -> Dict[str, Any]:
        route = self._embedding_route()
        identity = {
            # v7 deterministically bounds remote document inputs in addition
            # to committing vector, chunk, cache, and incremental state.
            "builder_schema": 7,
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
            "source_selection_digest": source_selection.digest,
        }
        if route.provider == "openai":
            identity["embedding_document_max_chars"] = self.embedding_document_max_chars
        if self.additional_ignore_dirs:
            identity["additional_ignore_dirs"] = sorted(
                set(self.additional_ignore_dirs)
            )
        return identity

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
        if route.provider == "openai":
            kwargs["max_input_chars"] = self.embedding_document_max_chars
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
        from pathlib import Path, PurePosixPath

        from .._atomic_directory import (
            PublicationDirectoryReader,
            reopen_authenticated_directory,
        )
        from .._captured_directory import OwnedDirectoryStage
        from ..index.embedding.artifact_integrity import (
            begin_vector_view_update,
            finish_vector_view_update,
        )
        from ..index.embedding.builders import _PrivateBuildDirectory

        source_selection = _source_selection_for_build(
            self.source_selection,
            kwargs.get("source_selection"),
        )
        output_path = Path(kwargs["output_dir"])
        publication_stage = OwnedDirectoryStage.prepare(
            output_path,
            allow_empty_destination=True,
        )

        def copy_generation(reader: PublicationDirectoryReader) -> None:
            for record in reader.file_records():
                relative = PurePosixPath(record.path)
                with reader.iter_authenticated_chunks(
                    relative,
                    max_bytes=record.size,
                ) as chunks:
                    publication_stage.write_file(
                        relative,
                        chunks,
                        mode=record.mode,
                        max_bytes=record.size,
                    )

        try:
            with _PrivateBuildDirectory(
                prefix=f".{output_path.name}.generation-build-",
                parent=output_path.parent,
            ) as build_root:
                build_path = build_root.path
                writer_path = build_root.writer_path
                with build_root.path_operation("vector update marker begin"):
                    begin_vector_view_update(writer_path)
                build_kwargs = dict(kwargs)
                build_kwargs["source_selection"] = source_selection
                build_kwargs["output_dir"] = str(writer_path)
                build_kwargs["_published_output_dir"] = str(output_path)
                build_kwargs["_build_root_guard"] = build_root
                status = self._build_once(scope, **build_kwargs)
                with build_root.path_operation("vector update marker finish"):
                    finish_vector_view_update(writer_path)

                model_suffix = self._embedding_route().model.replace("/", "__")
                generation_ownership = build_root.capture_ownership(
                    required_root_file=f"config_{model_suffix}.json",
                )
                reopen_authenticated_directory(
                    build_path,
                    generation_ownership,
                    copy_generation,
                )

            publication_stage.publish()
        except BaseException:  # noqa: B036 - preserve publication interruptions
            try:
                publication_stage.discard()
            except BaseException:  # noqa: B036 - retain the generation failure
                logger.exception("Could not isolate failed vector generation stage")
            raise
        return status

    def _build_once(self, scope: str, **kwargs: Any) -> IndexStatus:
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]
        published_output_dir: str = kwargs.get("_published_output_dir", output_dir)
        build_root_guard = kwargs.get("_build_root_guard")

        from pathlib import Path

        from ..index.embedding.builders import (
            _PrivateBuildDirectory,
            build_hierarchical_vector_store,
        )
        from ..index.incremental import (
            EmbeddingsCache,
            IncrementalChunkStore,
            IncrementalState,
        )

        if not isinstance(build_root_guard, _PrivateBuildDirectory):
            raise RuntimeError("full vector build requires its private root owner")
        build_root_guard.require_writer_path(Path(output_dir))
        build_root_guard.verify("full vector build entry")
        source_selection = _source_selection_for_build(
            self.source_selection,
            kwargs.get("source_selection"),
        )
        artifact_identity = self._artifact_identity_for_selection(source_selection)
        with build_root_guard.path_operation("hierarchical vector build"):
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
                # ``build`` is the compiler's full-materialization path.
                # Reusing an artifact merely because its model config exists
                # would stamp stale vectors with the current source identity.
                force_rebuild=True,
                strict_chunking=True,
                additional_ignore_dirs=self.additional_ignore_dirs,
                source_selection=source_selection,
                # ``build`` owns the complete private generation tree.  Its
                # outer OwnedDirectoryStage is the publication boundary.
                _atomic_publish=False,
                _build_root_guard=build_root_guard,
            )
        _require_selected_vector_documents(
            vs,
            source_selection,
            repository_root=repo_path,
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

        with build_root_guard.path_operation("incremental chunk seed save"):
            chunk_store.save(Path(output_dir) / "chunk_store.pkl")
        logger.info(
            "Seeded embeddings cache with %d vectors from initial build.",
            emb_cache.size(),
        )
        with build_root_guard.path_operation("embedding cache seed save"):
            emb_cache.save(Path(output_dir) / "embeddings_cache.pkl")

        # Persist incremental state so callers don't need to track last_commit
        inc_state = IncrementalState(
            last_commit=head_commit,
            chunk_store_path="chunk_store.pkl",
            embeddings_cache_path="embeddings_cache.pkl",
            index_path=str(Path(published_output_dir).expanduser().resolve()),
            build_levels=list(self.build_levels),
        )
        with build_root_guard.path_operation("incremental state seed save"):
            inc_state.save(Path(output_dir))
        with build_root_guard.path_operation("persistence fingerprint read"):
            persistence_config = self._persistence_config_fingerprint(output_dir)

        return IndexStatus(
            index_type="vector",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=published_output_dir,
            metadata={
                **artifact_identity,
                "persistence_config_fingerprint": persistence_config,
                "document_count": doc_count,
                "last_commit": head_commit,
            },
        )

    def incremental_update(self, scope: str, **kwargs: Any) -> IndexStatus:
        """Update the vector view, rebuilding on recoverable path failures."""

        source_selection = _source_selection_for_build(
            self.source_selection,
            kwargs.get("source_selection"),
        )
        kwargs = dict(kwargs)
        kwargs["source_selection"] = source_selection

        from ..native_index_authorization import (
            MissingNativeIndexAuthorizationError,
            NativeIndexAuthorizationError,
        )

        try:
            return self._incremental_update_once(scope, **kwargs)
        except MissingNativeIndexAuthorizationError as exc:
            return self._rebuild_after_incremental_failure(scope, kwargs, exc)
        except NativeIndexAuthorizationError:
            # An explicit capability is caller-supplied authority.  Never turn
            # a malformed, foreign-process, or mismatched token into a source
            # rebuild that appears to have accepted it.
            raise
        except Exception as exc:  # noqa: BLE001 - failed deltas must not publish
            return self._rebuild_after_incremental_failure(scope, kwargs, exc)

    def _rebuild_after_incremental_failure(
        self,
        scope: str,
        kwargs: Dict[str, Any],
        exc: Exception,
    ) -> IndexStatus:
        logger.warning("vector: incremental update failed (%s); rebuilding", exc)
        build_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key not in {"last_commit", "previous_artifact_config"}
        }
        status = self.build(scope, **build_kwargs)
        status.metadata["update_mode"] = "full_rebuild"
        status.metadata["incremental_fallback_reason"] = type(exc).__name__
        return status

    def _incremental_update_once(self, scope: str, **kwargs: Any) -> IndexStatus:
        """Validate one delta request and route it to an atomic full rebuild.

        Required kwargs
        ---------------
        repo_path : str
            Absolute path to the repository root.
        output_dir : str
            Directory where the vector index (and incremental state) live.
        last_commit : str
            The git SHA recorded when the index was last fully built.
            Pass an empty string to force a full rebuild.

        Path-based vector persistence cannot safely update the live generation
        in place.  After exact authorization and state validation, this method
        therefore raises a private recoverable signal.  The public wrapper
        rebuilds one complete private generation and switches it atomically.
        """
        from ..native_index_authorization import (
            require_native_index_authorization_preflight,
        )

        native_index_authorization = kwargs.get("native_index_authorization")
        # Reject before constructing an embedding backend. The public wrapper
        # rebuilds only for a missing token and propagates explicit bad tokens.
        require_native_index_authorization_preflight(
            native_index_authorization,
            view_type="vector",
        )

        from pathlib import Path

        from ..index.incremental import IncrementalState

        output_dir: str = kwargs["output_dir"]
        last_commit: str = kwargs.get("last_commit", "")

        from ..index.embedding.artifact_integrity import (
            require_authorized_vector_view,
            require_complete_vector_view,
        )

        require_complete_vector_view(output_dir)
        artifact_identity = self.artifact_identity()
        previous_artifact = kwargs.get("previous_artifact_config")
        if previous_artifact is not None and not isinstance(previous_artifact, dict):
            raise ValueError("previous vector artifact config must be a mapping")
        load_identity = (
            dict(previous_artifact)
            if previous_artifact is not None
            else artifact_identity
        )
        require_authorized_vector_view(
            output_dir,
            native_index_authorization,
            load_identity,
        )

        inc_state = IncrementalState.load(Path(output_dir))
        if inc_state is None:
            raise ValueError("incremental state is missing")
        if not last_commit:
            last_commit = inc_state.last_commit
        if not last_commit or inc_state.last_commit != last_commit:
            raise ValueError(
                "incremental state commit does not match the manifest start commit"
            )
        if inc_state.build_levels != list(self.build_levels):
            raise ValueError("incremental state levels do not match the vector view")
        if inc_state.chunk_store_path != "chunk_store.pkl":
            raise ValueError("incremental state has a non-canonical chunk store path")
        if inc_state.embeddings_cache_path != "embeddings_cache.pkl":
            raise ValueError(
                "incremental state has a non-canonical embeddings cache path"
            )
        expected_index_path = Path(output_dir).expanduser().resolve()
        if (
            not inc_state.index_path
            or Path(inc_state.index_path).expanduser().resolve() != expected_index_path
        ):
            raise ValueError("incremental state points to a different vector view")

        chunk_store_path = Path(output_dir) / "chunk_store.pkl"
        embeddings_cache_path = Path(output_dir) / "embeddings_cache.pkl"

        # Schema-v6 compiler state always uses the canonical JSON/NPZ forms.
        # Legacy pickle-only state is rebuilt through the builder-schema gate.
        chunk_store_json = chunk_store_path.with_suffix(".json")
        emb_cache_json = embeddings_cache_path.with_suffix(".json")
        emb_cache_npz = embeddings_cache_path.with_suffix(".npz")

        has_chunk_store = chunk_store_json.is_file()
        has_emb_cache = emb_cache_json.is_file() and emb_cache_npz.is_file()

        if not has_chunk_store or not has_emb_cache:
            raise ValueError("incremental chunk or embedding state is missing")
        raise _AtomicIncrementalRebuildRequired(
            "in-place vector delta publication is disabled; rebuild one "
            "complete generation"
        )


_ZOEKT_SHARD_TREE_RECEIPT_SCHEMA = "codenib.zoekt-shard-tree.v1"
_ZOEKT_MAX_SOURCE_ENTRIES = 500_000
_ZOEKT_MAX_SOURCE_METADATA_BYTES = 128 * 1024 * 1024
_ZOEKT_MAX_SOURCE_PATH_BYTES = 4_096
_ZOEKT_MAX_SOURCE_PATH_COMPONENTS = 256
_ZOEKT_BLOB_COPY_BYTES = 1024 * 1024
_ZOEKT_PROTECTED_FLAGS = frozenset({"branches", "index", "prefix", "submodules"})


@dataclass(frozen=True, slots=True)
class _ZoektGitTreeEntry:
    mode: str
    object_id: str
    path: str


def _zoekt_hash_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _zoekt_git_env() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _zoekt_extra_args(extra_args: object) -> list[str]:
    if type(extra_args) is not list:
        raise TypeError("Zoekt extra_args must be a list of single-token flags")
    selected: list[str] = []
    for argument in extra_args:
        if (
            type(argument) is not str
            or not argument.startswith("-")
            or argument in {"-", "--"}
            or "=" not in argument
            or "\x00" in argument
            or any(character.isspace() for character in argument)
        ):
            raise ValueError(
                "Zoekt extra_args must use single-token flags with = values"
            )
        flag = argument.lstrip("-").partition("=")[0]
        if not flag or flag in _ZOEKT_PROTECTED_FLAGS:
            raise ValueError("Zoekt extra_args cannot override a protected flag")
        selected.append(argument)
    return selected


def _zoekt_source_commit(repo_path: str, requested: object) -> str:
    if requested is None:
        revision = "HEAD^{commit}"
        expected = None
    elif (
        type(requested) is str
        and len(requested) == 40
        and all(character in "0123456789abcdef" for character in requested)
    ):
        revision = f"{requested}^{{commit}}"
        expected = requested
    else:
        raise RuntimeError(
            "Zoekt could not bind an immutable Git source commit; "
            "no Zoekt output was created"
        )

    resolved = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", revision],
        check=False,
        capture_output=True,
        env=_zoekt_git_env(),
    )
    try:
        source_commit = resolved.stdout.strip().decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            "Zoekt could not bind an immutable Git source commit; "
            "no Zoekt output was created"
        ) from exc
    if (
        resolved.returncode != 0
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit)
        or (expected is not None and source_commit != expected)
    ):
        raise RuntimeError(
            "Zoekt could not bind an immutable Git source commit; "
            "no Zoekt output was created"
        )
    return source_commit


def _zoekt_commit_tree(
    repo_path: str,
    source_commit: str,
    source_selection: RepositorySourceSelection,
) -> tuple[list[_ZoektGitTreeEntry], str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            repo_path,
            "ls-tree",
            "-rz",
            "--full-tree",
            source_commit,
        ],
        check=False,
        capture_output=True,
        env=_zoekt_git_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Zoekt could not prove the selected tracked-file surface; "
            "no Zoekt output was created"
        )

    entries: list[_ZoektGitTreeEntry] = []
    paths: set[str] = set()
    excluded_paths: list[str] = []
    metadata_bytes = 0
    tree_digest = hashlib.sha256()
    tree_digest.update(b"codenib.zoekt-commit-tree.v2\0")
    raw_entry_count = 0
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        raw_entry_count += 1
        if raw_entry_count > _ZOEKT_MAX_SOURCE_ENTRIES:
            raise RuntimeError("Zoekt commit tree exceeds its entry limit")
        raw_header, separator, raw_path = raw_entry.partition(b"\t")
        fields = raw_header.split(b" ")
        if not separator or not raw_path or len(fields) != 3:
            raise RuntimeError("Zoekt commit tree has a malformed entry")
        try:
            mode, object_type, object_id = (
                field.decode("ascii", errors="strict") for field in fields
            )
        except UnicodeDecodeError as exc:
            raise RuntimeError("Zoekt commit tree has a malformed entry") from exc
        if len(object_id) != 40 or any(
            character not in "0123456789abcdef" for character in object_id
        ):
            raise RuntimeError("Zoekt commit tree has a malformed object id")
        try:
            relative = repository_relative_source_path(
                repo_path,
                os.fsdecode(raw_path),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Zoekt tracked-file surface contains a non-canonical path"
            ) from exc
        encoded_path = os.fsencode(relative)
        if (
            len(encoded_path) > _ZOEKT_MAX_SOURCE_PATH_BYTES
            or len(relative.split("/")) > _ZOEKT_MAX_SOURCE_PATH_COMPONENTS
        ):
            raise RuntimeError("Zoekt commit tree path exceeds its supported limits")
        metadata_bytes += len(encoded_path) + 96
        if metadata_bytes > _ZOEKT_MAX_SOURCE_METADATA_BYTES:
            raise RuntimeError("Zoekt commit tree exceeds its metadata limit")
        if relative in paths:
            raise RuntimeError("Zoekt commit tree contains a duplicate path")
        paths.add(relative)

        visible = repository_path_is_visible(
            relative,
            selection=source_selection,
        )
        if not visible:
            excluded_paths.append(relative)
        elif mode == "160000" and object_type == "commit":
            raise RuntimeError(
                "Zoekt cannot publish a visible gitlink/submodule: " + relative
            )
        elif (mode, object_type) not in {
            ("100644", "blob"),
            ("100755", "blob"),
            ("120000", "blob"),
        }:
            raise RuntimeError(
                "Zoekt commit tree contains an unsupported visible entry: " + relative
            )

        for value in (mode.encode("ascii"), object_id.encode("ascii"), encoded_path):
            _zoekt_hash_field(tree_digest, value)
        if visible:
            entries.append(
                _ZoektGitTreeEntry(
                    mode=mode,
                    object_id=object_id,
                    path=relative,
                )
            )

    if excluded_paths:
        raise RuntimeError(
            "Zoekt cannot prove the repository source selection because "
            "tracked files are excluded: " + ", ".join(excluded_paths[:5])
        )
    return entries, tree_digest.hexdigest()


def _zoekt_index_surface(
    repo_path: str,
    entries: list[_ZoektGitTreeEntry],
    source_selection: RepositorySourceSelection,
) -> None:
    result = subprocess.run(
        ["git", "-C", repo_path, "ls-files", "--stage", "-z"],
        check=False,
        capture_output=True,
        env=_zoekt_git_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Zoekt could not prove the selected Git index surface; "
            "no Zoekt output was created"
        )
    expected = {entry.path: (entry.mode, entry.object_id) for entry in entries}
    observed: dict[str, tuple[str, str]] = {}
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        raw_header, separator, raw_path = raw_entry.partition(b"\t")
        fields = raw_header.split(b" ")
        if not separator or not raw_path or len(fields) != 3:
            raise RuntimeError("Zoekt Git index has a malformed entry")
        try:
            mode, object_id, stage = (
                field.decode("ascii", errors="strict") for field in fields
            )
            relative = repository_relative_source_path(
                repo_path,
                os.fsdecode(raw_path),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Zoekt Git index has a malformed entry") from exc
        if not repository_path_is_visible(relative, selection=source_selection):
            continue
        if stage != "0" or relative in observed:
            raise RuntimeError(
                "Zoekt cannot publish because the selected Git index is unmerged"
            )
        observed[relative] = (mode, object_id)
    if observed != expected:
        raise RuntimeError(
            "Zoekt cannot publish because the selected Git index differs "
            "from the immutable commit"
        )


def _zoekt_selected_untracked(
    repo_path: str,
    source_selection: RepositorySourceSelection,
) -> list[str]:
    result = subprocess.run(
        ["git", "-C", repo_path, "ls-files", "--others", "-z"],
        check=False,
        capture_output=True,
        env=_zoekt_git_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Zoekt could not prove the selected untracked-file surface; "
            "no Zoekt output was created"
        )
    selected: list[str] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = repository_relative_source_path(
                repo_path,
                os.fsdecode(raw_path),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Zoekt untracked-file surface contains a non-canonical path"
            ) from exc
        if repository_path_is_visible(relative, selection=source_selection):
            selected.append(relative)
    return selected


def _zoekt_read_exact(stream: Any, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining:
        chunk = stream.read(remaining)
        if not isinstance(chunk, bytes) or not chunk:
            raise RuntimeError("Zoekt could not read an immutable Git blob")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _zoekt_read_fd_exact(descriptor: int, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise RuntimeError("Zoekt selected worktree file was truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _zoekt_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _zoekt_open_parent(repo_path: str, relative: str) -> tuple[int, str]:
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or os.readlink not in os.supports_dir_fd
    ):
        raise RuntimeError(
            "Zoekt cannot prove lexical worktree containment on this host"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(
            os.path.abspath(os.path.expanduser(repo_path)),
            flags,
        )
        parts = relative.split("/")
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child)
                raise RuntimeError(
                    "Zoekt selected worktree path has a non-directory ancestor: "
                    + relative
                )
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _zoekt_verify_regular_blob(
    git_stream: Any,
    blob_size: int,
    parent: int,
    name: str,
    entry: _ZoektGitTreeEntry,
    metadata: os.stat_result,
) -> None:
    expected_executable = entry.mode == "100755"
    if not stat.S_ISREG(metadata.st_mode) or bool(metadata.st_mode & 0o111) != (
        expected_executable
    ):
        raise RuntimeError(
            "Zoekt selected worktree mode differs from the immutable commit: "
            + entry.path
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise RuntimeError(
                "Zoekt selected worktree file differs from the immutable commit: "
                + entry.path
            )
        if opened.st_size != blob_size:
            raise RuntimeError(
                "Zoekt selected worktree bytes differ from the immutable commit: "
                + entry.path
            )
        remaining = blob_size
        while remaining:
            chunk_size = min(remaining, _ZOEKT_BLOB_COPY_BYTES)
            expected = _zoekt_read_exact(git_stream, chunk_size)
            actual = _zoekt_read_fd_exact(descriptor, chunk_size)
            if actual != expected:
                raise RuntimeError(
                    "Zoekt selected worktree bytes differ from the immutable "
                    "commit: " + entry.path
                )
            remaining -= chunk_size
        if os.read(descriptor, 1):
            raise RuntimeError(
                "Zoekt selected worktree file grew while it was verified: " + entry.path
            )
        after = os.fstat(descriptor)
        path_after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _zoekt_file_identity(after) != _zoekt_file_identity(opened) or (
            path_after.st_dev,
            path_after.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise RuntimeError(
                "Zoekt selected worktree file changed while it was verified: "
                + entry.path
            )
    finally:
        os.close(descriptor)


def _zoekt_verify_worktree_blobs(
    repo_path: str,
    entries: list[_ZoektGitTreeEntry],
) -> None:
    if not entries:
        return
    try:
        process = subprocess.Popen(
            ["git", "-C", repo_path, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=_zoekt_git_env(),
        )
    except OSError as exc:
        raise RuntimeError(
            "Zoekt could not open the immutable Git blob surface"
        ) from exc
    succeeded = False
    try:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Zoekt could not open the immutable Git blob surface")
        for entry in entries:
            process.stdin.write(entry.object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline()
            fields = header.rstrip(b"\n").split(b" ")
            if len(fields) != 3:
                raise RuntimeError("Zoekt received a malformed Git blob header")
            try:
                blob_id = fields[0].decode("ascii", errors="strict")
                object_type = fields[1].decode("ascii", errors="strict")
                blob_size = int(fields[2].decode("ascii", errors="strict"))
            except ValueError as exc:
                raise RuntimeError(
                    "Zoekt received a malformed Git blob header"
                ) from exc
            if blob_id != entry.object_id or object_type != "blob" or blob_size < 0:
                raise RuntimeError("Zoekt received the wrong immutable Git blob")

            try:
                parent, name = _zoekt_open_parent(repo_path, entry.path)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(
                    "Zoekt selected worktree path is missing or not lexical: "
                    + entry.path
                ) from exc
            try:
                try:
                    metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except OSError as exc:
                    raise RuntimeError(
                        "Zoekt selected worktree path is missing: " + entry.path
                    ) from exc
                if entry.mode == "120000":
                    if not stat.S_ISLNK(metadata.st_mode):
                        raise RuntimeError(
                            "Zoekt selected worktree link differs from the "
                            "immutable commit: " + entry.path
                        )
                    target = os.fsencode(os.readlink(name, dir_fd=parent))
                    expected = _zoekt_read_exact(process.stdout, blob_size)
                    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if target != expected or _zoekt_file_identity(
                        after
                    ) != _zoekt_file_identity(metadata):
                        raise RuntimeError(
                            "Zoekt selected worktree link differs from the "
                            "immutable commit: " + entry.path
                        )
                else:
                    _zoekt_verify_regular_blob(
                        process.stdout,
                        blob_size,
                        parent,
                        name,
                        entry,
                        metadata,
                    )
            finally:
                os.close(parent)
            if _zoekt_read_exact(process.stdout, 1) != b"\n":
                raise RuntimeError("Zoekt received a malformed Git blob payload")
        process.stdin.close()
        returncode = process.wait()
        if returncode != 0:
            raise RuntimeError("Zoekt could not read the immutable Git blob surface")
        succeeded = True
    except OSError as exc:
        raise RuntimeError(
            "Zoekt could not verify the immutable Git blob surface"
        ) from exc
    finally:
        if not succeeded:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            process.wait()


def _zoekt_shard_tree_receipt(
    output_dir: str,
) -> tuple[dict[str, Any], int, Any]:
    from pathlib import Path

    from .._atomic_directory import (
        capture_directory_ownership,
        directory_ownership_file_records,
        directory_ownership_inventory,
        require_publishable_directory_ownership,
    )

    def require_flat_regular_file(
        relative: str,
        kind: str,
        _mode: int,
        _size: int,
    ) -> None:
        if kind != "file" or "/" in relative:
            raise RuntimeError(
                "Zoekt shard output must be a flat tree of regular files"
            )

    ownership = capture_directory_ownership(
        Path(output_dir),
        allow_empty_root=True,
        entry_policy=require_flat_regular_file,
    )
    require_publishable_directory_ownership(ownership, label="Zoekt shard tree")
    inventory = directory_ownership_inventory(ownership)
    records = directory_ownership_file_records(ownership)
    if len(inventory) != len(records):
        raise RuntimeError("Zoekt shard output contains a non-file entry")
    files = [
        {
            "path": record.path,
            "size": record.size,
            "sha256": record.sha256,
        }
        for record in records
    ]
    shard_count = sum(record.path.endswith(".zoekt") for record in records)
    if shard_count == 0:
        raise RuntimeError("zoekt-git-index produced no .zoekt shards")
    canonical = json.dumps(
        {
            "schema": _ZOEKT_SHARD_TREE_RECEIPT_SCHEMA,
            "files": files,
        },
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    receipt = {
        "schema": _ZOEKT_SHARD_TREE_RECEIPT_SCHEMA,
        "digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        "file_count": len(files),
        "total_bytes": sum(record.size for record in records),
        "files": files,
    }
    return receipt, shard_count, ownership


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
    source_selection: RepositorySourceSelection = field(
        default_factory=RepositorySourceSelection
    )

    def __post_init__(self) -> None:
        self.source_selection = _snapshot_source_selection(self.source_selection)

    def artifact_identity(self) -> Dict[str, Any]:
        selection = _snapshot_source_selection(self.source_selection)
        return {
            "builder_schema": 3,
            "source_contract": "immutable-git-commit-tree-v2",
            "worktree_contract": "selected-files-match-source-commit-v2",
            "shard_tree_receipt_schema": _ZOEKT_SHARD_TREE_RECEIPT_SCHEMA,
            "submodules": False,
            "source_selection_digest": selection.digest,
        }

    def build(self, scope: str, **kwargs: Any) -> IndexStatus:
        source_selection = _source_selection_for_build(
            self.source_selection,
            kwargs.get("source_selection"),
        )
        if source_selection.exclude_subtrees:
            raise RuntimeError(
                "Zoekt does not support custom repository source selection; "
                "no Zoekt output was created"
            )
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]
        extra_args = _zoekt_extra_args(self.extra_args)
        source_commit = _zoekt_source_commit(
            repo_path,
            kwargs.get("source_commit"),
        )
        tracked_entries, tracked_digest = _zoekt_commit_tree(
            repo_path,
            source_commit,
            source_selection,
        )
        _zoekt_index_surface(repo_path, tracked_entries, source_selection)
        selected_untracked = _zoekt_selected_untracked(repo_path, source_selection)
        if selected_untracked:
            raise RuntimeError(
                "Zoekt cannot publish because selected untracked files are absent "
                "from its immutable commit: " + ", ".join(selected_untracked[:5])
            )
        _zoekt_verify_worktree_blobs(repo_path, tracked_entries)

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

        from pathlib import Path, PurePosixPath

        from .._atomic_directory import (
            PublicationDirectoryReader,
            reopen_authenticated_directory,
        )
        from .._captured_directory import OwnedDirectoryStage
        from ..index.embedding.builders import _PrivateBuildDirectory

        output_path = Path(output_dir).expanduser().absolute()
        publication_stage = OwnedDirectoryStage.prepare(
            output_path,
            allow_empty_destination=True,
        )

        def copy_generation(reader: PublicationDirectoryReader) -> None:
            for record in reader.file_records():
                relative = PurePosixPath(record.path)
                with reader.iter_authenticated_chunks(
                    relative,
                    max_bytes=record.size,
                ) as chunks:
                    publication_stage.write_file(
                        relative,
                        chunks,
                        mode=record.mode,
                        max_bytes=record.size,
                    )

        try:
            with _PrivateBuildDirectory(
                prefix=f".{output_path.name}.generation-build-",
                parent=output_path.parent,
            ) as build_root:
                writer_path = build_root.writer_path
                try:
                    writer_descriptor = int(writer_path.name)
                except ValueError as exc:
                    raise RuntimeError(
                        "Zoekt private writer path has no inherited descriptor"
                    ) from exc
                cmd = [
                    binary_path,
                    "-index",
                    str(writer_path),
                    *extra_args,
                    "-submodules=false",
                    f"-branches={source_commit}",
                    "-prefix=",
                    repo_path,
                ]
                logger.info("Building Zoekt index: %s", " ".join(cmd))
                try:
                    with build_root.path_operation("Zoekt shard generation"):
                        subprocess.run(
                            cmd,
                            check=True,
                            capture_output=True,
                            text=True,
                            env=_zoekt_git_env(),
                            pass_fds=(writer_descriptor,),
                        )
                except subprocess.CalledProcessError as exc:
                    raise RuntimeError(
                        f"zoekt-git-index failed (rc={exc.returncode}): "
                        f"{(exc.stderr or '').strip()}"
                    ) from exc

                (
                    shard_tree_receipt,
                    shard_count,
                    generation_ownership,
                ) = _zoekt_shard_tree_receipt(str(build_root.path))
                reopen_authenticated_directory(
                    build_root.path,
                    generation_ownership,
                    copy_generation,
                )
            publication_stage.publish()
        except BaseException:  # noqa: B036 - preserve publication interruptions
            try:
                publication_stage.discard()
            except BaseException:  # noqa: B036 - retain the generation failure
                logger.exception("Could not isolate failed Zoekt generation stage")
            raise

        return IndexStatus(
            index_type="zoekt",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                **self.artifact_identity(),
                "shard_count": shard_count,
                "shard_tree_receipt": shard_tree_receipt,
                "binary": binary_path,
                "tracked_source_count": len(tracked_entries),
                "tracked_source_sha256": tracked_digest,
                "source_commit": source_commit,
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
    source_selection: RepositorySourceSelection = field(
        default_factory=RepositorySourceSelection
    )
    allow_partial_languages: bool = False
    allow_partial_index: bool = False
    source_coverage_fallback: bool = False
    # Product onboarding may prohibit package-manager/build-system preparation
    # inside the target checkout. Existing index callers retain the historical
    # behavior unless they opt out explicitly.
    allow_project_preparation: bool = True
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
        source_selection = _snapshot_source_selection(self.source_selection)
        return self._artifact_identity_for_selection(source_selection)

    def _artifact_identity_for_selection(
        self,
        source_selection: RepositorySourceSelection,
    ) -> Dict[str, Any]:
        graph_languages = self.languages or [self.language]
        return {
            "builder_schema": 6,
            "languages": list(graph_languages),
            "graph_route": self.graph_route,
            "exclude_patterns": sorted(self.exclude_patterns),
            "allow_partial_languages": self.allow_partial_languages,
            "allow_partial_index": self.allow_partial_index,
            "source_coverage_fallback": self.source_coverage_fallback,
            "allow_project_preparation": self.allow_project_preparation,
            "target_dir": None,
            "repository_filter_policy": REPOSITORY_FILTER_POLICY_VERSION,
            "source_selection_digest": source_selection.digest,
        }

    def __post_init__(self) -> None:
        self.source_selection = _snapshot_source_selection(self.source_selection)
        if self.allow_partial_index and not self.source_coverage_fallback:
            raise ValueError(
                "allow_partial_index requires source_coverage_fallback so a "
                "partial compiler artifact cannot be published as complete"
            )

    def build(self, scope: str, **kwargs: Any) -> IndexStatus:
        source_selection = _source_selection_for_build(
            self.source_selection,
            kwargs.get("source_selection"),
        )
        artifact_identity = self._artifact_identity_for_selection(source_selection)
        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]

        from ..ls_router import build_graph_for_languages_with_report
        from ..scip_interface.query_surface import query_surface_sha256
        from .artifact_fingerprints import (
            graph_output_artifact_fingerprint,
            regular_file_fingerprint,
            scip_decoded_artifact_fingerprints,
        )

        os.makedirs(output_dir, exist_ok=True)
        graph_languages = self.languages or [self.language]
        effective_exclude_patterns = _selection_exclude_hints(
            self.exclude_patterns,
            source_selection,
        )
        build_kwargs = {
            "languages": graph_languages,
            "project_name": os.path.basename(os.path.abspath(repo_path)),
            "skip_level": None,
            "exclude_patterns": effective_exclude_patterns,
            "graph_route": self.graph_route,
            "capture_artifact_receipts": True,
        }
        if not self.allow_project_preparation:
            build_kwargs["allow_project_preparation"] = False
        if self.allow_partial_index:
            build_kwargs["allow_partial_index"] = True
        result = build_graph_for_languages_with_report(
            repo_path,
            output_dir,
            allow_partial=self.allow_partial_languages,
            **build_kwargs,
        )
        graph_output_receipt = result.graph_output_receipt

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
        elif graph_output_receipt is not None:
            writer_artifact = graph_output_artifact_fingerprint(
                output_dir,
                receipt=graph_output_receipt,
            )
            writer_query_surface = writer_artifact.pop("query_surface_sha256")
            if writer_query_surface != query_surface_sha256(graph):
                raise ValueError(
                    "symbol graph query surface changed after its writer returned"
                )

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
                exclude_patterns=effective_exclude_patterns,
                source_selection=source_selection,
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
        (
            source_selection_report,
            lsp_occurrence_artifact,
        ) = _constrain_and_save_selected_graph(
            graph,
            output_dir,
            source_selection,
        )
        graph_output_path = os.path.join(output_dir, "graph.pkl")
        graph_output_receipt = {
            "path": os.path.realpath(graph_output_path),
            **regular_file_fingerprint(graph_output_path),
            "query_surface_sha256": query_surface_sha256(graph),
        }

        node_count = len(graph.graph.vs)
        edge_count = len(graph.graph.es)
        if node_count == 0:
            raise RuntimeError("symbol graph builder returned an empty graph")
        available_languages = (
            list(graph_languages)
            if fallback_report is not None
            else result.available_languages
        )
        scip_decoded_artifacts = scip_decoded_artifact_fingerprints(
            output_dir,
            receipts=result.decoded_input_receipts,
            languages=list(result.available_languages),
            multi_language_layout=len(graph_languages) != 1,
        )
        graph_artifact_receipt = graph_output_artifact_fingerprint(
            output_dir,
            receipt=graph_output_receipt,
        )
        writer_query_surface = graph_artifact_receipt.pop("query_surface_sha256")
        observed_query_surface = query_surface_sha256(graph)
        if writer_query_surface != observed_query_surface:
            raise ValueError(
                "symbol graph query surface changed after its writer returned"
            )
        graph_artifact = graph_artifact_receipt

        return IndexStatus(
            index_type="symbol_graph",
            state=IndexState.FRESH,
            last_built=time.time(),
            age_seconds=0.0,
            scope=scope,
            path=output_dir,
            metadata={
                **artifact_identity,
                "node_count": node_count,
                "edge_count": edge_count,
                "language": available_languages[0],
                "available_languages": available_languages,
                "compiler_available_languages": result.available_languages,
                "index_generation_reports": result.index_generation_reports,
                "scip_decoded_artifacts": scip_decoded_artifacts,
                "graph_artifact": graph_artifact,
                "lsp_occurrence_artifact": lsp_occurrence_artifact,
                "query_surface_sha256": observed_query_surface,
                "update_mode": "full_rebuild",
                "partial_index": bool(compiler_partial_languages),
                "failed_languages": result.failed_languages,
                "partial": result.partial,
                **(
                    {"source_coverage_report": fallback_report}
                    if fallback_report is not None
                    else {}
                ),
                "source_selection_report": source_selection_report,
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
        source_selection = _source_selection_for_build(
            self.source_selection,
            kwargs.get("source_selection"),
        )
        previous_artifact = kwargs.get("previous_artifact_config")
        if previous_artifact is not None and not isinstance(previous_artifact, dict):
            raise ValueError("previous graph artifact config must be a mapping")
        selection_changed = (
            previous_artifact is None and bool(source_selection.exclude_subtrees)
        ) or (
            previous_artifact is not None
            and previous_artifact.get("source_selection_digest")
            != source_selection.digest
        )

        repo_path: str = kwargs["repo_path"]
        output_dir: str = kwargs["output_dir"]
        last_commit: str = kwargs.get("last_commit", "") or ""

        reason = (
            "source selection changed since the previous graph generation"
            if selection_changed
            else self._incremental_blocker(repo_path, output_dir, last_commit)
        )
        if reason:
            logger.info("symbol_graph: full rebuild (%s)", reason)
            return self.build(
                scope, **{k: v for k, v in kwargs.items() if k != "last_commit"}
            )

        try:
            status = self._patch_graph(
                scope,
                repo_path,
                output_dir,
                last_commit,
                source_selection=source_selection,
            )
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
        self,
        scope: str,
        repo_path: str,
        output_dir: str,
        last_commit: str,
        *,
        source_selection: Optional[RepositorySourceSelection] = None,
    ) -> Optional[IndexStatus]:
        """Run the LSP patcher over every configured language. None = rebuild."""
        from ..graph.code_graph import CodeGraph
        from ..graph.incremental.graph_patcher import LANGUAGE_EXTENSIONS, GraphPatcher
        from ..graph.incremental.patcher_base import IncrementalPatchRebuildRequired

        graph_path = os.path.join(output_dir, "graph.pkl")
        graph = CodeGraph.load_graph(graph_path)
        occurrence_path = os.path.join(output_dir, "lsp_index.pkl")
        has_prior_occurrence_index = False
        if os.path.isfile(occurrence_path):
            from ..scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex

            # Incremental patching explicitly starts from the prior complete
            # generation.  Keep that handoff local to this path rather than
            # letting the full-build publication helper discover ambient
            # sidecars in its destination directory.
            graph.lsp_occurrence_index = SCIPOccurrenceIndex.load(occurrence_path)
            has_prior_occurrence_index = True
        selected = _source_selection_for_build(
            self.source_selection,
            source_selection,
        )
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
                if has_prior_occurrence_index:
                    # GraphPatcher updates graph nodes and edges but has no
                    # occurrence-sidecar delta contract.  Admitting the graph
                    # while republishing the previous occurrence rows would
                    # create a mixed generation, so source changes require a
                    # full rebuild whenever that sidecar exists.
                    logger.info(
                        "symbol_graph: full rebuild requested for %s "
                        "(occurrence index has no incremental update contract)",
                        lang,
                    )
                    return None

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

        (
            source_selection_report,
            lsp_occurrence_artifact,
        ) = _constrain_and_save_selected_graph(
            graph,
            output_dir,
            selected,
        )
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
                **self._artifact_identity_for_selection(selected),
                "node_count": node_count,
                "language": graph_languages[0],
                "update_mode": "incremental",
                "changed_files": changed_total,
                "patch_seconds": round(elapsed, 3),
                "earlier_commit": last_commit,
                "later_commit": head,
                "patch_stats": per_language,
                "source_selection_report": source_selection_report,
                "lsp_occurrence_artifact": lsp_occurrence_artifact,
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
    additional_chunk_ignore_dirs: Optional[List[str]] = None,
    source_selection: RepositorySourceSelection = DEFAULT_REPOSITORY_SOURCE_SELECTION,
    allow_partial_graph_languages: bool = False,
    allow_partial_graph_index: bool = False,
    graph_source_coverage_fallback: bool = False,
    allow_graph_project_preparation: bool = True,
) -> None:
    """Register all standard index builders with sensible defaults."""
    selected = _snapshot_source_selection(source_selection)
    langs = languages or ["python"]
    chunk_ignore_dirs = sorted(set(additional_chunk_ignore_dirs or ()))
    registry.register(
        "bm25",
        BM25IndexBuilder(
            languages=langs,
            additional_ignore_dirs=chunk_ignore_dirs,
            source_selection=selected,
        ),
    )

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
            additional_ignore_dirs=chunk_ignore_dirs,
            source_selection=selected,
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
            allow_project_preparation=allow_graph_project_preparation,
            exclude_patterns=(
                list(exclude_patterns)
                if exclude_patterns is not None
                else default_exclude_patterns()
            ),
            source_selection=selected,
        ),
    )
    # Zoekt is registered unconditionally; build() raises a clear error at
    # invocation time if the binary is unavailable so the IndexCompiler can
    # mark the entry as failed without aborting other index builds.
    registry.register("zoekt", ZoektIndexBuilder(source_selection=selected))
