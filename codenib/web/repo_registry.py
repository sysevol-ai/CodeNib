# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Load pre-indexed dataset repos and expose one agent per repo.

Each repo gets its **own** ``SkillRegistry`` instance (not the global
singleton) so that skill executors stay bound to that repo's indexes. This
gives clean per-repo isolation and lets us build a ready-to-run ``AgentRunner``
once at startup and reuse it for every request — retrieval is read-only, so
concurrent queries are safe.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from importlib.util import find_spec
from threading import Lock, RLock
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Tuple

from .._atomic_directory import _annotate_secondary_error
from ..compiler.artifact_fingerprints import require_bm25_manifest_artifact
from ..compiler.manifest import IndexEntry, RepoManifest
from ..index.embedding._lifecycle import close_vector_after_failure
from ..log_utils import get_logger
from ..provider_routes import normalize_endpoint, resolve_embedding_artifact_route
from ..repository_source_selection import RepositorySourceSelection
from ..repository_summary import read_bound_repository_summary, read_repository_summary
from ..source_fingerprint import is_secure_source_fingerprint_v2
from .config import QAConfig, RepoEntry, load_registry
from .schemas import GraphCoverage, RepoInfo

if TYPE_CHECKING:
    from ..agent.runner import AgentRunner
    from ..graph.code_graph import CodeGraph
    from ..index.embedding.vector_store import CodeVectorStore
    from ..index.sparse_idx.bm25_index import BM25CodeIndexer
    from ..llm.litellm_chat import LiteLLMChat
    from ..native_index_authorization import NativeIndexAuthorization
    from ..source_fingerprint import RepositorySourceBinding, RepositorySourceReader

logger = get_logger(__name__)

_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent",
    "skills",
)

# Interactive Ask prompt: keep the model on the query-facing retrieval contract
# and require implementation evidence before it explains repository behaviour.
_DEMO_SYSTEM_PROMPT = (
    "You answer questions about a code repository for a developer explorer. "
    "Start with `repository_search`. For a broad or multi-part question, issue "
    "focused follow-up searches for the distinct concepts or lifecycle stages. "
    "When evidence exposes a candidate identifier, run a targeted search for "
    "its exact identifier and defining file; do not claim it is absent while "
    "an unresolved candidate identifier remains. "
    "Trace wrapper chains breadth-first: combine every unresolved identifier "
    "visible in the current evidence into one focused follow-up search instead "
    "of spending one search on each wrapper. Do not name a downstream "
    "implementation as fact until its implementation evidence was retrieved. "
    "Explain behaviour only after finding implementation evidence. Treat tests, "
    "examples, documentation, and validation scripts as corroboration, not as "
    "runtime mechanisms, unless the user asks about them explicitly. Distinguish "
    "build-time checks from runtime checks and current behaviour from intended "
    "design. For prevention, validation, or guarantee questions, find both the "
    "predicate and the loader or provider call site that acts on it; warnings "
    "and tests do not prove enforcement. Never infer behaviour from a function "
    "name or use speculative language such as 'likely' or 'presumably'. State "
    "the fields used by an actual branch or comparison: computing age or a "
    "timestamp after a predicate does not make time the freshness criterion. "
    "Distinguish building an artifact from marking it stale, publishing it, "
    "and loading it. Keep the final answer under 500 words unless the user asks "
    "for more depth. Write a direct, well-structured final answer and name the "
    "strongest two to five repository-relative files or symbols so the reader "
    "can open them. For every action requested by the user, follow wrappers to "
    "the concrete implementation; a name or docstring saying that a method "
    "writes, loads, validates, or publishes an artifact is not evidence that "
    "the action occurs. Include a helper only when the retrieved implementation "
    "shows it lies on the requested execution path; nearby search results are "
    "not architecture. If the evidence is incomplete, state that limitation "
    "instead of inventing a mechanism."
)


def _fresh_registry():
    """Create an isolated registry that bypasses the global singleton."""
    from ..agent.skills.registry import SkillRegistry

    reg = object.__new__(SkillRegistry)
    reg._skills = {}
    return reg


def _vector_store_type():
    from ..index.embedding.vector_store import CodeVectorStore

    return CodeVectorStore


def _ask_llm_type():
    from ..llm.litellm_chat import LiteLLMChat

    return LiteLLMChat


def _manifest_requires_authenticated_source(manifest: Any) -> bool:
    """Whether a persisted current-era manifest forbids live-source fallback."""

    return type(
        getattr(manifest, "source_selection", None)
    ) is RepositorySourceSelection and bool(getattr(manifest, "source_fingerprint", ""))


def _require_authenticated_source_paths(
    paths: Any,
    source_reader: "RepositorySourceReader",
    *,
    subject: str,
) -> None:
    """Reject persisted view paths absent from the authenticated source set."""

    for path in paths:
        if (
            not isinstance(path, str)
            or source_reader.captured_relative_path(path) is None
        ):
            raise ValueError(
                f"{subject} contains a source path outside the authenticated "
                f"repository: {path!r}"
            )


def _require_authenticated_documents(
    documents: Any,
    source_reader: "RepositorySourceReader",
    *,
    subject: str,
) -> None:
    paths = []
    for document in documents or ():
        metadata = getattr(document, "metadata", None)
        paths.append(metadata.get("file") if isinstance(metadata, dict) else None)
    _require_authenticated_source_paths(paths, source_reader, subject=subject)


