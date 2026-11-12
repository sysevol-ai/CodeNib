# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Bring every thread that tickets follow up to date with few requests.

One listing asks GitHub which issues and pull requests changed since the last
poll. Only those that some ticket follows are fetched again, so the cost of a
poll follows how much changed, not how many tickets are open.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .github_source import GitHubClient, fetch_thread
from .serve import ThreadView, linked_numbers
from .tickets import TicketBook

STATE_NAME = "poll.json"


@dataclass
class PollResult:
    changed_on_github: int = 0
    refetched: list[int] = field(default_factory=list)
    new_events: dict[int, int] = field(default_factory=dict)
    tickets_with_news: list[int] = field(default_factory=list)
    requests: int = 0


def _checks_moved(client: GitHubClient, repo: str, number: int, events) -> bool:
    """Whether an open pull request has a new head or new check results.

    Two requests, against the dozen or more a full fetch of a pull request
    takes, so an idle pull request costs little to keep watching.
    """

    pull = client.get(f"repos/{repo}/pulls/{number}")
    if not pull:
        return False
    head = (pull.get("head") or {}).get("sha") or ""
    known_commits = {e.meta.get("sha") for e in events if e.kind == "commit"}
    if head and head not in known_commits:
        return True
    known = {
        (e.address.rsplit("/", 1)[-1], e.meta.get("conclusion"))
        for e in events
        if e.kind == "check" and e.meta.get("sha") == head
    }
    runs = client.pages(f"repos/{repo}/commits/{head}/check-runs", key="check_runs")
    current = {
        (str(run.get("id")), run.get("conclusion") or run.get("status") or "?")
        for run in runs
    }
    return current != known


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll(
    client: GitHubClient,
    book: TicketBook,
    *,
    now: Callable[[], str] = _now,
    on_progress: Callable[[str], None] | None = None,
) -> PollResult:
    """Refetch followed threads that changed since the previous poll.

    GitHub does not mark a pull request as updated when a check finishes, so a
    followed pull request that is still open is probed for a new head or new
    check results on every poll, and fetched only when either moved.
    """

    store = book.store
    state_path = store.dir / STATE_NAME
    since = ""
    if state_path.exists():
        since = json.loads(state_path.read_text(encoding="utf-8")).get("since", "")
    started = now()
    before = client.requests
    followers = book.subscribed_threads()
    result = PollResult()

    wanted: set[int] = set()
    if since:
        listed = client.pages(
            f"repos/{store.repo}/issues?state=all&sort=updated&direction=asc"
            f"&since={since}"
        )
        result.changed_on_github = len(listed)
        wanted = {item["number"] for item in listed} & set(followers)
    else:
        wanted = set(followers)  # first poll: everything followed
    for number in followers:
        header = store.header(number)
        if header is None:
            wanted.add(number)
        elif number not in wanted and header.is_pr:
            events = store.events(number)
            if ThreadView(header, events).state() == "open" and _checks_moved(
                client, store.repo, number, events
            ):
                wanted.add(number)

    for number in sorted(wanted):
        if on_progress:
            on_progress(f"#{number}")
        fetched = fetch_thread(client, store.repo, number)
        if fetched is None:
            continue
        header, events = fetched
        written = store.append(number, events)
        store.write_header(header)
        result.refetched.append(number)
        if written:
            result.new_events[number] = written
        # A thread a ticket's root newly links to joins on the next inbox read;
        # fetch it now so that read has it.
        for ticket_number in followers.get(number, []):
            if ticket_number != number:
                continue
            for ref in linked_numbers(header, events):
                if not store.has(ref):
                    other = fetch_thread(client, store.repo, ref)
                    if other is not None:
                        store.append(ref, other[1])
                        store.write_header(other[0])
                        result.new_events[ref] = len(other[1])

    news = {t for thread in result.new_events for t in followers.get(thread, [])}
    for ticket in book.all():
        if ticket.number in result.new_events:
            news.add(ticket.number)
    result.tickets_with_news = sorted(news)
    result.requests = client.requests - before

    store.dir.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"since": started}), encoding="utf-8")
    tmp.replace(state_path)
    return result


__all__ = ["PollResult", "poll"]
