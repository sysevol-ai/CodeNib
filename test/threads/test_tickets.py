# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import subprocess

import pytest

from codenib.threads.events import ThreadEvent, ThreadHeader, event_address
from codenib.threads.log_store import ThreadStore
from codenib.threads.poll import poll
from codenib.threads.tickets import TicketBook, board, open_ticket, read_inbox

REPO = "acme/widget"
SOURCE = "def parse_config(path):\n    return open(path).read()\n"


def _event(number, kind, ident, at, body="", actor="ann", **meta):
    address = event_address(REPO, number, kind, ident)
    return ThreadEvent(address, number, kind, actor, at, False, body, meta)


def _thread(store, number, body, *, is_pr=False, links=None, at="2026-01-01T00:00:00Z"):
    header = ThreadHeader(REPO, number, is_pr, f"thread {number}", "open", "ann", at)
    header.links = links or {}
    store.write_header(header)
    store.append(number, [_event(number, "opened", number, at, body)])


@pytest.fixture()
def checkout(tmp_path):
    repo = tmp_path / "checkout"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "config.py").write_text(SOURCE)
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    return repo


@pytest.fixture()
def book(tmp_path):
    store = ThreadStore(REPO, tmp_path / "store")
    _thread(
        store,
        1,
        "Crash in `parse_config`, see src/config.py:1",
        links={"3": "mentions"},
    )
    _thread(
        store,
        2,
        "Slow start; `parse_config` reads the file twice",
        links={"3": "mentions"},
    )
    _thread(store, 3, "Tracking issue for config loading")
    _thread(store, 4, "Unrelated typo in the README")
    return TicketBook(store)


def test_inbox_delivers_each_event_once_from_any_followed_thread(book, checkout):
    ticket, first = open_ticket(book, 1, checkout=checkout)
    assert ticket.threads == [1, 3] and "# Thread context" in first.text
    assert read_inbox(book, book.get(1), checkout=checkout) is None

    book.store.append(3, [_event(3, "comment", 30, "2026-01-02T00:00:00Z",
                                 "We will return an empty dict.")])  # fmt: skip
    book.store.append(1, [_event(1, "comment", 10, "2026-01-03T00:00:00Z",
                                 "Any update?")])  # fmt: skip
    update = read_inbox(book, book.get(1), checkout=checkout)
    assert "# Thread update" in update.text
    assert "We will return an empty dict." in update.text  # from the linked thread
    assert "Any update?" in update.text  # from its own thread
    assert "Crash in" not in update.text  # nothing already read
    assert read_inbox(book, book.get(1), checkout=checkout) is None


def test_one_thread_feeds_every_ticket_that_follows_it(book, checkout):
    open_ticket(book, 1, checkout=checkout)
    open_ticket(book, 2, checkout=checkout)
    assert book.subscribed_threads()[3] == [1, 2]
    book.store.append(3, [_event(3, "comment", 31, "2026-01-02T00:00:00Z",
                                 "Decision: cache the parsed file.")])  # fmt: skip
    for number in (1, 2):
        assert "Decision: cache" in read_inbox(book, book.get(number)).text
    # Reading with --peek semantics leaves the cursor where it was.
    book.store.append(3, [_event(3, "comment", 32, "2026-01-04T00:00:00Z", "ping")])
    assert "ping" in read_inbox(book, book.get(1), advance=False).text
    assert "ping" in read_inbox(book, book.get(1)).text


def test_a_thread_linked_later_joins_the_ticket(book, checkout):
    open_ticket(book, 4, checkout=checkout)
    assert book.get(4).threads == [4]
    _thread(book.store, 9, "Fixes #4", is_pr=True, links={"4": "closes"},
            at="2026-01-05T00:00:00Z")  # fmt: skip
    book.store.append(4, [_event(4, "cross_reference", "9-0", "2026-01-05T00:00:01Z",
                                 **{"from": 9, "is_pr": True})])  # fmt: skip
    update = read_inbox(book, book.get(4))
    assert "#9 (pull request for this issue)" in update.text
    assert book.get(4).threads == [4, 9]


