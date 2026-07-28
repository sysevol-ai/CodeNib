# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Append-only, transport-neutral messages addressed to Repository Guardian."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

MAX_MESSAGE_CHARS = 16_000
MAX_SCOPE_ITEMS = 32
MAX_SCOPE_CHARS = 512


def _head(repo_path: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class GuardianMessage:
    """One untrusted external perspective, stamped by the receiving runtime."""

    id: str
    sequence: int
    sender: str
    content: str
    observed_head: str
    created_at: str
    scope: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "GuardianMessage":
        return cls(
            id=str(raw["id"]),
            sequence=int(raw["sequence"]),
            sender=str(raw["sender"]),
            content=str(raw["content"]),
            observed_head=str(raw.get("observed_head", "")),
            created_at=str(raw["created_at"]),
            scope=[str(item) for item in raw.get("scope", [])],
        )


class MessageInbox:
    """A JSONL journal shared by filesystem and MCP transports."""

    def __init__(self, path: str, *, repo_path: str) -> None:
        self.path = Path(path)
        self.repo_path = repo_path

    @staticmethod
    def _normalize_scope(scope: Optional[Iterable[str]]) -> list[str]:
        values = [str(item).strip() for item in (scope or []) if str(item).strip()]
        if len(values) > MAX_SCOPE_ITEMS:
            raise ValueError(f"scope accepts at most {MAX_SCOPE_ITEMS} entries")
        if any(len(item) > MAX_SCOPE_CHARS for item in values):
            raise ValueError(
                f"each scope entry accepts at most {MAX_SCOPE_CHARS} characters"
            )
        return values

    def append(
        self,
        content: str,
        *,
        sender: str,
        scope: Optional[Iterable[str]] = None,
    ) -> GuardianMessage:
        normalized = str(content).strip()
        if not normalized:
            raise ValueError("message must not be empty")
        if len(normalized) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message exceeds the {MAX_MESSAGE_CHARS}-character limit")
        sender = str(sender).strip()
        if not sender:
            raise ValueError("sender must not be empty")
        normalized_scope = self._normalize_scope(scope)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            sequence = 1
            for line in handle:
                try:
                    sequence = max(
                        sequence, int(json.loads(line).get("sequence", 0)) + 1
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            message = GuardianMessage(
                id=f"msg_{sequence:08d}",
                sequence=sequence,
                sender=sender,
                content=normalized,
                observed_head=_head(self.repo_path),
                created_at=datetime.now(timezone.utc).isoformat(),
                scope=normalized_scope,
            )
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(message.to_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return message

    def read_recent(self, *, limit: int = 20) -> list[GuardianMessage]:
        if not self.path.exists():
            return []
        limit = max(1, min(int(limit), 100))
        messages: list[GuardianMessage] = []
        with self.path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            for line in handle:
                try:
                    messages.append(GuardianMessage.from_dict(json.loads(line)))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return messages[-limit:]


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Send an untrusted message to Guardian."
    )
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--scope", action="append", default=[])
    args = parser.parse_args(argv)
    content = sys.stdin.read()
    message = MessageInbox(args.inbox, repo_path=args.repo).append(
        content,
        sender="solver:filesystem",
        scope=args.scope,
    )
    print(json.dumps(message.to_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
