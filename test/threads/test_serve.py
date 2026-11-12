# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import subprocess

import pytest

from codenib.threads.events import ThreadEvent, ThreadHeader, event_address
from codenib.threads.extract import clean_body, failing_lines, mentions
from codenib.threads.github_source import fetch_thread
from codenib.threads.log_store import ThreadStore
from codenib.threads.serve import build_packet, count_tokens, load_bundle, short
from codenib.threads.sync import sync_thread

REPO = "acme/widget"
SOURCE = """\
def parse_config(path):
    return open(path).read()


def render(value):
    return str(value)
"""


def _event(number, kind, ident, at, body="", actor="ann", bot=False, **meta):
    return ThreadEvent(
        event_address(REPO, number, kind, ident),
        number,
        kind,
        actor,
        at,
        bot,
        body,
        meta,
    )


def _header(number, is_pr=False, title="t", links=None):
    return ThreadHeader(REPO, number, is_pr, title, "open", "ann", "2026-01-01T00:00:00Z",
                        links=links or {})  # fmt: skip


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
def store(tmp_path):
    store = ThreadStore(REPO, tmp_path / "store")
    shared = "The config loader crashes on an empty file. " * 3
    store.write_header(
        _header(7, title="Crash on empty config", links={"9": "mentions"})
    )
    store.append(
        7,
        [
            _event(7, "opened", 7, "2026-01-01T00:00:00Z",
                   f"{shared}\n\nSee `parse_config` in src/config.py:2", title="Crash"),
            _event(7, "labeled", 1, "2026-01-01T00:01:00Z", label="bug"),
            _event(7, "comment", 2, "2026-01-02T00:00:00Z",
                   "> quoted earlier text\n\nWe decided to return an empty dict."),
            _event(7, "comment", 3, "2026-01-02T00:05:00Z", "coverage 91%",
                   actor="cov[bot]", bot=True),
            _event(7, "cross_reference", "9-0", "2026-01-03T00:00:00Z", **{"from": 9}),
            _event(7, "comment", 4, "2026-01-05T00:00:00Z", "Any update on this?"),
        ],
    )  # fmt: skip
    store.write_header(_header(9, is_pr=True, title="Handle empty config",
                               links={"7": "closes"}))  # fmt: skip
    store.append(
        9,
        [
            _event(9, "opened", 9, "2026-01-03T00:00:00Z", f"{shared}\n\nFixes #7."),
            _event(9, "check", 50, "2026-01-03T01:00:00Z", bot=True, name="unit",
                   conclusion="failure", sha="abc12345",
                   log="ok line\nAssertionError: expected {} in src/config.py:2\nbye"),
            _event(9, "review_comment", 60, "2026-01-04T00:00:00Z", "Use a guard here.",
                   actor="rev", path="src/config.py", line=2),
        ],
    )  # fmt: skip
    return store


def test_clean_body_and_failing_lines():
    assert clean_body("> old\n\nnew <!-- hidden -->") == "new"
    log = "\n".join(["step"] * 10 + ["error: boom"] + ["after"] * 10)
    kept = failing_lines(log, context=1)
    assert "error: boom" in kept and kept.count("\n") == 2


def test_mentions_find_paths_frames_and_symbols():
    found = mentions(
        'File "/x/src/config.py", line 2, in parse_config\n'
        "see src/app.rs:10 and `Widget::render_all` or call load_cfg()"
    )
    kinds = {(m.kind, m.path, m.line, m.symbol) for m in found}
    assert ("frame", "/x/src/config.py", 2, "parse_config") in kinds
    assert ("path", "src/app.rs", 10, "") in kinds
    assert ("symbol", "", 0, "render_all") in kinds
    assert ("symbol", "", 0, "load_cfg") in kinds


def test_append_is_idempotent_and_keeps_latest_edit(store):
    events = store.events(7)
    assert store.append(7, events) == 0
    edited = _event(7, "comment", 4, "2026-01-05T00:00:00Z", "Edited question")
    assert store.append(7, [edited]) == 1
    bodies = [e.body for e in store.events(7) if e.address.endswith("/comment/4")]
    assert bodies == ["Edited question"]


