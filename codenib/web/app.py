# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""FastAPI service for the DeepWiki-style demo.

Endpoints:
- ``GET  /api/health``  — liveness + loaded repo count.
- ``GET  /api/repos``   — list indexed repos.
- ``POST /api/chat``    — ask a question about a repo, get answer + citations.

Run with::

    codenib-web                         # uses demo_repos.yaml
    uvicorn codenib.web.app:app       # for autoreload during dev
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from ..log_utils import get_logger
from ..repository_filters import repository_path_is_visible
from ..repository_source_selection import RepositorySourceSelection
from ..wiki import WikiBuilder
from ..wiki.media_evidence import build_media_evidence_pack
from ..wiki.media_generation import (
    image_generator_from_config,
    materialize_media_slots,
    read_generated_media_asset,
    redact_media_evidence_packs,
)
from ..wiki.narrator import Narrator
from ..wiki.sqlite_store import SQLiteWikiStore
from .config import load_config
from .index_job_writes import (
    IndexJobConflictError,
    IndexJobRequestError,
    IndexJobWriteError,
    IndexJobWriter,
)
from .index_jobs import (
    IndexJobNotFoundError,
    IndexJobReader,
    IndexJobReadError,
    overlay_active_job,
)
from .index_status import (
    IndexUpdateCapability,
    build_repo_index_status,
    validate_index_update_capabilities,
)
from .local_index_runtime import (
    LocalIndexRuntimeService,
    open_local_index_runtime_service,
)
from .local_index_service import LocalIndexServiceError
from .native_authority import authorize_local_manifest_vector
from .ports import argparse_tcp_port
from .repo_registry import RepoBundle, RepoRegistry
from .repository_files import bound_source_slice
from .request_limits import RequestBodyLimitMiddleware
from .schemas import (
    ChatRequest,
    ChatResponse,
    EdgeLabelRequest,
    EdgeLabelResponse,
    IndexJobCreateRequest,
    IndexJobStatusResponse,
    RepoIndexStatus,
    RepoInfo,
    agent_result_to_response,
)

_WIKI_MEDIA_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

logger = get_logger(__name__)

_MISSING_APP_STATE = object()


@dataclass(slots=True)
class _LocalRuntimeLifespanState:
    service: LocalIndexRuntimeService | None = None
    startup_cleanup_pending: bool = False


class _LiveLocalIndexJobWriter:
    """Gate writes on the live repository and observable runtime health."""

    __slots__ = ("_registry", "_service", "_writer")

    def __init__(
        self,
        service: LocalIndexRuntimeService,
        writer: IndexJobWriter,
        registry: RepoRegistry,
    ) -> None:
        if not isinstance(writer, IndexJobWriter):
            raise TypeError("local runtime writer does not implement its contract")
        self._service = service
        self._writer = writer
        self._registry = registry

    def create(
        self,
        repo_id: str,
        *,
        indexes: tuple[str, ...],
        mode: str,
        force: bool,
        idempotency_key: str,
    ) -> IndexJobStatusResponse:
        # A pin is the admission point: a concurrent reload may retire this
        # generation afterwards, but a request that starts after retirement
        # must not reach the lifespan-wide writer bound to the old checkout.
        with self._registry.pin(repo_id) as bundle:
            if bundle is None:
                raise IndexJobNotFoundError(
                    "Web repository is no longer configured for index updates"
                )
            if not self._service.accepts_repository(repo_id, bundle):
                raise IndexJobWriteError(
                    "Web repository no longer matches the local index runtime"
                )
            # Health is an entry-time availability signal, not part of the
            # catalog transaction. A job accepted just before a loop fault is
            # durable and remains recoverable by the next service process.
            if self._service.state != "running" or self._service.healthy is not True:
                raise IndexJobWriteError("local index runtime is unhealthy")
            return self._writer.create(
                repo_id,
                indexes=indexes,
                mode=mode,
                force=force,
                idempotency_key=idempotency_key,
            )


def _live_local_index_capabilities(
    service: LocalIndexRuntimeService,
    repo_id: str,
    bundle: RepoBundle,
) -> Mapping[str, IndexUpdateCapability] | None:
    """Resolve capabilities against the bundle already pinned by the route."""

    if service.state != "running" or service.healthy is not True:
        return None
    if not service.accepts_repository(repo_id, bundle):
        return None
    try:
        return service.capabilities(repo_id)
    except KeyError:
        return None


def _has_pending_publication_cleanup(failure: BaseException) -> bool:
    """Fail closed when startup retained an unfinished cleanup owner."""

    try:
        owners = BaseException.__getattribute__(
            failure,
            "publication_cleanup_owners",
        )
    except AttributeError:
        return False
    except BaseException:  # noqa: B036 - malformed metadata retains the registry
        return True
    return type(owners) is not tuple or bool(owners)


def _manifest_source_selection(bundle) -> RepositorySourceSelection:
    """Return the current persisted selection, defaulting legacy manifests."""

    selection = getattr(getattr(bundle, "manifest", None), "source_selection", None)
    return (
        selection
        if type(selection) is RepositorySourceSelection
        else RepositorySourceSelection()
    )


