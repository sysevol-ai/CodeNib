# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Append-only, untrusted messages addressed to Guardian."""

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


@dataclass(frozen=True, slots=True)
class GuardianMessage:
    id: str
    sequence: int
    sender: str
    content: str
    observed_head: str
    created_at: str
    scope: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "GuardianMessage":
        scope = raw.get("scope", [])
        return cls(
            id=str(raw["id"]),
            sequence=int(raw["sequence"]),
            sender=str(raw["sender"]),
            content=str(raw["content"]),
            observed_head=str(raw.get("observed_head", "")),
            created_at=str(raw["created_at"]),
            scope=[str(item) for item in scope] if isinstance(scope, list) else [],
        )


class MessageInbox:
    def __init__(self, path: str | Path, *, repo_path: str | Path) -> None:
        self.path = Path(path)
        self.repo_path = str(repo_path)

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
        messages: list[GuardianMessage] = []
        with self.path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            for line in handle:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        messages.append(GuardianMessage.from_dict(value))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return messages[-max(1, min(int(limit), 100)) :]


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--scope", action="append", default=[])
    args = parser.parse_args(argv)
    message = MessageInbox(args.inbox, repo_path=args.repo).append(
        sys.stdin.read(), sender="solver:filesystem", scope=args.scope
    )
    print(json.dumps(message.to_dict(), sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["GuardianMessage", "MessageInbox"]