@dataclass
class RepoBundle:
    """Everything needed to answer questions about one repo."""

    entry: RepoEntry
    manifest: RepoManifest
    runner: Optional["AgentRunner"] = None
    # Read-only handles reused by the wiki builder (index-derived docs).
    vector_store: Optional["CodeVectorStore"] = None
    bm25: Optional["BM25CodeIndexer"] = None
    chat_available: bool = False
    view_loader: Optional[Callable[["RepoBundle"], None]] = None
    runtime_loader: Optional[Callable[["RepoBundle"], None]] = None
    # Borrowed exact source reader. Its creator retains and closes the owning
    # binding; escaped readers become unusable when that owner closes it.
    source_reader: Optional["RepositorySourceReader"] = None

    def __post_init__(self) -> None:
        self._views_lock = Lock()
        self._runtime_lock = Lock()
        self._views_loaded = self.view_loader is None
        self._runtime_loaded = self.runner is not None

    def ensure_views(self) -> None:
        """Load retrieval views without constructing the interactive agent."""
        if self._views_loaded:
            return
        with self._views_lock:
            if self._views_loaded:
                return
            if self.view_loader is None:
                raise RuntimeError("repo view loader is not configured")
            self.view_loader(self)
            self._views_loaded = True

    def ensure_runtime(self) -> None:
        """Load retrieval views and the QA runner on first chat request."""
        self.ensure_views()
        if self._runtime_loaded:
            return
        with self._runtime_lock:
            if self._runtime_loaded:
                return
            # A standalone maintenance worker may have released the views
            # between the optimistic check above and this runtime lease.
            self.ensure_views()
            if self.runtime_loader is None:
                raise RuntimeError("repo runtime loader is not configured")
            self.runtime_loader(self)
            self._runtime_loaded = True

    def release_views(self) -> bool:
        """Release retrieval artifacts when no interactive runtime owns them.

        This is intended for bounded maintenance jobs such as Wiki prewarming.
        An initialized Ask runner retains its retrieval contexts, so those
        bundles deliberately stay loaded for the lifetime of the process.
        """

        with self._runtime_lock:
            if self._runtime_loaded:
                return False
            with self._views_lock:
                if self._runtime_loaded:
                    return False
                vector_store = self.vector_store
                self.vector_store = None
                self.bm25 = None
                self._views_loaded = self.view_loader is None
                self._file_count_cache = None
                if vector_store is not None:
                    vector_store.close()
                return True

    def info(self) -> RepoInfo:
        capabilities = dict(self.manifest.capabilities)
        # Advertise codemap only when the manifest declares a current, readable
        # symbol graph that this runtime can decode.
        graph_path = self._graph_path()
        graph_load_failed = getattr(self, "_code_graph_loaded", False) and (
            getattr(self, "_code_graph", None) is None
        )
        capabilities["codemap"] = bool(
            graph_path is not None
            and find_spec("igraph") is not None
            and self._graph_schema_is_current()
            and not graph_load_failed
        )
        capabilities["chat"] = self.chat_available
        display_name = self.entry.repo
        if self.entry.commit_short:
            display_name += f" @ {self.entry.commit_short}"
        return RepoInfo(
            id=self.entry.instance_id,
            name=display_name,
            repo=self.entry.repo,
            base_commit=self.entry.base_commit,
            commit_short=self.entry.commit_short,
            language=self.entry.language,
            description=self._description(),
            problem_statement=self.entry.problem_statement,
            languages=self.manifest.languages,
            file_count=self._file_count(),
            capabilities=capabilities,
            graph_coverage=self.graph_coverage(),
        )

    def graph_coverage(self) -> GraphCoverage | None:
        """Describe partial multi-language graph coverage when metadata exists."""

        entry = self.manifest.indexes.get("symbol_graph")
        if entry is None:
            return None
        metadata = entry.metadata or {}
        available = [
            str(language)
            for language in metadata.get("available_languages") or ()
            if str(language).strip()
        ]
        failures = metadata.get("failed_languages") or {}
        unavailable = (
            [str(language) for language in failures if str(language).strip()]
            if isinstance(failures, dict)
            else []
        )
        if not available and not unavailable:
            return None
        return GraphCoverage(
            available_languages=available,
            unavailable_languages=unavailable,
            partial=bool(metadata.get("partial") or unavailable),
        )

    def graph_setup(self):
        """Return repository-aware setup diagnostics for Dependency Map."""

        from ..graph.setup import diagnose_graph_setup

        languages = list(self.manifest.languages)
        if not languages and self.entry.language:
            languages = [self.entry.language]
        return diagnose_graph_setup(
            self.entry.repo_dir,
            languages,
        )

    def _graph_path(self) -> Optional[str]:
        """Locate the current manifest-bound symbol-graph pickle, if any."""
        cached = getattr(self, "_graph_path_cache", "?")
        if cached != "?":
            return cached
        found = None
        entry = self.manifest.indexes.get("symbol_graph")
        if (
            entry is not None
            and self.manifest.index_is_current("symbol_graph")
            and getattr(entry, "path", None)
        ):
            candidate = (
                entry.path
                if entry.path.endswith(".pkl")
                else os.path.join(entry.path, "graph.pkl")
            )
            if os.path.isfile(candidate):
                found = candidate
        self._graph_path_cache = found
        return found

    def _graph_schema_version(self) -> Optional[int]:
        """Return the persisted graph schema using a bounded, safe prefix read."""

        cached = getattr(self, "_graph_schema_version_cache", "?")
        if cached != "?":
            return cached
        path = self._graph_path()
        if path is None:
            version = None
        else:
            from ..graph.code_graph import persisted_graph_schema_version

            version = persisted_graph_schema_version(path)
        self._graph_schema_version_cache = version
        return version

    def _graph_schema_is_current(self) -> bool:
        from ..graph.code_graph import current_graph_schema_version

        return self._graph_schema_version() == current_graph_schema_version()

    def code_graph(self) -> Optional[CodeGraph]:
        """Lazily load + cache the repo's symbol graph (None if unavailable)."""
        if self.source_reader is None and _manifest_requires_authenticated_source(
            self.manifest
        ):
            self._code_graph_error = (
                "manifest-selected repository has no authenticated source reader"
            )
            return None
        if getattr(self, "_code_graph_loaded", False):
            return self._code_graph
        self._code_graph_loaded = True
        self._code_graph = None
        self._code_graph_error = None
        path = self._graph_path()
        if path is None:
            return None
        graph_entry = self.manifest.indexes.get("symbol_graph")
        if graph_entry is None or not self.manifest.index_is_current("symbol_graph"):
            self._code_graph_error = (
                "symbol graph has no current manifest-bound artifact entry"
            )
            return None
        try:
            from ..compiler.artifact_quality import graph_source_paths
            from ..compiler.graph_artifact import load_authenticated_graph_artifact

            self._code_graph = load_authenticated_graph_artifact(graph_entry)
            if self.source_reader is not None:
                _require_authenticated_source_paths(
                    graph_source_paths(self._code_graph),
                    self.source_reader,
                    subject="symbol graph",
                )
            logger.info(
                "codemap: loaded symbol graph for %r (%s)", self.entry.instance_id, path
            )
        except Exception as exc:  # noqa: BLE001 - stale/old-format graph: skip codemap
            logger.warning("codemap: graph at %s unusable: %s", path, exc)
            self._code_graph_error = str(exc)
            self._code_graph = None
        return self._code_graph

    def graph_unavailable_note(self) -> str:
        """Return an actionable reason when the dependency graph cannot load."""

        if find_spec("igraph") is None:
            return (
                "Dependency graph support is unavailable. Install CodeNib with "
                "the graph extra to inspect symbol_graph artifacts."
            )
        try:
            from ..graph.code_graph import current_graph_schema_version
        except ModuleNotFoundError as exc:
            return (
                "Dependency graph support is unavailable because the optional "
                f"dependency {exc.name!r} is missing. Install CodeNib with the "
                "graph extra."
            )

        persisted_schema = self._graph_schema_version()
        current_schema = current_graph_schema_version()
        if persisted_schema is not None and persisted_schema != current_schema:
            return (
                f"Dependency graph uses schema {persisted_schema}, but this server "
                f"requires schema {current_schema}. Rebuild symbol_graph for this "
                "repository."
            )
        load_error = str(getattr(self, "_code_graph_error", "") or "")
        schema_mismatch = re.search(
            r"schema_version=([^,]+), expected ([^.]+)", load_error
        )
        if schema_mismatch:
            observed, expected = schema_mismatch.groups()
            return (
                f"Dependency graph uses schema {observed}, but this server "
                f"requires schema {expected}. Rebuild symbol_graph for this "
                "repository."
            )
        entry = self.manifest.indexes.get("symbol_graph")
        if entry is None:
            return (
                "Dependency graph is not built. Use the graph profile to add "
                "a language-aware symbol graph for this repository."
            )
        if entry.status == "stale":
            return "Dependency graph is stale for this commit and must be updated."
        if entry.status == "failed":
            detail = str(entry.metadata.get("error") or "").strip()
            if detail:
                return f"Dependency graph build failed: {detail[:240]}"
            return "Dependency graph build failed; inspect the index build output."
        return "Dependency graph artifact could not be loaded; rebuild symbol_graph."

    def hierarchical_graph(self):
        """Lazily build + cache the repo-level compound graph for CodeGraph UI."""
        if self.source_reader is None and _manifest_requires_authenticated_source(
            self.manifest
        ):
            return None
        if getattr(self, "_hierarchical_graph_loaded", False):
            return self._hierarchical_graph
        self._hierarchical_graph_loaded = True
        self._hierarchical_graph = None
        graph = self.code_graph()
        if graph is None:
            return None
        try:
            from ..graph.hierarchy import build_hierarchical_code_graph

            self._hierarchical_graph = build_hierarchical_code_graph(
                graph,
                repo_dir=self.entry.repo_dir,
                source_paths=(
                    self.source_reader.file_paths
                    if self.source_reader is not None
                    else None
                ),
            )
            logger.info(
                "codemap: built hierarchical graph for %r "
                "(%d containment nodes, %d dependencies)",
                self.entry.instance_id,
                len(self._hierarchical_graph.containment),
                len(self._hierarchical_graph.dependencies),
            )
        except Exception as exc:  # noqa: BLE001 - hierarchy is a UI enhancement
            logger.warning(
                "codemap: failed to build hierarchy for %r: %s",
                self.entry.instance_id,
                exc,
                exc_info=True,
            )
            self._hierarchical_graph = None
        return self._hierarchical_graph

    def _file_count(self) -> int:
        cached = getattr(self, "_file_count_cache", None)
        if cached is not None:
            return cached
        indexes = getattr(self.manifest, "indexes", {}) or {}
        bm25 = indexes.get("bm25")
        metadata = getattr(bm25, "metadata", {}) or {}
        n = int(metadata.get("source_file_count") or 0)
        if not n:
            n = self.manifest.file_count or 0
        vs = self.vector_store
        if vs is not None:
            docs = getattr(vs, "l0_documents", None)
            if docs:
                n = len(docs)
        self._file_count_cache = n
        return n

    def _description(self) -> str:
        cached = getattr(self, "_description_cache", None)
        if cached is not None:
            return cached
        if self.source_reader is not None:
            desc = read_bound_repository_summary(self.source_reader)
        elif _manifest_requires_authenticated_source(self.manifest):
            desc = ""
        else:
            desc = read_repository_summary(self.entry.repo_dir)
        self._description_cache = desc
        return desc


