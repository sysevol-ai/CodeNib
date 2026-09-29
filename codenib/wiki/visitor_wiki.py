# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Persisted visitor Wiki attempts, using AgentWiki and the WikiStore facade.

No index/job database is introduced. One bounded run owns temporary source and
an explicit visitor credential. The result is a collection of ordinary Wiki
pages, an outline and a small progress envelope in a separate Wiki database.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable

from ..agent.runtime.grep_jev import PLANNER_MODEL
from ..log_utils import get_logger
from ..storage import WikiStore
from .agent_wiki import AgentWiki
from .multimodal import plan_media_slots
from .store import WikiGenerationBusyError
from .visitor_provider import (
    WIKI_MODELS,
    WIKI_RETRIEVAL_IDENTITY,
    VisitorProvider,
    WikiRunStopped,
    verify_key,
)
from .visitor_source import repository_revision, visitor_source

_ATTEMPTS = "visitor-wiki-attempts-v1"
_PUBLIC = "visitor-wiki-publications-v1"
_ADMISSION = "visitor-wiki-admission-v1"
_GENERATION = "visitor-wiki-generation-v1"
_MAX_ATTEMPTS = 100
_MAX_RUN_SECONDS = 1800
logger = get_logger(__name__)


class VisitorWikiError(Exception):
    """A bounded, credential-free public error."""


def _entry(attempt: str) -> str:
    return f"visitor:{attempt}"


def _flatten(pages):
    for page in pages:
        yield page
        yield from _flatten(page.get("children", []))