def _historical_source_slice(
    bundle,
    source_fn,
    commit: str,
    file: str,
    start: int | None,
    end: int | None,
):
    """Gate a Git-backed historical read with the active manifest policy."""

    if not repository_path_is_visible(
        file,
        selection=_manifest_source_selection(bundle),
    ):
        return None
    return source_fn(commit, file, start, end)


def _wiki_llm(config, *, model=None, max_tokens=4096):
    from ..llm.litellm_chat import LiteLLMChat

    return LiteLLMChat(
        model=model or config.wiki_generation_model,
        temperature=0.2,
        max_tokens=max_tokens,
        api_base=getattr(config, "wiki_generation_api_base", None),
        api_key=getattr(config, "wiki_generation_api_key", None),
        extra_kwargs=getattr(config, "wiki_generation_options", {}),
    )


def _wiki_narrator(config):
    wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
    return Narrator(
        model=config.wiki_generation_model,
        cache_dir=wiki_cache if config.wiki_agent else None,
        enabled=None if config.wiki_agent else False,
        api_base=getattr(config, "wiki_generation_api_base", None),
        api_key=getattr(config, "wiki_generation_api_key", None),
        model_options=getattr(config, "wiki_generation_options", {}),
    )


@contextmanager
def _configured_local_index_runtime(app, config, registry, lifecycle):
    """Expose one configured runtime only for its live service lifetime."""

    if type(lifecycle) is not _LocalRuntimeLifespanState:
        raise TypeError("local runtime lifespan state must use the exact model")

    storage = getattr(config, "index_storage", None)
    if storage is None:
        yield None
        return
    if getattr(config, "mode", "sparse") != "sparse":
        raise LocalIndexServiceError(
            "local index runtime currently requires sparse Web mode"
        )

    try:
        with open_local_index_runtime_service(storage, registry) as service:
            lifecycle.service = service
            writer = _LiveLocalIndexJobWriter(service, service.writer, registry)
            capabilities = partial(_live_local_index_capabilities, service)
            bindings = {
                "index_runtime_service": service,
                "index_job_reader": service.reader,
                "index_job_writer": writer,
                "index_update_capabilities_resolver": capabilities,
            }
            previous = {
                name: getattr(app.state, name, _MISSING_APP_STATE) for name in bindings
            }
            try:
                for name, value in bindings.items():
                    setattr(app.state, name, value)
                yield service
            finally:
                # Stop exposing this generation before its synchronous close joins
                # the worker and reconciler. Preserve any state a test harness or
                # embedding application deliberately replaced while it was live.
                for name, value in bindings.items():
                    if getattr(app.state, name, _MISSING_APP_STATE) is not value:
                        continue
                    prior = previous[name]
                    if prior is _MISSING_APP_STATE:
                        delattr(app.state, name)
                    else:
                        setattr(app.state, name, prior)
    except BaseException as failure:  # noqa: B036 - preserve startup authority
        if lifecycle.service is None:
            lifecycle.startup_cleanup_pending = _has_pending_publication_cleanup(
                failure
            )
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    # The production service is the local administrator boundary: it verifies
    # source fingerprint v2 and the exact captured vector tree through the
    # injected native-index resolver. RepoRegistry separately retains the
    # manifest-selected repository source authority used by every current-tree
    # Web and Wiki read.
    registry = RepoRegistry(
        config,
        native_index_authorization_resolver=authorize_local_manifest_vector,
        # The Web demo may serve BM25 when a legacy/missing vector view cannot
        # be authorized. The vector bytes are never opened on this path; strict
        # authorization and integrity failures still fail closed once a v2
        # source-bound capability is present.
        allow_missing_native_index_authorization=True,
    )
    runtime_lifecycle = _LocalRuntimeLifespanState()
    try:
        logger.info("Loading QA repos from %s ...", config.registry_path)
        registry.load_all()
        app.state.registry = registry
        app.state.wiki_builders = {}
        # Wiki persistence is a regenerable, optional product surface. Open it
        # on the first Wiki request so a damaged cache cannot block search and
        # source APIs during application startup.
        app.state.wiki_store = None
        app.state.edge_labelers = {}
        app.state.commit_windows = {}
        # Shared LLM narrator for DeepWiki-style prose; cached on disk, fails soft
        # to templated text when no model/creds are available.
        app.state.narrator = _wiki_narrator(config)
        logger.info(
            "Wiki narrator: model=%s enabled=%s cache=%s",
            app.state.narrator.model,
            app.state.narrator.enabled,
            app.state.narrator.cache_dir,
        )
        with _configured_local_index_runtime(
            app,
            config,
            registry,
            runtime_lifecycle,
        ) as configured:
            if configured is not None:
                logger.info(
                    "Local index runtime: state=%s healthy=%s",
                    configured.state,
                    configured.healthy,
                )
            logger.info("Ready: %d repo(s) available", len(registry.list_infos()))
            yield
    finally:
        try:
            # A failed runtime close keeps its registry dependency reachable
            # through the retained cleanup owner on the raised exception. Do
            # not retire repository generations underneath a still-live loop.
            runtime_service = runtime_lifecycle.service
            if not runtime_lifecycle.startup_cleanup_pending and (
                runtime_service is None or runtime_service.closed
            ):
                registry.close()
            else:
                logger.error(
                    "Local index runtime did not settle; retaining RepoRegistry"
                )
        finally:
            for name in ("wiki_builders", "edge_labelers", "commit_windows"):
                cache = getattr(app.state, name, None)
                if isinstance(cache, dict):
                    cache.clear()


