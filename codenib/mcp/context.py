# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""ServerContext - loads a RepoManifest and selected index objects from disk.

Holds vector, symbol_graph, BM25, regex, and Zoekt indexes. Each loads
independently; failures land in ``ctx.errors`` and skipped or failed views keep
their corresponding attribute at ``None`` so tools can surface a clear error at
call time rather than blocking server startup.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from .._atomic_directory import (
    PublicationDirectoryReader,
    reopen_authenticated_directory,
)
from .._bounded_json import iter_bounded_json_array
from ..compiler.artifact_fingerprints import require_bm25_manifest_artifact
from ..compiler.manifest import RepoManifest
from ..provider_routes import resolve_embedding_artifact_route

if TYPE_CHECKING:
    from ..artifacts.runtime import ContextArtifactBinding
    from ..graph.code_graph import CodeGraph
    from ..index.embedding.vector_store import CodeVectorStore
    from ..index.regex_idx import RegexNodeIndex
    from ..index.sparse_idx import BM25CodeIndexer
    from ..index.trigram import ZoektSearcher
    from ..native_index_authorization import NativeIndexAuthorization
    from ..source_fingerprint import RepositorySourceBinding

logger = logging.getLogger(__name__)

_VIEW_LOADERS = (
    ("symbol_graph", "_load_symbol_graph"),
    ("bm25", "_load_bm25"),
    ("regex_index", "_load_regex_index"),
    ("zoekt", "_load_zoekt"),
    ("vector", "_load_vector"),
)
RUNTIME_VIEW_NAMES = frozenset(name for name, _ in _VIEW_LOADERS)
_PORTABLE_ARTIFACT_RUNTIME_VIEWS = frozenset({"bm25", "vector"})
_MAX_ARTIFACT_METADATA_BYTES = 16 * 1024 * 1024
_MAX_ARTIFACT_DOCUMENTS_BYTES = 256 * 1024 * 1024
_VIEW_DEPENDENCIES = {
    "regex_index": frozenset({"symbol_graph"}),
}


def _reject_duplicate_json_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate artifact BM25 JSON key: {key}")
        result[key] = value
    return result


def _bounded_json_int(value: str) -> int:
    if len(value) > 1_024:
        raise ValueError("artifact BM25 JSON integer is too large")
    return int(value)


def _bounded_json_float(value: str) -> float:
    if len(value) > 1_024:
        raise ValueError("artifact BM25 JSON number is too large")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("artifact BM25 JSON number is not finite")
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"artifact BM25 JSON constant is not finite: {value}")


