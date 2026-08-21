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
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path, PurePosixPath
from time import perf_counter
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from ..log_utils import get_logger
from ..repository_filters import repository_path_is_visible
from ..repository_source_selection import RepositorySourceSelection
from ..wiki import WikiBuilder
from ..wiki.media_generation import (
    image_generator_from_config,
    materialize_media_slots,
    read_generated_media_asset,
)
from ..wiki.narrator import Narrator
from .config import load_config
from .native_authority import authorize_local_manifest_vector
from .ports import argparse_tcp_port
from .repo_registry import RepoRegistry
from .repository_files import bound_source_slice
from .request_limits import RequestBodyLimitMiddleware
from .schemas import (
    ChatRequest,
    ChatResponse,
    EdgeLabelRequest,
    EdgeLabelResponse,
    RepoInfo,
    agent_result_to_response,
)

_WIKI_MEDIA_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

logger = get_logger(__name__)


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
    try:
        logger.info("Loading QA repos from %s ...", config.registry_path)
        registry.load_all()
        app.state.registry = registry
        app.state.wiki_builders = {}
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
        logger.info("Ready: %d repo(s) available", len(registry.list_infos()))
        yield
    finally:
        registry.close()


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
    response = await call_next(request)
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


def _bundle(repo_id: str):
    bundle = _registry().get(repo_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Unknown repo: {repo_id!r}")
    return bundle


def _wiki(repo_id: str):
    """Lazily build + cache a wiki per repo (conceptual agent wiki by default)."""
    cache = app.state.wiki_builders
    if repo_id not in cache:
        config = load_config()
        if getattr(config, "wiki_agent", True):
            from ..wiki.agent_wiki import AgentWiki

            wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
            cache[repo_id] = AgentWiki(
                _bundle(repo_id),
                config.wiki_generation_model,
                cache_dir=wiki_cache,
                llm=_wiki_llm(config),
                api_base=config.wiki_generation_api_base,
                api_key=config.wiki_generation_api_key,
            )
        else:
            cache[repo_id] = WikiBuilder(
                _bundle(repo_id), narrator=getattr(app.state, "narrator", None)
            )
    return cache[repo_id]


def _wiki_media_dir(config, repo_id: str, page_id: str) -> Path:
    root = Path(os.path.abspath(config.data_dir)) / "wiki_media"
    repo_key = hashlib.sha256(repo_id.encode("utf-8")).hexdigest()
    page_key = hashlib.sha256(page_id.encode("utf-8")).hexdigest()
    return root / repo_key / page_key


def _materialize_wiki_media(repo_id: str, page_id: str, page: dict) -> dict:
    """Attach generated media assets when a wiki media provider is configured."""

    config = load_config()
    generator = image_generator_from_config(config)
    if generator is None:
        return page
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
        return page


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


def _edge_labeler(repo_id: str, commit: str | None = None):
    """Build a labeler whose source reader matches the requested graph commit."""
    cache = app.state.edge_labelers
    bundle = _bundle(repo_id)
    base_commit = str(bundle.entry.base_commit or "")
    source_fn = None
    source_commit = base_commit
    if commit:
        window = _commit_window(repo_id)
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
    if cache_key not in cache:
        from .edge_label import EdgeLabeler

        config = load_config()
        wiki_cache = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
        namespace = f"{bundle.entry.instance_id}@{source_commit}"
        model = config.edge_label_model or config.wiki_generation_model
        cache[cache_key] = EdgeLabeler(
            source_fn=source_fn,
            model=model,
            cache_dir=wiki_cache,
            cache_namespace=namespace,
            llm=_wiki_llm(config, model=model, max_tokens=1024),
            api_base=config.wiki_generation_api_base,
            api_key=config.wiki_generation_api_key,
        )
    return cache[cache_key]


def _commit_window(repo_id: str):
    """Lazily build + cache this repo's per-commit graph window."""
    cache = app.state.commit_windows
    if repo_id not in cache:
        from .commit_window import CommitWindow

        cache[repo_id] = CommitWindow(_bundle(repo_id).entry.repo_dir)
    return cache[repo_id]


def _window_stats_for(repo_id: str):
    """Commit-window cost figures for *repo_id*, or None when it has no window."""
    from .schemas import WindowStats

    window = _commit_window(repo_id)
    if not window.available:
        return None
    stats = window.summary().get("stats")
    return WindowStats(**stats) if stats else None


@app.get("/api/health")
async def health() -> dict:
    registry = getattr(app.state, "registry", None)
    return {
        "status": "ok",
        "repos": len(registry.list_infos()) if registry else 0,
    }


@app.get("/api/repos", response_model=list[RepoInfo])
async def list_repos() -> list[RepoInfo]:
    infos = _registry().list_infos()
    # Surface the (global) edge-label toggle per repo so the UI can gate the
    # feature. repo_registry.py is skip-worktree, so inject it here instead.
    edge_on = bool(getattr(load_config(), "edge_labels", False))
    for info in infos:
        info.capabilities = {**info.capabilities, "edge_labels": edge_on}
        # Attach commit-window cost figures so the landing page can say what
        # incremental maintenance bought, rather than only naming a commit.
        # CommitWindow is lazy and cached per repo, and _bundle() is a registry
        # lookup with no graph loading, so this stays one cheap request.
        try:
            info.incremental = await asyncio.to_thread(_window_stats_for, info.id)
        except HTTPException:
            # Registry and bundle can disagree transiently; a missing bundle
            # just means no window figures for that card.
            info.incremental = None
    return infos


@app.get("/api/repos/{repo_id}/wiki")
async def wiki_tree(repo_id: str, cached_only: bool = False) -> dict:
    builder = _wiki(repo_id)
    if cached_only:
        cached_page_tree = getattr(builder, "cached_page_tree", None)
        pages = (
            await asyncio.to_thread(cached_page_tree)
            if callable(cached_page_tree)
            else None
        )
        pages = pages or []
    else:
        pages = await asyncio.to_thread(builder.page_tree)
    return {"repo": _bundle(repo_id).entry.repo, "pages": pages}


@app.get("/api/repos/{repo_id}/wiki/{page_id}")
async def wiki_page(
    repo_id: str,
    page_id: str,
    materialize_media: bool = True,
) -> dict:
    page = await asyncio.to_thread(_wiki(repo_id).page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Unknown wiki page: {page_id!r}")
    if "media_slots" not in page:
        page = {**page, "media_slots": []}
    if materialize_media and page.get("media_slots"):
        page = await asyncio.to_thread(_materialize_wiki_media, repo_id, page_id, page)
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
    return page


@app.get("/api/repos/{repo_id}/wiki-media/{page_id}/{filename:path}")
async def wiki_media_asset(repo_id: str, page_id: str, filename: str):
    _bundle(repo_id)  # 404 on unknown repo
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
        content=payload, media_type=_WIKI_MEDIA_TYPES[suffix], headers=headers
    )


@app.get("/api/repos/{repo_id}/wiki/{page_id}/graph")
async def wiki_page_graph(repo_id: str, page_id: str) -> dict:
    """Induced dependency subgraph over a wiki page's cited symbols.

    Lets a wiki page render as a *view over the graph* (subsystem symbols + how
    they connect), using the same ``{nodes, edges}`` payload as ``/codemap``.
    """
    builder = _wiki(repo_id)
    page_citations = getattr(builder, "page_citations", None)
    if callable(page_citations):
        citations = await asyncio.to_thread(page_citations, page_id)
        if citations is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown wiki page: {page_id!r}",
            )
    else:
        # The deterministic WikiBuilder does not make a model call, so its
        # existing page contract remains a safe compatibility fallback.
        page = await asyncio.to_thread(builder.page, page_id)
        if page is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown wiki page: {page_id!r}",
            )
        citations = page.get("citations", []) if isinstance(page, dict) else []
    bundle = _bundle(repo_id)
    graph = await asyncio.to_thread(bundle.code_graph)
    if graph is None:
        return {
            "available": False,
            "nodes": [],
            "edges": [],
            "mermaid": "",
            "note": bundle.graph_unavailable_note(),
        }
    from .codemap import build_page_subgraph

    hierarchy_graph = await asyncio.to_thread(bundle.hierarchical_graph)
    return await asyncio.to_thread(
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
    bundle = _bundle(repo_id)
    result = None
    if commit:
        window = _commit_window(repo_id)
        entry = window.resolve(commit) if window.available else None
        if entry is not None:
            result = await asyncio.to_thread(
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
                result = await asyncio.to_thread(
                    bound_source_slice, source_reader, file, start, end
                )
    else:
        source_reader = getattr(bundle, "source_reader", None)
        if source_reader is not None:
            result = await asyncio.to_thread(
                bound_source_slice, source_reader, file, start, end
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
    _bundle(repo_id)  # 404 on unknown repo
    window = _commit_window(repo_id)
    return await asyncio.to_thread(window.summary)


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
    bundle = _bundle(repo_id)
    # Prefer a commit-window snapshot when one exists: an explicit ``commit``
    # selects that point in history, and an absent one still defaults to the
    # window's newest commit so the graph matches the selector's default.
    window = _commit_window(repo_id)
    graph = None
    selected_commit = None
    loaded_from_window = False
    fell_back = False
    if window.available:
        entry = window.resolve(commit)
        if entry is None and commit:
            raise HTTPException(
                status_code=404, detail=f"Unknown commit for this repo: {commit!r}"
            )
        graph = await asyncio.to_thread(window.graph_for, commit)
        if entry is not None and graph is not None:
            selected_commit = entry.get("sha")
            loaded_from_window = True
    if graph is None:
        # The snapshot was absent or unloadable (e.g. a graph.pkl written under
        # an older schema_version). Serving the repo's default graph is fine;
        # reporting it as the requested commit is not -- the client would render
        # one commit's graph under another commit's label.
        graph = await asyncio.to_thread(bundle.code_graph)
        selected_commit = bundle.entry.base_commit
        fell_back = window.available
    if graph is None:
        return {
            "available": False,
            "nodes": [],
            "edges": [],
            "hierarchy": {"root": "hier::root", "nodes": [], "open_files": []},
            "mermaid": "",
            "note": bundle.graph_unavailable_note(),
            "setup": bundle.graph_setup().to_dict(),
        }
    from .codemap import build_codemap

    hierarchy_graph = (
        None
        if loaded_from_window
        else await asyncio.to_thread(bundle.hierarchical_graph)
    )
    result = await asyncio.to_thread(
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
    bundle = _bundle(repo_id)
    window = _commit_window(repo_id)
    graph = None
    selected_commit = None
    loaded_from_window = False
    fell_back = False
    if window.available:
        entry = window.resolve(commit)
        if entry is None and commit:
            raise HTTPException(
                status_code=404, detail=f"Unknown commit for this repo: {commit!r}"
            )
        graph = await asyncio.to_thread(window.graph_for, commit)
        if entry is not None and graph is not None:
            selected_commit = entry.get("sha")
            loaded_from_window = True
    if graph is None:
        graph = await asyncio.to_thread(bundle.code_graph)
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
            "hierarchy": {"root": "hier::root", "nodes": [], "open_files": []},
            "mermaid": "",
            "note": bundle.graph_unavailable_note(),
            "setup": bundle.graph_setup().to_dict(),
        }
    from .modulemap import build_modulemap

    result = await asyncio.to_thread(
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
    _bundle(repo_id)  # 404 on unknown repo
    try:
        labeler = _edge_labeler(repo_id, req.commit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    anchors = [a.model_dump() for a in req.anchors]
    label, cached = await asyncio.to_thread(
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

    bundle = _registry().get(req.repo_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Unknown repo: {req.repo_id!r}")
    await asyncio.to_thread(bundle.ensure_runtime)
    if bundle.runner is None:
        raise HTTPException(status_code=503, detail="repo runtime is unavailable")

    # Earlier messages give the agent context so it can resolve follow-ups
    # like "what calls it?".
    chat_history = [{"role": m.role, "content": m.content} for m in req.messages[:-1]]

    try:
        result = await asyncio.to_thread(
            bundle.runner.run, query, chat_history=chat_history
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean 500 to the client
        logger.error("agent run failed for %r: %s", req.repo_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="agent run failed") from exc

    source_reader = getattr(bundle, "source_reader", None)
    if source_reader is None:
        raise HTTPException(
            status_code=503,
            detail="authenticated repository source is unavailable",
        )
    return await asyncio.to_thread(
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