app = FastAPI(title="CodeNib Code QA", lifespan=lifespan)

app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=load_config().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_timing(request: Request, call_next):
    """Expose backend time and log slow API paths without query contents."""

    started_at = perf_counter()
    try:
        response = await call_next(request)
    finally:
        _prune_retired_generation_caches()
    duration_ms = (perf_counter() - started_at) * 1000
    response.headers.append("Server-Timing", f"codenib;dur={duration_ms:.1f}")
    if request.url.path.startswith("/api/") and duration_ms >= 2_000:
        logger.info(
            "Slow API request: %s %s %.1f ms",
            request.method,
            request.url.path,
            duration_ms,
        )
    return response


@app.exception_handler(RequestValidationError)
async def request_validation_error(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return useful validation details without echoing untrusted payloads."""
    errors = []
    for raw_error in exc.errors():
        error = {key: value for key, value in raw_error.items() if key != "input"}
        context = error.get("ctx")
        if isinstance(context, dict):
            error["ctx"] = {key: str(value) for key, value in context.items()}
        errors.append(error)
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": errors}),
    )


def _registry() -> RepoRegistry:
    registry = getattr(app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Server still starting up")
    return registry


def _index_job_reader() -> IndexJobReader:
    reader = getattr(app.state, "index_job_reader", None)
    if not isinstance(reader, IndexJobReader):
        raise HTTPException(
            status_code=503,
            detail="Durable index job status is not configured",
        )
    return reader


def _index_job_writer() -> IndexJobWriter:
    writer = getattr(app.state, "index_job_writer", None)
    if not isinstance(writer, IndexJobWriter):
        raise HTTPException(
            status_code=503,
            detail="Durable index job creation is not configured",
        )
    return writer


def _index_update_capabilities(repo_id: str, bundle: RepoBundle):
    """Resolve writer capabilities for one pinned Web repository generation."""

    resolver = getattr(app.state, "index_update_capabilities_resolver", None)
    if resolver is not None and not callable(resolver):
        raise HTTPException(
            status_code=503,
            detail="Index update capabilities are unavailable",
        )
    try:
        candidate = (
            getattr(app.state, "index_update_capabilities", None)
            if resolver is None
            else resolver(repo_id, bundle)
        )
        return validate_index_update_capabilities(candidate)
    except Exception as exc:
        logger.warning(
            "Index update capability resolution unavailable: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Index update capabilities are unavailable",
        ) from exc


def _bundle(repo_id: str):
    bundle = _registry().get(repo_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Unknown repo: {repo_id!r}")
    return bundle


@contextmanager
def _pinned_bundle(repo_id: str):
    """Keep one repository generation alive for a complete Web operation."""

    registry = getattr(app.state, "registry", None)
    pin = getattr(registry, "pin", None)
    if not callable(pin):
        # Preserve the small injected registry contract used by offline tools;
        # the production RepoRegistry always supplies generation pinning.
        yield _bundle(repo_id)
        return
    with pin(repo_id) as bundle:
        if bundle is None:
            raise HTTPException(status_code=404, detail=f"Unknown repo: {repo_id!r}")
        yield bundle


_THREAD_CALL_SUCCEEDED = object()
_THREAD_CALL_FAILED = object()


def _capture_thread_outcome(function, args, kwargs):
    """Keep control-flow exceptions out of asyncio Future transport."""

    try:
        return (_THREAD_CALL_SUCCEEDED, function(*args, **kwargs))
    except BaseException as failure:  # noqa: B036 - preserve exact identity
        return (_THREAD_CALL_FAILED, failure)


async def _run_pinned_thread(function, /, *args, **kwargs):
    """Keep the surrounding bundle lease until an offloaded call settles."""

    worker = asyncio.create_task(
        asyncio.to_thread(_capture_thread_outcome, function, args, kwargs)
    )
    try:
        outcome = await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling an asyncio.to_thread() awaiter cannot stop a thread that is
        # already using the pinned generation.  Consume repeated cancellation
        # requests until that worker settles, then let the original cancellation
        # leave the surrounding pin context.  Any worker failure is secondary to
        # the already-observed request cancellation and is consumed here so it
        # cannot become an un-retrieved task exception.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: B036 - cancellation stays primary
                break
        if worker.done() and not worker.cancelled():
            try:
                worker.result()
            except BaseException:  # noqa: B036 - consume secondary worker failure
                pass
        raise
    state, value = outcome
    if state is _THREAD_CALL_FAILED:
        if issubclass(type(value), (StopIteration, StopAsyncIteration)):
            # Iteration sentinels cannot cross an async function boundary:
            # PEP 479 would otherwise rewrite them implicitly. Make that
            # unavoidable mapping explicit and retain the exact worker error.
            raise RuntimeError("thread worker raised an iteration sentinel") from value
        raise value
    return value


def _generation_cached(cache: dict, key: str, repo_id: str, bundle, factory):
    """Reuse a helper only while it is bound to the active bundle generation."""

    for cached_key, cached_value in tuple(cache.items()):
        if not (
            isinstance(cached_value, tuple)
            and len(cached_value) == 3
            and cached_value[0] == repo_id
            and cached_value[1] is not bundle
        ):
            continue
        cache.pop(cached_key, None)
    cached = cache.get(key)
    if (
        isinstance(cached, tuple)
        and len(cached) == 3
        and cached[0] == repo_id
        and cached[1] is bundle
    ):
        return cached[2]
    value = factory()
    cache[key] = (repo_id, bundle, value)
    return value


def _prune_retired_generation_caches(registry=None) -> None:
    """Drop helpers whose bundle is no longer the published generation."""

    if registry is None:
        registry = getattr(app.state, "registry", None)
    get_bundle = getattr(registry, "get", None)
    if not callable(get_bundle):
        return
    for name in ("wiki_builders", "edge_labelers", "commit_windows"):
        cache = getattr(app.state, name, None)
        if not isinstance(cache, dict):
            continue
        for key, cached in tuple(cache.items()):
            if not (isinstance(cached, tuple) and len(cached) == 3):
                continue
            repo_id, bundle, _helper = cached
            try:
                current = get_bundle(repo_id)
            except BaseException:  # noqa: B036 - cache pruning is best effort
                continue
            if current is not bundle:
                cache.pop(key, None)


def _wiki(repo_id: str, bundle=None):
    """Lazily build + cache a wiki per repo (conceptual agent wiki by default)."""
    if bundle is None:
        bundle = _bundle(repo_id)
    cache = app.state.wiki_builders

    def build():
        config = load_config()
        if getattr(config, "wiki_agent", True):
            from ..wiki.agent_wiki import AgentWiki

            wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
            store = getattr(app.state, "wiki_store", None)
            if store is None:
                store = SQLiteWikiStore(Path(wiki_cache) / "wiki.sqlite3")
                app.state.wiki_store = store
            return AgentWiki(
                bundle,
                config.wiki_generation_model,
                cache_dir=wiki_cache,
                store=store,
                llm=_wiki_llm(config),
                api_base=config.wiki_generation_api_base,
                api_key=config.wiki_generation_api_key,
            )
        return WikiBuilder(bundle, narrator=getattr(app.state, "narrator", None))

    return _generation_cached(cache, repo_id, repo_id, bundle, build)


def _wiki_media_dir(config, repo_id: str, page_id: str) -> Path:
    root = Path(os.path.abspath(config.data_dir)) / "wiki_media"
    repo_key = hashlib.sha256(repo_id.encode("utf-8")).hexdigest()
    page_key = hashlib.sha256(page_id.encode("utf-8")).hexdigest()
    return root / repo_key / page_key


def _wiki_media_source_snippet(source_reader, citation: Mapping) -> str | None:
    source = bound_source_slice(
        source_reader,
        str(citation.get("file") or ""),
        citation.get("start_line"),
        citation.get("end_line"),
    )
    if not isinstance(source, Mapping):
        return None
    return str(source.get("content") or "") or None


def _wiki_media_evidence_builder(bundle, page: Mapping):
    evidence = page.get("evidence")
    relations = evidence.get("relations") or () if isinstance(evidence, Mapping) else ()
    source_reader = getattr(bundle, "source_reader", None)
    source_loader = (
        partial(_wiki_media_source_snippet, source_reader)
        if source_reader is not None
        else None
    )
    return partial(
        build_media_evidence_pack,
        page_id=str(page.get("id") or ""),
        page_title=str(page.get("title") or ""),
        page_markdown=str(page.get("markdown") or ""),
        citations=page.get("citations") or (),
        relations=relations,
        source_reader=source_loader,
    )


def _materialize_wiki_media(
    repo_id: str,
    page_id: str,
    page: dict,
    bundle=None,
) -> dict:
    """Attach generated media assets when a wiki media provider is configured."""

    public_page = redact_media_evidence_packs(page)
    config = load_config()
    generator = image_generator_from_config(config)
    if generator is None:
        return public_page
    try:
        asset_base = (
            f"api/repos/{quote(repo_id, safe='')}/wiki-media/"
            f"{quote(page_id, safe='')}"
        )
        return materialize_media_slots(
            page,
            generator=generator,
            output_dir=_wiki_media_dir(config, repo_id, page_id),
            asset_base_path=asset_base,
            evidence_builder=_wiki_media_evidence_builder(
                bundle if bundle is not None else _bundle(repo_id),
                page,
            ),
        )
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - media is optional, wiki still loads
        logger.warning(
            "Wiki media generation failed for %s/%s: %s",
            repo_id,
            page_id,
            exc,
            exc_info=True,
        )
        return public_page


def _safe_media_filename(value: str) -> str:
    if (
        not value
        or len(value.encode("utf-8")) > 256
        or not value.isascii()
        or any(not (character.isalnum() or character in "._-") for character in value)
    ):
        raise HTTPException(status_code=404, detail="media asset not found")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise HTTPException(status_code=404, detail="media asset not found")
    if len(path.parts) != 1:
        raise HTTPException(status_code=404, detail="media asset not found")
    filename = path.name
    if Path(filename).suffix.lower() not in _WIKI_MEDIA_TYPES:
        raise HTTPException(status_code=404, detail="media asset not found")
    return filename


def _edge_labeler(repo_id: str, commit: str | None = None, bundle=None):
    """Build a labeler whose source reader matches the requested graph commit."""
    cache = app.state.edge_labelers
    if bundle is None:
        bundle = _bundle(repo_id)
    base_commit = str(bundle.entry.base_commit or "")
    source_fn = None
    source_commit = base_commit
    if commit:
        window = _commit_window(repo_id, bundle)
        entry = window.resolve(commit) if window.available else None
        if entry is not None:
            source_commit = str(entry.get("sha") or "")
            source_fn = partial(
                _historical_source_slice,
                bundle,
                window.source_for,
                source_commit,
            )
        elif commit.strip().lower() not in {
            base_commit.lower(),
            base_commit[:8].lower(),
        }:
            raise ValueError(f"unknown commit for repository: {commit}")
    if source_fn is None:
        source_reader = getattr(bundle, "source_reader", None)
        source_fn = (
            partial(bound_source_slice, source_reader)
            if source_reader is not None
            else lambda *_args, **_kwargs: None
        )

    cache_key = f"{repo_id}@{source_commit}"

    def build():
        from .edge_label import EdgeLabeler

        config = load_config()
        wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
        namespace = f"{repo_id}@{source_commit}"
        model = config.edge_label_model or config.wiki_generation_model
        return EdgeLabeler(
            source_fn=source_fn,
            model=model,
            cache_dir=wiki_cache,
            cache_namespace=namespace,
            llm=_wiki_llm(config, model=model, max_tokens=1024),
            api_base=config.wiki_generation_api_base,
            api_key=config.wiki_generation_api_key,
        )

    return _generation_cached(cache, cache_key, repo_id, bundle, build)


def _commit_window(repo_id: str, bundle=None):
    """Lazily build + cache this repo's per-commit graph window."""
    if bundle is None:
        bundle = _bundle(repo_id)
    cache = app.state.commit_windows

    def build():
        from .commit_window import CommitWindow

        return CommitWindow(bundle.entry.repo_dir)

    return _generation_cached(cache, repo_id, repo_id, bundle, build)


def _window_stats_for_bundle(repo_id: str, bundle):
    """Commit-window cost figures bound to one already-pinned generation."""

    from .schemas import WindowStats

    window = _commit_window(repo_id, bundle)
    if not window.available:
        return None
    stats = window.summary().get("stats")
    return WindowStats(**stats) if stats else None


def _window_stats_for(repo_id: str):
    """Commit-window cost figures for *repo_id*, or None when it has no window."""

    with _pinned_bundle(repo_id) as bundle:
        return _window_stats_for_bundle(repo_id, bundle)


@app.get("/api/health")
async def health() -> dict:
    registry = getattr(app.state, "registry", None)
    return {
        "status": "ok",
        "repos": len(registry.list_infos()) if registry else 0,
    }


@app.get("/api/repos", response_model=list[RepoInfo])
async def list_repos() -> list[RepoInfo]:
    registry = _registry()
    edge_on = bool(getattr(load_config(), "edge_labels", False))

    async def decorate(info: RepoInfo, bundle=None) -> RepoInfo:
        # Surface the global edge-label toggle per repo so the UI can gate the
        # feature, then derive window figures from that same generation.
        info.capabilities = {**info.capabilities, "edge_labels": edge_on}
        try:
            if bundle is None:
                info.incremental = await asyncio.to_thread(
                    _window_stats_for,
                    info.id,
                )
            else:
                info.incremental = await _run_pinned_thread(
                    _window_stats_for_bundle,
                    info.id,
                    bundle,
                )
        except HTTPException:
            info.incremental = None
        return info

    pin_all = getattr(registry, "pin_all", None)
    if callable(pin_all):
        with pin_all() as bundles:
            return [await decorate(bundle.info(), bundle) for bundle in bundles]

    # Preserve the intentionally small injected registry contract used by
    # offline tools and route tests. Production always takes the coherent path.
    return [await decorate(info) for info in registry.list_infos()]


@app.get(
    "/api/repos/{repo_id}/index-status",
    response_model=RepoIndexStatus,
)
async def index_status(repo_id: str) -> RepoIndexStatus:
    """Return a detached status snapshot for one pinned bundle generation."""

    with _pinned_bundle(repo_id) as bundle:
        kwargs = {
            "update_capabilities": _index_update_capabilities(repo_id, bundle),
        }
        head_resolver = getattr(app.state, "index_head_resolver", None)
        if callable(head_resolver):
            kwargs["current_head_resolver"] = head_resolver
        status = await _run_pinned_thread(
            build_repo_index_status,
            bundle,
            **kwargs,
        )
    reader = getattr(app.state, "index_job_reader", None)
    if reader is None:
        return status
    if not isinstance(reader, IndexJobReader):
        raise HTTPException(
            status_code=503,
            detail="Durable index job status is unavailable",
        )
    try:
        active_job = await asyncio.to_thread(reader.active, repo_id)
        return overlay_active_job(status, active_job)
    except IndexJobNotFoundError:
        return status
    except (IndexJobReadError, ValueError) as exc:
        logger.warning(
            "Durable index job overlay unavailable: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Durable index job status is unavailable",
        ) from exc


@app.get(
    "/api/index-jobs/{job_id}",
    response_model=IndexJobStatusResponse,
)
async def index_job_status(
    job_id: str,
    after_sequence: Annotated[int, Query(ge=0, lt=2**63)] = 0,
    event_limit: Annotated[int, Query(ge=1, le=64)] = 64,
) -> IndexJobStatusResponse:
    """Return one authorized durable job and a bounded event page."""

    try:
        return await asyncio.to_thread(
            _index_job_reader().get,
            job_id,
            after_sequence=after_sequence,
            event_limit=event_limit,
        )
    except (IndexJobNotFoundError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Index job not found") from exc
    except IndexJobReadError as exc:
        logger.warning(
            "Durable index job read unavailable: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Durable index job status is unavailable",
        ) from exc


@app.post(
    "/api/repos/{repo_id}/index-jobs",
    response_model=IndexJobStatusResponse,
    status_code=202,
)
async def create_index_job(
    repo_id: str,
    request: IndexJobCreateRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=256),
    ],
) -> IndexJobStatusResponse:
    """Atomically enqueue one explicitly supported durable index update."""

    try:
        return await asyncio.to_thread(
            _index_job_writer().create,
            repo_id,
            indexes=tuple(request.indexes),
            mode=request.mode,
            force=request.force,
            idempotency_key=idempotency_key,
        )
    except IndexJobNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="Repository is not configured for index updates",
        ) from exc
    except IndexJobRequestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IndexJobConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="An index update is already active or the idempotency key conflicts",
        ) from exc
    except IndexJobWriteError as exc:
        logger.warning(
            "Durable index job creation unavailable: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Durable index job creation is unavailable",
        ) from exc