def _load_artifact_bm25_metadata(payload: bytes) -> Mapping[str, Any]:
    value = json.loads(
        payload,
        object_pairs_hook=_reject_duplicate_json_object,
        parse_int=_bounded_json_int,
        parse_float=_bounded_json_float,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("artifact BM25 metadata must be a JSON object")
    return value


class _DimensionProbeEmbedding:
    """Avoid model/API initialization while validating saved vector files."""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_query(self, _text: str) -> list[float]:
        return [0.0] * self.dimension

    def embed_documents(self, _texts: list[str]) -> list[list[float]]:
        raise RuntimeError("validation probe cannot embed documents")


def _close_vector(vector: CodeVectorStore) -> None:
    try:
        vector.close()
    except Exception as exc:
        logger.warning("Failed to release vector resources: %s", exc)


def _stop_zoekt(searcher: ZoektSearcher) -> None:
    try:
        searcher.stop()
    except Exception as exc:
        logger.warning("Failed to stop Zoekt runtime: %s", exc)


def _resolve_views(views: Iterable[str] | None) -> frozenset[str]:
    """Validate a view selection and add runtime dependencies."""

    if views is None:
        return RUNTIME_VIEW_NAMES
    if isinstance(views, (str, bytes)):
        raise TypeError("views must be an iterable of view names, not a string")

    requested = frozenset(views)
    invalid_types = sorted(
        {type(view).__name__ for view in requested if not isinstance(view, str)}
    )
    if invalid_types:
        raise TypeError(
            "view names must be strings; received " + ", ".join(invalid_types)
        )

    unknown = requested - RUNTIME_VIEW_NAMES
    if unknown:
        supported = ", ".join(sorted(RUNTIME_VIEW_NAMES))
        raise ValueError(
            f"unknown runtime views {sorted(unknown)!r}; supported: {supported}"
        )

    selected = set(requested)
    pending = list(requested)
    while pending:
        view = pending.pop()
        for dependency in _VIEW_DEPENDENCIES.get(view, ()):
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return frozenset(selected)


@dataclass
class ServerContext:
    """Runtime context for the MCP server.

    Holds the loaded manifest and runtime-loaded index objects. Missing or
    failed indexes stay ``None``; tools check at call time and return
    descriptive errors.
    """

    manifest: RepoManifest
    symbol_graph: Optional[CodeGraph] = None
    bm25: Optional[BM25CodeIndexer] = None
    regex_index: Optional[RegexNodeIndex] = None
    zoekt: Optional[ZoektSearcher] = None
    vector: Optional[CodeVectorStore] = None
    lsp_provider: Optional[Any] = field(default=None, repr=False)
    lsp_provider_selection: Dict[str, Any] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)
    artifact: Optional[Mapping[str, Any]] = None
    source_error: Optional[str] = "source binding has not been verified"
    _lsp_allow_native: bool = field(default=False, init=False, repr=False)
    _lsp_native_disabled_reason: str = field(
        default="local_source_not_verified", init=False, repr=False
    )
    _view_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _native_index_authorization: Optional[NativeIndexAuthorization] = field(
        default=None,
        repr=False,
    )
    _artifact_binding: Optional[ContextArtifactBinding] = field(
        default=None,
        repr=False,
    )
    _source_binding: Optional[RepositorySourceBinding] = field(
        default=None,
        repr=False,
    )

    @property
    def source_verified(self) -> bool:
        """Return whether live reads retain exact content-byte authority."""

        return self._source_binding is not None and self._source_binding.usable

    @property
    def source_verification_scope(self) -> str | None:
        """M1 authenticates v2 content bytes, never mutable Git HEAD state."""

        return "content-bytes" if self.source_verified else None

    @property
    def commit_verified(self) -> bool:
        """Mutable checkout commit provenance requires an M2 source snapshot."""

        return False

    def _install_repository_source(
        self,
        source: RepositorySourceBinding | None,
    ) -> None:
        """Publish a transferred source in this already-reachable owner."""

        self._source_binding = source

    def read_source_bytes(self, relative: str, *, max_bytes: int) -> bytes:
        """Read one repository file through the retained source authority."""

        if self._source_binding is None:
            raise RuntimeError(
                f"source reads are unavailable: {self.source_error or 'unverified'}"
            )
        try:
            return self._source_binding.read_bytes(relative, max_bytes=max_bytes)
        except Exception as exc:
            self.source_error = str(exc)
            raise

    def verify_source_status(self) -> bool:
        """Refresh whole-tree source truth before publishing verified status."""

        if self._source_binding is None:
            return False
        try:
            self._source_binding.verify_snapshot()
        except Exception as exc:
            self.source_error = self._source_binding.failure_reason or str(exc)
            return False
        self.source_error = None
        return True

    @classmethod
    def load(
        cls,
        manifest_path: RepoManifest | str | Path,
        *,
        views: Iterable[str] | None = None,
        artifact: Mapping[str, Any] | None = None,
        artifact_binding: ContextArtifactBinding | None = None,
        native_index_authorization: NativeIndexAuthorization | None = None,
        source_binding: RepositorySourceBinding | None = None,
    ) -> ServerContext:
        """Load a manifest and the selected runtime views.

        ``views=None`` preserves the MCP server's load-all behavior. Explicit
        selections avoid importing or starting unrelated view runtimes. Each
        selected view is loaded independently; a failure in one does not block
        the others. Failed views are recorded in ``errors``.
        """
        ctx: ServerContext | None = None
        try:
            selected = _resolve_views(views)
            if artifact_binding is not None and native_index_authorization is not None:
                raise ValueError(
                    "portable artifact bindings cannot authorize native vector parsing"
                )
            artifact_origin = artifact is not None or artifact_binding is not None
            if artifact_origin:
                disallowed = selected - _PORTABLE_ARTIFACT_RUNTIME_VIEWS
                if views is not None and disallowed:
                    raise ValueError(
                        "portable artifact contexts cannot load native views: "
                        + ", ".join(sorted(disallowed))
                    )
                selected &= _PORTABLE_ARTIFACT_RUNTIME_VIEWS
            if artifact_binding is not None:
                if (
                    not isinstance(manifest_path, RepoManifest)
                    or manifest_path.to_dict() != artifact_binding.manifest.to_dict()
                ):
                    raise ValueError(
                        "artifact runtime manifest must come from its verified binding"
                    )
                manifest = artifact_binding.manifest
            else:
                manifest = (
                    manifest_path
                    if isinstance(manifest_path, RepoManifest)
                    else RepoManifest.load(manifest_path)
                )

            ctx = cls(
                manifest=manifest,
                artifact=dict(artifact) if artifact is not None else None,
                _native_index_authorization=native_index_authorization,
                _artifact_binding=artifact_binding,
                _source_binding=None,
            )
            if artifact_binding is not None:
                artifact_binding.install_source_binding(
                    ctx._install_repository_source,
                    expected=source_binding,
                    require_expected=source_binding is not None,
                )
            else:
                ctx._install_repository_source(source_binding)
            owned_source = ctx._source_binding

            if owned_source is not None:
                from ..source_fingerprint import (
                    is_secure_source_fingerprint_v2,
                    lexical_repository_path,
                )

                if (
                    not owned_source.usable
                    or not is_secure_source_fingerprint_v2(manifest.source_fingerprint)
                    or owned_source.fingerprint != manifest.source_fingerprint
                    or owned_source.file_count != manifest.file_count
                    or owned_source.root != lexical_repository_path(manifest.repo_path)
                ):
                    raise ValueError(
                        "repository source authority does not match the manifest"
                    )
                owned_source.verify_snapshot()

            ctx.source_error = (
                None
                if owned_source is not None
                else "source binding has not been verified"
            )

            ctx.load_views(selected)
            ctx.configure_lsp_provider(
                allow_native=False,
                native_disabled_reason=(
                    "portable_artifact_uses_persisted_graph"
                    if artifact_origin
                    else "local_source_not_verified"
                ),
            )

            cap_summary = {k: v for k, v in manifest.capabilities.items() if v}
            loaded = [
                view
                for view, _ in _VIEW_LOADERS
                if getattr(ctx, view, None) is not None
            ]
            logger.info(
                "ServerContext ready  repo=%s  commit=%s  requested=%s  loaded=%s  "
                "capabilities=%s  errors=%s",
                manifest.repo_path,
                manifest.commit[:8] if manifest.commit else "N/A",
                sorted(selected),
                loaded or "none",
                cap_summary or "none",
                list(ctx.errors) or "none",
            )
            return ctx
        except BaseException as primary:  # noqa: B036 - preserve primary
            from ..artifacts.runtime import (
                _raise_source_cleanup_failure,
                _source_cleanup_owner_is_pending,
            )

            cleanup_source = (
                ctx._source_binding
                if ctx is not None and ctx._source_binding is not None
                else source_binding if artifact_binding is None else None
            )
            cleanup_failure: BaseException | None = None
            if cleanup_source is not None:
                try:
                    cleanup_source.close()
                except BaseException as exc:  # noqa: B036 - apply shared priority
                    cleanup_failure = exc
            pending_owner = (
                cleanup_source
                if _source_cleanup_owner_is_pending(cleanup_source)
                else None
            )
            _raise_source_cleanup_failure(
                primary,
                cleanup_failure,
                pending_owner,
            )

    @property
    def loaded_views(self) -> frozenset[str]:
        """Return the runtime views currently available in this context."""

        return frozenset(
            view
            for view, _loader_name in _VIEW_LOADERS
            if getattr(self, view, None) is not None
        )

    def load_views(
        self,
        views: Iterable[str],
        *,
        native_index_authorization: NativeIndexAuthorization | None = None,
    ) -> Dict[str, str]:
        """Load additional manifest views without disturbing loaded resources.

        View dependencies are resolved in the same way as :meth:`load`. The
        operation is idempotent and serialized so query-time planners can
        safely request only the backends selected for a query. The returned
        mapping contains requested views that remain unavailable.
        """

        selected = _resolve_views(views)
        with self._view_lock:
            if self.artifact is not None or self._artifact_binding is not None:
                disallowed = selected - _PORTABLE_ARTIFACT_RUNTIME_VIEWS
                if disallowed:
                    raise ValueError(
                        "portable artifact contexts cannot load native views: "
                        + ", ".join(sorted(disallowed))
                    )
            if native_index_authorization is not None:
                if self._artifact_binding is not None:
                    raise ValueError(
                        "portable artifact bindings cannot authorize native parsing"
                    )
                self._native_index_authorization = native_index_authorization
            for view, loader_name in _VIEW_LOADERS:
                if view not in selected or getattr(self, view, None) is not None:
                    continue
                getattr(self, loader_name)()
                if getattr(self, view, None) is not None:
                    self.errors.pop(view, None)
            if "symbol_graph" in selected:
                self.configure_lsp_provider(
                    allow_native=self._lsp_allow_native,
                    native_disabled_reason=self._lsp_native_disabled_reason,
                )
            return {
                view: self.errors.get(view, "view did not load")
                for view in selected
                if getattr(self, view, None) is None
            }

    def close(self) -> None:
        """Release runtime resources owned by this context."""

        with self._view_lock:
            self.lsp_provider = None
            if self.zoekt is not None:
                _stop_zoekt(self.zoekt)
                self.zoekt = None
            if self.vector is not None:
                _close_vector(self.vector)
                self.vector = None
            if self._source_binding is not None:
                self._source_binding.close()
                self._source_binding = None
                self.source_error = "source binding is closed"

    def configure_lsp_provider(
        self,
        *,
        allow_native: bool,
        native_disabled_reason: str = "native_provider_not_authorized",
    ) -> Dict[str, Any]:
        """Bind a runtime-only provider without mutating persisted artifacts."""

        from ..agent.lsp_provider import select_checkout_lsp_provider

        self._lsp_allow_native = allow_native
        self._lsp_native_disabled_reason = native_disabled_reason
        provider, selection = select_checkout_lsp_provider(
            project_root=self.manifest.repo_path,
            languages=self.manifest.languages,
            symbol_graph=self.symbol_graph,
            allow_native=allow_native,
            native_disabled_reason=native_disabled_reason,
        )
        self.lsp_provider = provider
        self.lsp_provider_selection = selection
        return dict(selection)

    @classmethod
    def validate_views(
        cls,
        manifest: RepoManifest | str | Path,
        *,
        views: Iterable[str],
        native_index_authorization: NativeIndexAuthorization | None = None,
    ) -> Dict[str, str]:
        """Open selected artifacts without initializing a vector query model.

        The returned mapping contains only unavailable views. Vector indexes
        follow the normal FAISS/document load path with a fixed-dimension
        embedding probe. Temporary vector and Zoekt resources are released
        before returning.
        """

        selected = _resolve_views(views)
        resolved_manifest = (
            manifest
            if isinstance(manifest, RepoManifest)
            else RepoManifest.load(manifest)
        )
        ctx = cls(
            manifest=resolved_manifest,
            _native_index_authorization=native_index_authorization,
        )
        available: set[str] = set()
        try:
            for view, loader_name in _VIEW_LOADERS:
                if view not in selected:
                    continue
                loader = getattr(ctx, loader_name)
                if view == "vector":
                    loader(probe=True)
                else:
                    loader()
                if getattr(ctx, view, None) is not None:
                    available.add(view)
            return {
                view: ctx.errors.get(view, "view did not load")
                for view in selected
                if view not in available
            }
        finally:
            if ctx.zoekt is not None:
                _stop_zoekt(ctx.zoekt)
                ctx.zoekt = None
            if ctx.vector is not None:
                _close_vector(ctx.vector)
                ctx.vector = None

    # ------------------------------------------------------------------
    # Private loaders
    # ------------------------------------------------------------------

    def _load_symbol_graph(self) -> None:
        entry = self.manifest.indexes.get("symbol_graph")
        if not entry or not self.manifest.index_is_current("symbol_graph"):
            return
        try:
            from ..graph.code_graph import CodeGraph

            graph_path = Path(entry.path)
            pkl = graph_path / "graph.pkl" if graph_path.is_dir() else graph_path
            self.symbol_graph = CodeGraph.load_graph(str(pkl))
            logger.info("Loaded symbol_graph from %s", pkl)
        except Exception as exc:
            self.errors["symbol_graph"] = str(exc)
            logger.warning("Failed to load symbol_graph: %s", exc)

    def _load_bm25(self) -> None:
        entry = self.manifest.indexes.get("bm25")
        if not entry or not self.manifest.index_is_current("bm25"):
            return
        try:
            from ..index.sparse_idx import BM25CodeIndexer
            from ..index.sparse_idx.bm25_index import SOURCE_MODE_PERSISTED_ONLY

            indexer = BM25CodeIndexer()
            if self._artifact_binding is None:
                if self.artifact is not None:
                    raise ValueError(
                        "portable BM25 loading requires a verified artifact binding"
                    )
                require_bm25_manifest_artifact(entry)
                indexer.load_index(entry.path)
            else:
                artifact = self._artifact_binding.artifact

                def load_portable_bm25(
                    reader: PublicationDirectoryReader,
                ) -> None:
                    metadata = _load_artifact_bm25_metadata(
                        reader.read_bytes(
                            "views/bm25/bm25_metadata.json",
                            max_bytes=_MAX_ARTIFACT_METADATA_BYTES,
                        )
                    )
                    with reader.open_authenticated_file(
                        "views/bm25/documents.json",
                        max_bytes=_MAX_ARTIFACT_DOCUMENTS_BYTES,
                    ) as documents:
                        indexer.load_index_values(
                            iter_bounded_json_array(
                                documents,
                                label="portable artifact BM25 documents",
                            ),
                            metadata,
                            source_mode=SOURCE_MODE_PERSISTED_ONLY,
                        )

                reopen_authenticated_directory(
                    artifact.root,
                    artifact.ownership,  # type: ignore[arg-type]
                    load_portable_bm25,
                )
            if self._source_binding is not None:
                indexer.bind_repository_source(self._source_binding)
            else:
                indexer.project_root = None
                indexer.source_mode = SOURCE_MODE_PERSISTED_ONLY
            self.bm25 = indexer
            logger.info("Loaded BM25 index from %s", entry.path)
        except Exception as exc:
            self.errors["bm25"] = str(exc)
            logger.warning("Failed to load BM25 index: %s", exc)

    def _load_regex_index(self) -> None:
        """Regex index piggybacks on symbol_graph; no separate manifest entry."""
        if self.symbol_graph is None:
            return
        try:
            from ..index.regex_idx import RegexNodeIndex

            self.regex_index = RegexNodeIndex(self.symbol_graph)
            logger.info("Built RegexNodeIndex (%d nodes)", len(self.regex_index.nodes))
        except Exception as exc:
            self.errors["regex_index"] = str(exc)
            logger.warning("Failed to build RegexNodeIndex: %s", exc)

    def _load_zoekt(self) -> None:
        """Spawn a ``zoekt-webserver`` against the indexed shard directory.

        The Zoekt binary is a soft dependency. When the binary is missing
        or the webserver fails to come up, the failure is recorded in
        :attr:`errors` and ``self.zoekt`` stays ``None`` so the
        ``search_zoekt`` MCP tool can return a clear error.
        """
        entry = self.manifest.indexes.get("zoekt")
        if not entry or not self.manifest.index_is_current("zoekt"):
            return
        try:
            from ..index.trigram import ZoektSearcher, ZoektUnavailableError
        except Exception as exc:
            self.errors["zoekt"] = str(exc)
            logger.warning("Failed to load Zoekt runtime: %s", exc)
            return

        searcher = None
        try:
            searcher = ZoektSearcher(index_dir=entry.path)
            searcher.start()
            self.zoekt = searcher
            logger.info(
                "Started zoekt-webserver  shards=%s  port=%d",
                entry.path,
                searcher.port,
            )
        except ZoektUnavailableError as exc:
            if searcher is not None:
                _stop_zoekt(searcher)
            self.errors["zoekt"] = str(exc)
            logger.warning("Zoekt unavailable: %s", exc)
        except Exception as exc:
            if searcher is not None:
                _stop_zoekt(searcher)
            self.errors["zoekt"] = str(exc)
            logger.warning("Failed to start zoekt-webserver: %s", exc)

    def _load_vector(self, *, probe: bool = False) -> None:
        """Load vector embedding index if available."""
        entry = self.manifest.indexes.get("vector")
        if not entry or not self.manifest.index_is_current("vector"):
            return
        if self._native_index_authorization is None:
            self.errors["vector"] = (
                "native vector parsing requires external authorization; "
                "portable artifacts remain inert by default"
            )
            logger.warning("Vector view is inert without external authorization")
            return

        vector = None
        try:
            from ..index.embedding.vector_store import CodeVectorStore

            cfg = entry.config
            route = resolve_embedding_artifact_route(cfg)
            kwargs = route.embedding_backend_kwargs()
            if probe:
                kwargs["embedding"] = _DimensionProbeEmbedding(route.dimension)
            else:
                kwargs.update(route.client_kwargs())

            vector = CodeVectorStore(
                embedding_model=route.model,
                embedding_provider=route.provider,
                dimension=route.dimension,
                index_metric=cfg.get("index_metric", "ip"),
                store_path=entry.path,
                artifact_metadata=cfg,
                **kwargs,
            )

            # Load FAISS index from disk
            vector.load(
                native_index_authorization=self._native_index_authorization,
            )

            # Validate model consistency
            loaded_model = vector.embedding_model
            if loaded_model != route.model:
                raise RuntimeError(
                    f"Embedding model mismatch: manifest specifies "
                    f"{route.model!r}, but loaded store has {loaded_model!r}. "
                    f"Re-run indexing with the correct model."
                )

            stats = vector.get_stats()
            if stats["total_documents"] <= 0:
                raise RuntimeError("vector index contains no documents")
            self.vector = vector
            logger.info(
                "Loaded vector index from %s (%d docs, model=%s)",
                entry.path,
                stats["total_documents"],
                route.model,
            )
        except Exception as exc:
            if vector is not None:
                _close_vector(vector)
            self.vector = None
            self.errors["vector"] = str(exc)
            logger.warning("Failed to load vector index: %s", exc)
