# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tickets: units of work that follow threads and remember what they have read.

A ticket starts from one issue or pull request. It subscribes to that thread
and to the threads linked to it, and it may follow others. Each ticket keeps a
cursor, so asking for its inbox returns only what its threads gained since the
last time, whichever of them it came from. One thread can feed many tickets;
nothing is copied, each ticket only keeps its own cursor.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .anchor import CodeAnchor, is_source_path
from .in_flight import match_in_flight
from .log_store import ThreadStore
from .serve import (
    DEFAULT_BUDGET,
    DEFAULT_CODE_BUDGET,
    Bundle,
    Packet,
    ThreadView,
    build_packet,
    load_bundle,
)


@dataclass
class Ticket:
    number: int
    threads: list[int] = field(default_factory=list)
    cursor: str = ""
    directory: str = ""
    opened_at: str = ""
    # How far the ticket has read each followed thread's log. A position, not
    # a time, so that an edit of an old comment still counts as unread.
    positions: dict[str, int] = field(default_factory=dict)
    # [path, symbol, first line, last line] the ticket's threads point at.
    anchors: list[list] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Ticket":
        return cls(**data)


class TicketBook:
    """Tickets of one repository, one small JSON file each."""

    def __init__(self, store: ThreadStore) -> None:
        self.store = store
        self.dir = store.dir / "tickets"

    def _path(self, number: int) -> Path:
        return self.dir / f"{int(number)}.json"

    def get(self, number: int) -> Ticket | None:
        path = self._path(number)
        if not path.exists():
            return None
        return Ticket.from_json(json.loads(path.read_text(encoding="utf-8")))

    def put(self, ticket: Ticket) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(ticket.number)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(ticket.to_json(), sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def remove(self, number: int) -> bool:
        path = self._path(number)
        if not path.exists():
            return False
        path.unlink()
        return True

    def all(self) -> list[Ticket]:
        if not self.dir.is_dir():
            return []
        tickets = [
            self.get(int(p.stem)) for p in self.dir.glob("*.json") if p.stem.isdigit()
        ]
        return sorted((t for t in tickets if t), key=lambda t: t.number)

    def subscribed_threads(self) -> dict[int, list[int]]:
        """Thread number to the tickets that follow it."""

        followers: dict[int, list[int]] = {}
        for ticket in self.all():
            for thread in ticket.threads:
                followers.setdefault(thread, []).append(ticket.number)
        return followers


def _anchor_pairs(packet: Packet) -> list[list]:
    pairs = []
    for anchor in packet.anchors:
        if is_source_path(anchor.path):
            pair = [anchor.path, anchor.symbol, anchor.start_line, anchor.end_line]
            if pair[:2] not in [p[:2] for p in pairs]:
                pairs.append(pair)
    return pairs


def _positions(store: ThreadStore, threads: list[int]) -> dict[str, int]:
    return {str(number): store.log_length(number) for number in threads}


def _news(book: "TicketBook", ticket: Ticket, bundle: Bundle) -> Bundle | None:
    """The part of ``bundle`` the ticket has not read, or ``None`` if none."""

    def unread(view: ThreadView) -> ThreadView:
        position = ticket.positions.get(str(view.number), 0)
        fresh = book.store.addresses_from(view.number, position)
        events = [e for e in view.events if e.address in fresh]
        return ThreadView(view.header, events, view.relation)

    root = unread(bundle.root)
    linked = [unread(view) for view in bundle.linked]
    if not root.events and not any(view.events for view in linked):
        return None
    return Bundle(root, linked)


def open_ticket(
    book: TicketBook,
    number: int,
    *,
    directory: str = "",
    follow: list[int] | None = None,
    budget: int = DEFAULT_BUDGET,
    code_budget: int = DEFAULT_CODE_BUDGET,
    checkout: Path | None = None,
    rev: str = "HEAD",
    open_pulls: list | None = None,
) -> tuple[Ticket, Packet] | None:
    """Start work on a thread: build its first context and remember the cursor.

    Opening a ticket that already exists starts it over from a full context and
    keeps the threads it follows.
    """

    existing = book.get(number)
    followed = list(
        dict.fromkeys([*(existing.threads if existing else []), *(follow or [])])
    )
    bundle = load_bundle(book.store, number, subscribed=followed)
    if bundle is None:
        return None
    packet = build_packet(
        bundle,
        budget=budget,
        code_budget=code_budget,
        checkout=checkout,
        rev=rev,
        open_pulls=open_pulls,
    )
    root = bundle.root
    ticket = Ticket(
        number=number,
        threads=[view.number for view in bundle.views],
        cursor=packet.head,
        directory=directory or (existing.directory if existing else ""),
        opened_at=root.events[-1].at if root.events else "",
        positions=_positions(book.store, [view.number for view in bundle.views]),
        anchors=_anchor_pairs(packet),
    )
    book.put(ticket)
    return ticket, packet


def pending(book: TicketBook, ticket: Ticket) -> int:
    """How many events of the ticket's threads it has not read."""

    bundle = load_bundle(book.store, ticket.number, subscribed=ticket.threads)
    news = _news(book, ticket, bundle) if bundle else None
    return sum(len(view.events) for view in news.views) if news else 0


def read_inbox(
    book: TicketBook,
    ticket: Ticket,
    *,
    budget: int = DEFAULT_BUDGET,
    code_budget: int = DEFAULT_CODE_BUDGET,
    checkout: Path | None = None,
    rev: str = "HEAD",
    advance: bool = True,
) -> Packet | None:
    """What the ticket's threads gained since it last read, or ``None``.

    This covers new events, edits of events it had read, and threads that
    became linked since then, which join the ticket. With ``advance`` the read
    positions move, so nothing is delivered twice.
    """

    bundle = load_bundle(book.store, ticket.number, subscribed=ticket.threads)
    news = _news(book, ticket, bundle) if bundle else None
    if bundle is None or news is None:
        return None
    packet = build_packet(
        bundle,
        budget=budget,
        code_budget=code_budget,
        checkout=checkout,
        rev=rev,
        news=news,
    )
    if advance:
        ticket.threads = [view.number for view in bundle.views]
        ticket.cursor = packet.head
        ticket.positions = _positions(book.store, ticket.threads)
        for pair in _anchor_pairs(packet):
            if pair[:2] not in [p[:2] for p in ticket.anchors]:
                ticket.anchors.append(pair)
        book.put(ticket)
    return packet


@dataclass
class BoardRow:
    ticket: Ticket
    title: str
    state: str
    unread: int
    shares_code_with: dict[int, list[str]]  # other ticket -> shared symbols
    open_pulls: dict[int, list[str]]  # open pull request -> symbols it changes


def board(book: TicketBook, open_pulls: list | None = None) -> list[BoardRow]:
    """Every ticket with its unread count and the tickets working the same code."""

    tickets = book.all()
    owners: dict[tuple[str, str], list[int]] = {}
    for ticket in tickets:
        for path, symbol, *_ in ticket.anchors:
            owners.setdefault((path, symbol), []).append(ticket.number)
    rows = []
    for ticket in tickets:
        header = book.store.header(ticket.number)
        if header is None:
            continue
        shared: dict[int, list[str]] = {}
        for path, symbol, *_ in ticket.anchors:
            # Two tickets meet when they name the same symbol. Naming the same
            # file is too weak to report: large files are shared by everything.
            if not symbol:
                continue
            for other in owners.get((path, symbol), []):
                if other != ticket.number:
                    shared.setdefault(other, []).append(symbol)
        pulls: dict[int, list[str]] = {}
        if open_pulls:
            # Only a pull request that changes lines of a named symbol is
            # listed. Sharing a file is not: one wide refactoring would then
            # show up against every ticket.
            located = [
                CodeAnchor(path, symbol, int(span[0]), int(span[1]))
                for path, symbol, *span in ticket.anchors
                if symbol and len(span) == 2 and span[0]
            ]
            for match in match_in_flight(located, open_pulls, set(ticket.threads)):
                if match.symbols:
                    pulls[match.pull.number] = match.symbols
        state = ThreadView(header, book.store.events(ticket.number)).state()
        rows.append(
            BoardRow(ticket, header.title, state, pending(book, ticket), shared, pulls)
        )
    return rows


__all__ = [
    "BoardRow",
    "Ticket",
    "TicketBook",
    "board",
    "open_ticket",
    "pending",
    "read_inbox",
]
