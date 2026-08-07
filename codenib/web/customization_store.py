# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Ephemeral, in-memory store for reader customizations.

A *prior* is how a reader asked a page to be written (a style instruction and/or
a section structure). Priors are deliberately NOT persisted: the durable default
wiki is authoritative, and a customization should last only while a reader is
using it and vanish on restart. This store therefore lives in process memory,
with lazy TTL + LRU cleanup and no background thread — which keeps the
single-process demo deploy free of any scheduler.

Two scopes, cascading most-specific-wins:

    wiki prior   (base house style, applies to every page)
        page prior   (overrides / extends, for one page)

``resolve(page_id)`` merges them. The store also memoizes the transformed
markdown per (page, prior) so repeated views don't re-run the model.

The public surface is small on purpose: swapping this for a shared backend
(needed only if the server ever runs multiple workers) is a single-file change.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# Cleanup bounds. Idle priors are dropped after TTL; the total is capped so a
# burst of distinct customizations cannot grow memory without bound.
DEFAULT_TTL_SECONDS = 30 * 60
DEFAULT_MAX_ENTRIES = 64

SCOPE_WIKI = "wiki"
SCOPE_PAGE = "page"


@dataclass(frozen=True, slots=True)
class Prior:
    """A reader's stated preference for how a page should read."""

    instruction: str = ""
    structure: Tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.instruction or self.structure)

    def fingerprint(self) -> str:
        """Stable key material for memoizing a transform of this prior."""
        payload = self.instruction + "\0" + "\x1f".join(self.structure)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class _Entry:
    prior: Prior
    last_used: float
    # Memoized transformed markdown, keyed by (source markdown hash + prior fp).
    outputs: Dict[str, str] = field(default_factory=dict)


def _key(scope: str, target: str) -> str:
    # target is the page id for page scope, and "" for wiki scope.
    return f"{scope}:{target}" if scope == SCOPE_PAGE else SCOPE_WIKI


class CustomizationStore:
    """Global, in-RAM prior store with lazy TTL + LRU cleanup."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: Dict[str, _Entry] = {}
        self._lock = threading.RLock()

    # -- mutation ---------------------------------------------------------

    def set_prior(self, scope: str, target: str, prior: Prior) -> None:
        """Store (or clear, when empty) a prior for a scope/target."""
        if scope not in (SCOPE_WIKI, SCOPE_PAGE):
            raise ValueError(f"unknown scope: {scope!r}")
        key = _key(scope, target)
        with self._lock:
            self._sweep(time.time())
            if prior.is_empty():
                self._entries.pop(key, None)
                return
            self._entries[key] = _Entry(prior=prior, last_used=time.time())
            self._evict_over_cap()

    def drop_prior(self, scope: str, target: str) -> bool:
        """Remove a prior. Returns True if one was present."""
        key = _key(scope, target)
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # -- read -------------------------------------------------------------

    def resolve(self, page_id: str) -> Optional[Prior]:
        """Merge the wiki-level and page-level priors for *page_id*.

        Most-specific-wins: the page instruction is appended after the wiki
        instruction, and a page structure replaces the wiki structure. Touching
        either entry marks it used (so an actively-viewed customization does not
        expire out from under the reader).
        """
        now = time.time()
        with self._lock:
            self._sweep(now)
            wiki = self._entries.get(_key(SCOPE_WIKI, ""))
            page = self._entries.get(_key(SCOPE_PAGE, page_id))
            if wiki is None and page is None:
                return None

            instructions: List[str] = []
            structure: Tuple[str, ...] = ()
            for entry in (wiki, page):
                if entry is None:
                    continue
                entry.last_used = now
                if entry.prior.instruction:
                    instructions.append(entry.prior.instruction)
                if entry.prior.structure:
                    structure = entry.prior.structure  # page overrides wiki
            merged = Prior(
                instruction="\n\n".join(instructions),
                structure=structure,
            )
            return None if merged.is_empty() else merged

    def transformed(
        self,
        page_id: str,
        markdown: str,
        prior: Prior,
        produce: Callable[[], str],
    ) -> str:
        """Return the transformed markdown, memoized by (source, prior).

        *produce* runs the (expensive) transform on a cache miss. It is called
        outside the lock so a slow model call does not block other requests.
        """
        src_fp = hashlib.sha1(markdown.encode("utf-8")).hexdigest()[:16]
        cache_key = f"{src_fp}:{prior.fingerprint()}"
        page_key = _key(SCOPE_PAGE, page_id)
        wiki_key = _key(SCOPE_WIKI, "")

        with self._lock:
            for key in (page_key, wiki_key):
                entry = self._entries.get(key)
                if entry and cache_key in entry.outputs:
                    entry.last_used = time.time()
                    return entry.outputs[cache_key]

        result = produce()

        with self._lock:
            # Attach the memoized output to whichever scope entry exists, so it is
            # dropped together with its prior. Prefer the page entry.
            for key in (page_key, wiki_key):
                entry = self._entries.get(key)
                if entry is not None:
                    entry.outputs[cache_key] = result
                    entry.last_used = time.time()
                    break
        return result

    # -- housekeeping (callers already hold the lock) --------------------

    def _sweep(self, now: float) -> None:
        expired = [k for k, e in self._entries.items() if now - e.last_used > self._ttl]
        for k in expired:
            self._entries.pop(k, None)

    def _evict_over_cap(self) -> None:
        while len(self._entries) > self._max:
            oldest = min(self._entries, key=lambda k: self._entries[k].last_used)
            self._entries.pop(oldest, None)

    # -- introspection (tests / debug) -----------------------------------

    def size(self) -> int:
        with self._lock:
            return len(self._entries)
