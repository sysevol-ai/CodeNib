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
import os
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..log_utils import get_logger
from ..wiki import WikiBuilder
from ..wiki.narrator import Narrator
from .config import load_config
from .ports import argparse_tcp_port
from .repo_registry import RepoRegistry
from .repository_files import live_source_slice
from .request_limits import RequestBodyLimitMiddleware
from .schemas import (
    ChatRequest,
    ChatResponse,
    CustomizeRequest,
    CustomizeResponse,
    EdgeLabelRequest,
    EdgeLabelResponse,
    RepoInfo,
    agent_result_to_response,
)

logger = get_logger(__name__)


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
    registry = RepoRegistry(config)
    logger.info("Loading QA repos from %s ...", config.registry_path)
    registry.load_all()
    app.state.registry = registry
    app.state.wiki_builders = {}
    app.state.edge_labelers = {}
    app.state.commit_windows = {}
    # Ephemeral, in-RAM reader customizations (human prior injection). Not
    # persisted: the durable wiki is authoritative and customizations vanish on
    # restart. A single Customizer reuses the wiki model/creds.
    from ..wiki.customizer import Customizer
    from .customization_store import CustomizationStore

    app.state.customizations = CustomizationStore()
    app.state.customizer = Customizer(_wiki_llm(config))
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


app = FastAPI(title="CodeNib Code QA", lifespan=lifespan)

app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=load_config().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
            source_fn = partial(window.source_for, source_commit)
        elif commit.strip().lower() not in {
            base_commit.lower(),
            base_commit[:8].lower(),
        }:
            raise ValueError(f"unknown commit for repository: {commit}")
    if source_fn is None:
        source_fn = partial(live_source_slice, bundle.entry.repo_dir)

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
async def wiki_tree(repo_id: str) -> dict:
    builder = _wiki(repo_id)
    pages = await asyncio.to_thread(builder.page_tree)
    return {"repo": _bundle(repo_id).entry.repo, "pages": pages}


def _apply_customization(page: dict, page_id: str) -> dict:
    """Rewrite ``page['markdown']`` to any active reader prior for *page_id*.

    Runs in a worker thread (it may call the model). Fail-soft: the customizer
    itself returns the original markdown on error, and a resolved-but-empty prior
    leaves the page untouched. Sets ``customized`` so the client can label it.
    """
    store = getattr(app.state, "customizations", None)
    customizer = getattr(app.state, "customizer", None)
    page = {**page, "customized": False}
    if store is None or customizer is None:
        return page
    prior = store.resolve(page_id)
    if prior is None:
        return page
    markdown = page.get("markdown") or ""
    if not markdown:
        return page
    new_markdown = store.transformed(
        page_id,
        markdown,
        prior,
        produce=lambda: customizer.apply(
            markdown,
            instruction=prior.instruction,
            structure=list(prior.structure),
        ),
    )
    if new_markdown and new_markdown != markdown:
        page = {**page, "markdown": new_markdown, "customized": True}
    return page


@app.get("/api/repos/{repo_id}/wiki/{page_id}")
async def wiki_page(repo_id: str, page_id: str) -> dict:
    page = await asyncio.to_thread(_wiki(repo_id).page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Unknown wiki page: {page_id!r}")
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
    return await asyncio.to_thread(_apply_customization, page, page_id)


@app.post("/api/repos/{repo_id}/customize", response_model=CustomizeResponse)
async def customize_wiki(repo_id: str, req: CustomizeRequest) -> CustomizeResponse:
    """Set (or clear) a reader prior for a page or the whole wiki.

    An empty instruction *and* empty structure clears any existing prior for the
    scope/target. A page-scoped request returns the transformed markdown; a
    wiki-scoped request stores the prior and lets pages transform lazily on view.
    """
    from ..wiki.customizer import clean_instruction, clean_structure
    from .customization_store import SCOPE_PAGE, SCOPE_WIKI, Prior

    _bundle(repo_id)  # 404 on unknown repo
    scope = req.scope if req.scope in (SCOPE_WIKI, SCOPE_PAGE) else SCOPE_PAGE
    store = app.state.customizations

    prior = Prior(
        instruction=clean_instruction(req.instruction),
        structure=tuple(clean_structure(req.structure)),
    )
    target = req.target if scope == SCOPE_PAGE else ""
    await asyncio.to_thread(store.set_prior, scope, target, prior)

    if prior.is_empty():
        return CustomizeResponse(ok=True, customized=False, scope=scope, target=target)

    # Page scope: transform now so the client gets the result in one round-trip.
    if scope == SCOPE_PAGE and target:
        page = await asyncio.to_thread(_wiki(repo_id).page, target)
        if page is not None:
            applied = await asyncio.to_thread(_apply_customization, page, target)
            return CustomizeResponse(
                ok=True,
                markdown=applied.get("markdown", ""),
                customized=bool(applied.get("customized")),
                scope=scope,
                target=target,
            )
    return CustomizeResponse(ok=True, customized=True, scope=scope, target=target)


@app.delete("/api/repos/{repo_id}/customize", response_model=CustomizeResponse)
async def reset_customization(
    repo_id: str, scope: str = "page", target: str = ""
) -> CustomizeResponse:
    """Drop a prior so the default page returns. Idempotent."""
    from .customization_store import SCOPE_PAGE, SCOPE_WIKI

    _bundle(repo_id)
    scope = scope if scope in (SCOPE_WIKI, SCOPE_PAGE) else SCOPE_PAGE
    target = target if scope == SCOPE_PAGE else ""
    await asyncio.to_thread(app.state.customizations.drop_prior, scope, target)
    return CustomizeResponse(ok=True, customized=False, scope=scope, target=target)


@app.get("/api/repos/{repo_id}/wiki/{page_id}/graph")
async def wiki_page_graph(repo_id: str, page_id: str) -> dict:
    """Induced dependency subgraph over a wiki page's cited symbols.

    Lets a wiki page render as a *view over the graph* (subsystem symbols + how
    they connect), using the same ``{nodes, edges}`` payload as ``/codemap``.
    """
    page = await asyncio.to_thread(_wiki(repo_id).page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Unknown wiki page: {page_id!r}")
    bundle = _bundle(repo_id)
    graph = await asyncio.to_thread(bundle.code_graph)
    if graph is None:
        return {"available": False, "nodes": [], "edges": [], "mermaid": ""}
    from .codemap import build_page_subgraph

    hierarchy_graph = await asyncio.to_thread(bundle.hierarchical_graph)
    citations = page.get("citations", []) if isinstance(page, dict) else []
    return await asyncio.to_thread(
        build_page_subgraph,
        graph,
        citations,
        repo_dir=bundle.entry.repo_dir,
        hierarchy_graph=hierarchy_graph,
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
                window.source_for, commit, file, start, end
            )
        else:
            base = str(bundle.entry.base_commit or "")
            requested = commit.strip().lower()
            if requested not in {base.lower(), base[:8].lower()}:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown commit for this repo: {commit!r}",
                )
            result = await asyncio.to_thread(
                live_source_slice, bundle.entry.repo_dir, file, start, end
            )
    else:
        result = await asyncio.to_thread(
            live_source_slice, bundle.entry.repo_dir, file, start, end
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

    return agent_result_to_response(
        result, repo_path=getattr(bundle.manifest, "repo_path", "")
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
