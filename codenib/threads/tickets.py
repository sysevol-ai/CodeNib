# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tickets: units of work that follow threads and remember what they have read.

A ticket starts from one issue or pull request. It subscribes to that thread
and to the threads linked to it, and it may follow others. Each ticket keeps a
cursor, so asking for its inbox returns only what its threads gained since the
last time, whichever of them it came from. One thread can feed many tickets;
nothing is copied, each ticket only keeps its own cursor.

An agent that runs unattended claims its inbox instead of reading it. A claim
delivers the news under a lease and moves nothing; the agent acknowledges the
delivery when its work is done. If it crashes first, the lease runs out and
the next claim delivers the same news again. While a lease is held, no other
worker can claim the ticket, so two runs never work the same news.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

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
    # A delivery that a worker has claimed and not yet acknowledged: its id,
    # the worker, when the lease ends, and the read state that acknowledging
    # it commits (threads, positions, cursor, anchors). Empty when none.
    lease: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        data = asdict(self)
        # Without a claim the file looks as it did before claims existed, so
        # an older CodeNib sharing the store can still read it.
        if not data["lease"]:
            del data["lease"]
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Ticket":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


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

    @contextmanager
    def locked(self, number: int) -> Iterator[None]:
        """Hold the ticket while it is read, changed and written back.

        The lock lasts milliseconds; what keeps a second worker away for the
        length of a run is the lease stored in the ticket.
        """

        from filelock import FileLock

        self.dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.dir / f"{int(number)}.lock"), timeout=30):
            yield

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

    with book.locked(number):
        return _open_ticket(
            book,
            number,
            directory=directory,
            follow=follow,
            budget=budget,
            code_budget=code_budget,
            checkout=checkout,
            rev=rev,
            open_pulls=open_pulls,
        )


def _open_ticket(
    book: TicketBook,
    number: int,
    *,
    directory: str,
    follow: list[int] | None,
    budget: int,
    code_budget: int,
    checkout: Path | None,
    rev: str,
    open_pulls: list | None,
) -> tuple[Ticket, Packet] | None:
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


DEFAULT_LEASE_SECONDS = 30 * 60


class TicketBusy(RuntimeError):
    """Another worker holds the ticket's lease."""

    def __init__(self, number: int, worker: str, expires: str) -> None:
        super().__init__(number, worker, expires)
        self.number = number
        self.worker = worker
        self.expires = expires

    def __str__(self) -> str:
        return (
            f"ticket #{self.number} is claimed by "
            f"{self.worker or 'another worker'} until {self.expires}"
        )


class StaleDelivery(RuntimeError):
    """The delivery being acknowledged is no longer the ticket's claim."""


@dataclass
class Delivery:
    """News claimed under a lease. Acknowledge ``id`` when the work is done."""

    id: str
    worker: str
    expires: str
    packet: Packet

    def to_json(self) -> dict:
        return {
            "delivery": self.id,
            "worker": self.worker,
            "expires": self.expires,
            **self.packet.to_json(),
        }