def test_packet_is_filtered_deduplicated_anchored_and_cited(store, checkout):
    packet = build_packet(load_bundle(store, 7), budget=2000, checkout=checkout)
    text = packet.text
    assert "open issue" in text and "labels: bug" in text
    assert "We decided to return an empty dict." in text
    assert "quoted earlier text" not in text  # quoted reply removed
    assert "coverage 91%" not in text  # bot removed
    assert text.count("The config loader crashes") == 3  # PR copy of the ask dropped
    assert "src/config.py:1-2 `parse_config()`" in text  # resolved to the symbol
    assert "#9 (pull request for this issue)" in text and "CI: 1 failing (unit)" in text
    assert "AssertionError" not in text.split("## Failing CI")[0]
    assert "[#9/review_comment/60] src/config.py:2 rev: Use a guard here." in text
    assert packet.head == event_address(REPO, 7, "comment", 4)
    assert "Coverage: complete." in text and "bot comments (1)" in text
    code = text.split("## Code\n")[1]
    assert "### src/config.py:1-2 `parse_config()`" in code
    assert "return open(path).read()" in code
    without_code = build_packet(
        load_bundle(store, 7), budget=2000, code_budget=0, checkout=checkout
    )
    assert "## Code\n" not in without_code.text
    assert without_code.tokens <= 2000 < 2000 + 1500 >= packet.tokens


def test_budget_keeps_status_and_ask_first(store, checkout):
    packet = build_packet(
        load_bundle(store, 7), budget=260, code_budget=0, checkout=checkout
    )
    assert packet.tokens <= 260
    assert "Coverage: partial." in packet.text
    assert "Crash on empty config" in packet.text
    assert packet.omitted  # something had to go, and the packet says so
    assert "Omitted for budget:" in packet.text


def test_delta_returns_only_what_followed_the_cursor(store, checkout):
    cursor = short(event_address(REPO, 9, "opened", 9))
    delta = build_packet(load_bundle(store, 7), since=cursor, checkout=checkout)
    assert "Thread update" in delta.text
    assert "Any update on this?" in delta.text
    assert "Use a guard here." in delta.text
    assert "We decided to return an empty dict." not in delta.text
    assert "## Ask" not in delta.text
    full = build_packet(load_bundle(store, 7), checkout=checkout)
    assert delta.tokens < full.tokens
    with pytest.raises(KeyError):
        build_packet(load_bundle(store, 7), since="#7/comment/999")


def test_as_of_hides_later_events_and_threads(store):
    bundle = load_bundle(store, 7, at="2026-01-02T12:00:00Z")
    assert [v.number for v in bundle.views] == [7]
    assert "Any update" not in build_packet(bundle).text


class _FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        number = int(path.rsplit("/", 1)[-1])
        if "/pulls/" in path:
            return {"merged_at": None, "base": {"sha": "b"}, "head": {"sha": "h"}}
        issue = {
            "title": f"thread {number}", "state": "open", "user": {"login": "ann"},
            "created_at": "2026-01-01T00:00:00Z", "body": "Fixes #7" if number == 9 else "x",
        }  # fmt: skip
        if number == 9:
            issue["pull_request"] = {}
        return issue if number in (7, 9) else None

    def pages(self, path, key=None):
        self.calls.append(path)
        if path.endswith("issues/7/timeline"):
            return [
                {"event": "subscribed", "created_at": "2026-01-01T00:00:01Z"},
                {"event": "commented", "id": 2, "user": {"login": "bo", "type": "Bot"},
                 "created_at": "2026-01-02T00:00:00Z", "body": "hi"},
                {"event": "cross-referenced", "created_at": "2026-01-03T00:00:00Z",
                 "actor": {"login": "ann"},
                 "source": {"issue": {"number": 9, "title": "fix", "pull_request": {}}}},
            ]  # fmt: skip
        return []

    def text(self, path):
        return None


