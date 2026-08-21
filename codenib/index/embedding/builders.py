#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reusable embedding builders for hierarchical pipelines."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..._atomic_directory import (
    PublicationDirectoryReader,
    capture_directory_ownership,
    directory_ownership_root_identity,
    discard_owned_directory,
    lexical_directory_path,
    reopen_authenticated_directory,
)
from ..._captured_directory import OwnedDirectoryStage
from ...code_chunker import CodeChunker, RepoChunkingConfig
from ...log_utils import get_logger
from ...profiler import Profiler
from ...repository_filters import repository_path_is_visible
from ...repository_source_selection import (
    RepositorySourceSelection,
    repository_relative_source_path,
)
from ._lifecycle import close_vector_after_failure
from .artifact_integrity import VECTOR_VIEW_UPDATE_MARKER
from .vector_store import CodeVectorStore

if TYPE_CHECKING:
    from ...native_index_authorization import NativeIndexAuthorization

logger = get_logger(__name__)

_MODEL_LEVEL_FILE_TEMPLATES = (
    "config_{model_suffix}.json",
    "index_{model_suffix}.faiss",
    "documents_{model_suffix}.pkl",
    "documents_{model_suffix}.json",
    "index_{model_suffix}.pkl",
)
_GENERATION_STATE_ROOT_FILES = frozenset(
    {
        VECTOR_VIEW_UPDATE_MARKER,
        "chunk_store.json",
        "chunk_store.pkl",
        "embeddings_cache.json",
        "embeddings_cache.npz",
        "embeddings_cache.pkl",
        "incremental_state.json",
    }
)


def _metadata_root_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


