# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Persisted types for issue and pull request threads.

A thread is stored as an append-only list of :class:`ThreadEvent`. Each event
has a stable address, ``gh:<owner>/<repo>#<number>/<kind>/<id>``, that a served
context cites and that a consumer can pass back to fetch the raw event or to
ask for everything after it.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

# Kinds whose body was written by a participant.
BODY_KINDS = frozenset({"opened", "comment", "review", "review_comment"})
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})


def event_address(repo: str, number: int, kind: str, ident: Any) -> str:
    return f"gh:{repo}#{number}/{kind}/{ident}"


def body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass(frozen=True)
class ThreadEvent:
    """One immutable event on a thread."""

    address: str
    thread: int
    kind: str
    actor: str
    at: str
    is_bot: bool = False
    body: str = ""
    # Small structured facts: label name, review state, commit sha, check
    # conclusion, the thread a cross-reference came from, a review comment's
    # path and diff hunk, a failed job's log.
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return body_digest(self.body + repr(sorted(self.meta.items())))

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ThreadEvent":
        return cls(
            address=data["address"],
            thread=int(data["thread"]),
            kind=data["kind"],
            actor=data.get("actor", "?"),
            at=data.get("at", ""),
            is_bot=bool(data.get("is_bot", False)),
            body=data.get("body", ""),
            meta=dict(data.get("meta") or {}),
        )


@dataclass
class ThreadHeader:
    """Facts about a thread that code decides, never a model."""

    repo: str
    number: int
    is_pr: bool
    title: str
    state: str  # open | closed | merged
    author: str
    created_at: str
    closed_at: str = ""
    base_sha: str = ""
    head_sha: str = ""
    # Threads this one is tied to, with the reason: closes, closed-by, mentions.
    links: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ThreadHeader":
        return cls(**{**data, "links": dict(data.get("links") or {})})


__all__ = [
    "BODY_KINDS",
    "FAILED_CONCLUSIONS",
    "ThreadEvent",
    "ThreadHeader",
    "body_digest",
    "event_address",
]
