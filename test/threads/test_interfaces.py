# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import codenib.threads as threads
from codenib.threads.events import ThreadEvent, ThreadHeader, event_address
from codenib.threads.mcp_server import build_server

REPO = "acme/widget"


def _event(number, kind, ident, at, body=""):
    address = event_address(REPO, number, kind, ident)
    return ThreadEvent(address, number, kind, "ann", at, False, body)


@pytest.fixture()
def root(tmp_path):
    store = threads.ThreadStore(REPO, tmp_path)
    header = ThreadHeader(
        REPO, 7, False, "Crash", "open", "ann", "2026-01-01T00:00:00Z"
    )
    store.write_header(header)
    store.append(7, [_event(7, "opened", 7, "2026-01-01T00:00:00Z", "It crashes.")])
    return tmp_path


def _text(result) -> str:
    return "".join(part.text for part in result.content)


def test_python_api_names_resolve():
    assert set(threads.__all__) == set(threads._EXPORTS)
    for name in threads.__all__:
        assert getattr(threads, name) is not None
    with pytest.raises(AttributeError):
        threads.no_such_name


def test_mcp_tools_serve_context_then_only_news(root):
    server = build_server(REPO, root, None)

    async def scenario():
        names = [tool.name for tool in await server.list_tools()]
        assert names == [
            "thread_context", "ticket_open", "ticket_inbox", "tickets_board",
            "thread_events",
        ]  # fmt: skip
        context = _text(await server.call_tool("thread_context", {"number": 7}))
        assert "# Thread context: acme/widget#7" in context and "It crashes." in context
        assert "It crashes." in _text(
            await server.call_tool("ticket_open", {"number": 7})
        )
        nothing = _text(await server.call_tool("ticket_inbox", {"number": 7}))
        assert nothing == "Nothing new for ticket #7."

        threads.ThreadStore(REPO, root).append(
            7, [_event(7, "comment", 2, "2026-01-02T00:00:00Z", "Fixed on main?")]
        )
        board = _text(await server.call_tool("tickets_board", {}))
        assert "#7 [open, 1 unread] Crash" in board
        news = _text(await server.call_tool("ticket_inbox", {"number": 7}))
        assert "Fixed on main?" in news and "It crashes." not in news
        raw = _text(await server.call_tool("thread_events", {"ids": ["#7/comment/2"]}))
        assert raw.startswith("[#7/comment/2]") and "Fixed on main?" in raw

    asyncio.run(scenario())


def test_cli_ticket_round_trip_with_json(root, tmp_path):
    def run(*args):
        done = subprocess.run(
            [sys.executable, "-m", "codenib", "threads", *args, "--repo", REPO,
             "--store", str(root)],
            capture_output=True, text=True,
            env={**__import__("os").environ,
                 "PYTHONPATH": str(Path(__file__).parents[2])},
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        return done.stdout

    opened = json.loads(run("open", "7", "--offline", "--no-anchors", "--json"))
    assert opened["head"].endswith("#7/opened/7") and "It crashes." in opened["text"]
    assert run("inbox", "7", "--no-anchors") == ""
    threads.ThreadStore(REPO, root).append(
        7, [_event(7, "comment", 3, "2026-01-03T00:00:00Z", "Still failing.")]
    )
    board = json.loads(run("board", "--json"))
    assert board[0]["number"] == 7 and board[0]["unread"] == 1
    assert "Still failing." in run("inbox", "7", "--no-anchors", "--peek")
    assert (
        "Still failing."
        in json.loads(run("inbox", "7", "--no-anchors", "--json"))["text"]
    )
    assert run("inbox", "7", "--no-anchors") == ""
    assert "closed" in run("close", "7")
