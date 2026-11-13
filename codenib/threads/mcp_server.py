# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A small MCP server for thread context and tickets.

It is separate from CodeNib's code-search MCP server, so adding it to an agent
does not change which code tools that agent sees. Every tool returns the same
text the command line prints. Thread text is data: a tool never acts on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field

from .log_store import ThreadStore

INSTRUCTIONS = (
    "Context for GitHub issues and pull requests of one repository. Call "
    "thread_context before reading a thread with gh: it returns the ask, the "
    "discussion, review comments, failing checks, linked threads and the code "
    "they point at, and says whether it is complete. For ongoing work, "
    "ticket_open once, then ticket_inbox to get only what is new."
)

_Number = Annotated[int, Field(ge=1, description="Issue or pull request number")]
_Budget = Annotated[int, Field(ge=200, le=20000, description="Target size in tokens")]


def build_server(
    repo: str,
    store_root: Path | None,
    checkout: Path | None,
    *,
    api_url: str | None = None,
):
    """An MCP server bound to one repository, its thread store and a checkout."""

    from mcp.server import MCPServer

    from .._version import __version__
    from .github_source import connect
    from .in_flight import read_index
    from .serve import build_packet, load_bundle, short
    from .sync import sync_thread
    from .tickets import TicketBook, board, open_ticket, read_inbox

    store = ThreadStore(repo, store_root)
    book = TicketBook(store)
    server = MCPServer(
        "codenib-threads", version=__version__, instructions=INSTRUCTIONS
    )

    def ensure(number: int, refresh: bool) -> None:
        if refresh or not store.has(number):
            sync_thread(connect(api_url), store, number)
        if not store.has(number):
            raise ValueError(f"{repo}#{number} was not found")

    @server.tool(
        name="thread_context",
        description=(
            "One issue or pull request with its linked threads and the code "
            "they point at, inside a token budget. Pass `since` (the cursor "
            "printed at the end of an earlier context) to get only what "
            "happened after it."
        ),
    )
    def thread_context(
        number: _Number, budget: _Budget = 3000, since: str = "", refresh: bool = False
    ) -> str:
        ensure(number, refresh)
        try:
            packet = build_packet(
                load_bundle(store, number),
                budget=budget,
                since=since or None,
                checkout=checkout,
                open_pulls=read_index(store),
            )
        except KeyError as exc:
            raise ValueError(str(exc.args[0])) from exc
        return packet.text

    @server.tool(
        name="ticket_open",
        description=(
            "Start work on an issue or pull request. Returns its full context "
            "and remembers what was read, so ticket_inbox can return only news."
        ),
    )
    def ticket_open(number: _Number, budget: _Budget = 3000) -> str:
        ensure(number, False)
        opened = open_ticket(
            book,
            number,
            budget=budget,
            checkout=checkout,
            open_pulls=read_index(store),
        )
        if opened is None:
            raise ValueError(f"{repo}#{number} was not found")
        return opened[1].text

    @server.tool(
        name="ticket_inbox",
        description=(
            "What the threads of a ticket gained since it was last read: new "
            "comments, reviews, check results and newly linked threads. Marks "
            "them read unless peek is true. Set refresh to fetch from GitHub "
            "first."
        ),
    )
    def ticket_inbox(
        number: _Number,
        budget: _Budget = 3000,
        peek: bool = False,
        refresh: bool = False,
    ) -> str:
        ticket = book.get(number)
        if ticket is None:
            raise ValueError(f"no ticket for #{number}; call ticket_open first")
        if refresh:
            client = connect(api_url)
            for thread in ticket.threads:
                sync_thread(client, store, thread, linked=thread == number)
        packet = read_inbox(
            book, ticket, budget=budget, checkout=checkout, advance=not peek
        )
        return packet.text if packet else f"Nothing new for ticket #{number}."

    @server.tool(
        name="tickets_board",
        description=(
            "All open tickets: unread events, and which other tickets or open "
            "pull requests work on the same code."
        ),
    )
    def tickets_board() -> str:
        rows = board(book, read_index(store))
        if not rows:
            return "No tickets."
        lines = []
        for row in rows:
            unread = f"{row.unread} unread" if row.unread else "up to date"
            lines.append(f"#{row.ticket.number} [{row.state}, {unread}] {row.title}")
            for other, symbols in sorted(row.shares_code_with.items()):
                names = ", ".join(f"`{s}`" for s in dict.fromkeys(symbols))
                lines.append(f"    same code as ticket #{other}: {names}")
            for number, symbols in sorted(row.open_pulls.items())[:8]:
                names = ", ".join(f"`{s}`" for s in symbols[:3])
                lines.append(f"    open pull request #{number} changes {names}")
        return "\n".join(lines)

    @server.tool(
        name="thread_events",
        description=(
            "Raw thread events by id, such as '#12/comment/345', exactly as "
            "stored: use it to check a citation or read a post that a context "
            "shortened. A failed check's event carries its log."
        ),
    )
    def thread_events(
        ids: Annotated[list[str], Field(min_length=1, max_length=20)],
        max_chars: Annotated[int, Field(ge=500, le=200000)] = 20000,
    ) -> str:
        import re

        wanted = {short(i) for i in ids}
        numbers = {int(m.group(1)) for i in wanted if (m := re.match(r"#(\d+)/", i))}
        out = []
        for number in sorted(numbers):
            for event in store.events(number):
                if short(event.address) not in wanted:
                    continue
                out.append(
                    f"[{short(event.address)}] {event.at} {event.kind} by {event.actor}"
                )
                for part in (event.meta.get("hunk"), event.body, event.meta.get("log")):
                    if part:
                        out.append(part)
        if not out:
            raise ValueError("no stored event has that id")
        text = "\n".join(out)
        if len(text) > max_chars:
            text = text[-max_chars:] + "\n[... beginning cut; raise max_chars ...]"
        return text

    return server


__all__ = ["INSTRUCTIONS", "build_server"]