class VisitorWikis:
    """Own visitor generation and persisted read-only results.

    Invariant: one process at a time performs funded visitor generation in this
    database. The existing WikiStore generation guard is held for the entire
    run. Under that guard, matching the submitted revision and request identity,
    then publishing its increment, is the run's linearization point. A stale
    queued run does nothing. Admission records a separate request identity before
    dispatch; stop cancels both the active and pending identities, so a resumed
    worker cannot miss a stop or inherit cancellation from an earlier request.
    Process death releases the guard; an explicit owner resume acquires it and
    reuses saved AgentWiki pages. No age-based lock stealing or automatic retry.

    Lock order: generation guard, short admission guard, then AgentWiki's
    ordinary per-entry guards. Creation and cancellation take only admission;
    it is released before dispatch. A busy waiter updates only its request
    envelope, never the active owner's progress. The in-process
    mutex only protects thread admission and is never held while acquiring a
    store guard. Cancellation is a separate run-specific Wiki envelope, so it
    cannot overwrite a concurrent page/progress publication.
    """

    def __init__(
        self,
        store: WikiStore,
        *,
        prepare=visitor_source,
        resolve=repository_revision,
        provider=VisitorProvider,
        verify=verify_key,
        wiki_factory=AgentWiki,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.prepare, self.resolve = prepare, resolve
        self.provider, self.verify, self.wiki_factory = provider, verify, wiki_factory
        self.clock = clock
        self._mutex = threading.Lock()
        self._active: set[str] = set()
        self._recent: dict[str, float] = {}
        self._closing = threading.Event()

    def _read(self, attempt):
        entry = self.store.read(_entry(attempt))
        if entry is None:
            raise VisitorWikiError("This saved Wiki was not found.")
        return copy.deepcopy(entry.envelope["data"])

    def _save(self, state):
        state["updated_at"] = self.clock()
        self.store.publish(
            entry_id=_entry(state["id"]),
            repository_id=_ATTEMPTS,
            envelope={"data": state},
        )

    def _pending(self, attempt):
        entry = self.store.read(f"{_entry(attempt)}:request")
        return copy.deepcopy(entry.envelope["data"]) if entry else None

    def _save_pending(self, attempt, pending):
        self.store.publish(
            entry_id=f"{_entry(attempt)}:request",
            repository_id=attempt,
            envelope={"data": pending},
        )

    def status(self, attempt):
        state = self._read(attempt)
        state.pop("owner_hash")
        for page in _flatten(state["pages"]):
            status = state["page_states"].get(page["id"])
            page["cache_state"] = (
                "ready"
                if status == "ready"
                else "degraded" if status == "needs_review" else "cold"
            )
        state["stalled"] = (
            state["status"] in {"queued", "running"}
            and self.clock() - state["updated_at"] > 180
        )
        pending = self._pending(attempt)
        if pending and pending["revision"] == state["revision"]:
            if pending["status"] == "busy":
                state["message"] = (
                    "The resume could not start while another generation is active. "
                    "Ready chapters are saved; try again shortly."
                )
                if state["status"] == "queued":
                    state.update(status="partial", stage="paused", stalled=False)
            elif state["status"] != "running" or state["stalled"]:
                state.update(
                    status="queued",
                    stage="queued",
                    active_page="",
                    updated_at=pending["at"],
                    stalled=self.clock() - pending["at"] > 180,
                    message="Your Wiki generation is waiting to start.",
                )
        state.pop("revision")
        state.pop("request_id", None)
        publication = self.store.read(f"{_entry(attempt)}:publication")
        state["published"] = bool(
            publication and publication.envelope["data"].get("published")
        )
        return state

    def page(self, attempt, page_id):
        self._read(attempt)
        entry = self.store.read(f"{_entry(attempt)}:page:{page_id}")
        if entry is None:
            raise VisitorWikiError("This page is not ready yet.")
        page = copy.deepcopy(entry.envelope["data"])
        # Render the saved, validated architecture directly. This needs neither
        # an image provider nor the temporary source checkout removed on exit.
        page["media_slots"] = plan_media_slots(
            page_id=page_id,
            title=page.get("title", page_id),
            citations=page.get("citations", []),
            architecture=page.get("architecture"),
        )
        return page

    def graphs(self, attempt, page_id):
        state = self._read(attempt)
        if page_id not in {page["id"] for page in _flatten(state["pages"])}:
            raise VisitorWikiError("This Wiki chapter was not found.")
        entry = self.store.read(f"{_entry(attempt)}:graphs")
        page = self.store.read(f"{_entry(attempt)}:graph:{page_id}")
        return {
            "commit": state["commit"],
            **(
                copy.deepcopy(entry.envelope["data"])
                if entry
                else {
                    "coverage": {
                        "available": False,
                        "reason": "not_indexed",
                        "note": "This Wiki was generated without a code index.",
                    },
                    "system_map": {"available": False, "areas": [], "links": []},
                }
            ),
            "code_graph": (
                copy.deepcopy(page.envelope["data"])
                if page
                else {"available": False, "nodes": [], "edges": [], "mermaid": ""}
            ),
        }

    def save_graph_views(self, state, bundle, check):
        """Save Wiki projections while the existing generation guard is held.

        Publish the summary last. Reads never index, use a model, or change
        visibility. Operator backfills hold the same guard and pin the same
        commit as generation; page prose is untouched.
        """
        from .visitor_graph import wiki_graph_views

        pages = [
            self.page(state["id"], p["id"])
            for p in _flatten(state["pages"])
            if state["page_states"].get(p["id"]) == "ready"
        ]
        try:
            summary, views = wiki_graph_views(bundle, state["pages"], pages, check)
        except Exception as exc:
            logger.warning(
                "Visitor Wiki graph projection failed (%s)", type(exc).__name__
            )
            summary, views = {
                "coverage": {
                    "available": False,
                    "reason": "projection_failed",
                    "note": (
                        "The code maps could not be saved. "
                        "Completed chapters remain available."
                    ),
                },
                "system_map": {"available": False, "areas": [], "links": []},
            }, {}
        summary.update(
            commit=state["commit"],
            source_fingerprint=getattr(
                getattr(bundle, "manifest", None), "source_fingerprint", None
            ),
        )
        for page_id, view in views.items():
            check()
            self.store.publish(
                entry_id=f"{_entry(state['id'])}:graph:{page_id}",
                repository_id=state["id"],
                envelope={"data": view},
            )
        self.store.publish(
            entry_id=f"{_entry(state['id'])}:graphs",
            repository_id=state["id"],
            envelope={"data": summary},
        )

    def missing_graph_views(self, reasons=("index_failed", "not_indexed")):
        """Saved Wikis with ready pages whose code maps are missing for *reasons*."""
        found = []
        for entry in self.store.scan(repository_ids=[_ATTEMPTS]):
            state = entry.envelope["data"]
            if not state.get("commit") or "ready" not in state["page_states"].values():
                continue
            graphs = self.store.read(f"{_entry(state['id'])}:graphs")
            reason = (
                graphs.envelope["data"]["coverage"].get("reason")
                if graphs
                else "not_indexed"
            )
            if reason in reasons:
                found.append(
                    {
                        "id": state["id"],
                        "repository": state["repository"],
                        "reason": reason,
                    }
                )
        return found

    def backfill_graph_views(self, attempt, check=lambda: None):
        """Rebuild one saved Wiki's code maps at its pinned commit.

        An operator repair for Wikis saved while indexing could not run. It
        holds the generation guard, downloads the saved commit again and
        replaces only the graph entries. Pages, prose and cost are untouched,
        and no model is called.
        """
        with self.store.generation_guard(_GENERATION):
            state = self._read(attempt)
            if not state.get("commit"):
                raise VisitorWikiError("This saved Wiki has no pinned commit.")
            with self.prepare(
                state["repository"], state["commit"], attempt, check, lambda *_: None
            ) as (bundle, _source):
                self.save_graph_views(state, bundle, check)
        saved = self.store.read(f"{_entry(attempt)}:graphs")
        return copy.deepcopy(saved.envelope["data"]["coverage"])

    def public_wikis(self):
        return sorted(
            (
                copy.deepcopy(entry.envelope["data"])
                for entry in self.store.scan(repository_ids=[_PUBLIC])
                if entry.envelope["data"].get("published") is True
            ),
            key=lambda item: item["published_at"],
            reverse=True,
        )

    def publish_wiki(self, attempt, owner, published):
        """Owner opt-in only; an atomic Wiki envelope is the visibility point.

        The admission guard serializes owner publication changes. Publication
        has its own envelope so progress writes cannot undo an unpublish. No
        model call or public catalog write occurs during ordinary generation.
        """
        with self.store.generation_guard(_ADMISSION):
            state = self._read(attempt)
            self._authorize(state, owner)
            card = {"id": attempt, "published": False}
            if published:
                if state["status"] != "complete" or not state["page_states"]:
                    raise VisitorWikiError(
                        "Finish generating this Wiki before publishing."
                    )
                for page_id, status in state["page_states"].items():
                    page = self.page(attempt, page_id)
                    if (
                        status != "ready"
                        or page.get("grounding", {}).get("valid") is not True
                        or page.get("quality", {}).get("valid") is False
                        or page.get("generation", {}).get("mode") == "degraded"
                    ):
                        raise VisitorWikiError(
                            "All chapters must pass source checks before publishing."
                        )
                overview = self.page(attempt, "overview")
                paragraphs = re.split(r"\n\s*\n", overview["markdown"])
                lead = next(
                    (
                        p
                        for p in paragraphs
                        if p.strip() and not p.lstrip().startswith(("#", ">", "```"))
                    ),
                    "",
                )
                lead = re.sub(r"\[E\d+\]\(#evidence-E\d+\)", "", lead)
                lead = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", lead)
                summary = re.sub(
                    r"\s+", " ", lead.replace("`", "").replace("**", "")
                ).strip()[:360]
                previous = self.store.read(f"{_entry(attempt)}:publication")
                prior = previous.envelope["data"] if previous else {}
                card = {
                    "id": attempt,
                    "published": True,
                    "repository": state["repository"],
                    "commit": state["commit"],
                    "summary": summary,
                    "chapters": len(state["page_states"]),
                    "languages": state.get("languages", []),
                    "published_at": prior.get("published_at", self.clock()),
                }
            self.store.publish(
                entry_id=f"{_entry(attempt)}:publication",
                repository_id=_PUBLIC,
                envelope={"data": card},
            )
        return {"published": published}

    def _authorize(self, state, owner):
        digest = hashlib.sha256(owner.encode()).hexdigest()
        if not hmac.compare_digest(state["owner_hash"], digest):
            raise VisitorWikiError(
                "Resume or stop this Wiki from the browser that created it."
            )

    def cancel(self, attempt, owner):
        with self.store.generation_guard(_ADMISSION):
            state = self._read(attempt)
            self._authorize(state, owner)
            if state["status"] == "complete":
                return
            targets = set()
            if state.get("request_id"):
                targets.add(state["request_id"])
            elif state["run_id"]:
                targets.add(str(state["run_id"]))
            pending = self._pending(attempt)
            if pending and pending["revision"] == state["revision"]:
                targets.add(pending["id"])
            for request_id in targets:
                self.store.publish(
                    entry_id=f"{_entry(attempt)}:cancel:{request_id}",
                    repository_id=attempt,
                    envelope={"data": {"cancelled": True}},
                )

    def close(self):
        self._closing.set()

    def submit(
        self, attempt, repository, owner, key, budget, client, model=PLANNER_MODEL
    ):
        if model not in WIKI_MODELS:
            raise VisitorWikiError("Unsupported Wiki model.")
        now = self.clock()
        with self._mutex:
            if (
                self._closing.is_set()
                or attempt in self._active
                or len(self._active) >= 2
            ):
                raise VisitorWikiError(
                    "Generation is busy. Try again shortly; no new model call was started."
                )
            self._recent = {ip: at for ip, at in self._recent.items() if now - at < 60}
            if client in self._recent or len(self._recent) >= 1024:
                raise VisitorWikiError(
                    "Please wait one minute before starting another run."
                )
            self._recent[client] = now
            self._active.add(attempt)
        try:
            # Invalid/private repositories must not consume permanent capacity.
            # Network validation stays outside the short admission guard.
            self.verify(key)

            def check_admission():
                if self._closing.is_set():
                    raise WikiRunStopped("The server is stopping; try again shortly.")

            check_admission()
            commit = (
                self.resolve(repository, check_admission)
                if self.store.read(_entry(attempt)) is None
                else ""
            )
            check_admission()
            with self.store.generation_guard(_ADMISSION):
                existing = self.store.read(_entry(attempt))
                if existing is not None:
                    state = copy.deepcopy(existing.envelope["data"])
                    self._authorize(state, owner)
                    if state["repository"] != repository:
                        raise VisitorWikiError("A saved Wiki cannot change repository.")
                    if state["status"] == "complete":
                        with self._mutex:
                            self._active.discard(attempt)
                        return self.status(attempt)
                else:
                    if (
                        len(self.store.scan(repository_ids=[_ATTEMPTS]))
                        >= _MAX_ATTEMPTS
                    ):
                        raise VisitorWikiError(
                            "This demo has reached its saved Wiki capacity."
                        )
                    state = {
                        "id": attempt,
                        "repository": repository,
                        "commit": commit,
                        "owner_hash": hashlib.sha256(owner.encode()).hexdigest(),
                        "revision": 0,
                        "run_id": 0,
                        "request_id": "",
                        "status": "queued",
                        "stage": "queued",
                        "active_page": "",
                        "created_at": now,
                        "updated_at": now,
                        "pages": [],
                        "page_states": {},
                        "skipped_files": [],
                        "message": "",
                        "reported_cost_usd": 0.0,
                        "calls": 0,
                        "unreported_call_cost": False,
                        "budget_usd": budget,
                        "history": [],
                        "model": model,
                        "scope": "focused",
                        "retrieval_identity": WIKI_RETRIEVAL_IDENTITY,
                    }
                    self._save(state)
                request_id = secrets.token_hex(16)
                self._save_pending(
                    attempt,
                    {
                        "id": request_id,
                        "revision": state["revision"],
                        "status": "queued",
                        "at": self.clock(),
                    },
                )
            self._dispatch({**state, "request_id": request_id}, key, budget)
            return self.status(attempt)
        except WikiRunStopped as exc:
            with self._mutex:
                self._active.discard(attempt)
            raise VisitorWikiError(str(exc)) from None
        except BaseException:
            with self._mutex:
                self._active.discard(attempt)
            raise

    def _dispatch(self, state, key, budget):
        threading.Thread(
            target=self._run,
            args=(state, key, budget),
            name="codenib-visitor-wiki",
            daemon=True,
        ).start()

    def _run(self, submitted, key, budget):
        attempt = submitted["id"]
        state = None
        provider = None
        deadline = time.monotonic() + _MAX_RUN_SECONDS
        try:
            with self.store.generation_guard(_GENERATION):
                with self.store.generation_guard(_ADMISSION):
                    current = self._read(attempt)
                    if current["revision"] != submitted["revision"]:
                        return
                    pending = self._pending(attempt)
                    if not pending or pending["id"] != submitted["request_id"]:
                        return
                    state = current
                    state["revision"] += 1
                    state["run_id"] += 1
                    state.update(
                        request_id=submitted["request_id"],
                        status="running",
                        stage="connecting",
                        message="",
                        active_page="",
                        budget_usd=budget,
                    )
                    self._save(state)
                previous_cost, previous_calls = (
                    state["reported_cost_usd"],
                    state["calls"],
                )
                previous_unknown = state["unreported_call_cost"]

                def progress(stage, page):
                    state.update(stage=stage, active_page=page)
                    event = {"stage": stage, "page": page, "at": self.clock()}
                    state["history"] = [*state["history"], event][-200:]
                    self._save(state)

                def check():
                    if self._closing.is_set() or self.store.read(
                        f"{_entry(attempt)}:cancel:{state['request_id']}"
                    ):
                        raise WikiRunStopped(
                            "Generation stopped. Completed pages are saved."
                        )
                    if time.monotonic() > deadline:
                        raise WikiRunStopped(
                            "This run reached its time limit. "
                            "Resume to finish the remaining pages."
                        )

                def usage(data):
                    state.update(data)
                    state["reported_cost_usd"] += previous_cost
                    state["calls"] += previous_calls
                    state["unreported_call_cost"] |= previous_unknown
                    self._save(state)

                progress("connecting", "")
                model = state.get("model", PLANNER_MODEL)
                provider = self.provider(key, budget, check, usage, model=model)
                key = ""
                try:
                    check()
                    if not state["commit"]:
                        state["commit"] = self.resolve(state["repository"], check)
                        self._save(state)
                    with self.prepare(
                        state["repository"], state["commit"], attempt, check, progress
                    ) as (bundle, source):
                        state["skipped_files"] = getattr(bundle, "skipped_files", [])
                        state["source_files"] = getattr(
                            getattr(bundle, "manifest", None), "file_count", None
                        )
                        state["languages"] = getattr(
                            getattr(bundle, "manifest", None), "languages", []
                        )
                        self._save(state)
                        retrieval_identity = state.get(
                            "retrieval_identity", "grep_jev_v1"
                        )
                        wiki_context = retrieval_identity == WIKI_RETRIEVAL_IDENTITY
                        wiki = self.wiki_factory(
                            bundle,
                            f"openrouter/{model}",
                            store=self.store,
                            llm=provider,
                            source_retriever=lambda query, limit: provider.retrieve(
                                source, query, limit, wiki_context=wiki_context
                            ),
                            retrieval_identity=retrieval_identity,
                            story_review=wiki_context,
                            progress=progress,
                            concise=state.get("scope") == "concise",
                            focused=state.get("scope") == "focused",
                        )
                        progress("outline", "")
                        outline = wiki.outline()
                        if not outline.get("pages") or outline.get("error"):
                            raise WikiRunStopped(
                                "The Wiki outline could not be completed. Resume to try again."
                            )
                        state["pages"] = wiki.page_tree()
                        pages = list(_flatten(state["pages"]))
                        if len(pages) > 120:
                            raise WikiRunStopped(
                                "The Wiki outline exceeds the page limit."
                            )
                        self._save(state)
                        for meta in pages:
                            check()
                            page_id = meta["id"]
                            # The cached valid page is reused without generation.
                            page = wiki.cached_page(page_id)
                            if page is None:
                                state["page_states"][page_id] = "running"
                                progress("retrieving", page_id)
                                page = wiki.page(page_id, retry_degraded_now=True)
                            check()
                            progress("checking", page_id)
                            valid = bool(
                                page
                                and (page.get("generation") or {}).get("mode")
                                != "degraded"
                                and (page.get("grounding") or {}).get("valid") is True
                                and (page.get("quality") or {}).get("valid")
                                is not False
                            )
                            if valid:
                                saved = copy.deepcopy(page)
                                for citation in saved.get("citations", []):
                                    snippet = wiki.source(
                                        citation["file"],
                                        citation.get("start_line"),
                                        citation.get("end_line"),
                                    )
                                    if snippet:
                                        citation["content"] = snippet["content"][:24000]
                                self.store.publish(
                                    entry_id=f"{_entry(attempt)}:page:{page_id}",
                                    repository_id=attempt,
                                    envelope={"data": saved},
                                )
                            state["page_states"][page_id] = (
                                "ready" if valid else "needs_review"
                            )
                            self._save(state)
                        progress("saving_graphs", "")
                        self.save_graph_views(state, bundle, check)
                        complete = all(
                            state["page_states"].get(page["id"]) == "ready"
                            for page in pages
                        )
                        state.update(
                            status="complete" if complete else "partial",
                            stage="complete",
                            active_page="",
                            message=(
                                ""
                                if complete
                                else (
                                    "Some pages did not pass source checks. "
                                    "Ready pages are saved; resume to retry the others."
                                )
                            ),
                        )
                except WikiRunStopped as exc:
                    state.update(status="partial", message=str(exc))
                except Exception as exc:
                    # Never persist exception/provider bodies, prompts or headers.
                    logger.warning(
                        "Visitor Wiki stopped in %s (%s)",
                        state["stage"],
                        type(exc).__name__,
                    )
                    state.update(
                        status="partial",
                        message=(
                            "Generation could not finish. "
                            "Completed pages are saved; resume to retry."
                        ),
                    )
                finally:
                    if provider is not None:
                        provider.close()
                    for page_id, status in state["page_states"].items():
                        if status == "running":
                            state["page_states"][page_id] = "pending"
                    # Invalidate submissions made while this owner was active,
                    # including a request queued after its initial publication.
                    state["revision"] += 1
                    self._save(state)
        except WikiGenerationBusyError:
            # A contender can observe an active owner's revision. It must not
            # rewrite that owner's state just because its own wait timed out.
            with self.store.generation_guard(_ADMISSION):
                current = self._read(attempt)
                pending = self._pending(attempt)
                if (
                    current["revision"] == submitted["revision"]
                    and pending
                    and pending["id"] == submitted["request_id"]
                ):
                    pending.update(status="busy", at=self.clock())
                    self._save_pending(attempt, pending)
        finally:
            key = ""
            if provider is not None:
                provider.close()
            with self._mutex:
                self._active.discard(attempt)
