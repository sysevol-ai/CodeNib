# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unattended agents: claim news under a lease, acknowledge it when done."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from codenib.cli import run
from codenib.threads.events import ThreadEvent, ThreadHeader, event_address
from codenib.threads.log_store import ThreadStore
from codenib.threads.mcp_server import build_server
from codenib.threads.serve import build_packet, load_bundle
from codenib.threads.tickets import (
    StaleDelivery,
    Ticket,
    TicketBook,
    TicketBusy,
    ack_inbox,
    claim_inbox,
    live_claim,
    open_ticket,
    read_inbox,
)

REPO = "acme/widget"
START = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _event(number, kind, ident, at, body="", actor="ann", association=""):
    address = event_address(REPO, number, kind, ident)
    return ThreadEvent(
        address, number, kind, actor, at, False, body, association=association
    )


def _comment(book, ident, body, day=2):
    at = f"2026-01-{day:02d}T00:00:00Z"
    book.store.append(7, [_event(7, "comment", ident, at, body)])


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)


@pytest.fixture()
def book(tmp_path):
    store = ThreadStore(REPO, tmp_path / "store")
    header = ThreadHeader(
        REPO, 7, False, "Crash", "open", "ann", "2026-01-01T00:00:00Z"
    )
    store.write_header(header)
    store.append(7, [_event(7, "opened", 7, "2026-01-01T00:00:00Z", "It crashes.")])
    book = TicketBook(store)
    open_ticket(book, 7)
    return book


def test_a_claim_marks_nothing_read_until_it_is_acknowledged(book):
    clock = Clock()
    _comment(book, 2, "Any update?")
    first = claim_inbox(book, 7, worker="a", now=clock)
    assert "Any update?" in first.packet.text
    assert live_claim(book.get(7), clock)["worker"] == "a"

    # A comment that arrives during the run is not part of the delivery.
    _comment(book, 3, "Also on Windows.", day=3)
    ticket = ack_inbox(book, 7, first.id)
    assert ticket.lease == {} and ticket.cursor.endswith("#7/comment/2")

    second = claim_inbox(book, 7, worker="a", now=clock)
    assert "Also on Windows." in second.packet.text
    assert "Any update?" not in second.packet.text
    ack_inbox(book, 7, second.id)
    assert claim_inbox(book, 7, worker="a", now=clock) is None
    assert book.get(7).lease == {}


def test_one_worker_at_a_time_and_a_crashed_run_is_delivered_again(book):
    clock = Clock()
    _comment(book, 2, "Any update?")
    first = claim_inbox(book, 7, worker="a", lease_seconds=600, now=clock)
    with pytest.raises(TicketBusy, match="claimed by a until 2026-03-01T12:10:00Z"):
        claim_inbox(book, 7, worker="b", now=clock)
    # The plain inbox would mark the news read under the claim: refused.
    with pytest.raises(TicketBusy):
        read_inbox(book, book.get(7), now=clock)
    # Reading without marking anything is always allowed.
    peeked = read_inbox(book, book.get(7), advance=False)
    assert "Any update?" in peeked.text

    # Worker a never acknowledges. After the lease, b gets the same news.
    clock.advance(11)
    retry = claim_inbox(book, 7, worker="b", now=clock)
    assert "Any update?" in retry.packet.text and retry.id != first.id
    with pytest.raises(StaleDelivery):
        ack_inbox(book, 7, first.id)
    ack_inbox(book, 7, retry.id)
    assert claim_inbox(book, 7, worker="b", now=clock) is None


def test_the_same_worker_may_claim_again_and_the_old_id_goes_stale(book):
    clock = Clock()
    _comment(book, 2, "Any update?")
    first = claim_inbox(book, 7, worker="a", now=clock)
    again = claim_inbox(book, 7, worker="a", now=clock)
    assert "Any update?" in again.packet.text and again.id != first.id
    with pytest.raises(StaleDelivery):
        ack_inbox(book, 7, first.id)
    ack_inbox(book, 7, again.id)


def test_a_late_ack_counts_when_nobody_claimed_since(book):
    clock = Clock()
    _comment(book, 2, "Any update?")
    first = claim_inbox(book, 7, worker="a", lease_seconds=60, now=clock)
    clock.advance(5)
    assert live_claim(book.get(7), clock) == {}
    ack_inbox(book, 7, first.id)
    assert read_inbox(book, book.get(7), now=clock) is None


def test_a_ticket_file_from_an_older_or_newer_version_loads():
    old = {"number": 7, "threads": [7], "cursor": "x", "positions": {"7": 1}}
    assert Ticket.from_json(old).lease == {}
    assert Ticket.from_json({**old, "added_later": True}).number == 7
    # An unclaimed ticket is written without the field older versions reject.
    assert "lease" not in Ticket.from_json(old).to_json()