@app.get("/api/repos/{repo_id}/wiki")
async def wiki_tree(repo_id: str, cached_only: bool = False) -> dict:
    with _pinned_bundle(repo_id) as bundle:
        builder = _wiki(repo_id, bundle)
        if cached_only:
            cached_page_tree = getattr(builder, "cached_page_tree", None)
            pages = (
                await _run_pinned_thread(cached_page_tree)
                if callable(cached_page_tree)
                else None
            )
            pages = pages or []
        else:
            pages = await _run_pinned_thread(builder.page_tree)
        return {"repo": bundle.entry.repo, "pages": pages}


@app.get("/api/repos/{repo_id}/wiki/{page_id}")
async def wiki_page(
    repo_id: str,
    page_id: str,
    materialize_media: bool = True,
) -> dict:
    with _pinned_bundle(repo_id) as bundle:
        page = await _run_pinned_thread(_wiki(repo_id, bundle).page, page_id)
        if page is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown wiki page: {page_id!r}",
            )
        if "media_slots" not in page:
            page = {**page, "media_slots": []}
        if materialize_media and page.get("media_slots"):
            page = await _run_pinned_thread(
                _materialize_wiki_media,
                repo_id,
                page_id,
                page,
                bundle,
            )
        if "generation" not in page:
            page = {
                **page,
                "generation": {
                    "mode": "offline",
                    "model": None,
                    "repaired": False,
                },
                "grounding": {
                    "valid": True,
                    "citation_coverage": 1.0,
                    "cited_evidence": len(page.get("citations") or []),
                    "evidence_count": len(page.get("citations") or []),
                    "relation_count": 0,
                },
            }
        return redact_media_evidence_packs(page)