class _PrivateBuildDirectory:
    """Retain and continuously verify one private path-based build root.

    Model/index libraries still require a filesystem path.  Capturing only
    after those libraries return is insufficient: a callback can rename the
    private root and recreate its old name.  This owner captures the empty root
    before the first callback, retains its directory descriptor where the host
    supports it, and brackets every path-based operation with a no-follow root
    binding check.
    """

    def __init__(self, *, parent: Path, prefix: str) -> None:
        self.path = lexical_directory_path(
            Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
        )
        self._descriptor = -1
        self._closed = False
        self._dirty = False
        self._writer_path: Path | None = None
        self._cleanup_ownership = capture_directory_ownership(self.path)
        self._root_identity = directory_ownership_root_identity(self._cleanup_ownership)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            try:
                self._descriptor = os.open(self.path, flags)
            except OSError:
                if os.name != "nt":
                    raise
            self.verify("private build root initialization")
            if self._descriptor >= 0:
                for descriptor_root in (Path("/proc/self/fd"), Path("/dev/fd")):
                    candidate = descriptor_root / str(self._descriptor)
                    try:
                        if (
                            _metadata_root_identity(os.stat(candidate))
                            == self._root_identity
                        ):
                            self._writer_path = candidate
                            break
                    except OSError:
                        continue
        except BaseException as primary_error:
            if self._descriptor >= 0:
                os.close(self._descriptor)
                self._descriptor = -1
            try:
                discard_owned_directory(self.path, self._cleanup_ownership)
            except BaseException as cleanup_error:  # noqa: B036 - retain primary
                add_note = getattr(primary_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "private build initialization cleanup also failed: "
                        f"{cleanup_error}"
                    )
            raise

    @property
    def writer_path(self) -> Path:
        """Return a path whose resolution is anchored to the retained root fd."""

        self.verify("descriptor-anchored writer path selection")
        if self._writer_path is None:
            raise RuntimeError(
                "safe path-based vector builds require a descriptor-anchored "
                "directory path on this platform"
            )
        try:
            if (
                _metadata_root_identity(os.stat(self._writer_path))
                != self._root_identity
            ):
                raise RuntimeError("descriptor-anchored private build path changed")
        except OSError as exc:
            raise RuntimeError(
                "descriptor-anchored private build path changed"
            ) from exc
        return self._writer_path

    def require_writer_path(self, path: Path) -> None:
        if lexical_directory_path(path) != self.writer_path:
            raise RuntimeError(
                "path-based vector writer is not anchored to its private root"
            )

    def verify(self, operation: str) -> None:
        if self._closed:
            raise RuntimeError("private build root owner is closed")
        try:
            if self._descriptor >= 0:
                path_metadata = os.stat(self.path, follow_symlinks=False)
                path_identity = _metadata_root_identity(path_metadata)
                descriptor_identity = _metadata_root_identity(
                    os.fstat(self._descriptor)
                )
            else:
                ownership = capture_directory_ownership(
                    self.path,
                    allow_empty_root=True,
                )
                path_identity = directory_ownership_root_identity(ownership)
                descriptor_identity = path_identity
                path_metadata = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(
                f"private build root changed during {operation}"
            ) from exc
        if (
            not stat.S_ISDIR(path_metadata.st_mode)
            or path_identity != self._root_identity
            or descriptor_identity != self._root_identity
        ):
            raise RuntimeError(f"private build root changed during {operation}")

    @contextmanager
    def path_operation(self, operation: str):
        """Require the same retained root immediately before and after a call."""

        self.verify(f"before {operation}")
        self._dirty = True
        try:
            yield
        except BaseException as primary_error:  # noqa: B036 - retain interrupt
            try:
                self.verify(f"after failed {operation}")
            except BaseException as continuity_error:  # noqa: B036
                add_note = getattr(primary_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "private build root verification also failed: "
                        f"{continuity_error}"
                    )
            raise
        self.verify(f"after {operation}")

    def capture_ownership(self, *, required_root_file: str) -> object:
        self.verify("before final ownership capture")
        ownership = capture_directory_ownership(
            self.path,
            required_root_file=required_root_file,
        )
        if directory_ownership_root_identity(ownership) != self._root_identity:
            raise RuntimeError("private build root changed during final capture")
        self.verify("after final ownership capture")
        self._cleanup_ownership = ownership
        self._dirty = False
        return ownership

    def close(self) -> None:
        if self._closed:
            return
        cleanup_error: BaseException | None = None
        try:
            if self._dirty:
                self.verify("cleanup capture")
                ownership = capture_directory_ownership(
                    self.path,
                    allow_empty_root=True,
                )
                if directory_ownership_root_identity(ownership) != self._root_identity:
                    raise RuntimeError("private build root changed during cleanup")
                self._cleanup_ownership = ownership
        except BaseException as exc:  # noqa: B036 - close the retained handle
            cleanup_error = exc
        finally:
            if self._descriptor >= 0:
                try:
                    os.close(self._descriptor)
                except OSError as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
                self._descriptor = -1
            self._closed = True
        try:
            discard_owned_directory(self.path, self._cleanup_ownership)
        except BaseException as exc:  # noqa: B036 - report safe orphaning
            if cleanup_error is None:
                cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error

    def __enter__(self) -> _PrivateBuildDirectory:
        return self

    def __exit__(self, _exc_type, exc, _traceback) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:  # noqa: B036 - retain primary
            if exc is None:
                raise
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(f"private build root cleanup also failed: {cleanup_error}")


def _model_level_file_names(model_suffix: str) -> tuple[str, ...]:
    return tuple(
        template.format(model_suffix=model_suffix)
        for template in _MODEL_LEVEL_FILE_TEMPLATES
    )


def _replaced_generation_paths(
    model_suffix: str,
) -> frozenset[str]:
    paths = {
        f"config_{model_suffix}.json",
        f".config_{model_suffix}.json.save-in-progress",
        *_GENERATION_STATE_ROOT_FILES,
    }
    paths.update(
        f"{level}/{name}"
        for level in ("l0", "l2")
        for name in _model_level_file_names(model_suffix)
    )
    return frozenset(paths)


