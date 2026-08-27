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
from dataclasses import dataclass, field, replace
from functools import partial
from importlib.util import find_spec
from threading import Lock, RLock, local
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Tuple
from weakref import ReferenceType, ref

from .._atomic_directory import _annotate_secondary_error
from .._owned_file_publication import _CancellationSafeRLock
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
    from ..mcp.retained_context import (
        RetainedServerContextOwner,
        RetainedServerContextResult,
    )
    from ..native_index_authorization import NativeIndexAuthorization
    from ..source_fingerprint import RepositorySourceBinding, RepositorySourceReader
    from .index_job_activation import IndexJobRuntimeActivation
    from .index_jobs import IndexJobRepoBinding

logger = get_logger(__name__)
_REGISTRY_CLEANUP_CONTEXT = local()
_REGISTRY_RELOAD_CONTEXT = local()
_REGISTRY_DEFERRED_DRAIN_CONTEXT = local()
_REGISTRY_LOCK_RESULT_MISSING = object()


@dataclass(slots=True)
class _RegistryLockOutcome:
    """One lock callback result carried across normal lock settlement."""

    value: Any = _REGISTRY_LOCK_RESULT_MISSING
    error: BaseException | None = None


class _DeferredRegistryDrain:
    """One strong cleanup wakeup that can be invalidated across threads."""

    __slots__ = ("registry", "token", "__weakref__")

    def __init__(self, registry: "RepoRegistry") -> None:
        self.registry: RepoRegistry | None = registry
        self.token = object()


def _capture_registry_lock_outcome(
    operation: Callable[[], Any],
    captured: List[_RegistryLockOutcome],
) -> _RegistryLockOutcome:
    """Keep operation failures out of the lifecycle lock's unwind path."""

    outcome = _RegistryLockOutcome()
    captured.append(outcome)
    try:
        outcome.value = operation()
    except BaseException as error:  # noqa: B036 - unwrap after lock release
        outcome.error = error
    return outcome


def _base_exception_context(error: BaseException) -> BaseException | None:
    """Read one operation-origin context without hostile attribute dispatch."""

    try:
        context = vars(BaseException)["__context__"].__get__(error, type(error))
    except BaseException:  # noqa: B036 - inspection cannot replace failure
        return None
    if not issubclass(type(context), BaseException):
        return None
    try:
        error_traceback = vars(BaseException)["__traceback__"].__get__(
            error,
            type(error),
        )
        capture_frames = []
        while error_traceback is not None:
            frame = error_traceback.tb_frame
            if frame.f_code is _capture_registry_lock_outcome.__code__:
                capture_frames.append(frame)
            error_traceback = error_traceback.tb_next
        context_traceback = vars(BaseException)["__traceback__"].__get__(
            context,
            type(context),
        )
        while context_traceback is not None:
            frame = context_traceback.tb_frame
            if any(frame is capture_frame for capture_frame in capture_frames):
                return context
            context_traceback = context_traceback.tb_next
    except BaseException:  # noqa: B036 - inspection cannot replace failure
        return None
    return None


def _unwrap_registry_lock_outcome(outcome: _RegistryLockOutcome) -> Any:
    """Return or raise one durable result after its lock has settled."""

    if outcome.error is not None:
        raise outcome.error
    return outcome.value


def _settle_registry_lock_outcome(outcome: _RegistryLockOutcome) -> Any:
    """Preserve a durable operation failure across an interrupted unwrap."""

    try:
        return _unwrap_registry_lock_outcome(outcome)
    except BaseException as unwrap_failure:  # noqa: B036 - retain primary
        if outcome.error is not None and outcome.error is not unwrap_failure:
            _raise_with_cleanup_failure(outcome.error, unwrap_failure)
        raise


def _deferred_registry_drain_entries() -> Tuple[_DeferredRegistryDrain, ...]:
    """Return live cleanup tickets queued by this thread."""

    entries = getattr(_REGISTRY_DEFERRED_DRAIN_CONTEXT, "entries", ())
    live = tuple(entry for entry in entries if entry.registry is not None)
    if len(live) != len(entries):
        try:
            _REGISTRY_DEFERRED_DRAIN_CONTEXT.entries = live
        finally:
            _REGISTRY_DEFERRED_DRAIN_CONTEXT.entries = live
    return live


def _defer_registry_retired_drain_once(registry: "RepoRegistry") -> None:
    """Publish one identity-deduplicated retired-drain wakeup."""

    pending = _deferred_registry_drain_entries()
    with registry._generation_lock:
        if not registry._retired_bundles:
            registry._invalidate_deferred_drain_tickets_locked()
            return
        ticket = next(
            (entry for entry in pending if entry.registry is registry),
            None,
        )
        if ticket is None:
            ticket = _DeferredRegistryDrain(registry)
            pending = (*pending, ticket)
            registry._deferred_drain_tickets[:] = [
                ticket_ref
                for ticket_ref in registry._deferred_drain_tickets
                if ticket_ref() is not None
            ]
            registry._deferred_drain_tickets.append(ref(ticket))
        else:
            ticket.token = object()
        try:
            _REGISTRY_DEFERRED_DRAIN_CONTEXT.entries = pending
        finally:
            _REGISTRY_DEFERRED_DRAIN_CONTEXT.entries = pending


def _defer_registry_retired_drain(registry: "RepoRegistry") -> None:
    """Keep one retired drain strongly reachable across interruption."""

    try:
        _defer_registry_retired_drain_once(registry)
    finally:
        # The first attempt can be interrupted after the caller drops the
        # final lease but before its ticket reaches thread-local storage. The
        # idempotent fallback either finishes that publication or observes
        # that another thread already settled the retired generations.
        _defer_registry_retired_drain_once(registry)


def _remove_deferred_registry_retired_drain(registry: "RepoRegistry") -> None:
    """Discard this thread's settled or lease-blocked tickets for a registry."""

    pending = _deferred_registry_drain_entries()
    removed = tuple(entry for entry in pending if entry.registry is registry)
    if not removed:
        return
    for ticket in removed:
        registry._discard_deferred_drain_ticket(ticket)
    updated = tuple(entry for entry in pending if entry.registry is not None)
    try:
        _REGISTRY_DEFERRED_DRAIN_CONTEXT.entries = updated
    finally:
        _REGISTRY_DEFERRED_DRAIN_CONTEXT.entries = updated


def _live_registry_thread_context(
    context: Any,
    lock_attribute: str,
) -> Tuple["RepoRegistry", ...]:
    """Return markers whose guarding lock is still owned by this thread.

    An asynchronous exception can land after a context manager's ``__enter__``
    has stored its thread-local marker but before the ``with`` statement has
    installed ``__exit__``.  The surrounding registry operation still releases
    its real RLock while unwinding.  Treat that lock as the durable authority
    and prune any marker that outlives it, so one interrupted callback cannot
    poison every later operation on the same thread.
    """

    registries = getattr(context, "registries", ())
    active = []
    for registry in registries:
        lock = object.__getattribute__(registry, lock_attribute)
        if lock.held_by_current_thread():
            active.append(registry)
    if len(active) != len(registries):
        live = tuple(active)
        try:
            context.registries = live
        finally:
            context.registries = live
        return live
    return registries


class _RegistryThreadContext:
    """Track one registry lock context with interruption-safe settlement."""

    __slots__ = ("_context", "_kind", "_previous_registries", "_registry")

    def __init__(self, context: Any, registry: "RepoRegistry", kind: str) -> None:
        self._context = context
        self._registry = registry
        self._kind = kind
        self._previous_registries: Tuple["RepoRegistry", ...] = ()

    def _activate(self) -> None:
        self._previous_registries = getattr(
            self._context,
            "registries",
            (),
        )
        try:
            self._context.registries = (
                *self._previous_registries,
                self._registry,
            )
        except BaseException:  # noqa: B036 - never leave a partial marker
            try:
                self._context.registries = self._previous_registries
            finally:
                self._context.registries = self._previous_registries
            raise

    def _restore(self) -> BaseException | None:
        failure: BaseException | None = None
        try:
            self._context.registries = self._previous_registries
        except BaseException as exc:  # noqa: B036 - retry restoration below
            failure = exc
        finally:
            try:
                self._context.registries = self._previous_registries
            except BaseException as exc:  # noqa: B036 - preserve both failures
                failure = _retain_cleanup_failure(failure, exc)
        return failure

    def run(self, operation: Callable[[], Any]) -> Any:
        """Run one callback after installing all marker-cleanup finalizers."""

        missing = object()
        result: Any = missing
        primary: BaseException | None = None
        restore_failure: BaseException | None = None
        try:
            try:
                try:
                    self._activate()
                    result = operation()
                except BaseException as exc:  # noqa: B036 - settle exact primary
                    primary = exc
                retry_failure = self._restore()
                if retry_failure is not None:
                    restore_failure = _retain_cleanup_failure(
                        restore_failure,
                        retry_failure,
                    )
            except BaseException as exc:  # noqa: B036 - retry below
                restore_failure = _retain_cleanup_failure(
                    restore_failure,
                    exc,
                )
        finally:
            # This outer settlement is installed before marker activation, so
            # even an asynchronous exception at an inner-finally boundary gets
            # a second idempotent restoration attempt.
            try:
                retry_failure = self._restore()
            except BaseException as exc:  # noqa: B036 - retain exact failure
                restore_failure = _retain_cleanup_failure(
                    restore_failure,
                    exc,
                )
            else:
                if retry_failure is not None:
                    restore_failure = _retain_cleanup_failure(
                        restore_failure,
                        retry_failure,
                    )

        if primary is not None:
            if restore_failure is not None:
                _raise_with_cleanup_failure(primary, restore_failure)
            raise primary
        if restore_failure is not None:
            raise restore_failure
        if result is missing:
            raise RuntimeError("registry thread context did not run its operation")
        return result


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