@app.get("/api/repos/{repo_id}/wiki-media/{page_id}/{filename:path}")
async def wiki_media_asset(repo_id: str, page_id: str, filename: str):
    with _pinned_bundle(repo_id):
        safe_filename = _safe_media_filename(filename)
        payload = read_generated_media_asset(
            _wiki_media_dir(load_config(), repo_id, page_id), safe_filename
        )
        if payload is None:
            raise HTTPException(status_code=404, detail="media asset not found")
        suffix = Path(safe_filename).suffix.lower()
        headers = {
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if suffix == ".svg":
            headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return Response(
            content=payload,
            media_type=_WIKI_MEDIA_TYPES[suffix],
            headers=headers,
        )


@app.get("/api/repos/{repo_id}/wiki/{page_id}/graph")
async def wiki_page_graph(repo_id: str, page_id: str) -> dict:
    """Induced dependency subgraph over a wiki page's cited symbols.

    Lets a wiki page render as a *view over the graph* (subsystem symbols + how
    they connect), using the same ``{nodes, edges}`` payload as ``/codemap``.
    """
    with _pinned_bundle(repo_id) as bundle:
        builder = _wiki(repo_id, bundle)
        page_citations = getattr(builder, "page_citations", None)
        if callable(page_citations):
            citations = await _run_pinned_thread(page_citations, page_id)
            if citations is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown wiki page: {page_id!r}",
                )
        else:
            # The deterministic WikiBuilder does not make a model call, so its
            # existing page contract remains a safe compatibility fallback.
            page = await _run_pinned_thread(builder.page, page_id)
            if page is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown wiki page: {page_id!r}",
                )
            citations = page.get("citations", []) if isinstance(page, dict) else []
        graph = await _run_pinned_thread(bundle.code_graph)
        if graph is None:
            return {
                "available": False,
                "nodes": [],
                "edges": [],
                "mermaid": "",
                "note": bundle.graph_unavailable_note(),
            }
        from .codemap import build_page_subgraph

        hierarchy_graph = await _run_pinned_thread(bundle.hierarchical_graph)
        return await _run_pinned_thread(
            build_page_subgraph,
            graph,
            citations,
            repo_dir=bundle.entry.repo_dir,
            hierarchy_graph=hierarchy_graph,
            source_reader=bundle.source_reader,
        )