def _copy_captured_tree(
    reader: PublicationDirectoryReader,
    stage: OwnedDirectoryStage,
    *,
    excluded: frozenset[str] = frozenset(),
) -> None:
    """Copy authenticated regular files into an authority-owned private stage."""

    for record in reader.file_records():
        relative = PurePosixPath(record.path)
        if relative.as_posix() in excluded:
            continue
        with reader.iter_authenticated_chunks(
            relative,
            max_bytes=record.size,
        ) as chunks:
            stage.write_file(
                relative,
                chunks,
                mode=record.mode,
                max_bytes=record.size,
            )


def _profiler_section(profiler, label, metadata=None):
    """Return an active profiler section context if profiling is enabled."""
    if profiler is None:
        return nullcontext()
    return profiler.section(label, metadata)


def _snapshot_source_selection(
    source_selection: RepositorySourceSelection | None,
) -> RepositorySourceSelection:
    """Capture one detached exact-type source-selection policy."""

    if source_selection is None:
        return RepositorySourceSelection()
    if type(source_selection) is not RepositorySourceSelection:
        raise TypeError("source_selection must be a RepositorySourceSelection")
    return RepositorySourceSelection(source_selection.exclude_subtrees)


def _require_selected_paths(
    source_selection: RepositorySourceSelection,
    paths: Any,
    *,
    repository_root: str,
    subject: str,
) -> None:
    """Reject non-canonical or excluded repository-relative paths."""

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