def _load_retained_bm25_view(
    bundle: "RepoBundle",
    *,
    runtime_owner: Any,
) -> None:
    """Reborrow one retained BM25 view without reopening its published path."""

    if runtime_owner.state != "active":
        raise RuntimeError("retained BM25 runtime owner is not active")
    context = runtime_owner.context
    if context.manifest is not bundle.manifest:
        raise RuntimeError("retained BM25 runtime manifest identity changed")
    if not context.verify_source_status():
        raise RuntimeError("retained BM25 repository source changed")
    if context.bm25 is None or context.loaded_views != frozenset({"bm25"}):
        raise RuntimeError("retained BM25 runtime view is unavailable")
    bundle.bm25 = context.bm25


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
    index_job_activation: Optional["IndexJobRuntimeActivation"] = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.index_job_activation is not None:
            from .index_job_activation import IndexJobRuntimeActivation

            if type(self.index_job_activation) is not IndexJobRuntimeActivation:
                raise TypeError("repo bundle index-job activation is invalid")
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
    if issubclass(type(current), Exception) and not issubclass(type(later), Exception):
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
    """Validate and detach one persistent repository registry key."""

    if not issubclass(type(value), str):
        raise ValueError("repository instance_id must be non-empty text")
    instance_id = str.__str__(value)
    if not instance_id:
        raise ValueError("repository instance_id must be non-empty text")
    return instance_id


def _require_monotonic_index_job_activation(
    instance_id: str,
    previous: object,
    candidate: object,
) -> None:
    """Reject runtime publication regressions and non-durable replacement."""

    from .index_job_activation import IndexJobRuntimeActivation

    for value in (previous, candidate):
        if value is not None and type(value) is not IndexJobRuntimeActivation:
            raise RuntimeError("repository runtime activation identity is invalid")
    if previous is not None and previous.repo_id != instance_id:
        raise RuntimeError(
            "active repository runtime activation targets another Web repo"
        )
    if candidate is not None and candidate.repo_id != instance_id:
        raise RuntimeError("repository runtime activation targets another Web repo")
    if previous is None:
        return
    if candidate is None:
        raise RuntimeError(
            "registry metadata cannot replace a durable runtime generation"
        )
    if (
        candidate.repository_id != previous.repository_id
        or candidate.ref_name != previous.ref_name
    ):
        raise RuntimeError("repository runtime activation storage binding changed")
    if candidate.ref_generation < previous.ref_generation:
        raise RuntimeError("repository runtime activation generation regressed")
    if candidate.ref_generation == previous.ref_generation and (
        candidate.snapshot_id != previous.snapshot_id
        or candidate.ref_updated_at != previous.ref_updated_at
    ):
        raise RuntimeError("repository runtime activation fence conflicts")


_REPO_LOOKUP_MISS = object()


def _plain_repo_lookup_key(value: Any) -> Any:
    """Detach real string keys and safely map other legacy misses to a sentinel."""

    if issubclass(type(value), str):
        return str.__str__(value)
    return _REPO_LOOKUP_MISS


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


def _close_orphan_cleanup_owner(owner: Any) -> Tuple[bool, Optional[BaseException]]:
    """Attempt one orphan authority without letting failures escape its claim."""

    failure: BaseException | None = None
    try:
        closed = bool(owner.closed)
    except BaseException as exc:  # noqa: B036 - uncertain means pending
        closed = False
        failure = _retain_cleanup_failure(failure, exc)
    if not closed:
        try:
            owner.close()
        except BaseException as exc:  # noqa: B036 - caller retains retry ownership
            failure = _retain_cleanup_failure(failure, exc)
        try:
            closed = bool(owner.closed)
        except BaseException as exc:  # noqa: B036 - uncertain means pending
            closed = False
            failure = _retain_cleanup_failure(failure, exc)
    return closed, failure


