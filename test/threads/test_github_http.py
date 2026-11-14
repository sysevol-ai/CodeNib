# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The real HTTP client against a local stand-in for GitHub."""

import pytest

from codenib.threads.github_source import (
    connect,
    default_api_url,
    fetch_thread,
    resolve_token,
)
from codenib.threads.in_flight import fetch_open_pulls
from codenib.threads.log_store import ThreadStore
from codenib.threads.serve import build_packet, load_bundle
from codenib.threads.sync import sync_thread

REPO = "acme/widget"


def test_token_and_api_url_come_from_the_environment(github, monkeypatch):
    assert resolve_token() == "test-token" and default_api_url() == github.url
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", "other")
    assert resolve_token() == "other"
    with pytest.raises(ValueError):
        connect("http://example.com/api")  # a token is never sent in clear text


def test_issue_is_read_across_pages_with_the_token(github):
    header, events = fetch_thread(connect(), REPO, 7)
    assert header.title == "Crash on empty config" and not header.is_pr
    assert [e.kind for e in events] == [
        "opened", "labeled", "comment", "comment", "cross_reference",
    ]  # fmt: skip
    assert events[2].is_bot and events[3].body == "Return an empty dict."
    assert header.links == {"9": "mentions"}
    assert {auth for _, auth in github.requests} == {"Bearer test-token"}
    assert fetch_thread(connect(), REPO, 404) is None


def test_pull_request_brings_reviews_diff_checks_and_the_failed_log(github):
    header, events = fetch_thread(connect(), REPO, 9)
    kinds = [e.kind for e in events]
    assert header.is_pr and header.links == {"7": "closes"}
    assert header.head_sha == "head111"
    for kind in ("commit", "diff", "check", "review", "review_comment"):
        assert kind in kinds
    review = next(e for e in events if e.kind == "review")
    assert review.meta["state"] == "changes_requested"
    diff = next(e for e in events if e.kind == "diff")
    assert diff.meta["files"] == ["src/config.py"] and diff.meta["additions"] == 3
    failed = next(e for e in events if e.kind == "check" and e.meta["name"] == "unit")
    assert "--- FAIL: TestParse" in failed.meta["log"]
    # The log came through a redirect to another host, which got no token.
    assert github.storage_seen == [None]


def test_sync_then_serve_end_to_end(github, tmp_path):
    store = ThreadStore(REPO, tmp_path)
    client = connect()
    written = sync_thread(client, store, 7)
    assert set(written) == {7, 9} and written[7] == 5
    before = len(github.requests)
    assert sync_thread(client, store, 7) == {7: 0}  # idempotent, link not refetched
    assert len(github.requests) - before == 3  # issue plus two timeline pages

    text = build_packet(load_bundle(store, 7)).text
    assert "Return an empty dict." in text and "triaged" not in text
    assert "#9 (pull request for this issue)" in text
    assert "reviews: changes requested by rev" in text
    assert "--- FAIL: TestParse" in text and "Guard here." in text


def test_a_rate_limit_is_waited_out_and_a_server_error_is_reported(github):
    client = connect()
    waits = []
    client._sleep = waits.append
    github.rate_limit_once = True
    assert client.get(f"repos/{REPO}/issues/7")["number"] == 7
    assert waits == [1]
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.get(f"repos/{REPO}/boom")
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.pages(f"repos/{REPO}/boom")


def test_open_pull_requests_are_indexed_without_bots(github):
    pulls = fetch_open_pulls(connect(), REPO)
    assert [p.number for p in pulls] == [9]  # the bot's pull request is skipped
    assert pulls[0].files == {"src/config.py": [[2, 2]]}
