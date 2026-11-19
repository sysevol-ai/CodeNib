# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The agent's outbox: what it wants to say, held until a maintainer sends it.

A run ends with a reply for the thread, a question for the maintainers, or
nothing. Replies and questions are filed here, one small JSON file each. The
agent never posts: a maintainer reads the drafts, then sends, discards or
answers them. Sending is the only part of CodeNib threads that writes to
GitHub, and it happens only on that command.

A thread has at most one waiting draft. A newer run replaces it, so a busy
thread still gets one reply, not one per comment.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

# Appended to every comment the outbox posts. The comment comes back on the
# next poll as a post by the maintainer who sent it; the mark tells the agent,
# and the worker, that it is the agent's own reply and not a new request.
AGENT_MARK = "<!-- codenib-threads-agent -->"

WAITING = ("draft", "question")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Message:
    id: str
    ticket: int
    kind: str  # reply | question
    body: str
    created: str
    # draft | question | sent | discarded | superseded | answered
    status: str
    reason: str = ""
    delivery: str = ""
    sent_at: str = ""
    comment_id: int = 0
    comment_url: str = ""
    answer: str = ""

    @property
    def waiting(self) -> bool:
        return self.status in WAITING

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Message":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class Outbox:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def _path(self, ident: str) -> Path:
        if not ident or "/" in ident or ident.startswith("."):
            raise KeyError(ident)
        return self.dir / f"{ident}.json"

    def get(self, ident: str) -> Message | None:
        path = self._path(ident)
        if not path.exists():
            return None
        return Message.from_json(json.loads(path.read_text(encoding="utf-8")))

    def put(self, message: Message) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(message.id)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(message.to_json(), indent=1), encoding="utf-8")
        tmp.replace(path)

    def all(self, ticket: int | None = None) -> list[Message]:
        if not self.dir.is_dir():
            return []
        found = []
        for path in self.dir.glob("*.json"):
            message = Message.from_json(json.loads(path.read_text(encoding="utf-8")))
            if ticket is None or message.ticket == ticket:
                found.append(message)
        return sorted(found, key=lambda m: (m.created, m.id))

    def waiting(self) -> list[Message]:
        return [m for m in self.all() if m.waiting]

    def file(
        self,
        ticket: int,
        kind: str,
        body: str,
        *,
        reason: str = "",
        delivery: str = "",
        now: str = "",
    ) -> Message:
        """File a reply or question, replacing the thread's waiting one."""

        if kind not in ("reply", "question"):
            raise ValueError(f"unknown message kind {kind!r}")
        created = now or _now()
        for older in self.all(ticket):
            if older.waiting:
                older.status = "superseded"
                self.put(older)
        stamp = created.replace("-", "").replace(":", "").rstrip("Z")
        message = Message(
            id=f"{stamp}-{ticket}-{secrets.token_hex(2)}",
            ticket=ticket,
            kind=kind,
            body=body.strip(),
            created=created,
            status="draft" if kind == "reply" else "question",
            reason=reason,
            delivery=delivery,
        )
        self.put(message)
        return message

    def sent_addresses(self, repo: str, ticket: int) -> set[str]:
        """Event addresses of the comments this outbox posted on a thread."""

        from .events import event_address

        return {
            event_address(repo, ticket, "comment", m.comment_id)
            for m in self.all(ticket)
            if m.status == "sent" and m.comment_id
        }


class SendError(RuntimeError):
    pass


def post_comment(
    repo: str, number: int, body: str, *, token: str, api_url: str
) -> dict:
    """Post one issue or pull request comment and return GitHub's answer."""

    url = f"{api_url.rstrip('/')}/repos/{repo}/issues/{int(number)}/comments"
    request = urllib.request.Request(
        url,
        data=json.dumps({"body": body}).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "codenib-threads",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SendError(
            f"GitHub refused the comment on #{number}: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SendError(f"could not reach GitHub: {exc.reason}") from exc


def send(
    outbox: Outbox,
    message: Message,
    *,
    repo: str,
    token: str,
    api_url: str,
    now: str = "",
) -> Message:
    """Post a waiting reply on its thread and record where it went."""

    if message.kind != "reply" or message.status != "draft":
        raise SendError(
            f"{message.id} is a {message.status} {message.kind}; only a reply "
            "draft can be sent"
        )
    posted = post_comment(
        repo,
        message.ticket,
        f"{message.body}\n\n{AGENT_MARK}",
        token=token,
        api_url=api_url,
    )
    message.status = "sent"
    message.sent_at = now or _now()
    message.comment_id = int(posted.get("id") or 0)
    message.comment_url = str(posted.get("html_url") or "")
    outbox.put(message)
    return message


__all__ = [
    "AGENT_MARK",
    "Message",
    "Outbox",
    "SendError",
    "post_comment",
    "send",
]