@dataclass(slots=True)
class _OwnedRepoBundle:
    """One bundle generation plus every resource required to retire it."""

    bundle: RepoBundle
    source_binding: Optional["RepositorySourceBinding"]
    source_cleanup_owner: Any


def _retain_cleanup_failure(
    current: BaseException | None,
    later: BaseException,
) -> BaseException:
    """Keep cleanup order without demoting cancellation-class failures."""

    if current is None:
        return later
    if isinstance(current, Exception) and not isinstance(later, Exception):
        preferred = later
        secondary = current
        label = "earlier repository cleanup also failed"
    else:
        preferred = current
        secondary = later
        label = "additional repository cleanup also failed"
    try:
        _annotate_secondary_error(preferred, label, secondary)
    except BaseException:  # noqa: B036 - diagnostics cannot replace cleanup
        pass
    return preferred


def _raise_with_cleanup_failure(
    primary: BaseException,
    cleanup_failure: BaseException,
) -> None:
    """Raise the preferred exact failure and retain the other as its cause."""

    if primary is cleanup_failure:
        raise primary
    preferred = _retain_cleanup_failure(primary, cleanup_failure)
    if preferred is cleanup_failure:
        raise cleanup_failure from primary
    raise primary from cleanup_failure


def _plain_repo_instance_id(value: Any) -> str:
    """Detach a registry key from caller-defined string subclass methods."""

    if not isinstance(value, str):
        raise ValueError("repository instance_id must be non-empty text")
    instance_id = str.__str__(value)
    if not instance_id:
        raise ValueError("repository instance_id must be non-empty text")
    return instance_id


def _log_cleanup_failure(message: str, failure: BaseException) -> None:
    """Report retryable cleanup without invoking hostile exception accessors."""

    try:
        traceback = vars(BaseException)["__traceback__"].__get__(
            failure,
            type(failure),
        )
        logger.error(
            message,
            failure,
            exc_info=(type(failure), failure, traceback),
        )
    except BaseException:  # noqa: B036 - diagnostics cannot replace cleanup
        pass