def test_board_shows_unread_and_tickets_on_the_same_symbol(book, checkout):
    for number in (1, 2, 4):
        open_ticket(book, number, checkout=checkout)
    book.store.append(3, [_event(3, "comment", 33, "2026-01-02T00:00:00Z", "note")])
    rows = {row.ticket.number: row for row in board(book)}
    assert rows[1].unread == 1 and rows[2].unread == 1 and rows[4].unread == 0
    assert rows[1].shares_code_with == {2: ["parse_config()"]}
    assert rows[2].shares_code_with == {1: ["parse_config()"]}
    assert rows[4].shares_code_with == {}
    assert book.remove(4) and 4 not in {t.number for t in book.all()}


class _FakeGitHub:
    """Serves thread 3 with one new comment; everything else is unchanged."""

    def __init__(self, changed, news=True):
        self.changed = changed
        self.news = news
        self.requests = 0
        self.fetched = []

    def pages(self, path, key=None, max_pages=30):
        self.requests += 1
        if "/issues?state=all" in path:
            return [{"number": n} for n in self.changed]
        if self.news and path.endswith("issues/3/timeline"):
            return [{"event": "commented", "id": 77, "user": {"login": "bo"},
                     "created_at": "2026-02-01T00:00:00Z", "body": "polled news"}]  # fmt: skip
        return []

    def get(self, path):
        self.requests += 1
        number = int(path.rsplit("/", 1)[-1])
        self.fetched.append(number)
        return {"title": f"thread {number}", "state": "open", "user": {"login": "ann"},
                "created_at": "2026-01-01T00:00:00Z", "body": "x"}  # fmt: skip

    def text(self, path):
        return None


def test_poll_fetches_only_followed_threads_that_changed(book, checkout):
    open_ticket(book, 1, checkout=checkout)
    open_ticket(book, 2, checkout=checkout)
    first = poll(_FakeGitHub([], news=False), book, now=lambda: "2026-01-10T00:00:00Z")
    assert first.refetched == [1, 2, 3]  # the first poll fetches all followed

    github = _FakeGitHub(changed=[3, 4, 50, 51])  # 4, 50, 51 are followed by no ticket
    result = poll(github, book, now=lambda: "2026-02-02T00:00:00Z")
    assert result.changed_on_github == 4
    assert result.refetched == [3] and github.fetched == [3]
    assert result.new_events == {3: 1}
    assert result.tickets_with_news == [1, 2]  # one fetch, two tickets informed
    assert result.requests == 3  # the listing, the issue, its timeline
    assert "polled news" in read_inbox(book, book.get(1)).text
    assert "polled news" in read_inbox(book, book.get(2)).text


def test_poll_probes_an_idle_open_pull_request_instead_of_fetching_it(book, checkout):
    _thread(book.store, 9, "Fixes #4", is_pr=True, links={"4": "closes"})
    book.store.append(
        9,
        [
            _event(9, "commit", "h1", "2026-01-02T00:00:00Z", sha="h1"),
            _event(9, "check", 5, "2026-01-02T01:00:00Z", name="unit",
                   conclusion="success", sha="h1"),
        ],
    )  # fmt: skip
    open_ticket(book, 4, checkout=checkout, follow=[9])
    (book.store.dir / "poll.json").write_text('{"since": "2026-01-10T00:00:00Z"}')

    class _Probe(_FakeGitHub):
        conclusion = "success"

        def get(self, path):
            if "/pulls/" in path:
                self.requests += 1
                return {"head": {"sha": "h1"}, "base": {"sha": "b"}}
            return super().get(path)

        def pages(self, path, key=None, max_pages=30):
            if path.endswith("commits/h1/check-runs"):
                self.requests += 1
                return [{"id": 5, "name": "unit", "conclusion": self.conclusion}]
            return super().pages(path, key, max_pages)

    idle = _Probe(changed=[])
    result = poll(idle, book, now=lambda: "2026-01-11T00:00:00Z")
    assert result.refetched == [] and result.requests == 3  # listing plus the probe

    failing = _Probe(changed=[])
    failing.conclusion = "failure"
    result = poll(failing, book, now=lambda: "2026-01-12T00:00:00Z")
    assert result.refetched == [9] and result.tickets_with_news == [4]