@app.get("/api/repos/{repo_id}/source")
async def source(
    repo_id: str,
    file: str,
    start: int | None = None,
    end: int | None = None,
    commit: str | None = None,
) -> dict:
    """Read source from the commit that produced the active graph payload."""
    with _pinned_bundle(repo_id) as bundle:
        result = None
        if commit:
            window = _commit_window(repo_id, bundle)
            entry = window.resolve(commit) if window.available else None
            if entry is not None:
                result = await _run_pinned_thread(
                    _historical_source_slice,
                    bundle,
                    window.source_for,
                    commit,
                    file,
                    start,
                    end,
                )
            else:
                base = str(bundle.entry.base_commit or "")
                requested = commit.strip().lower()
                if requested not in {base.lower(), base[:8].lower()}:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Unknown commit for this repo: {commit!r}",
                    )
                source_reader = getattr(bundle, "source_reader", None)
                if source_reader is not None:
                    result = await _run_pinned_thread(
                        bound_source_slice,
                        source_reader,
                        file,
                        start,
                        end,
                    )
        else:
            source_reader = getattr(bundle, "source_reader", None)
            if source_reader is not None:
                result = await _run_pinned_thread(
                    bound_source_slice,
                    source_reader,
                    file,
                    start,
                    end,
                )
        if result is None:
            raise HTTPException(status_code=404, detail=f"File not found: {file!r}")
        return result


