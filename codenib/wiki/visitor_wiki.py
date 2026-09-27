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
import threading
import time
from collections.abc import Callable

from ..log_utils import get_logger
from ..storage import WikiStore
from .agent_wiki import AgentWiki
from .store import WikiGenerationBusyError
from .visitor_provider import VisitorProvider, WikiRunStopped, verify_key
from .visitor_source import repository_revision, visitor_source

_ATTEMPTS = "visitor-wiki-attempts-v1"
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
    run. Under that guard, matching the submitted revision and publishing its
    increment is the run's linearization point. A stale queued run does nothing.
    Process death releases the guard; an explicit owner resume acquires it and
    reuses saved AgentWiki pages. No age-based lock stealing or automatic retry.

    Lock order: generation guard, short admission guard, then AgentWiki's
    ordinary per-entry guards. Creation takes only admission and releases it
    before dispatch. The in-process
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

    def status(self, attempt):
        state = self._read(attempt)
        state.pop("owner_hash")
        state.pop("revision")
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
        return state

    def page(self, attempt, page_id):
        self._read(attempt)
        entry = self.store.read(f"{_entry(attempt)}:page:{page_id}")
        if entry is None:
            raise VisitorWikiError("This page is not ready yet.")
        return entry.envelope["data"]

    def _authorize(self, state, owner):
        digest = hashlib.sha256(owner.encode()).hexdigest()
        if not hmac.compare_digest(state["owner_hash"], digest):
            raise VisitorWikiError(
                "Resume or stop this Wiki from the browser that created it."
            )

    def cancel(self, attempt, owner):
        state = self._read(attempt)
        self._authorize(state, owner)
        run_id = state["run_id"] + (state["status"] == "queued")
        self.store.publish(
            entry_id=f"{_entry(attempt)}:cancel:{run_id}",
            repository_id=attempt,
            envelope={"data": {"cancelled": True}},
        )

    def close(self):
        self._closing.set()

    def submit(self, attempt, repository, owner, key, budget, client):
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
            # Authenticate before allocating durable attempts or downloading source.
            self.verify(key)
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
                        "commit": "",
                        "owner_hash": hashlib.sha256(owner.encode()).hexdigest(),
                        "revision": 0,
                        "run_id": 0,
                        "status": "queued",
                        "stage": "queued",
                        "active_page": "",
                        "created_at": now,
                        "updated_at": now,
                        "pages": [],
                        "page_states": {},
                        "message": "",
                        "reported_cost_usd": 0.0,
                        "calls": 0,
                        "unreported_call_cost": False,
                        "budget_usd": budget,
                        "history": [],
                    }
                    self._save(state)
            self._dispatch(state, key, budget)
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
                    state = current
                    state["revision"] += 1
                    state["run_id"] += 1
                    state.update(
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
                        f"{_entry(attempt)}:cancel:{state['run_id']}"
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
                provider = self.provider(key, budget, check, usage)
                key = ""
                try:
                    check()
                    if not state["commit"]:
                        state["commit"] = self.resolve(state["repository"], check)
                        self._save(state)
                    with self.prepare(
                        state["repository"], state["commit"], attempt, check, progress
                    ) as (bundle, source):
                        wiki = self.wiki_factory(
                            bundle,
                            "openrouter/anthropic/claude-sonnet-4.6",
                            store=self.store,
                            llm=provider,
                            source_retriever=lambda query, limit: provider.retrieve(
                                source, query, limit
                            ),
                            retrieval_identity="grep_jev_v1",
                            progress=progress,
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
            # Mark a rejected queued run without racing the next owner. Both
            # the owner's first revision increment and this transition take
            # admission; a changed revision always wins over this waiter.
            with self.store.generation_guard(_ADMISSION):
                current = self._read(attempt)
                if current["revision"] == submitted["revision"]:
                    current["revision"] += 1
                    current.update(
                        status="partial",
                        stage="paused",
                        message=(
                            "Another Wiki is generating. "
                            "Your attempt is saved; resume shortly."
                        ),
                    )
                    self._save(current)
        finally:
            key = ""
            if provider is not None:
                provider.close()
            with self._mutex:
                self._active.discard(attempt)
