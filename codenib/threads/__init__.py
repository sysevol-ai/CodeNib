# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Thread context and tickets for GitHub issues and pull requests (preview)."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

# Public names and the module each lives in. Resolved on first use, so that
# importing the package does not load the parsers.
_EXPORTS = {
    "ThreadEvent": "events",
    "ThreadHeader": "events",
    "ThreadStore": "log_store",
    "GitHubClient": "github_source",
    "fetch_thread": "github_source",
    "sync_thread": "sync",
    "Bundle": "serve",
    "Packet": "serve",
    "build_packet": "serve",
    "load_bundle": "serve",
    "Ticket": "tickets",
    "TicketBook": "tickets",
    "open_ticket": "tickets",
    "read_inbox": "tickets",
    "board": "tickets",
    "poll": "poll",
    "fetch_open_pulls": "in_flight",
    "read_index": "in_flight",
    "write_index": "in_flight",
}

if TYPE_CHECKING:
    from .events import ThreadEvent, ThreadHeader
    from .github_source import GitHubClient, fetch_thread
    from .in_flight import fetch_open_pulls, read_index, write_index
    from .log_store import ThreadStore
    from .poll import poll
    from .serve import Bundle, Packet, build_packet, load_bundle
    from .sync import sync_thread
    from .tickets import Ticket, TicketBook, board, open_ticket, read_inbox


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTS])


__all__ = [
    "Bundle",
    "GitHubClient",
    "Packet",
    "ThreadEvent",
    "ThreadHeader",
    "ThreadStore",
    "Ticket",
    "TicketBook",
    "board",
    "build_packet",
    "fetch_open_pulls",
    "fetch_thread",
    "load_bundle",
    "open_ticket",
    "poll",
    "read_inbox",
    "read_index",
    "sync_thread",
    "write_index",
]