def _vector_cleanup_is_complete(vector_store: Any) -> bool:
    """Read the vector store's monotonic cleanup attestation when available."""

    try:
        return bool(vector_store.closed)
    except AttributeError:
        return False


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
        self._bundle_leases: Dict[object, Tuple[int, ...]] = {}
        self._retired_bundles: Dict[int, _OwnedRepoBundle] = {}
        # One atomic mapping entry claims both a retired bundle and its source
        # authority. This avoids coupled-set release gaps under asynchronous
        # exceptions; orphan-only authorities use their own atomic claim set.
        self._cleanup_in_progress: Dict[int, Optional[int]] = {}
        self._orphan_cleanup_in_progress: set[int] = set()
        self._orphan_cleanup_owners: List[Any] = []
        # Deferred drain tickets strongly retain this registry only until a
        # cleanup attempt settles. Keep weak back-references so another thread
        # can invalidate stale thread-local tickets after a successful retry.
        self._deferred_drain_tickets: List[ReferenceType[_DeferredRegistryDrain]] = []
        # Cleanup mutates retryable owners outside the generation lock. Keep
        # every drain and close serialized so shutdown cannot return while a
        # different thread is still closing an unleased generation or owner.
        self._cleanup_lock = _CancellationSafeRLock()
        self._closed = False
        # Serialize registry-file snapshots through publication and removal
        # reconciliation. Without this boundary, an older load_all() could
        # retire a generation that a concurrent refresh() just published from
        # a newer snapshot. External reload/shutdown paths take this before
        # cleanup; cleanup callbacks never reenter it.
        self._registry_reload_lock = _CancellationSafeRLock()
        # One embedding model per model name, shared across repos: the GPU model
        # is loaded once instead of once per CodeVectorStore (one per repo).
        self._embeddings: Dict[Tuple[str, str, int, Optional[str], str], object] = {}
        self._embedding_load_lock = RLock()

    def _cleanup_callback_is_active(self) -> bool:
        """Whether this thread is executing any registry cleanup hook."""

        return bool(
            _live_registry_thread_context(
                _REGISTRY_CLEANUP_CONTEXT,
                "_cleanup_lock",
            )
        )

    def _owns_current_cleanup_callback(self) -> bool:
        """Whether this thread owns this registry through a cleanup hook."""

        return any(
            registry is self
            for registry in _live_registry_thread_context(
                _REGISTRY_CLEANUP_CONTEXT,
                "_cleanup_lock",
            )
        )

    def _reload_operation_is_active(self) -> bool:
        """Whether this thread is inside any serialized registry reload."""

        return bool(
            _live_registry_thread_context(
                _REGISTRY_RELOAD_CONTEXT,
                "_registry_reload_lock",
            )
        )

    def _owns_current_reload_operation(self) -> bool:
        """Whether this thread owns this registry's reload operation."""

        return any(
            registry is self
            for registry in _live_registry_thread_context(
                _REGISTRY_RELOAD_CONTEXT,
                "_registry_reload_lock",
            )
        )

    def _run_registry_lock_once(
        self,
        lock: _CancellationSafeRLock,
        operation: Callable[[], Any],
    ) -> Any:
        """Run one operation and unwrap its failure only after lock release."""

        baseline = lock._logical_depth()
        captured: List[_RegistryLockOutcome] = []
        try:
            outcome = lock.run(
                partial(
                    _capture_registry_lock_outcome,
                    operation,
                    captured,
                )
            )
        except BaseException as lock_failure:  # noqa: B036 - reconcile first
            # ``run`` normally reconciles both acquisition and release. This
            # outer pass covers an asynchronous exception at its own handler or
            # normal-settlement entry before that reconciliation call starts.
            lock._release_preserving(baseline, lock_failure)
            outcome = captured[-1] if captured else None
            operation_failure = outcome.error if outcome is not None else None
            if (
                operation_failure is None
                and outcome is not None
                and outcome.value is _REGISTRY_LOCK_RESULT_MISSING
            ):
                # An asynchronous exception may land at the operation's except
                # handler before its error can be stored in the durable slot.
                # CPython retains that earlier exact object as the implicit
                # context; inspect it without caller-defined dispatch.
                operation_failure = _base_exception_context(lock_failure)
            if operation_failure is not None:
                _raise_with_cleanup_failure(operation_failure, lock_failure)
            raise
        try:
            return _settle_registry_lock_outcome(outcome)
        except BaseException as settlement_failure:  # noqa: B036 - retry once
            if outcome.error is not None and outcome.error is not settlement_failure:
                _raise_with_cleanup_failure(outcome.error, settlement_failure)
            raise

    def _flush_deferred_retired_drains(
        self,
        *,
        attempted_tokens: Optional[set[object]] = None,
    ) -> BaseException | None:
        """Drain cross-registry lease releases after every lock edge is gone."""

        first_failure: BaseException | None = None
        attempted = set(attempted_tokens or ())
        while True:
            target_entry = None
            for ticket in _deferred_registry_drain_entries():
                target = ticket.registry
                token = ticket.token
                if target is not None and token not in attempted:
                    target_entry = (ticket, target, token)
                    break
            if target_entry is None:
                break
            ticket, target, token = target_entry
            attempted.add(token)
            failure: BaseException | None = None
            try:
                failure = target._drain_retired(settle_deferred=False)
            except BaseException as exc:  # noqa: B036 - settle after locks
                failure = exc
            try:
                _, waits_for_lease = target._retired_drain_state()
            except BaseException as exc:  # noqa: B036 - keep retry authority
                waits_for_lease = False
                failure = _retain_cleanup_failure(failure, exc)
            if failure is None and waits_for_lease:
                target._discard_deferred_drain_ticket(
                    ticket,
                    expected_token=token,
                )
            if failure is not None:
                first_failure = _retain_cleanup_failure(
                    first_failure,
                    failure,
                )
        return first_failure

    def _can_flush_deferred_retired_drains(self) -> bool:
        """Whether this thread has released every registry lifecycle edge."""

        return (
            not self._cleanup_lock.held_by_current_thread()
            and not self._registry_reload_lock.held_by_current_thread()
            and not self._cleanup_callback_is_active()
            and not self._reload_operation_is_active()
        )

    def _run_registry_lock(
        self,
        lock: _CancellationSafeRLock,
        operation: Callable[[], Any],
    ) -> Any:
        """Run one nested lock scope and settle deferred drains at its edge."""

        outcome = _RegistryLockOutcome()
        deferred_failure: BaseException | None = None
        try:
            outcome.value = self._run_registry_lock_once(lock, operation)
        except BaseException as exc:  # noqa: B036 - settle deferred work next
            outcome.error = exc

        if self._can_flush_deferred_retired_drains():
            try:
                deferred_failure = self._flush_deferred_retired_drains()
            except BaseException as exc:  # noqa: B036 - retain durable queue
                deferred_failure = exc

        primary = outcome.error
        if primary is not None:
            if deferred_failure is not None:
                _raise_with_cleanup_failure(primary, deferred_failure)
            raise primary
        if deferred_failure is not None:
            if not issubclass(type(deferred_failure), Exception):
                raise deferred_failure
            _log_cleanup_failure(
                "Deferred repository cleanup remains pending: %s",
                deferred_failure,
            )
        if outcome.value is _REGISTRY_LOCK_RESULT_MISSING:
            raise RuntimeError("registry lifecycle lock did not run its operation")
        return outcome.value

    def _run_serialized_cleanup(self, operation: Callable[[], Any]) -> Any:
        """Serialize mutation of retryable cleanup authorities."""

        return self._run_registry_lock(self._cleanup_lock, operation)

    def _cleanup_callback(self) -> _RegistryThreadContext:
        """Mark caller cleanup code so registry reentry cannot invert locks."""

        return _RegistryThreadContext(
            _REGISTRY_CLEANUP_CONTEXT,
            self,
            "cleanup",
        )

    def _run_cleanup_callback(self, operation: Callable[[], Any]) -> Any:
        """Run caller cleanup code under an interruption-safe thread marker."""

        return self._cleanup_callback().run(operation)

    def _run_serialized_reload(self, operation: Callable[[], Any]) -> Any:
        """Serialize one reload without holding cleanup across candidate builds."""

        if self._cleanup_callback_is_active():
            raise RuntimeError("repository reload cannot start during cleanup")
        if self._reload_operation_is_active():
            raise RuntimeError("repository reload cannot reenter another reload")

        def reload_operation() -> Any:
            # Join cleanup already in flight before the registry snapshot, but
            # release cleanup while metadata and runtime views are prepared.
            def require_open() -> None:
                with self._generation_lock:
                    if self._closed:
                        raise RuntimeError("repository registry is closed")

            self._run_serialized_cleanup(require_open)
            marker = _RegistryThreadContext(
                _REGISTRY_RELOAD_CONTEXT,
                self,
                "reload",
            )
            return marker.run(operation)

        return self._run_registry_lock(
            self._registry_reload_lock,
            reload_operation,
        )

    def load_all(self) -> None:
        """Load registry metadata, atomically replacing matching generations.

        A repeated load no longer tears down the serving generation first. Each
        replacement is authenticated independently and then published under the
        generation lock. A failed replacement leaves the previous bundle live,
        while active repositories absent from the complete snapshot are retired.
        """

        def operation() -> None:
            entries = load_registry(self._config.registry_path)
            if not entries:
                logger.warning(
                    "No QA registry at %s — run scripts/build_qa_index.py first.",
                    self._config.registry_path,
                )
            seen: set[str] = set()
            shutdown_failure: Optional[BaseException] = None
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
                    try:
                        self._retain_exception_cleanup(instance_id, exc)
                    except BaseException as cleanup_failure:  # noqa: B036
                        _raise_with_cleanup_failure(exc, cleanup_failure)
                    with self._generation_lock:
                        closed = self._closed
                    if closed and shutdown_failure is None:
                        shutdown_failure = exc
                    logger.error(
                        "Failed to load %r: %s",
                        instance_id,
                        exc,
                        exc_info=True,
                    )
                    if closed:
                        break
            self._retire_entries_absent_from(seen)
            if shutdown_failure is not None:
                raise shutdown_failure
            self._raise_if_closed_after_cleanup()

        self._run_serialized_reload(operation)

    def refresh(self, repo_id: str) -> None:
        """Prepare and atomically publish a complete new bundle generation.

        The active bundle remains untouched when metadata authentication, view
        loading, graph loading, or Ask runtime construction fails. The caller
        must use ``pin()`` for any subsequent access; a refresh never escapes an
        unleased bundle that another concurrent refresh could retire.
        """

        requested_repo_id = repo_id

        def operation() -> None:
            plain_repo_id = _plain_repo_instance_id(requested_repo_id)
            entries = []
            for entry in load_registry(self._config.registry_path):
                try:
                    instance_id = _plain_repo_instance_id(entry.instance_id)
                except ValueError as exc:
                    logger.error(
                        "Skipping invalid repository entry while refreshing %r: %s",
                        plain_repo_id,
                        exc,
                    )
                    continue
                if instance_id == plain_repo_id:
                    entries.append(entry)
            if len(entries) != 1:
                raise ValueError(
                    "repository registry must contain exactly one entry for "
                    f"{plain_repo_id!r}"
                )
            entry = entries[0]
            if not os.path.exists(entry.manifest_path):
                raise FileNotFoundError(entry.manifest_path)
            self._replace_entry(entry, prepare_runtime=True)

        self._run_serialized_reload(operation)

    def attest_retained_bm25_snapshot_if_equivalent(
        self,
        binding: "IndexJobRepoBinding",
        activation: "IndexJobRuntimeActivation",
        *,
        transfer_if_current: Callable[[Callable[[], None]], None],
    ) -> bool:
        """Guard-attest an equivalent incumbent under the reload fence.

        The reload lock is acquired before the durable current-result guard,
        matching the lock order used by full snapshot publication. A normal
        registry reload therefore cannot retire the incumbent between the
        runtime proof and the durable guarded transfer.
        """

        from .index_job_activation import (
            IndexJobActivationError,
            IndexJobRuntimeActivation,
        )
        from .index_jobs import IndexJobRepoBinding

        if type(binding) is not IndexJobRepoBinding:
            raise TypeError("binding must be an exact IndexJobRepoBinding")
        if type(activation) is not IndexJobRuntimeActivation:
            raise TypeError("activation must be an exact IndexJobRuntimeActivation")
        if (
            activation.repo_id != binding.repo_id
            or activation.repository_id != binding.repository_id
            or activation.ref_name != binding.ref_name
        ):
            raise ValueError("retained BM25 activation binding differs")
        if not callable(transfer_if_current):
            raise TypeError("retained BM25 guarded transfer must be callable")

        instance_id = binding.repo_id

        def operation() -> bool:
            with self._generation_lock:
                bundle = self._bundles.get(instance_id)
                if bundle is None:
                    raise IndexJobActivationError(
                        "Web repository has no active runtime generation"
                    )
                incumbent = bundle.index_job_activation
                if incumbent is None:
                    return False
                if type(incumbent) is not IndexJobRuntimeActivation:
                    raise IndexJobActivationError(
                        "active Web runtime activation identity is invalid"
                    )
                if (
                    incumbent.repo_id != binding.repo_id
                    or incumbent.repository_id != binding.repository_id
                    or incumbent.ref_name != binding.ref_name
                ):
                    raise IndexJobActivationError(
                        "active Web runtime activation binding changed"
                    )
                if incumbent.ref_generation > activation.ref_generation:
                    raise IndexJobActivationError(
                        "active Web runtime generation is newer than publication"
                    )
                if incumbent.ref_generation < activation.ref_generation:
                    return False
                if (
                    incumbent.snapshot_id != activation.snapshot_id
                    or incumbent.ref_updated_at != activation.ref_updated_at
                ):
                    raise IndexJobActivationError(
                        "active Web runtime generation conflicts with publication"
                    )

            def require_incumbent() -> None:
                with self._generation_lock:
                    current = self._bundles.get(instance_id)
                    if current is not bundle:
                        raise IndexJobActivationError(
                            "active Web runtime changed during guarded attestation"
                        )
                    current_activation = current.index_job_activation
                    if (
                        type(current_activation) is not IndexJobRuntimeActivation
                        or current_activation.repo_id != binding.repo_id
                        or current_activation.repository_id != binding.repository_id
                        or current_activation.ref_name != binding.ref_name
                        or current_activation.ref_generation
                        != activation.ref_generation
                        or current_activation.snapshot_id != activation.snapshot_id
                        or current_activation.ref_updated_at
                        != activation.ref_updated_at
                    ):
                        raise IndexJobActivationError(
                            "active Web runtime changed during guarded attestation"
                        )

            result = transfer_if_current(require_incumbent)
            if result is not None:
                raise RuntimeError("retained BM25 guarded transfer returned a value")
            return True

        return self._run_serialized_reload(operation)

    def replace_retained_bm25_snapshot(
        self,
        binding: "IndexJobRepoBinding",
        activation: "IndexJobRuntimeActivation",
        runtime_owner: "RetainedServerContextOwner",
        *,
        transfer_if_current: Callable[[Callable[[], None]], None],
    ) -> None:
        """Publish one current retained BM25 result as a pinned generation.

        Validation leaves the caller's owner untouched. Once an active owner
        passes that boundary, the registry retains it through publication or
        retryable cleanup. After all runtime preparation, the caller-supplied
        durable guard invokes the complete source-checked RCU transfer while
        retaining its ref-writer fence.
        """

        from ..mcp.retained_context import RetainedServerContextOwner
        from .index_job_activation import IndexJobRuntimeActivation
        from .index_jobs import IndexJobRepoBinding

        if type(binding) is not IndexJobRepoBinding:
            raise TypeError("binding must be an exact IndexJobRepoBinding")
        if type(activation) is not IndexJobRuntimeActivation:
            raise TypeError("activation must be an exact IndexJobRuntimeActivation")
        if (
            activation.repo_id != binding.repo_id
            or activation.repository_id != binding.repository_id
            or activation.ref_name != binding.ref_name
        ):
            raise ValueError("retained BM25 activation binding differs")
        if type(runtime_owner) is not RetainedServerContextOwner:
            raise TypeError("runtime_owner must be an exact RetainedServerContextOwner")
        if runtime_owner.state != "active":
            raise RuntimeError("retained BM25 runtime owner must be active")
        if not callable(transfer_if_current):
            raise TypeError("retained BM25 guarded transfer must be callable")

        instance_id = binding.repo_id

        def guard_source_checked_publish(publish: Callable[[], None]) -> None:
            def transfer() -> None:
                if not runtime_owner.context.verify_source_status():
                    raise RuntimeError("retained BM25 repository source changed")
                result = publish()
                if result is not None:
                    raise RuntimeError(
                        "retained BM25 runtime transfer returned a value"
                    )

            result = transfer_if_current(transfer)
            if result is not None:
                raise RuntimeError("retained BM25 guarded transfer returned a value")

        def operation() -> None:
            owned = self._build_retained_bm25_snapshot(
                binding,
                activation,
                runtime_owner,
            )
            self._prepare_and_publish_owned(
                instance_id,
                owned,
                prepare_runtime=True,
                guarded_publish=guard_source_checked_publish,
            )

        try:
            self._run_serialized_reload(operation)
        except BaseException as primary:  # noqa: B036 - retain accepted owner
            try:
                cleanup_failure = self._retain_cleanup_owner(
                    instance_id,
                    runtime_owner,
                )
            except BaseException as settlement_failure:  # noqa: B036
                _raise_with_cleanup_failure(primary, settlement_failure)
            if cleanup_failure is not None:
                _raise_with_cleanup_failure(primary, cleanup_failure)
            raise

    def load_and_replace_retained_bm25_snapshot(
        self,
        binding: "IndexJobRepoBinding",
        activation: "IndexJobRuntimeActivation",
        *,
        loader: Callable[
            ["RetainedServerContextOwner"],
            "RetainedServerContextResult",
        ],
        transfer_if_current: Callable[[Callable[[], None]], None],
    ) -> None:
        """Own one retained loader through its guarded runtime publication."""

        from ..mcp.retained_context import (
            RetainedServerContextOwner,
            RetainedServerContextResult,
        )
        from .index_job_activation import IndexJobRuntimeActivation
        from .index_jobs import IndexJobRepoBinding

        if type(binding) is not IndexJobRepoBinding:
            raise TypeError("binding must be an exact IndexJobRepoBinding")
        if type(activation) is not IndexJobRuntimeActivation:
            raise TypeError("activation must be an exact IndexJobRuntimeActivation")
        if (
            activation.repo_id != binding.repo_id
            or activation.repository_id != binding.repository_id
            or activation.ref_name != binding.ref_name
        ):
            raise ValueError("retained BM25 activation binding differs")
        if not callable(loader):
            raise TypeError("retained BM25 runtime loader must be callable")
        if not callable(transfer_if_current):
            raise TypeError("retained BM25 guarded transfer must be callable")

        runtime_owner = RetainedServerContextOwner()
        instance_id = binding.repo_id
        try:
            result = loader(runtime_owner)
            if (
                type(result) is not RetainedServerContextResult
                or runtime_owner.state != "active"
                or runtime_owner.result is not result
            ):
                raise RuntimeError(
                    "retained BM25 runtime loader returned an invalid result"
                )
            self.replace_retained_bm25_snapshot(
                binding,
                activation,
                runtime_owner,
                transfer_if_current=transfer_if_current,
            )
        except BaseException as primary:  # noqa: B036 - retain acquired owner
            try:
                cleanup_failure = self._settle_unpublished_cleanup_owner(
                    instance_id,
                    runtime_owner,
                )
            except BaseException as settlement_failure:  # noqa: B036
                _raise_with_cleanup_failure(primary, settlement_failure)
            if cleanup_failure is not None:
                _raise_with_cleanup_failure(primary, cleanup_failure)
            raise

    def _raise_if_closed_after_cleanup(
        self,
        cleanup_failure: Optional[BaseException] = None,
    ) -> None:
        """Reject false success when cleanup reentrantly shuts the registry."""

        with self._generation_lock:
            closed = self._closed
        if not closed:
            return
        error = RuntimeError("repository registry is closed")
        if cleanup_failure is not None:
            _raise_with_cleanup_failure(error, cleanup_failure)
        raise error

    def _retire_entries_absent_from(self, instance_ids: set[str]) -> None:
        """Remove active generations missing from one complete registry snapshot."""

        with self._generation_lock:
            if self._closed:
                return
            for instance_id in tuple(self._bundles):
                if instance_id in instance_ids:
                    continue
                self._retire_active_locked(instance_id)
            # A failed metadata capture has no bundle to retire, but its owner
            # is still part of this complete registry snapshot. Once the id is
            # absent, move that owner to the generic retry drain instead of
            # retaining it indefinitely until process shutdown.
            for instance_id in tuple(self._source_cleanup_owners):
                if instance_id in instance_ids or instance_id in self._bundles:
                    continue
                owner = self._source_cleanup_owners.get(instance_id)
                binding = self._source_bindings.get(instance_id)
                cleanup_owner = owner if owner is not None else binding
                if cleanup_owner is not None and not any(
                    candidate is cleanup_owner
                    for candidate in self._orphan_cleanup_owners
                ):
                    self._orphan_cleanup_owners.append(cleanup_owner)
                self._source_cleanup_owners.pop(instance_id, None)
                self._source_bindings.pop(instance_id, None)
        failure = self._drain_retired()
        if failure is not None:
            if not issubclass(type(failure), Exception):
                self._raise_if_closed_after_cleanup(failure)
                raise failure
            _log_cleanup_failure(
                "Removed repository cleanup remains pending: %s",
                failure,
            )
        orphan_failure = self._drain_orphan_cleanup()
        cleanup_failure = failure
        if orphan_failure is not None:
            cleanup_failure = (
                orphan_failure
                if cleanup_failure is None
                else _retain_cleanup_failure(cleanup_failure, orphan_failure)
            )
            if not issubclass(type(orphan_failure), Exception):
                self._raise_if_closed_after_cleanup(cleanup_failure)
                raise orphan_failure
            _log_cleanup_failure(
                "Removed repository cleanup remains pending: %s",
                orphan_failure,
            )
        self._raise_if_closed_after_cleanup(cleanup_failure)

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
            try:
                self._retain_exception_cleanup(instance_id, primary)
            except BaseException as cleanup_failure:  # noqa: B036
                _raise_with_cleanup_failure(primary, cleanup_failure)
            raise
        return self._prepare_and_publish_owned(
            instance_id,
            owned,
            prepare_runtime=prepare_runtime,
        )

    def _prepare_and_publish_owned(
        self,
        instance_id: str,
        owned: _OwnedRepoBundle,
        *,
        prepare_runtime: bool,
        guarded_publish: Callable[[Callable[[], None]], None] | None = None,
    ) -> RepoBundle:
        """Prepare a complete candidate and transfer its cleanup ownership."""

        try:
            if prepare_runtime:
                self._prepare_runtime_bundle(owned.bundle)
            if guarded_publish is None:
                self._publish_owned(instance_id, owned)
            else:
                result = guarded_publish(
                    partial(self._publish_owned, instance_id, owned)
                )
                if result is not None:
                    raise RuntimeError(
                        "guarded repository publication returned a value"
                    )
            return owned.bundle
        except BaseException as primary:  # noqa: B036 - retain candidate owner
            cleanup_failure: BaseException | None = None
            try:
                # Cancellation may land immediately after the atomic
                # publication. Both active and retired identity prove that the
                # registry accepted cleanup ownership for this generation.
                with self._generation_lock:
                    key = id(owned.bundle)
                    ownership_transferred = (
                        self._bundles.get(instance_id) is owned.bundle
                        or key in self._retired_bundles
                    )
                if not ownership_transferred:
                    cleanup_failure = self._retire_unpublished(owned)
            except BaseException as settlement_failure:  # noqa: B036
                _raise_with_cleanup_failure(primary, settlement_failure)
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

    def _build_retained_bm25_snapshot(
        self,
        binding: "IndexJobRepoBinding",
        activation: "IndexJobRuntimeActivation",
        runtime_owner: "RetainedServerContextOwner",
    ) -> _OwnedRepoBundle:
        """Bind an exact retained snapshot to one complete sparse Web bundle."""

        from ..artifacts.context import CONTEXT_ARTIFACT_SCHEMA, ContextArtifactResult
        from ..compiler.manifest_export import RepoManifestExportReceipt
        from ..compiler.manifest_materialization import (
            RepoManifestMaterializationResult,
        )
        from ..mcp.context import ServerContext
        from ..mcp.retained_context import (
            RetainedServerContextOwner,
            RetainedServerContextResult,
        )
        from ..source_fingerprint import lexical_repository_path
        from .index_job_activation import IndexJobRuntimeActivation
        from .index_jobs import IndexJobRepoBinding

        if type(binding) is not IndexJobRepoBinding:
            raise TypeError("binding must be an exact IndexJobRepoBinding")
        if type(activation) is not IndexJobRuntimeActivation:
            raise TypeError("activation must be an exact IndexJobRuntimeActivation")
        if type(runtime_owner) is not RetainedServerContextOwner:
            raise TypeError("runtime_owner must be an exact RetainedServerContextOwner")
        if (
            activation.repo_id != binding.repo_id
            or activation.repository_id != binding.repository_id
            or activation.ref_name != binding.ref_name
        ):
            raise ValueError("retained BM25 activation binding differs")
        if tuple(self._config.index_types()) != ("bm25",):
            raise ValueError("retained BM25 publication requires sparse Web mode")

        instance_id = binding.repo_id
        with self._generation_lock:
            current = self._bundles.get(instance_id)
        if type(current) is not RepoBundle:
            raise ValueError(
                f"Web repository {instance_id!r} has no active exact generation"
            )
        entry = current.entry
        current_manifest = current.manifest
        if type(entry) is not RepoEntry or type(current_manifest) is not RepoManifest:
            raise TypeError("active Web repository metadata uses an invalid type")
        if entry.instance_id != instance_id:
            raise RuntimeError("active Web repository identity changed")
        if set(current_manifest.indexes) != {"bm25"} or not (
            current_manifest.index_is_current("bm25")
        ):
            raise ValueError(
                "retained BM25 publication cannot replace a non-BM25 generation"
            )

        result = runtime_owner.result
        context = runtime_owner.context
        if type(result) is not RetainedServerContextResult:
            raise TypeError("retained BM25 result uses an invalid type")
        if type(context) is not ServerContext:
            raise TypeError("retained BM25 context uses an invalid type")
        materialization = result.materialization
        if type(materialization) is not RepoManifestMaterializationResult:
            raise TypeError("retained BM25 materialization uses an invalid type")
        artifact = materialization.artifact
        receipt = materialization.export_receipt
        if type(artifact) is not ContextArtifactResult:
            raise TypeError("retained BM25 artifact uses an invalid type")
        if type(receipt) is not RepoManifestExportReceipt:
            raise TypeError("retained BM25 export receipt uses an invalid type")
        manifest = context.manifest
        if type(manifest) is not RepoManifest:
            raise TypeError("retained BM25 manifest uses an invalid type")

        if (
            receipt.repository_id != binding.repository_id
            or receipt.repository_key != artifact.repository
            or receipt.repository_key != entry.repo
            or receipt.snapshot_id != activation.snapshot_id
            or receipt.ref_name is not None
            or receipt.ref_generation is not None
            or receipt.ref_updated_at is not None
        ):
            raise ValueError("retained BM25 snapshot identity is inconsistent")
        if (
            receipt.views != ("bm25",)
            or receipt.skipped_items != ()
            or artifact.views != ("bm25",)
            or result.loaded_views != ("bm25",)
            or result.view_error_items != ()
            or context.loaded_views != frozenset({"bm25"})
            or context.errors
            or context.bm25 is None
        ):
            raise ValueError("retained BM25 snapshot is not a complete BM25 view")
        if (
            runtime_owner.context is not context
            or runtime_owner.result is not result
            or artifact.commit != manifest.commit
            or set(manifest.indexes) != {"bm25"}
            or not manifest.index_is_current("bm25")
            or context.artifact
            != {
                "verified": True,
                "schema": CONTEXT_ARTIFACT_SCHEMA,
                "repository": artifact.repository,
                "commit": artifact.commit,
                "views": ["bm25"],
            }
        ):
            raise ValueError("retained BM25 manifest identity is inconsistent")

        entry_repo = lexical_repository_path(entry.repo_dir)
        active_repo = lexical_repository_path(current_manifest.repo_path)
        retained_repo = lexical_repository_path(manifest.repo_path)
        if entry_repo != active_repo or entry_repo != retained_repo:
            raise ValueError("retained BM25 repository path differs from Web source")
        try:
            physical_paths = {
                entry_repo.resolve(strict=True),
                active_repo.resolve(strict=True),
                retained_repo.resolve(strict=True),
            }
        except (OSError, RuntimeError) as exc:
            raise ValueError("retained BM25 repository path is unavailable") from exc
        if len(physical_paths) != 1 or not next(iter(physical_paths)).is_dir():
            raise ValueError("retained BM25 repository path identity changed")
        if not context.verify_source_status():
            raise ValueError("retained BM25 repository source changed")
        source_reader = context.borrow_source_reader()
        _require_authenticated_documents(
            context.bm25.documents,
            source_reader,
            subject="retained BM25 view",
        )

        retained_entry = replace(
            entry,
            base_commit=manifest.commit,
            manifest_path=os.fspath(artifact.manifest_path),
        )
        bundle = RepoBundle(
            entry=retained_entry,
            manifest=manifest,
            bm25=context.bm25,
            chat_available=current.chat_available,
            view_loader=partial(
                _load_retained_bm25_view,
                runtime_owner=runtime_owner,
            ),
            runtime_loader=self._load_repo_runtime,
            source_reader=source_reader,
            index_job_activation=activation,
        )
        return _OwnedRepoBundle(bundle, None, runtime_owner)

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

        instance_id = _plain_repo_instance_id(entry.instance_id)
        if type(entry.instance_id) is not str:
            entry = replace(entry, instance_id=instance_id)

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

        def operation() -> RepoBundle:
            instance_id = _plain_repo_instance_id(entry.instance_id)
            try:
                owned = self._build_repo_metadata(entry)
            except BaseException as primary:  # noqa: B036 - preserve retry owner
                try:
                    self._retain_exception_cleanup(instance_id, primary)
                except BaseException as cleanup_failure:  # noqa: B036
                    _raise_with_cleanup_failure(primary, cleanup_failure)
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
                error = RuntimeError("repository registry is closed")
                try:
                    cleanup_failure = self._retire_unpublished(owned)
                except BaseException as cleanup_failure:  # noqa: B036
                    _raise_with_cleanup_failure(error, cleanup_failure)
                if cleanup_failure is not None:
                    _raise_with_cleanup_failure(error, cleanup_failure)
                raise error
            if duplicate:
                error = ValueError(f"duplicate repository instance_id: {instance_id!r}")
                try:
                    cleanup_failure = self._retire_unpublished(owned)
                except BaseException as cleanup_failure:  # noqa: B036
                    _raise_with_cleanup_failure(error, cleanup_failure)
                if cleanup_failure is not None:
                    _raise_with_cleanup_failure(error, cleanup_failure)
                raise error
            return owned.bundle

        return self._run_serialized_reload(operation)

    def _retain_cleanup_owner(
        self,
        instance_id: str,
        owner: Any,
    ) -> Optional[BaseException]:
        """Retain one unpublished authority and settle it during shutdown."""

        from ..artifacts.runtime import _source_cleanup_owner_is_pending

        instance_id = _plain_repo_instance_id(instance_id)
        if not _source_cleanup_owner_is_pending(owner):
            return None
        with self._generation_lock:
            if (
                any(
                    candidate is owner
                    for candidate in self._source_cleanup_owners.values()
                )
                or any(
                    candidate is owner for candidate in self._source_bindings.values()
                )
                or any(candidate is owner for candidate in self._orphan_cleanup_owners)
                or any(
                    retired.source_cleanup_owner is owner
                    or retired.source_binding is owner
                    for retired in self._retired_bundles.values()
                )
            ):
                return None
            closed = self._closed
            if closed:
                self._orphan_cleanup_owners.append(owner)
            elif (
                instance_id not in self._source_cleanup_owners
                and instance_id not in self._source_bindings
                and instance_id not in self._bundles
            ):
                self._source_cleanup_owners[instance_id] = owner
            else:
                self._orphan_cleanup_owners.append(owner)
        return self._drain_orphan_cleanup() if closed else None

    def _settle_unpublished_cleanup_owner(
        self,
        instance_id: str,
        owner: Any,
    ) -> Optional[BaseException]:
        """Immediately settle a rejected owner, retaining only pending cleanup."""

        from ..artifacts.runtime import _source_cleanup_owner_is_pending

        instance_id = _plain_repo_instance_id(instance_id)
        cleanup_failure = self._retain_cleanup_owner(instance_id, owner)
        if not _source_cleanup_owner_is_pending(owner):
            return cleanup_failure

        with self._generation_lock:
            if self._closed:
                return cleanup_failure
            generation_owned = any(
                (
                    self._source_cleanup_owners.get(candidate_id) is owner
                    or self._source_bindings.get(candidate_id) is owner
                )
                and candidate_id in self._bundles
                for candidate_id in (
                    set(self._source_cleanup_owners) | set(self._source_bindings)
                )
            ) or any(
                retired.source_cleanup_owner is owner or retired.source_binding is owner
                for retired in self._retired_bundles.values()
            )
            if generation_owned:
                return cleanup_failure

            if not any(candidate is owner for candidate in self._orphan_cleanup_owners):
                # Publish retry ownership before removing any owner-only map
                # alias so interruption cannot make the authority unreachable.
                self._orphan_cleanup_owners.append(owner)
            for candidate_id, candidate in tuple(self._source_cleanup_owners.items()):
                if candidate is owner and candidate_id not in self._bundles:
                    self._source_cleanup_owners.pop(candidate_id, None)
            for candidate_id, candidate in tuple(self._source_bindings.items()):
                if candidate is owner and candidate_id not in self._bundles:
                    self._source_bindings.pop(candidate_id, None)

        orphan_failure = self._drain_orphan_cleanup()
        if orphan_failure is None:
            return cleanup_failure
        if cleanup_failure is None:
            return orphan_failure
        return _retain_cleanup_failure(cleanup_failure, orphan_failure)

    def _retain_exception_cleanup(
        self,
        instance_id: str,
        error: BaseException,
    ) -> None:
        try:
            attributes = BaseException.__getattribute__(error, "__dict__")
            owner = (
                dict.get(attributes, "source_cleanup_owner")
                if type(attributes) is dict
                else None
            )
        except BaseException:  # noqa: B036 - inspection cannot replace primary
            owner = None
        cleanup_failure = self._retain_cleanup_owner(instance_id, owner)
        if cleanup_failure is not None:
            if not issubclass(type(cleanup_failure), Exception):
                _raise_with_cleanup_failure(error, cleanup_failure)
            _log_cleanup_failure(
                "Unpublished repository cleanup remains pending: %s",
                cleanup_failure,
            )

    def _publish_owned(
        self,
        instance_id: str,
        owned: _OwnedRepoBundle,
    ) -> None:
        instance_id = _plain_repo_instance_id(instance_id)
        self._run_serialized_cleanup(
            partial(
                self._publish_owned_under_cleanup_lock,
                instance_id,
                owned,
            )
        )

    def _publish_owned_under_cleanup_lock(
        self,
        instance_id: str,
        owned: _OwnedRepoBundle,
    ) -> None:
        authority = (
            owned.source_cleanup_owner
            if owned.source_cleanup_owner is not None
            else owned.source_binding
        )
        with self._generation_lock:
            if self._closed:
                raise RuntimeError("repository registry is closed")
            if authority is not None and (
                any(
                    retired.source_cleanup_owner is authority
                    or retired.source_binding is authority
                    for retired in self._retired_bundles.values()
                )
                or any(
                    candidate is authority for candidate in self._orphan_cleanup_owners
                )
                or self._authority_cleanup_in_progress_locked(id(authority))
            ):
                raise RuntimeError("repository source cleanup authority is pending")

        # The caller owns the cleanup lock, but no generation lock, while this
        # caller-defined descriptor runs. Reload paths reject cleanup-callback
        # reentry, while a reentrant shutdown marks the registry closed without
        # waiting on the reload operation that is currently settling it.
        if authority is None:
            authority_closed = False
        else:
            authority_closed = self._run_cleanup_callback(
                lambda: bool(authority.closed)
            )

        with self._generation_lock:
            if self._closed:
                raise RuntimeError("repository registry is closed")
            if authority_closed:
                raise RuntimeError(
                    "repository source cleanup authority is already closed"
                )
            previous_bundle = self._bundles.get(instance_id)
            _require_monotonic_index_job_activation(
                instance_id,
                (
                    None
                    if previous_bundle is None
                    else previous_bundle.index_job_activation
                ),
                owned.bundle.index_job_activation,
            )
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
            try:
                if previous is not None:
                    self._retired_bundles[id(previous.bundle)] = previous
                else:
                    # A failed metadata capture may leave a retryable owner
                    # without a published bundle. A later first publish must
                    # not overwrite the only reference that can finish it.
                    orphan_owner = (
                        previous_owner
                        if previous_owner is not None
                        else previous_binding
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
                        orphan_appended = True
                        self._orphan_cleanup_owners.append(orphan_owner)
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
                        if not self._retired_bundles:
                            self._invalidate_deferred_drain_tickets_locked()
                raise
        failure = self._drain_retired()
        if failure is not None:
            if not issubclass(type(failure), Exception):
                self._raise_if_closed_after_cleanup(failure)
                raise failure
            _log_cleanup_failure(
                "Retired repository cleanup remains pending: %s",
                failure,
            )
        orphan_failure = self._drain_orphan_cleanup()
        cleanup_failure = failure
        if orphan_failure is not None:
            cleanup_failure = (
                orphan_failure
                if cleanup_failure is None
                else _retain_cleanup_failure(cleanup_failure, orphan_failure)
            )
            if not issubclass(type(orphan_failure), Exception):
                self._raise_if_closed_after_cleanup(cleanup_failure)
                raise orphan_failure
            _log_cleanup_failure(
                "Unpublished repository cleanup remains pending: %s",
                orphan_failure,
            )
        self._raise_if_closed_after_cleanup(cleanup_failure)

    def _retire_active_locked(
        self,
        instance_id: str,
    ) -> Optional[_OwnedRepoBundle]:
        bundle = self._bundles.get(instance_id)
        if bundle is None:
            return None
        source_binding = self._source_bindings.get(instance_id)
        cleanup_owner = self._source_cleanup_owners.get(instance_id)
        owned = _OwnedRepoBundle(bundle, source_binding, cleanup_owner)
        # Install durable retirement ownership before deleting any active-map
        # reference. An asynchronous exception can then create only a harmless
        # temporary alias, never an unreachable resource authority.
        self._retired_bundles[id(bundle)] = owned
        if self._bundles.get(instance_id) is bundle:
            self._bundles.pop(instance_id, None)
        if self._source_bindings.get(instance_id) is source_binding:
            self._source_bindings.pop(instance_id, None)
        if self._source_cleanup_owners.get(instance_id) is cleanup_owner:
            self._source_cleanup_owners.pop(instance_id, None)
        return owned

    def _retire_unpublished(
        self,
        owned: _OwnedRepoBundle,
    ) -> Optional[BaseException]:
        key = id(owned.bundle)
        with self._generation_lock:
            if key in self._retired_bundles:
                return None
            self._retired_bundles[key] = owned
        # This unwind owns only the rejected candidate. Retrying unrelated
        # generations here can close the same failed owner twice within one
        # publication attempt and can manufacture a new higher-priority
        # cancellation while preserving the original error.
        return self._drain_retired(only_keys={key})

    @staticmethod
    def _close_owned(
        owned: _OwnedRepoBundle,
        *,
        close_authority: bool = True,
    ) -> bool:
        """Close one unpinned generation and report whether cleanup completed."""

        bundle = owned.bundle
        first_failure: BaseException | None = None
        bundle.bm25 = None
        bundle.runner = None
        bundle.source_reader = None
        vector_store = bundle.vector_store
        if vector_store is not None:
            try:
                if _vector_cleanup_is_complete(vector_store):
                    bundle.vector_store = None
                else:
                    vector_store.close()
                    bundle.vector_store = None
            except BaseException as exc:  # noqa: B036 - continue source cleanup
                try:
                    if _vector_cleanup_is_complete(vector_store):
                        bundle.vector_store = None
                except BaseException as inspection_failure:  # noqa: B036
                    exc = _retain_cleanup_failure(exc, inspection_failure)
                first_failure = _retain_cleanup_failure(first_failure, exc)

        owner = owned.source_cleanup_owner
        owner_closed = owner is None
        if owner is not None and close_authority:
            try:
                owner_closed = bool(owner.closed)
            except BaseException as exc:  # noqa: B036 - uncertain means pending
                owner_closed = False
                first_failure = _retain_cleanup_failure(first_failure, exc)
            if not owner_closed:
                try:
                    owner.close()
                except BaseException as exc:  # noqa: B036 - retain retry owner
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                try:
                    owner_closed = bool(owner.closed)
                except BaseException as exc:  # noqa: B036 - uncertain means pending
                    owner_closed = False
                    first_failure = _retain_cleanup_failure(first_failure, exc)
            if owner_closed:
                owned.source_cleanup_owner = None
                owned.source_binding = None
        elif owned.source_binding is not None and close_authority:
            try:
                owner_closed = bool(owned.source_binding.closed)
            except BaseException as exc:  # noqa: B036 - uncertain means pending
                owner_closed = False
                first_failure = _retain_cleanup_failure(first_failure, exc)
            if not owner_closed:
                try:
                    owned.source_binding.close()
                except BaseException as exc:  # noqa: B036 - retain retry owner
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                try:
                    owner_closed = bool(owned.source_binding.closed)
                except BaseException as exc:  # noqa: B036 - uncertain means pending
                    owner_closed = False
                    first_failure = _retain_cleanup_failure(first_failure, exc)
            if owner_closed:
                owned.source_binding = None

        complete = (
            bundle.vector_store is None
            and owned.source_cleanup_owner is None
            and owned.source_binding is None
        )
        if first_failure is not None:
            raise first_failure
        return complete

    def _authority_cleanup_in_progress_locked(self, authority_id: int) -> bool:
        return authority_id in self._orphan_cleanup_in_progress or any(
            candidate == authority_id
            for candidate in self._cleanup_in_progress.values()
        )

    def _bundle_key_is_leased_locked(self, key: int) -> bool:
        return any(key in keys for keys in self._bundle_leases.values())

    def _release_retired_cleanup_claim(self, key: int) -> None:
        """Idempotently release one atomic bundle/authority cleanup claim."""

        try:
            with self._generation_lock:
                self._cleanup_in_progress.pop(key, None)
        finally:
            # A line-level asynchronous exception before the first pop still
            # runs this idempotent fallback while the owner remains durable.
            with self._generation_lock:
                self._cleanup_in_progress.pop(key, None)

    def _release_orphan_cleanup_claim(self, authority_id: int) -> None:
        """Idempotently release one orphan-only cleanup claim."""

        try:
            with self._generation_lock:
                self._orphan_cleanup_in_progress.discard(authority_id)
        finally:
            with self._generation_lock:
                self._orphan_cleanup_in_progress.discard(authority_id)

    def _settle_retired_cleanup_claim(
        self,
        key: int,
        owned: _OwnedRepoBundle,
        *,
        owner: Any,
        binding: Any,
        authority: Any,
        suppress_authority: bool,
        complete: bool,
    ) -> None:
        self._release_retired_cleanup_claim(key)
        with self._generation_lock:
            complete = complete or (
                owned.bundle.vector_store is None
                and owned.source_cleanup_owner is None
                and owned.source_binding is None
            )
            authority_settled = (
                not suppress_authority
                and authority is not None
                and (
                    (owner is not None and owned.source_cleanup_owner is None)
                    or (owner is None and owned.source_binding is None)
                )
            )
            if authority_settled:
                for retired in self._retired_bundles.values():
                    if retired.source_cleanup_owner is authority:
                        retired.source_cleanup_owner = None
                        retired.source_binding = None
                    elif retired.source_binding is authority:
                        retired.source_binding = None
                aliases = tuple(
                    candidate for candidate in (owner, binding) if candidate is not None
                )
                self._orphan_cleanup_owners[:] = [
                    candidate
                    for candidate in self._orphan_cleanup_owners
                    if not any(candidate is alias for alias in aliases)
                ]
            if complete and self._retired_bundles.get(key) is owned:
                self._retired_bundles.pop(key, None)
            if not self._retired_bundles:
                self._invalidate_deferred_drain_tickets_locked()

    def _settle_orphan_cleanup_claim(
        self,
        authority_id: int,
        owner: Any,
        *,
        closed: bool,
    ) -> None:
        self._release_orphan_cleanup_claim(authority_id)
        if closed:
            with self._generation_lock:
                self._orphan_cleanup_owners[:] = [
                    candidate
                    for candidate in self._orphan_cleanup_owners
                    if candidate is not owner
                ]

    def _drain_retired(
        self,
        *,
        only_keys: Optional[set[int]] = None,
        settle_deferred: bool = True,
    ) -> Optional[BaseException]:
        def operation() -> Optional[BaseException]:
            first_failure: BaseException | None = None
            with self._generation_lock:
                candidates = [
                    (key, owned)
                    for key, owned in self._retired_bundles.items()
                    if only_keys is None or key in only_keys
                ]
            attempted_authority_ids: set[int] = set()
            for key, owned in candidates:
                complete = False
                claimed_key = False
                suppress_authority = False
                owner = owned.source_cleanup_owner
                binding = owned.source_binding
                authority = owner if owner is not None else binding
                try:
                    with self._generation_lock:
                        authority_id = id(authority) if authority is not None else None
                        suppress_authority = authority_id is not None and (
                            authority_id in attempted_authority_ids
                            or self._authority_cleanup_in_progress_locked(authority_id)
                        )
                        if authority is not None:
                            active_authority_alias = any(
                                candidate is authority
                                for candidate in self._source_cleanup_owners.values()
                            ) or any(
                                candidate is authority
                                for candidate in self._source_bindings.values()
                            )
                            leased_authority_alias = any(
                                self._bundle_key_is_leased_locked(retired_key)
                                and (
                                    retired.source_cleanup_owner is authority
                                    or retired.source_binding is authority
                                )
                                for retired_key, retired in self._retired_bundles.items()
                                if retired_key != key
                            )
                            suppress_authority = suppress_authority or (
                                active_authority_alias or leased_authority_alias
                            )
                        if (
                            self._retired_bundles.get(key) is not owned
                            or any(
                                candidate is owned.bundle
                                for candidate in self._bundles.values()
                            )
                            or self._bundle_key_is_leased_locked(key)
                            or key in self._cleanup_in_progress
                        ):
                            continue
                        if authority_id is not None and not suppress_authority:
                            attempted_authority_ids.add(authority_id)
                        claimed_key = True
                        self._cleanup_in_progress[key] = (
                            authority_id if not suppress_authority else None
                        )
                        if suppress_authority:
                            if owner is not None:
                                owned.source_cleanup_owner = None
                                owned.source_binding = None
                            elif binding is not None:
                                owned.source_binding = None
                    complete = self._run_cleanup_callback(
                        partial(
                            self._close_owned,
                            owned,
                            close_authority=not suppress_authority,
                        )
                    )
                except BaseException as exc:  # noqa: B036 - visit every generation
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                finally:
                    try:
                        if claimed_key:
                            try:
                                self._settle_retired_cleanup_claim(
                                    key,
                                    owned,
                                    owner=owner,
                                    binding=binding,
                                    authority=authority,
                                    suppress_authority=suppress_authority,
                                    complete=complete,
                                )
                            except BaseException as exc:  # noqa: B036
                                first_failure = _retain_cleanup_failure(
                                    first_failure,
                                    exc,
                                )
                    finally:
                        try:
                            if claimed_key:
                                self._release_retired_cleanup_claim(key)
                        except BaseException as exc:  # noqa: B036 - claim is durable
                            first_failure = _retain_cleanup_failure(first_failure, exc)
            with self._generation_lock:
                if not self._retired_bundles:
                    self._invalidate_deferred_drain_tickets_locked()
            return first_failure

        if settle_deferred:
            return self._run_serialized_cleanup(operation)
        return self._run_registry_lock_once(self._cleanup_lock, operation)

    def _drain_orphan_cleanup(self) -> Optional[BaseException]:
        def operation() -> Optional[BaseException]:
            first_failure: BaseException | None = None
            with self._generation_lock:
                owners: List[Any] = []
                for owner in self._orphan_cleanup_owners:
                    if not any(candidate is owner for candidate in owners):
                        owners.append(owner)
            attempted_authority_ids: set[int] = set()
            for owner in owners:
                claimed = False
                closed = False
                authority_id = id(owner)
                try:
                    # A retained generation is the sole authority for retrying
                    # its cleanup. Keep aliases queued until that generation
                    # either settles or releases its final lease.
                    with self._generation_lock:
                        still_queued = any(
                            candidate is owner
                            for candidate in self._orphan_cleanup_owners
                        )
                        generation_owned = (
                            any(
                                candidate is owner
                                for candidate in self._source_cleanup_owners.values()
                            )
                            or any(
                                candidate is owner
                                for candidate in self._source_bindings.values()
                            )
                            or any(
                                retired.source_cleanup_owner is owner
                                or retired.source_binding is owner
                                for retired in self._retired_bundles.values()
                            )
                        )
                        if (
                            not still_queued
                            or generation_owned
                            or authority_id in attempted_authority_ids
                            or self._authority_cleanup_in_progress_locked(authority_id)
                        ):
                            continue
                        attempted_authority_ids.add(authority_id)
                        claimed = True
                        self._orphan_cleanup_in_progress.add(authority_id)
                    closed, failure = self._run_cleanup_callback(
                        partial(_close_orphan_cleanup_owner, owner)
                    )
                    if failure is not None:
                        first_failure = _retain_cleanup_failure(
                            first_failure,
                            failure,
                        )
                except BaseException as exc:  # noqa: B036 - keep owner reachable
                    first_failure = _retain_cleanup_failure(first_failure, exc)
                finally:
                    try:
                        if claimed:
                            try:
                                self._settle_orphan_cleanup_claim(
                                    authority_id,
                                    owner,
                                    closed=closed,
                                )
                            except BaseException as exc:  # noqa: B036
                                first_failure = _retain_cleanup_failure(
                                    first_failure,
                                    exc,
                                )
                    finally:
                        try:
                            if claimed:
                                self._release_orphan_cleanup_claim(authority_id)
                        except BaseException as exc:  # noqa: B036 - claim is durable
                            first_failure = _retain_cleanup_failure(first_failure, exc)
            return first_failure

        return self._run_serialized_cleanup(operation)

    def close(self) -> None:
        """Retire active generations; pinned requests release them later."""

        # Cleanup callbacks may reenter shutdown while a reload thread owns the
        # reload lock and is waiting to publish under this same cleanup lock.
        # Waiting for that reload here would form a lock cycle, so the owning
        # cleanup thread performs the idempotent shutdown transition directly;
        # the outer reload observes ``_closed`` and settles its candidate.
        if self._owns_current_cleanup_callback():
            self._close_under_cleanup_lock()
            return
        if self._cleanup_callback_is_active():
            raise RuntimeError(
                "repository shutdown cannot cross a registry cleanup callback"
            )
        if (
            self._reload_operation_is_active()
            and not self._owns_current_reload_operation()
        ):
            raise RuntimeError(
                "repository shutdown cannot cross a registry reload operation"
            )

        # An external shutdown joins the complete reload operation first, then
        # joins any cleanup drain. Slow builds hold only the reload lock, so
        # unrelated request lease releases remain free to finish.
        self._run_registry_lock(
            self._registry_reload_lock,
            lambda: self._run_serialized_cleanup(self._close_under_cleanup_lock),
        )

    def _close_under_cleanup_lock(self) -> None:
        """Close registry state while the current thread owns cleanup."""

        with self._generation_lock:
            self._closed = True
            for instance_id in tuple(self._bundles):
                self._retire_active_locked(instance_id)
            # Claim owner-only failures before invoking any caller-owned close
            # method. The orphan drain removes its queue before each callback,
            # so a reentrant close cannot acquire the same owner.
            owner_only_ids = tuple(self._source_cleanup_owners) + tuple(
                instance_id
                for instance_id in self._source_bindings
                if instance_id not in self._source_cleanup_owners
            )
            for instance_id in owner_only_ids:
                owner = self._source_cleanup_owners.get(instance_id)
                binding = self._source_bindings.get(instance_id)
                cleanup_owner = owner if owner is not None else binding
                if cleanup_owner is not None and not any(
                    candidate is cleanup_owner
                    for candidate in self._orphan_cleanup_owners
                ):
                    # Publish retry ownership before removing either map entry,
                    # so interruption leaves a reachable authority for retry.
                    self._orphan_cleanup_owners.append(cleanup_owner)
                self._source_cleanup_owners.pop(instance_id, None)
                self._source_bindings.pop(instance_id, None)

        first_failure = self._drain_retired()

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

        if self._cleanup_callback_is_active():
            raise RuntimeError("repository pin cannot start during cleanup")
        if (
            self._reload_operation_is_active()
            and not self._owns_current_reload_operation()
        ):
            raise RuntimeError(
                "repository pin cannot cross a registry reload operation"
            )
        lease_token = object()
        bundle: Optional[RepoBundle] = None
        keys: Tuple[int, ...] = ()
        primary: BaseException | None = None
        cleanup_failure: BaseException | None = None
        try:
            try:
                with self._generation_lock:
                    if self._closed:
                        raise RuntimeError("repository registry is closed")
                    repo_id = _plain_repo_lookup_key(repo_id)
                    bundle = self._bundles.get(repo_id)
                    keys = (id(bundle),) if bundle is not None else ()
                    if keys:
                        self._bundle_leases[lease_token] = keys
                yield bundle
            except BaseException as exc:  # noqa: B036 - settle lease cleanup
                primary = exc
        finally:
            try:
                if keys:
                    try:
                        self._release_bundle_lease(lease_token)
                    except BaseException as exc:  # noqa: B036
                        cleanup_failure = exc
            finally:
                if keys:
                    self._drop_bundle_lease(lease_token)
        if primary is not None:
            if cleanup_failure is not None:
                _raise_with_cleanup_failure(primary, cleanup_failure)
            raise primary
        if cleanup_failure is not None:
            raise cleanup_failure

    @contextmanager
    def pin_all(self) -> Iterator[Tuple[RepoBundle, ...]]:
        """Pin one coherent snapshot of every currently published bundle."""

        if self._cleanup_callback_is_active():
            raise RuntimeError("repository pin cannot start during cleanup")
        if (
            self._reload_operation_is_active()
            and not self._owns_current_reload_operation()
        ):
            raise RuntimeError(
                "repository pin cannot cross a registry reload operation"
            )
        lease_token = object()
        active = False
        bundles: Tuple[RepoBundle, ...] = ()
        keys: Tuple[int, ...] = ()
        primary: BaseException | None = None
        cleanup_failure: BaseException | None = None
        try:
            try:
                with self._generation_lock:
                    active = not self._closed
                    if active:
                        bundles = tuple(self._bundles.values())
                        keys = tuple(id(bundle) for bundle in bundles)
                        if keys:
                            self._bundle_leases[lease_token] = keys
                yield bundles
            except BaseException as exc:  # noqa: B036 - settle lease cleanup
                primary = exc
        finally:
            try:
                if active and keys:
                    try:
                        self._release_bundle_lease(lease_token)
                    except BaseException as exc:  # noqa: B036
                        cleanup_failure = exc
            finally:
                if active and keys:
                    self._drop_bundle_lease(lease_token)
        if primary is not None:
            if cleanup_failure is not None:
                _raise_with_cleanup_failure(primary, cleanup_failure)
            raise primary
        if cleanup_failure is not None:
            raise cleanup_failure

    def _drop_bundle_lease(self, lease_token: object) -> Tuple[int, ...]:
        keys: Tuple[int, ...] = ()
        try:
            with self._generation_lock:
                keys = self._bundle_leases.pop(lease_token, ())
        finally:
            # A trace/signal exception before the first pop still reaches this
            # idempotent fallback, so an exited pin can never remain leased.
            with self._generation_lock:
                fallback_keys = self._bundle_leases.pop(lease_token, ())
                if not keys:
                    keys = fallback_keys
        return keys

    def _discard_deferred_drain_ticket(
        self,
        ticket: _DeferredRegistryDrain,
        *,
        expected_token: object | None = None,
    ) -> bool:
        """Invalidate one exact deferred wakeup under generation ownership."""

        with self._generation_lock:
            if ticket.registry is not self or (
                expected_token is not None and ticket.token is not expected_token
            ):
                return False
            ticket.registry = None
            self._deferred_drain_tickets[:] = [
                ticket_ref
                for ticket_ref in self._deferred_drain_tickets
                if ticket_ref() is not None and ticket_ref() is not ticket
            ]
            return True

    def _invalidate_deferred_drain_tickets_locked(self) -> None:
        """Release every cross-thread ticket after all retired work settles."""

        for ticket_ref in self._deferred_drain_tickets:
            ticket = ticket_ref()
            if ticket is not None and ticket.registry is self:
                ticket.registry = None
        self._deferred_drain_tickets.clear()

    def _retired_drain_state(self) -> Tuple[bool, bool]:
        """Return pending state and whether leases alone block its drain."""

        with self._generation_lock:
            pending = bool(self._retired_bundles)
            if not pending:
                self._invalidate_deferred_drain_tickets_locked()
            return pending, pending and all(
                self._bundle_key_is_leased_locked(key) for key in self._retired_bundles
            )

    def _release_bundle_lease(self, lease_token: object) -> None:
        self._drop_bundle_lease(lease_token)
        cross_cleanup_callback = (
            self._cleanup_callback_is_active()
            and not self._owns_current_cleanup_callback()
        )
        cross_reload_operation = (
            self._reload_operation_is_active()
            and not self._owns_current_reload_operation()
        )
        if cross_cleanup_callback or cross_reload_operation:
            # The pin may have been entered before this cross-registry cleanup
            # callback or reload began. Its lease must still be dropped, but
            # synchronously taking the other registry's cleanup lock would let
            # reciprocal operations form an ABBA cycle. Queue the now-unleased
            # generation for the current thread's outermost lifecycle-lock
            # settlement, after every registry lock on that thread is gone.
            _defer_registry_retired_drain(self)
            return
        # This release is itself the wakeup for the target generation. Do not
        # let the generic lock wrapper immediately retry an older deferred
        # ticket before this first failure has been classified and retained.
        failure = self._drain_retired(settle_deferred=False)
        pending, waits_for_lease = self._retired_drain_state()
        if pending and (failure is not None or not waits_for_lease):
            _defer_registry_retired_drain(self)
        else:
            _remove_deferred_registry_retired_drain(self)
        # Cleanup callbacks may have released another registry's final lease.
        # Settle those tickets now that this cleanup lock is gone, but do not
        # retry this registry's just-attempted owner in the same unwind epoch.
        deferred_failure: BaseException | None = None
        if self._can_flush_deferred_retired_drains():
            attempted_tokens = {
                ticket.token
                for ticket in _deferred_registry_drain_entries()
                if ticket.registry is self
            }
            try:
                deferred_failure = self._flush_deferred_retired_drains(
                    attempted_tokens=attempted_tokens,
                )
            except BaseException as exc:  # noqa: B036 - tickets retain retry state
                deferred_failure = exc
        if failure is not None and deferred_failure is not None:
            if not issubclass(type(failure), Exception) or not issubclass(
                type(deferred_failure),
                Exception,
            ):
                _raise_with_cleanup_failure(failure, deferred_failure)
            failure = _retain_cleanup_failure(failure, deferred_failure)
        elif deferred_failure is not None:
            failure = deferred_failure
        if failure is not None:
            if not issubclass(type(failure), Exception):
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

        repo_id = _plain_repo_lookup_key(repo_id)
        with self._generation_lock:
            return self._bundles.get(repo_id)

    @property
    def retired_generation_count(self) -> int:
        """Number of old generations still leased or awaiting cleanup retry."""

        with self._generation_lock:
            return len(self._retired_bundles)
