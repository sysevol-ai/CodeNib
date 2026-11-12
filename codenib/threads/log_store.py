# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""File store for thread logs: one JSONL log and one header per thread.

Logs live under ``<codenib home>/threads/<owner>/<repo>/`` and nothing is
written into a checkout. A log is append-only. Syncing a thread twice changes
nothing; an edited comment arrives as a second event with the same address and
a new digest, and the latest one is what readers see.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..paths import user_state_dir
from .events import ThreadEvent, ThreadHeader

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def default_threads_root() -> Path:
    return user_state_dir() / "threads"


class ThreadStore:
    def __init__(self, repo: str, root: Path | None = None) -> None:
        if not _REPO_RE.match(repo) or ".." in repo:
            raise ValueError(f"not an owner/repo name: {repo!r}")
        self.repo = repo
        self.dir = (root or default_threads_root()) / repo

    def _log(self, number: int) -> Path:
        return self.dir / f"{int(number)}.jsonl"

    def _header(self, number: int) -> Path:
        return self.dir / f"{int(number)}.header.json"

    def has(self, number: int) -> bool:
        return self._log(number).exists() and self._header(number).exists()

    def numbers(self) -> list[int]:
        if not self.dir.is_dir():
            return []
        return sorted(int(p.stem) for p in self.dir.glob("*.jsonl") if p.stem.isdigit())

    def append(self, number: int, events: list[ThreadEvent]) -> int:
        """Append events not already stored. Returns how many were written."""

        seen = {(e.address, e.digest) for e in self._read(number)}
        fresh = [e for e in events if (e.address, e.digest) not in seen]
        if not fresh:
            return 0
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._log(number).open("a", encoding="utf-8") as handle:
            for event in fresh:
                handle.write(json.dumps(event.to_json(), sort_keys=True) + "\n")
        return len(fresh)

    def write_header(self, header: ThreadHeader) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._header(header.number)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(header.to_json(), sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def header(self, number: int) -> ThreadHeader | None:
        path = self._header(number)
        if not path.exists():
            return None
        return ThreadHeader.from_json(json.loads(path.read_text(encoding="utf-8")))

    def _read(self, number: int) -> list[ThreadEvent]:
        path = self._log(number)
        if not path.exists():
            return []
        events = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(ThreadEvent.from_json(json.loads(line)))
        return events

    def events(self, number: int) -> list[ThreadEvent]:
        """Current events in time order; a superseded edit is dropped."""

        latest: dict[str, ThreadEvent] = {}
        for event in self._read(number):
            latest[event.address] = event
        return sorted(latest.values(), key=lambda e: (e.at, e.address))


__all__ = ["ThreadStore", "default_threads_root"]