@app.get("/api/repos/{repo_id}/commits")
async def commits(repo_id: str) -> dict:
    """Commits selectable in the graph view, newest first.

    Backed by ``scripts/build_commit_window.py``. Repos without a prebuilt
    window return ``available=False`` and the UI keeps its single-commit label.
    """
    with _pinned_bundle(repo_id) as bundle:
        window = _commit_window(repo_id, bundle)
        return await _run_pinned_thread(window.summary)


@app.get("/api/repos/{repo_id}/codemap")
async def codemap(
    repo_id: str,
    symbol: str | None = None,
    direction: str = "both",
    depth: int = 2,
    max_nodes: int = 40,
    commit: str | None = None,
) -> dict:
    """Dependency subgraph ("codemap") around *symbol* (or a central default).

    Returns ``{available, root, nodes, edges, mermaid, ...}``; the ``mermaid``
    field renders directly in the frontend's existing diagram component.
    """
    with _pinned_bundle(repo_id) as bundle:
        # Prefer a commit-window snapshot when one exists: an explicit ``commit``
        # selects that point in history, and an absent one still defaults to the
        # window's newest commit so the graph matches the selector's default.
        window = _commit_window(repo_id, bundle)
        graph = None
        selected_commit = None
        loaded_from_window = False
        fell_back = False
        if window.available:
            entry = window.resolve(commit)
            if entry is None and commit:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown commit for this repo: {commit!r}",
                )
            graph = await _run_pinned_thread(window.graph_for, commit)
            if entry is not None and graph is not None:
                selected_commit = entry.get("sha")
                loaded_from_window = True
        if graph is None:
            # The snapshot was absent or unloadable (e.g. a graph.pkl written under
            # an older schema_version). Serving the repo's default graph is fine;
            # reporting it as the requested commit is not -- the client would render
            # one commit's graph under another commit's label.
            graph = await _run_pinned_thread(bundle.code_graph)
            selected_commit = bundle.entry.base_commit
            fell_back = window.available
        if graph is None:
            return {
                "available": False,
                "nodes": [],
                "edges": [],
                "hierarchy": {
                    "root": "hier::root",
                    "nodes": [],
                    "open_files": [],
                },
                "mermaid": "",
                "note": bundle.graph_unavailable_note(),
                "setup": bundle.graph_setup().to_dict(),
            }
        from .codemap import build_codemap

        hierarchy_graph = (
            None
            if loaded_from_window
            else await _run_pinned_thread(bundle.hierarchical_graph)
        )
        result = await _run_pinned_thread(
            build_codemap,
            graph,
            symbol,
            direction,
            depth,
            max_nodes,
            repo_dir=bundle.entry.repo_dir,
            repo_commit=selected_commit if loaded_from_window else None,
            hierarchy_graph=hierarchy_graph,
            source_reader=(
                None if loaded_from_window else getattr(bundle, "source_reader", None)
            ),
            source_selection=(
                _manifest_source_selection(bundle) if loaded_from_window else None
            ),
        )
        # Let the client confirm which snapshot it is looking at. ``fell_back``
        # marks the case where a window exists but its snapshot could not be served,
        # so the UI can say so instead of implying the selection took effect.
        if isinstance(result, dict):
            result["commit"] = selected_commit
            if fell_back:
                result["fell_back"] = True
        return result


