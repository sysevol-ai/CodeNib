# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bring the stored log of a thread, and of its linked threads, up to date."""

from __future__ import annotations

from typing import Callable

from .github_source import GitHubClient, fetch_thread
from .log_store import ThreadStore
from .serve import linked_numbers


def sync_thread(
    client: GitHubClient,
    store: ThreadStore,
    number: int,
    *,
    linked: bool = True,
    refresh_linked: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> dict[int, int]:
    """Sync one thread and the threads it links to. Returns new events per thread.

    Linked threads are fetched one level deep. A linked thread already in the
    store is left alone unless ``refresh_linked`` is set, so following a busy
    thread does not refetch its whole neighbourhood on every sync.
    """

    written: dict[int, int] = {}
    fetched = fetch_thread(client, store.repo, number, on_progress=on_progress)
    if fetched is None:
        return written
    header, events = fetched
    written[number] = store.append(number, events)
    store.write_header(header)
    if not linked:
        return written
    for ref in linked_numbers(header, events):
        if store.has(ref) and not refresh_linked:
            continue
        if on_progress:
            on_progress(f"linked thread #{ref}")
        other = fetch_thread(client, store.repo, ref, on_progress=on_progress)
        if other is None:
            continue
        written[ref] = store.append(ref, other[1])
        store.write_header(other[0])
    return written


__all__ = ["sync_thread"]