def default_worker() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expired(lease: dict, now: datetime) -> bool:
    try:
        ends = datetime.strptime(lease.get("expires", ""), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return True
    return ends.replace(tzinfo=timezone.utc) <= now


def live_claim(ticket: Ticket, now: Callable[[], datetime] = _utcnow) -> dict:
    """The ticket's open claim (id, worker, expires), or ``{}`` if none."""

    lease = ticket.lease
    if not lease or _expired(lease, now()):
        return {}
    return {key: lease.get(key, "") for key in ("id", "worker", "expires")}


def _read(
    book: TicketBook,
    ticket: Ticket,
    *,
    budget: int,
    code_budget: int,
    checkout: Path | None,
    rev: str,
) -> tuple[Packet, dict] | None:
    """The ticket's news and the read state that marking it read would set."""

    # Positions are taken before the logs are read. An event appended while
    # the news is built is then delivered again later, never skipped.
    before = _positions(book.store, ticket.threads)
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
    threads = [view.number for view in bundle.views]
    joined = [n for n in threads if str(n) not in before]
    state = {
        "threads": threads,
        "positions": {**before, **_positions(book.store, joined)},
        "cursor": packet.head,
        "anchors": _anchor_pairs(packet),
    }
    return packet, state


def _commit(ticket: Ticket, state: dict) -> None:
    ticket.threads = list(state["threads"])
    ticket.cursor = state["cursor"]
    for thread, position in state["positions"].items():
        ticket.positions[thread] = max(ticket.positions.get(thread, 0), position)
    for pair in state["anchors"]:
        if pair[:2] not in [p[:2] for p in ticket.anchors]:
            ticket.anchors.append(pair)


def read_inbox(
    book: TicketBook,
    ticket: Ticket,
    *,
    budget: int = DEFAULT_BUDGET,
    code_budget: int = DEFAULT_CODE_BUDGET,
    checkout: Path | None = None,
    rev: str = "HEAD",
    advance: bool = True,
    now: Callable[[], datetime] = _utcnow,
) -> Packet | None:
    """What the ticket's threads gained since it last read, or ``None``.

    This covers new events, edits of events it had read, and threads that
    became linked since then, which join the ticket. With ``advance`` the read
    positions move, so nothing is delivered twice; that is refused with
    :class:`TicketBusy` while another worker's claim is open. A read without
    ``advance`` is always allowed.
    """

    if not advance:
        read = _read(
            book,
            ticket,
            budget=budget,
            code_budget=code_budget,
            checkout=checkout,
            rev=rev,
        )
        return read[0] if read else None
    with book.locked(ticket.number):
        current = book.get(ticket.number) or ticket
        lease = current.lease
        if lease and not _expired(lease, now()):
            raise TicketBusy(current.number, lease.get("worker", ""), lease["expires"])
        read = _read(
            book,
            current,
            budget=budget,
            code_budget=code_budget,
            checkout=checkout,
            rev=rev,
        )
        if read is None:
            return None
        packet, state = read
        _commit(current, state)
        current.lease = {}
        book.put(current)
    # Callers may keep using the ticket they passed in.
    for name in (f.name for f in fields(Ticket)):
        setattr(ticket, name, getattr(current, name))
    return packet


def claim_inbox(
    book: TicketBook,
    number: int,
    *,
    worker: str = "",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    budget: int = DEFAULT_BUDGET,
    code_budget: int = DEFAULT_CODE_BUDGET,
    checkout: Path | None = None,
    rev: str = "HEAD",
    now: Callable[[], datetime] = _utcnow,
) -> Delivery | None:
    """Claim the ticket's news for one run, or ``None`` when nothing is new.

    Nothing is marked read until :func:`ack_inbox` is called with the
    delivery's id. A worker that claims again under the same name, such as
    a run that restarted, gets the news again with a new id. Any other
    worker gets :class:`TicketBusy` until the lease ends.
    """

    if lease_seconds < 1:
        raise ValueError("lease_seconds must be at least 1")
    worker = worker or default_worker()
    with book.locked(number):
        ticket = book.get(number)
        if ticket is None:
            raise KeyError(number)
        moment = now()
        lease = ticket.lease
        if lease and not _expired(lease, moment) and lease.get("worker") != worker:
            raise TicketBusy(number, lease.get("worker", ""), lease["expires"])
        read = _read(
            book,
            ticket,
            budget=budget,
            code_budget=code_budget,
            checkout=checkout,
            rev=rev,
        )
        if read is None:
            if lease:
                ticket.lease = {}
                book.put(ticket)
            return None
        packet, state = read
        delivery = Delivery(
            id=secrets.token_hex(8),
            worker=worker,
            expires=_iso(moment + timedelta(seconds=lease_seconds)),
            packet=packet,
        )
        ticket.lease = {
            "id": delivery.id,
            "worker": worker,
            "expires": delivery.expires,
            "state": state,
        }
        book.put(ticket)
    return delivery


def ack_inbox(book: TicketBook, number: int, delivery: str) -> Ticket:
    """Mark a claimed delivery read and end its lease.

    Only what that delivery contained is marked: events that arrived during
    the run stay unread for the next claim. Acknowledging after the lease
    ran out still works if no other worker has claimed the ticket since.
    """

    with book.locked(number):
        ticket = book.get(number)
        if ticket is None:
            raise KeyError(number)
        lease = ticket.lease
        if not lease or lease.get("id") != delivery:
            raise StaleDelivery(
                f"delivery {delivery} is not the open claim on ticket #{number}; "
                "it was acknowledged already, or the lease ran out and another "
                "worker claimed the ticket"
            )
        _commit(ticket, lease["state"])
        ticket.lease = {}
        book.put(ticket)
    return ticket


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
    "DEFAULT_LEASE_SECONDS",
    "BoardRow",
    "Delivery",
    "StaleDelivery",
    "Ticket",
    "TicketBook",
    "TicketBusy",
    "ack_inbox",
    "board",
    "claim_inbox",
    "default_worker",
    "live_claim",
    "open_ticket",
    "pending",
    "read_inbox",
]