@app.get("/api/repos/{repo_id}/modulemap")
async def modulemap(
    repo_id: str,
    focus: str | None = None,
    granularity: str = "auto",
    depth: int = 2,
    max_nodes: int = 60,
    include_tests: bool = False,
    commit: str | None = None,
) -> dict:
    """Module ("which file depends on which") view, projected from the symbol graph.

    The symbol codemap cannot answer this — a re-export barrel has no outgoing
    symbol references — so this aggregates reference edges through each symbol's
    file. Same ``{nodes, edges, hierarchy, mermaid}`` shape as ``/codemap``.
    """
    with _pinned_bundle(repo_id) as bundle:
        window = _commit_window(repo_id, bundle)
        graph = None
        selected_commit = None
        loaded_from_window = False
        fell_back = False
        if window.available:
            entry = window.resolve(commit)
            if entry is None and commit:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown commit for this repo: {commit!r}",
                )
            graph = await _run_pinned_thread(window.graph_for, commit)
            if entry is not None and graph is not None:
                selected_commit = entry.get("sha")
                loaded_from_window = True
        if graph is None:
            graph = await _run_pinned_thread(bundle.code_graph)
            selected_commit = bundle.entry.base_commit
            fell_back = window.available
        if graph is None:
            return {
                "available": False,
                "granularity": (
                    granularity if granularity in ("file", "directory") else "file"
                ),
                "nodes": [],
                "edges": [],
                "hierarchy": {
                    "root": "hier::root",
                    "nodes": [],
                    "open_files": [],
                },
                "mermaid": "",
                "note": bundle.graph_unavailable_note(),
                "setup": bundle.graph_setup().to_dict(),
            }
        from .modulemap import build_modulemap

        result = await _run_pinned_thread(
            build_modulemap,
            graph,
            focus,
            granularity,
            depth,
            max_nodes,
            repo_dir=bundle.entry.repo_dir,
            include_tests=include_tests,
            repo_commit=selected_commit if loaded_from_window else None,
            source_reader=(
                None if loaded_from_window else getattr(bundle, "source_reader", None)
            ),
            source_selection=(
                _manifest_source_selection(bundle) if loaded_from_window else None
            ),
        )
        if isinstance(result, dict):
            result["commit"] = selected_commit
            if fell_back:
                result["fell_back"] = True
        return result


@app.post("/api/repos/{repo_id}/edge-label", response_model=EdgeLabelResponse)
async def edge_label(repo_id: str, req: EdgeLabelRequest) -> EdgeLabelResponse:
    """Short LLM phrase describing how the source symbol uses the target.

    On-demand + cached; gated by ``edge_labels`` config. Returns ``label=""``
    (with ``disabled``/empty) whenever the feature is off or nothing could be
    generated — the UI falls back to its ref-count display.
    """
    if not bool(getattr(load_config(), "edge_labels", False)):
        return EdgeLabelResponse(label="", disabled=True)
    with _pinned_bundle(repo_id) as bundle:
        try:
            labeler = _edge_labeler(repo_id, req.commit, bundle)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        anchors = [a.model_dump() for a in req.anchors]
        label, cached = await _run_pinned_thread(
            labeler.label,
            req.source.file,
            req.source.line,
            req.source.end_line,
            req.source.label,
            req.target.file,
            req.target.line,
            req.target.end_line,
            req.target.label,
            anchors,
        )
        return EdgeLabelResponse(label=label, cached=cached)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(
            status_code=400, detail="last message must be from the user"
        )
    query = req.messages[-1].content.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")

    with _pinned_bundle(req.repo_id) as bundle:
        await _run_pinned_thread(bundle.ensure_runtime)
        if bundle.runner is None:
            raise HTTPException(status_code=503, detail="repo runtime is unavailable")

        # Earlier messages give the agent context so it can resolve follow-ups
        # like "what calls it?".
        chat_history = [
            {"role": message.role, "content": message.content}
            for message in req.messages[:-1]
        ]

        try:
            result = await _run_pinned_thread(
                bundle.runner.run,
                query,
                chat_history=chat_history,
            )
        except Exception as exc:  # noqa: BLE001 - clean 500 without query content
            logger.error(
                "agent run failed for %r: %s",
                req.repo_id,
                exc,
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail="agent run failed") from exc

        source_reader = getattr(bundle, "source_reader", None)
        if source_reader is None:
            raise HTTPException(
                status_code=503,
                detail="authenticated repository source is unavailable",
            )
        return await _run_pinned_thread(
            agent_result_to_response,
            result,
            repo_path=getattr(bundle.entry, "repo_dir", ""),
            source_reader=source_reader,
        )


def _parse_args(
    argv: list[str] | None = None,
    *,
    prog: str = "codenib-web",
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Start the CodeNib FastAPI service.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("CODENIB_DEMO_HOST", "127.0.0.1"),
        help="interface to bind",
    )
    parser.add_argument(
        "--port",
        type=argparse_tcp_port,
        default=os.environ.get("CODENIB_DEMO_PORT", "8000"),
        help="TCP port to bind",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="reload the server when source files change",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the ``codenib-web`` console entry point."""
    import uvicorn

    args = _parse_args(argv)
    application = "codenib.web.app:app" if args.reload else app
    uvicorn.run(application, host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