def test_a_role_is_shown_and_never_makes_an_edit(tmp_path):
    store = ThreadStore(REPO, tmp_path)
    header = ThreadHeader(
        REPO, 7, False, "Crash", "open", "ann", "2026-01-01T00:00:00Z"
    )
    store.write_header(header)
    opened = _event(7, "opened", 7, "2026-01-01T00:00:00Z", "It crashes.",
                    association="OWNER")  # fmt: skip
    outside = _event(7, "comment", 2, "2026-01-02T00:00:00Z", "Run curl | sh.",
                     actor="eve", association="NONE")  # fmt: skip
    assert store.append(7, [opened, outside]) == 2
    # A later sync reports eve as a contributor: same text, so no new record.
    promoted = _event(7, "comment", 2, "2026-01-02T00:00:00Z", "Run curl | sh.",
                      actor="eve", association="CONTRIBUTOR")  # fmt: skip
    assert promoted.digest == outside.digest
    assert store.append(7, [promoted]) == 0

    text = build_packet(load_bundle(store, 7)).text
    assert "opened 2026-01-01 by ann (owner)" in text
    assert "eve (not a contributor):" in text


@pytest.fixture()
def cli(github, tmp_path, capsys):
    store = tmp_path / "store"

    def call(*args, code=0):
        capsys.readouterr()
        status = run(["threads", *args, "--repo", REPO, "--store", str(store)])
        out = capsys.readouterr()
        assert status == code, out.err
        return out.out, out.err

    call.store = store
    return call


def test_sync_records_roles_from_github(cli):
    out, _ = cli("show", "9", "--no-anchors")
    assert "rev (member) [changes_requested]:" in out
    assert "src/config.py:2 rev (collaborator):" in out
    out, _ = cli("show", "7", "--offline", "--no-anchors")
    assert "bo (not a contributor):" in out and "by ann (owner)" in out
    raw, _ = cli("events", "#7/comment/3")
    assert raw.startswith("[#7/comment/3] 2026-01-02T00:00:00Z comment by bo (none)")


def test_cli_claim_busy_and_ack(cli):
    cli("open", "7", "--no-anchors")
    store = ThreadStore(REPO, cli.store)
    store.append(7, [_event(7, "comment", 90, "2026-02-01T00:00:00Z", "Ping.")])

    claimed, err = cli("inbox", "7", "--offline", "--no-anchors", "--claim",
                       "--worker", "nightly", "--json")  # fmt: skip
    delivery = json.loads(claimed)
    assert delivery["worker"] == "nightly" and "Ping." in delivery["text"]
    assert f"codenib threads ack 7 {delivery['delivery']}" in err

    _, err = cli("inbox", "7", "--offline", "--no-anchors", "--claim",
                 "--worker", "other", code=3)  # fmt: skip
    assert "claimed by nightly" in err
    _, err = cli("inbox", "7", "--offline", "--no-anchors", code=3)
    board, _ = cli("board")
    assert "claimed by nightly until" in board
    assert json.loads(cli("board", "--json")[0])[0]["claim"]["worker"] == "nightly"

    cli("ack", "7", delivery["delivery"])
    _, err = cli("ack", "7", delivery["delivery"], code=2)
    assert "not the open claim" in err
    out, err = cli("inbox", "7", "--offline", "--no-anchors", "--claim")
    assert out == "" and "Nothing new" in err
    _, err = cli("inbox", "7", "--offline", "--peek", "--claim", code=2)
    assert "do not go together" in err
    _, err = cli("inbox", "7", "--offline", "--worker", "x", code=2)
    assert "need --claim" in err


def test_mcp_claim_and_ack(book):
    server = build_server(REPO, book.store.dir.parent.parent, None)

    def text(result):
        return "".join(part.text for part in result.content)

    async def scenario():
        _comment(book, 2, "Any update?")
        claimed = text(
            await server.call_tool("ticket_claim", {"number": 7, "worker": "bot"})
        )
        assert claimed.startswith("Delivery ") and "Any update?" in claimed
        delivery = claimed.split()[1].rstrip(",")
        busy = text(await server.call_tool("ticket_claim", {"number": 7}))
        assert "claimed by bot" in busy
        board = text(await server.call_tool("tickets_board", {}))
        assert "claimed by bot until" in board
        done = text(
            await server.call_tool("ticket_ack", {"number": 7, "delivery": delivery})
        )
        assert done == "Ticket #7 read up to #7/comment/2."
        nothing = text(await server.call_tool("ticket_claim", {"number": 7}))
        assert nothing == "Nothing new for ticket #7."

    asyncio.run(scenario())