class RepoRegistry:
    """Holds the loaded :class:`RepoBundle` objects, keyed by instance id."""

    def __init__(
        self,
        config: QAConfig,
        *,
        native_index_authorization_resolver: (
            Callable[
                [RepoEntry, RepoManifest, IndexEntry],
                NativeIndexAuthorization | None,
            ]
            | None
        ) = None,
        allow_missing_native_index_authorization: bool = False,
    ) -> None:
        if (
            "vector" in config.index_types()
            and native_index_authorization_resolver is None
            and not allow_missing_native_index_authorization
        ):
            from ..native_index_authorization import (
                MissingNativeIndexAuthorizationError,
            )

            raise MissingNativeIndexAuthorizationError(
                "hybrid mode requires an external native-index authorization "
                "resolver"
            )
        self._config = config
        self._native_index_authorization_resolver = native_index_authorization_resolver
        self._allow_missing_native_index_authorization = (
            allow_missing_native_index_authorization
        )
        self._bundles: Dict[str, RepoBundle] = {}
        self._source_bindings: Dict[str, "RepositorySourceBinding"] = {}
        self._source_cleanup_owners: Dict[str, Any] = {}
        # Requests pin the exact bundle object they started with. Replacements
        # publish under this lock, while retired generations remain reachable
        # until their final request releases its lease.
        self._generation_lock = RLock()
        self._bundle_leases: Dict[int, int] = {}
        self._retired_bundles: Dict[int, _OwnedRepoBundle] = {}
        self._cleanup_in_progress: set[int] = set()
        self._orphan_cleanup_owners: List[Any] = []
        # Cleanup mutates retryable owners outside the generation lock. Keep
        # every drain and close serialized so shutdown cannot return while a
        # different thread is still closing an unleased generation or owner.
        self._cleanup_lock = RLock()
        self._closed = False
        # Serialize registry-file snapshots through publication and removal
        # reconciliation. Without this boundary, an older load_all() could
        # retire a generation that a concurrent refresh() just published from
        # a newer snapshot.
        self._registry_reload_lock = RLock()
        # One embedding model per model name, shared across repos: the GPU model
        # is loaded once instead of once per CodeVectorStore (one per repo).
        self._embeddings: Dict[Tuple[str, str, int, Optional[str], str], object] = {}
        self._embedding_load_lock = RLock()

    def load_all(self) -> None:
        """Load registry metadata, atomically replacing matching generations.

        A repeated load no longer tears down the serving generation first. Each
        replacement is authenticated independently and then published under the
        generation lock. A failed replacement leaves the previous bundle live,
        while active repositories absent from the complete snapshot are retired.
        """

        with self._registry_reload_lock:
            with self._generation_lock:
                if self._closed:
                    raise RuntimeError("repository registry is closed")
            entries = load_registry(self._config.registry_path)
            if not entries:
                logger.warning(
                    "No QA registry at %s — run scripts/build_qa_index.py first.",
                    self._config.registry_path,
                )
            seen: set[str] = set()
            for entry in entries:
                try:
                    instance_id = _plain_repo_instance_id(entry.instance_id)
                except ValueError as exc:
                    logger.error("Failed to load repository entry: %s", exc)
                    continue
                if instance_id in seen:
                    logger.error("Skipping duplicate repository id %r", instance_id)
                    continue
                seen.add(instance_id)
                if not os.path.exists(entry.manifest_path):
                    logger.warning(
                        "Skipping %r: manifest not found at %s",
                        instance_id,
                        entry.manifest_path,
                    )
                    continue
                try:
                    with self._generation_lock:
                        replacing_active = instance_id in self._bundles
                    # Preserve lazy first-start loading, but never replace a
                    # healthy serving generation with a candidate whose views
                    # and advertised runtime have not passed their full load.
                    self._replace_entry(
                        entry,
                        prepare_runtime=replacing_active,
                    )
                    logger.info("Registered %r (%s)", instance_id, entry.repo)
                except Exception as exc:  # noqa: BLE001 - keep other repos alive
                    self._retain_exception_cleanup(instance_id, exc)
                    logger.error(
                        "Failed to load %r: %s",
                        instance_id,
                        exc,
                        exc_info=True,
                    )
            self._retire_entries_absent_from(seen)

    def refresh(self, repo_id: str) -> None:
        """Prepare and atomically publish a complete new bundle generation.

        The active bundle remains untouched when metadata authentication, view
        loading, graph loading, or Ask runtime construction fails. The caller
        must use ``pin()`` for any subsequent access; a refresh never escapes an
        unleased bundle that another concurrent refresh could retire.
        """

        repo_id = _plain_repo_instance_id(repo_id)
        with self._registry_reload_lock:
            with self._generation_lock:
                if self._closed:
                    raise RuntimeError("repository registry is closed")
            entries = [
                entry
                for entry in load_registry(self._config.registry_path)
                if _plain_repo_instance_id(entry.instance_id) == repo_id
            ]
            if len(entries) != 1:
                raise ValueError(
                    "repository registry must contain exactly one entry for "
                    f"{repo_id!r}"
                )
            entry = entries[0]
            if not os.path.exists(entry.manifest_path):
                raise FileNotFoundError(entry.manifest_path)
            self._replace_entry(entry, prepare_runtime=True)

    def _retire_entries_absent_from(self, instance_ids: set[str]) -> None:
        """Remove active generations missing from one complete registry snapshot."""

        with self._generation_lock:
            if self._closed:
                return
            for instance_id in tuple(self._bundles):
                if instance_id in instance_ids:
                    continue
                owned = self._retire_active_locked(instance_id)
                if owned is not None:
                    self._retired_bundles[id(owned.bundle)] = owned
            # A failed metadata capture has no bundle to retire, but its owner
            # is still part of this complete registry snapshot. Once the id is
            # absent, move that owner to the generic retry drain instead of
            # retaining it indefinitely until process shutdown.
            for instance_id in tuple(self._source_cleanup_owners):
                if instance_id in instance_ids or instance_id in self._bundles:
                    continue
                owner = self._source_cleanup_owners.pop(instance_id, None)
                binding = self._source_bindings.pop(instance_id, None)
                cleanup_owner = owner if owner is not None else binding
                if cleanup_owner is not None and not any(
                    candidate is cleanup_owner
                    for candidate in self._orphan_cleanup_owners
                ):
                    self._orphan_cleanup_owners.append(cleanup_owner)
        failure = self._drain_retired()
        if failure is not None:
            if not isinstance(failure, Exception):
                raise failure
            _log_cleanup_failure(
                "Removed repository cleanup remains pending: %s",
                failure,
            )
        orphan_failure = self._drain_orphan_cleanup()
        if orphan_failure is not None:
            if not isinstance(orphan_failure, Exception):
                raise orphan_failure
            _log_cleanup_failure(
                "Removed repository cleanup remains pending: %s",
                orphan_failure,
            )

    def _replace_entry(
        self,
        entry: RepoEntry,
        *,
        prepare_runtime: bool,
    ) -> RepoBundle:
        instance_id = _plain_repo_instance_id(entry.instance_id)
        try:
            owned = self._build_repo_metadata(entry)
        except BaseException as primary:  # noqa: B036 - retain retry owner
            self._retain_exception_cleanup(instance_id, primary)
            raise
        try:
            if prepare_runtime:
                self._prepare_runtime_bundle(owned.bundle)
            self._publish_owned(instance_id, owned)
            return owned.bundle
        except BaseException as primary:  # noqa: B036 - retain candidate owner
            # Cancellation may land immediately after the atomic publication.
            # Inspect identity under the same lock instead of relying on a
            # caller-side flag whose next bytecode may never run.
            with self._generation_lock:
                published = self._bundles.get(instance_id) is owned.bundle
            if not published:
                cleanup_failure = self._retire_unpublished(owned)
                if cleanup_failure is not None:
                    _raise_with_cleanup_failure(primary, cleanup_failure)
            raise

    def _prepare_runtime_bundle(self, bundle: RepoBundle) -> None:
        """Load every runtime surface the service advertises before publish."""

        bundle.ensure_views()
        if bundle.chat_available:
            bundle.ensure_runtime()
        if bundle.info().capabilities.get("codemap"):
            if bundle.code_graph() is None:
                raise RuntimeError(bundle.graph_unavailable_note())
            # Hierarchy is an optional enhancement, but building it before the
            # swap prevents the first post-refresh request from observing a
            # partially initialized bundle.
            bundle.hierarchical_graph()

    def _build_repo_metadata(self, entry: RepoEntry) -> _OwnedRepoBundle:
        """Authenticate an unpublished candidate and retain all cleanup owners."""

        from ..artifacts.runtime import (
            SourceBindingCleanupOwner,
            _raise_source_cleanup_failure,
            _source_cleanup_owner_is_pending,
        )
        from ..compiler.manifest_source import (
            capture_repository_source_for_manifest,
            require_manifest_source_identity,
        )

        _plain_repo_instance_id(entry.instance_id)

        manifest = RepoManifest.load(entry.manifest_path)
        cleanup_owner = SourceBindingCleanupOwner()
        try:
            source_binding = capture_repository_source_for_manifest(
                entry.repo_dir,
                manifest,
                exclude_roots=(os.path.dirname(entry.manifest_path),),
                _source_owner=cleanup_owner.retain,
            )
            require_manifest_source_identity(
                source_binding.authenticated_identity_snapshot(),
                manifest,
                label="Web repository",
                mismatch_message=(
                    "repository source content does not match the manifest"
                ),
            )
            bundle = RepoBundle(
                entry=entry,
                manifest=manifest,
                chat_available=find_spec("litellm") is not None,
                view_loader=partial(
                    self._load_repo_views,
                    source_binding=source_binding,
                ),
                runtime_loader=self._load_repo_runtime,
                source_reader=source_binding.borrow_reader(),
            )
            return _OwnedRepoBundle(bundle, source_binding, cleanup_owner)
        except BaseException as primary:  # noqa: B036 - preserve + clean owner
            cleanup_failure: BaseException | None = None
            try:
                cleanup_owner.close()
            except BaseException as exc:  # noqa: B036 - shared cleanup priority
                cleanup_failure = exc
            pending_owner = (
                cleanup_owner
                if _source_cleanup_owner_is_pending(cleanup_owner)
                else None
            )
            if cleanup_failure is not None or pending_owner is not None:
                _raise_source_cleanup_failure(
                    primary,
                    cleanup_failure,
                    pending_owner,
                )
            raise

    def _load_repo_metadata(self, entry: RepoEntry) -> RepoBundle:
        """Legacy metadata loader used by offline callers and focused tests."""

        with self._registry_reload_lock:
            with self._generation_lock:
                if self._closed:
                    raise RuntimeError("repository registry is closed")
            instance_id = _plain_repo_instance_id(entry.instance_id)
            try:
                owned = self._build_repo_metadata(entry)
            except BaseException as primary:  # noqa: B036 - preserve retry owner
                self._retain_exception_cleanup(instance_id, primary)
                raise
            with self._generation_lock:
                closed = self._closed
                if closed:
                    duplicate = False
                elif (
                    instance_id in self._source_bindings
                    or instance_id in self._source_cleanup_owners
                ):
                    duplicate = True
                else:
                    duplicate = False
                    self._source_bindings[instance_id] = owned.source_binding
                    self._source_cleanup_owners[instance_id] = (
                        owned.source_cleanup_owner
                    )
            if closed:
                cleanup_failure = self._retire_unpublished(owned)
                error = RuntimeError("repository registry is closed")
                if cleanup_failure is not None:
                    _raise_with_cleanup_failure(error, cleanup_failure)
                raise error
            if duplicate:
                cleanup_failure = self._retire_unpublished(owned)
                error = ValueError(f"duplicate repository instance_id: {instance_id!r}")
                if cleanup_failure is not None:
                    _raise_with_cleanup_failure(error, cleanup_failure)
                raise error
            return owned.bundle

    def _retain_exception_cleanup(
        self,
        instance_id: str,
        error: BaseException,
    ) -> None:
        from ..artifacts.runtime import _source_cleanup_owner_is_pending

        try:
            attributes = BaseException.__getattribute__(error, "__dict__")
            owner = (
                dict.get(attributes, "source_cleanup_owner")
                if type(attributes) is dict
                else None
            )
        except BaseException:  # noqa: B036 - inspection cannot replace primary
            owner = None
        if not _source_cleanup_owner_is_pending(owner):
            return
        with self._generation_lock:
            if any(
                candidate is owner for candidate in self._source_cleanup_owners.values()
            ) or any(candidate is owner for candidate in self._orphan_cleanup_owners):
                return
            closed = self._closed
            if closed:
                self._orphan_cleanup_owners.append(owner)
            elif (
                instance_id not in self._source_cleanup_owners
                and instance_id not in self._bundles
            ):
                self._source_cleanup_owners[instance_id] = owner
            else:
                self._orphan_cleanup_owners.append(owner)
        if closed:
            cleanup_failure = self._drain_orphan_cleanup()
            if cleanup_failure is not None:
                if not isinstance(cleanup_failure, Exception):
                    _raise_with_cleanup_failure(error, cleanup_failure)
                _log_cleanup_failure(
                    "Unpublished repository cleanup remains pending: %s",
                    cleanup_failure,
                )

    def _publish_owned(self, instance_id: str, owned: _OwnedRepoBundle) -> None:
        instance_id = _plain_repo_instance_id(instance_id)
        with self._generation_lock:
            if self._closed:
                raise RuntimeError("repository registry is closed")
            previous_bundle = self._bundles.get(instance_id)
            previous_binding = self._source_bindings.get(instance_id)
            previous_owner = self._source_cleanup_owners.get(instance_id)
            previous = (
                _OwnedRepoBundle(
                    previous_bundle,
                    previous_binding,
                    previous_owner,
                )
                if previous_bundle is not None
                else None
            )
            orphan_owner = None
            orphan_appended = False
            if previous is not None:
                self._retired_bundles[id(previous.bundle)] = previous
            else:
                # A failed metadata capture may leave a retryable owner without
                # a published bundle. A later first publish must not overwrite
                # the only reference that can finish that cleanup.
                orphan_owner = (
                    previous_owner if previous_owner is not None else previous_binding
                )
                if (
                    orphan_owner is not None
                    and orphan_owner is not owned.source_cleanup_owner
                    and orphan_owner is not owned.source_binding
                    and not any(
                        candidate is orphan_owner
                        for candidate in self._orphan_cleanup_owners
                    )
                ):
                    self._orphan_cleanup_owners.append(orphan_owner)
                    orphan_appended = True
            try:
                # Publish the bundle pointer last. Readers take the same lock,
                # so none can observe candidate metadata with the old bundle.
                self._source_bindings[instance_id] = owned.source_binding
                self._source_cleanup_owners[instance_id] = owned.source_cleanup_owner
                self._bundles[instance_id] = owned.bundle
            except BaseException:  # noqa: B036 - restore pre-publication owner
                if self._bundles.get(instance_id) is not owned.bundle:
                    if previous is None:
                        if previous_binding is None:
                            self._source_bindings.pop(instance_id, None)
                        else:
                            self._source_bindings[instance_id] = previous_binding
                        if previous_owner is None:
                            self._source_cleanup_owners.pop(instance_id, None)
                        else:
                            self._source_cleanup_owners[instance_id] = previous_owner
                        if orphan_appended:
                            self._orphan_cleanup_owners[:] = [
                                candidate
                                for candidate in self._orphan_cleanup_owners
                                if candidate is not orphan_owner
                            ]
                    else:
                        self._source_bindings[instance_id] = previous.source_binding
                        self._source_cleanup_owners[instance_id] = (
                            previous.source_cleanup_owner
                        )
                        self._retired_bundles.pop(id(previous.bundle), None)
                raise
        failure = self._drain_retired()
        if failure is not None:
            if not isinstance(failure, Exception):
                raise failure
            _log_cleanup_failure(
                "Retired repository cleanup remains pending: %s",
                failure,
            )
        orphan_failure = self._drain_orphan_cleanup()
        if orphan_failure is not None:
            if not isinstance(orphan_failure, Exception):
                raise orphan_failure
            _log_cleanup_failure(
                "Unpublished repository cleanup remains pending: %s",
                orphan_failure,
            )

    def _retire_active_locked(
        self,
        instance_id: str,
    ) -> Optional[_OwnedRepoBundle]:
        bundle = self._bundles.pop(instance_id, None)
        source_binding = self._source_bindings.pop(instance_id, None)
        cleanup_owner = self._source_cleanup_owners.pop(instance_id, None)
        if bundle is None:
            return None
        return _OwnedRepoBundle(bundle, source_binding, cleanup_owner)

    def _retire_unpublished(
        self,
        owned: _OwnedRepoBundle,
    ) -> Optional[BaseException]:
        with self._generation_lock:
            self._retired_bundles[id(owned.bundle)] = owned
        return self._drain_retired()

    @staticmethod
    def _close_owned(owned: _OwnedRepoBundle) -> bool:
        """Close one unpinned generation and report whether cleanup completed."""

        bundle = owned.bundle
        first_failure: BaseException | None = None
        bundle.bm25 = None
        bundle.runner = None
        bundle.source_reader = None
        vector_store = bundle.vector_store
        if vector_store is not None:
            try:
                vector_store.close()
            except BaseException as exc:  # noqa: B036 - continue source cleanup
                first_failure = _retain_cleanup_failure(first_failure, exc)
            else:
                bundle.vector_store = None

        owner = owned.source_cleanup_owner
        owner_closed = owner is None
        if owner is not None:
            try:
                owner.close()
            except BaseException as exc:  # noqa: B036 - retain retry owner
                first_failure = _retain_cleanup_failure(first_failure, exc)
            try:
                owner_closed = bool(owner.closed)
            except BaseException as exc:  # noqa: B036 - uncertain means pending
                owner_closed = False
                first_failure = _retain_cleanup_failure(first_failure, exc)
        elif owned.source_binding is not None:
            try:
                owned.source_binding.close()
            except BaseException as exc:  # noqa: B036 - retain retry owner
                first_failure = _retain_cleanup_failure(first_failure, exc)
            try:
                owner_closed = bool(owned.source_binding.closed)
            except BaseException as exc:  # noqa: B036 - uncertain means pending
                owner_closed = False
                first_failure = _retain_cleanup_failure(first_failure, exc)

        if first_failure is not None:
            raise first_failure
        return bundle.vector_store is None and owner_closed

    def _drain_retired(self) -> Optional[BaseException]:
        with self._cleanup_lock:
            first_failure: BaseException | None = None
            with self._generation_lock:
                ready = [
                    (key, owned)
                    for key, owned in self._retired_bundles.items()
                    if self._bundle_leases.get(key, 0) == 0
                    and key not in self._cleanup_in_progress
                ]
                self._cleanup_in_progress.update(key for key, _owned in ready)
            for key, owned in ready:
                complete = False
                try:
                    complete = self._close_owned(owned)
                except BaseException as exc:  # noqa: B036 - visit every generation
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                finally:
                    with self._generation_lock:
                        self._cleanup_in_progress.discard(key)
                        if complete:
                            self._retired_bundles.pop(key, None)
            return first_failure

    def _drain_orphan_cleanup(self) -> Optional[BaseException]:
        with self._cleanup_lock:
            first_failure: BaseException | None = None
            pending: List[Any] = []
            with self._generation_lock:
                owners = tuple(self._orphan_cleanup_owners)
                self._orphan_cleanup_owners.clear()
            for owner in owners:
                try:
                    owner.close()
                except BaseException as exc:  # noqa: B036 - visit every owner
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                try:
                    closed = bool(owner.closed)
                except BaseException as exc:  # noqa: B036 - uncertain means pending
                    closed = False
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                if not closed:
                    pending.append(owner)
            with self._generation_lock:
                for owner in pending:
                    if not any(
                        candidate is owner for candidate in self._orphan_cleanup_owners
                    ):
                        self._orphan_cleanup_owners.append(owner)
            return first_failure

    def close(self) -> None:
        """Retire active generations; pinned requests release them later."""

        # A refresh that entered first either publishes or fully retires its
        # candidate before shutdown can report completion. The cleanup lock
        # also joins drains started by a final request lease release.
        with self._registry_reload_lock, self._cleanup_lock:
            with self._generation_lock:
                self._closed = True
                for instance_id in tuple(self._bundles):
                    owned = self._retire_active_locked(instance_id)
                    if owned is not None:
                        self._retired_bundles[id(owned.bundle)] = owned

            first_failure = self._drain_retired()

            # Owners left in these maps belong to metadata candidates that failed
            # before a bundle was published. They remain retryable just like retired
            # generations, but have no request lease to wait for.
            for instance_id, owner in tuple(self._source_cleanup_owners.items()):
                try:
                    owner.close()
                except BaseException as exc:  # noqa: B036 - finish every cleanup
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                try:
                    owner_closed = bool(owner.closed)
                except BaseException as exc:  # noqa: B036 - uncertain means pending
                    owner_closed = False
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                if owner_closed:
                    with self._generation_lock:
                        self._source_cleanup_owners.pop(instance_id, None)
                        self._source_bindings.pop(instance_id, None)

            orphan_failure = self._drain_orphan_cleanup()
            if orphan_failure is not None:
                first_failure = _retain_cleanup_failure(
                    first_failure,
                    orphan_failure,
                )
            if first_failure is not None:
                raise first_failure

    def _load_vector_store(
        self,
        vec_entry: Any,
        *,
        native_index_authorization: NativeIndexAuthorization,
    ) -> "CodeVectorStore":
        """Load a manifest vector view with the configured embedding backend."""
        from ..index.embedding.artifact_integrity import require_authorized_vector_view

        # The manifest's exact config is part of the native capability subject.
        # Legacy route defaults are query-time compatibility inputs only: adding
        # them to ``artifact_metadata`` would change the semantic contract after
        # the caller had already bound its token to ``vec_entry.config``.
        semantic_contract = dict(vec_entry.config or {})
        # Preliminary token shape checks are intentionally not enough: reject a
        # valid but wrong-tree or wrong-semantic capability before constructing
        # an embedding client/model. CodeVectorStore.load recaptures and checks
        # again, so any drift after this gate also fails before native parsing.
        require_authorized_vector_view(
            vec_entry.path,
            native_index_authorization,
            semantic_contract,
        )
        effective_route_config = dict(semantic_contract)
        legacy_route = not effective_route_config.get("embedding_route")
        legacy_fallbacks = []
        if legacy_route and not effective_route_config.get("embedding_provider"):
            # Pre-provider manifests from build_qa_index.py intentionally relied
            # on the QA runtime route. Keep that narrow compatibility path while
            # requiring all newly built artifacts to persist their provider.
            effective_route_config["embedding_provider"] = (
                self._config.embedding_provider
            )
            legacy_fallbacks.append(f"provider={self._config.embedding_provider}")
            if self._config.embedding_base_url:
                effective_route_config["embedding_endpoint"] = (
                    self._config.embedding_base_url
                )
        if (
            legacy_route
            and "dimension" not in effective_route_config
            and "embedding_dimension" not in effective_route_config
        ):
            effective_route_config["embedding_dimension"] = (
                self._config.embedding_dimension
            )
            legacy_fallbacks.append(f"dimension={self._config.embedding_dimension}")
        if legacy_fallbacks:
            logger.warning(
                "Vector artifact at %s is missing legacy route identity; "
                "using configured compatibility fallback(s): %s. Rebuild "
                "the manifest to persist its compatibility identity.",
                vec_entry.path,
                ", ".join(legacy_fallbacks),
            )
        route = resolve_embedding_artifact_route(effective_route_config)
        configured_endpoint = normalize_endpoint(self._config.embedding_base_url)
        if configured_endpoint is not None and configured_endpoint != route.endpoint:
            raise ValueError(
                "configured embedding endpoint does not match the vector artifact"
            )
        cache_key = (
            route.provider,
            route.model,
            route.dimension,
            route.compatibility_fingerprint,
        )
        embedding_kwargs = route.embedding_backend_kwargs()
        if self._config.embedding_api_key:
            if route.provider == "huggingface":
                raise ValueError(
                    "an embedding API key cannot reopen a local Hugging Face artifact"
                )
            client_kwargs: Dict[str, object] = {}
            if route.endpoint:
                client_kwargs["base_url"] = route.endpoint
            client_kwargs["api_key"] = self._config.embedding_api_key
        else:
            client_kwargs = route.client_kwargs()
        embedding_kwargs.update(client_kwargs)

        # Loading a cold local embedding model is the expensive part of opening
        # a repository view.  Serialize construction and authenticated load so
        # parallel prewarm workers cannot each observe an empty cache and place
        # a duplicate model on the GPU before either one publishes it.
        with self._embedding_load_lock:
            missing_embedding = object()
            previous_embedding = self._embeddings.get(cache_key, missing_embedding)
            vector_store = _vector_store_type()(
                embedding_model=route.model,
                embedding_provider=route.provider,
                dimension=route.dimension,
                index_metric=effective_route_config.get("index_metric", "ip"),
                store_path=vec_entry.path,
                embedding=self._embeddings.get(cache_key),
                artifact_metadata=semantic_contract,
                **embedding_kwargs,
            )
            candidate_embedding = missing_embedding
            try:
                candidate_embedding = vector_store.embedding
                vector_store.load(
                    vec_entry.path,
                    native_index_authorization=native_index_authorization,
                )
                # Publish the reusable client only after the authenticated vector
                # view loaded successfully.  The exception handler below rolls
                # this handoff back if cancellation lands before the return.
                self._embeddings[cache_key] = candidate_embedding
                return vector_store
            except BaseException as primary:  # noqa: B036 - close partial state
                if (
                    candidate_embedding is not missing_embedding
                    and self._embeddings.get(cache_key, missing_embedding)
                    is candidate_embedding
                ):
                    if previous_embedding is missing_embedding:
                        self._embeddings.pop(cache_key, None)
                    else:
                        self._embeddings[cache_key] = previous_embedding
                close_vector_after_failure(vector_store, primary)
                raise

    def _create_ask_llm(self) -> "LiteLLMChat":
        """Create the interactive model without mutating process-wide config."""
        return _ask_llm_type()(
            model=self._config.model,
            temperature=0.0,
            max_tokens=self._config.max_tokens,
            api_base=self._config.model_api_base,
            api_key=self._config.model_api_key,
            extra_kwargs=self._config.model_options,
        )

    def _load_repo_views(
        self,
        bundle: RepoBundle,
        *,
        source_binding: Optional["RepositorySourceBinding"] = None,
    ) -> None:
        """Load only the persisted retrieval views needed by static Wiki pages."""
        from ..index.sparse_idx.bm25_index import BM25CodeIndexer

        manifest = bundle.manifest
        source_reader = getattr(bundle, "source_reader", None)
        if source_reader is None and _manifest_requires_authenticated_source(manifest):
            raise RuntimeError(
                "manifest-selected repository has no authenticated source reader"
            )
        if source_binding is None:
            entry = getattr(bundle, "entry", None)
            source_binding = getattr(self, "_source_bindings", {}).get(
                getattr(entry, "instance_id", "")
            )

        bm25_index: Optional["BM25CodeIndexer"] = None
        vector_store: Optional["CodeVectorStore"] = None
        index_types = set(self._config.index_types())

        def use_bm25_fallback(reason: str) -> None:
            if bm25_index is None:
                raise ValueError(f"{reason}; no current BM25 fallback is available")
            logger.warning("%s; BM25 remains available", reason)
            bundle.vector_store = None
            bundle.bm25 = bm25_index

        bm25_entry = manifest.indexes.get("bm25")
        if (
            "bm25" in index_types
            and bm25_entry is not None
            and manifest.index_is_current("bm25")
        ):
            require_bm25_manifest_artifact(bm25_entry)
            bm25_index = BM25CodeIndexer()
            bm25_index.load_index(bm25_entry.path)
            if source_reader is not None:
                _require_authenticated_documents(
                    bm25_index.documents,
                    source_reader,
                    subject="BM25 view",
                )
                if source_binding is None:
                    raise RuntimeError(
                        "authenticated Web repository has no retained source binding"
                    )
                bm25_index.bind_repository_source(source_binding)

        vec_entry = manifest.indexes.get("vector")
        if "vector" in index_types:
            if vec_entry is None or not manifest.index_is_current("vector"):
                if self._allow_missing_native_index_authorization:
                    use_bm25_fallback("Skipping missing or stale optional vector view")
                else:
                    raise ValueError(
                        "hybrid mode requires a current vector manifest entry"
                    )
                return

            if (
                self._allow_missing_native_index_authorization
                and not is_secure_source_fingerprint_v2(
                    getattr(manifest, "source_fingerprint", None)
                )
            ):
                use_bm25_fallback(
                    "Skipping legacy native vector view at "
                    f"{vec_entry.path} without source fingerprint v2; rebuild "
                    "the repository manifest and vector artifact to restore "
                    "hybrid retrieval"
                )
                return

            authorization = None
            resolver = self._native_index_authorization_resolver
            if resolver is not None:
                authorization = resolver(bundle.entry, manifest, vec_entry)
            if authorization is None:
                if self._allow_missing_native_index_authorization:
                    use_bm25_fallback(
                        "Skipping optional native vector view at "
                        f"{vec_entry.path} without external authorization"
                    )
                    return
                else:
                    from ..native_index_authorization import (
                        MissingNativeIndexAuthorizationError,
                    )

                    raise MissingNativeIndexAuthorizationError(
                        "native-index authorization resolver returned no "
                        "authorization for a required hybrid vector view"
                    )
            else:
                vector_store = self._load_vector_store(
                    vec_entry,
                    native_index_authorization=authorization,
                )

        if source_reader is not None and vector_store is not None:
            try:
                for level in ("l0", "l2"):
                    _require_authenticated_documents(
                        getattr(vector_store, f"{level}_documents", ()) or (),
                        source_reader,
                        subject=f"vector {level} view",
                    )
            except BaseException as primary:  # noqa: B036 - close rejected view
                close_vector_after_failure(vector_store, primary)
                raise

        bundle.vector_store = vector_store
        bundle.bm25 = bm25_index

    def _load_repo_runtime(self, bundle: RepoBundle) -> None:
        from ..agent.runner import AgentRunner
        from ..agent.skills.loader import SkillLoader
        from ..compiler.params import SessionContext
        from ..ops.retrieve import RetrieveContext

        entry = bundle.entry
        manifest = bundle.manifest
        bm25_index = bundle.bm25
        vector_store = bundle.vector_store

        retrieve_ctx = RetrieveContext(
            bm25=bm25_index,
            vector_store=vector_store,
            default_top_k=10,
            default_level="l2",
        )
        contexts: Dict[str, object] = {"retrieve": retrieve_ctx}
        if vector_store is not None:
            from ..ops.rerank import RerankContext

            contexts["rerank"] = RerankContext(embedding_store=vector_store)

        registry = _fresh_registry()
        loader = SkillLoader()
        loader.load_all(_SKILLS_DIR, contexts=contexts, registry=registry)

        session_ctx = SessionContext(
            repo_path=manifest.repo_path,
            repo_size=manifest.file_count,
            primary_language=(
                manifest.languages[0] if manifest.languages else entry.language
            ),
        )

        llm = self._create_ask_llm()
        runner = AgentRunner(
            llm=llm,
            registry=registry,
            max_turns=self._config.max_turns,
            manifest=manifest,
            session_ctx=session_ctx,
            system_prompt=_DEMO_SYSTEM_PROMPT,
            # Ask exposes one query-facing retrieval contract. Internal branch
            # retrievers, rerankers, and aggregate operators remain available
            # to compiled pipelines but are not meaningful standalone tools.
            allow_skills={"repository_search"},
            # The demo answers from the retrieval indexes (BM25 + embeddings),
            # which return citable nodes that feed the answer's code pane. The
            # default read/grep/glob/bash tools return plain text (no
            # citations) and let the model grep-and-give-up, so withhold them.
            include_default_tools=False,
            # Keep prose answers: the Files:/Symbols:/Locations: schema turn is
            # for the localization eval and would replace the explanation.
            force_localization_contract=False,
            # A capped exploration should still end in a usable answer (one
            # extra tool-free turn) instead of mid-search chatter.
            force_final_answer=True,
            # The first prose response is a draft. Give the model one
            # evidence-audit pass with tool access so it can trace predicates
            # to enforcing call sites or remove unsupported claims.
            review_final_answer=True,
        )
        bundle.runner = runner

    # -- queries --

    @contextmanager
    def pin(self, repo_id: str) -> Iterator[Optional[RepoBundle]]:
        """Pin the current generation for the complete caller operation."""

        repo_id = _plain_repo_instance_id(repo_id)
        with self._generation_lock:
            if self._closed:
                raise RuntimeError("repository registry is closed")
            bundle = self._bundles.get(repo_id)
            key = id(bundle) if bundle is not None else None
            if key is not None:
                self._bundle_leases[key] = self._bundle_leases.get(key, 0) + 1
        try:
            yield bundle
        finally:
            if key is not None:
                self._release_bundle_keys((key,))

    @contextmanager
    def pin_all(self) -> Iterator[Tuple[RepoBundle, ...]]:
        """Pin one coherent snapshot of every currently published bundle."""

        with self._generation_lock:
            active = not self._closed
            if not active:
                bundles: Tuple[RepoBundle, ...] = ()
            else:
                bundles = tuple(self._bundles.values())
            keys = tuple(id(bundle) for bundle in bundles)
            for key in keys:
                self._bundle_leases[key] = self._bundle_leases.get(key, 0) + 1
        try:
            yield bundles
        finally:
            if active:
                self._release_bundle_keys(keys)

    def _release_bundle_keys(self, keys: Tuple[int, ...]) -> None:
        with self._generation_lock:
            for key in keys:
                leases = self._bundle_leases.get(key, 0)
                if leases <= 1:
                    self._bundle_leases.pop(key, None)
                else:
                    self._bundle_leases[key] = leases - 1
        failure = self._drain_retired()
        if failure is not None:
            if not isinstance(failure, Exception):
                raise failure
            _log_cleanup_failure(
                "Retired repository cleanup remains pending: %s",
                failure,
            )

    def list_infos(self) -> List[RepoInfo]:
        with self.pin_all() as bundles:
            return [bundle.info() for bundle in bundles]

    def get(self, repo_id: str) -> Optional[RepoBundle]:
        """Return a non-owning snapshot for offline, non-reloading callers."""

        repo_id = _plain_repo_instance_id(repo_id)
        with self._generation_lock:
            return self._bundles.get(repo_id)

    @property
    def retired_generation_count(self) -> int:
        """Number of old generations still leased or awaiting cleanup retry."""

        with self._generation_lock:
            return len(self._retired_bundles)