def test_fetch_and_sync_follow_links_once(tmp_path):
    client = _FakeClient()
    header, events = fetch_thread(client, REPO, 7)
    assert [e.kind for e in events] == ["opened", "comment", "cross_reference"]
    assert events[1].is_bot and header.links == {"9": "mentions"}
    assert fetch_thread(client, REPO, 404) is None

    store = ThreadStore(REPO, tmp_path)
    written = sync_thread(client, store, 7)
    assert written == {7: 3, 9: 1}
    assert store.header(9).links == {"7": "closes"}
    assert sync_thread(client, store, 7) == {7: 0}  # nothing new, link not refetched


def test_store_rejects_paths_outside_the_root(tmp_path):
    with pytest.raises(ValueError):
        ThreadStore("../evil", tmp_path)


def test_count_tokens_is_positive():
    assert count_tokens("hello world") > 0


def test_jobs_failing_alike_are_shown_once(store):
    log = "ok\n--- FAIL: TestGuard (1.61s)\n    /home/runner/w/config_test.go:7: boom"
    other = log.replace("1.61s", "2.03s").replace("/home/runner/w/", "D:/a/w/")
    store.append(
        9,
        [
            _event(9, "check", 51, "2026-01-03T01:00:01Z", bot=True, name="linux",
                   conclusion="failure", sha="abc12345", log=log),
            _event(9, "check", 52, "2026-01-03T01:00:02Z", bot=True, name="windows",
                   conclusion="failure", sha="abc12345", log=other),
        ],
    )  # fmt: skip
    text = build_packet(load_bundle(store, 9)).text
    assert text.count("--- FAIL: TestGuard") == 1
    assert "linux failed at abc12345 (same failure in: windows)" in text
    assert "unit failed at abc12345" in text  # a different failure stays separate


def test_client_waits_out_a_rate_limit(monkeypatch):
    import io
    import urllib.error

    from codenib.threads.github_source import GitHubClient

    client = GitHubClient("token")
    waits, calls = [], []

    class _Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 403, "limited", {"Retry-After": "7"}, io.BytesIO()
                )
            return io.BytesIO(b'{"ok": true}')

    client._opener = _Opener()
    client._sleep = waits.append
    assert client.get("repos/acme/widget/issues/1") == {"ok": True}
    assert waits == [7] and len(calls) == 2


def test_open_pulls_on_the_same_code_are_named(store, checkout):
    from codenib.threads.in_flight import OpenPull, base_ranges, read_index, write_index

    assert base_ranges("@@ -2 +2,3 @@ def parse_config\n-x\n+y\n@@ -9,2 +11 @@") == [
        [2, 2],
        [9, 10],
    ]
    pulls = [
        OpenPull(21, "Cache parsed config", "bo", "2026-01-04T00:00:00Z",
                 files={"src/config.py": [[2, 2]]}),
        OpenPull(22, "Tidy render", "cy", "2026-01-06T00:00:00Z", draft=True,
                 files={"src/config.py": [[6, 6]]}),
        OpenPull(23, "Docs", "di", "2026-01-07T00:00:00Z", files={"README.md": [[1, 1]]}),
        OpenPull(9, "Handle empty config", "ann", "2026-01-03T00:00:00Z",
                 files={"src/config.py": [[2, 2]]}),
    ]  # fmt: skip
    write_index(store, pulls)
    packet = build_packet(
        load_bundle(store, 7), checkout=checkout, open_pulls=read_index(store)
    )
    section = packet.text.split("## Also in progress on this code\n")[1].split("\n##")[
        0
    ]
    lines = section.strip().splitlines()
    assert lines[0].startswith("- #21 (open PR by bo, updated 2026-01-04) Cache parsed")
    assert "changes `parse_config()`" in lines[0]  # same symbol ranks first
    assert (
        lines[1].startswith("- #22 (draft open PR by cy") and "also edits" in lines[1]
    )
    assert len(lines) == 2  # unrelated #23 and the linked #9 are not repeated