def _require_selected_documents(
    vector_store: CodeVectorStore,
    source_selection: RepositorySourceSelection,
    *,
    repository_root: str,
) -> None:
    """Validate every vector document path against source selection."""

    for level in ("l0", "l2"):
        documents = getattr(vector_store, f"{level}_documents", ()) or ()
        paths = []
        for document in documents:
            metadata = getattr(document, "metadata", None)
            paths.append(metadata.get("file") if isinstance(metadata, dict) else None)
        _require_selected_paths(
            source_selection,
            paths,
            repository_root=repository_root,
            subject=f"vector {level} documents",
        )


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
    additional_ignore_dirs: Optional[List[str]] = None,
    source_selection: RepositorySourceSelection | None = None,
    artifact_metadata: Optional[Dict[str, Any]] = None,
    native_index_authorization: NativeIndexAuthorization | None = None,
    _atomic_publish: bool = True,
    _build_root_guard: _PrivateBuildDirectory | None = None,
) -> CodeVectorStore:
    """Build (or load) a hierarchical vector store (L0/L2) for a repository.

    When ``force_rebuild=False`` (the default) and a saved index for the
    requested ``embedding_model`` already exists at ``index_path``, the store
    is loaded from disk instead of re-chunking and re-embedding the repository.
    Pass ``force_rebuild=True`` to unconditionally rebuild. Rebuilds compose a
    complete private tree and atomically switch the directory only after save
    and ownership verification succeed.
    """
    selected = _snapshot_source_selection(source_selection)
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
    if not effective_force_rebuild:
        if config_path.exists():
            logger.info(
                "Pre-built index found at %s — loading instead of rebuilding "
                "(pass force_rebuild=True to override).",
                store_path,
            )
            artifact_contract = dict(artifact_metadata or {})
            from .artifact_integrity import require_authorized_vector_view

            require_authorized_vector_view(
                store_path,
                native_index_authorization,
                artifact_contract,
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
                artifact_metadata=artifact_contract,
                **(embedding_kwargs or {}),
            )
            try:
                vector_store.load(
                    str(store_path),
                    native_index_authorization=native_index_authorization,
                )
                _require_selected_documents(
                    vector_store,
                    selected,
                    repository_root=repo_path,
                )
                return vector_store
            except BaseException as primary:  # noqa: B036 - close partial state
                close_vector_after_failure(vector_store, primary)
                raise
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
    repo_cfg = RepoChunkingConfig(
        languages=languages,
        source_selection=selected,
    )
    repo_cfg.ignore_dirs.update(additional_ignore_dirs or ())

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
        _require_selected_paths(
            selected,
            (getattr(chunk, "file", None) for chunk in chunks_by_level[level]),
            repository_root=repo_path,
            subject=f"vector {level} chunks",
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

    def materialize(
        build_path: Path,
        build_root: _PrivateBuildDirectory,
    ) -> CodeVectorStore:
        with build_root.path_operation("vector store initialization"):
            vector_store = CodeVectorStore(
                embedding_model=embedding_model,
                embedding_provider=embedding_provider,
                dimension=embedding_dimension,
                index_metric=index_metric,
                index_type=index_type,
                ivf_nlist=ivf_nlist,
                ivf_nprobe=ivf_nprobe,
                store_path=str(build_path),
                profiler=profiler,
                embedding=embedding,
                artifact_metadata=artifact_metadata,
                **(embedding_kwargs or {}),
            )

        if l0_chunks:
            with build_root.path_operation("L0 vector materialization"):
                vector_store.add_code_chunks(
                    [chunk._asdict() for chunk in l0_chunks], level="l0"
                )
        if l2_chunks:
            with build_root.path_operation("L2 vector materialization"):
                vector_store.add_code_chunks(
                    [chunk._asdict() for chunk in l2_chunks], level="l2"
                )
        _require_selected_documents(
            vector_store,
            selected,
            repository_root=repo_path,
        )
        with build_root.path_operation("vector store save"):
            vector_store.save(str(build_path))
        return vector_store

    if _atomic_publish:
        publication_stage = OwnedDirectoryStage.prepare(
            store_path,
            allow_empty_destination=True,
        )
        try:
            previous_ownership = publication_stage.expected_destination_ownership
            if previous_ownership is not None:
                reopen_authenticated_directory(
                    store_path,
                    previous_ownership,  # type: ignore[arg-type]
                    lambda reader: _copy_captured_tree(
                        reader,
                        publication_stage,
                        excluded=_replaced_generation_paths(model_suffix),
                    ),
                )

            with _PrivateBuildDirectory(
                prefix=f".{store_path.name}.vector-build-",
                parent=store_path.parent,
            ) as build_root:
                build_path = build_root.path
                vector_store = materialize(build_root.writer_path, build_root)
                build_ownership = build_root.capture_ownership(
                    required_root_file=f"config_{model_suffix}.json",
                )
                reopen_authenticated_directory(
                    build_path,
                    build_ownership,
                    lambda reader: _copy_captured_tree(reader, publication_stage),
                )

            publication_stage.publish()
        except BaseException:  # noqa: B036 - preserve interrupts across rollback
            try:
                publication_stage.discard()
            except BaseException:  # noqa: B036 - retain the publication failure
                logger.exception("Could not isolate failed vector publication stage")
            raise
    else:
        # The compiler uses this only inside its private full-generation tree;
        # the outer OwnedDirectoryStage is the sole publication transaction.
        if _build_root_guard is None or not isinstance(
            _build_root_guard, _PrivateBuildDirectory
        ):
            raise RuntimeError(
                "non-atomic vector materialization requires its private root owner"
            )
        _build_root_guard.require_writer_path(store_path)
        _build_root_guard.verify("non-atomic vector materialization entry")
        vector_store = materialize(store_path, _build_root_guard)

    vector_store.store_path = store_path
    vector_store.chunk_stats = chunk_stats
    _require_selected_documents(
        vector_store,
        selected,
        repository_root=repo_path,
    )
    return vector_store


class VectorStoreBuilder:
    """Builder class wrapping common vector store build operations."""

    def __init__(self, profiler: Optional[Profiler] = None) -> None:
        self.profiler = profiler

    def hierarchical(self, **kwargs) -> CodeVectorStore:
        return build_hierarchical_vector_store(profiler=self.profiler, **kwargs)
