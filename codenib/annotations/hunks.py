# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A change's diff as hunks a person accepts or discards one at a time.

``parse_patch`` splits a ``git diff`` into files and hunks, each with a stable
id ``<file>.<hunk>``. ``patch_for`` rebuilds a patch holding only the chosen
hunks, which ``git apply --recount`` applies even when earlier hunks of the
same file were left out. A file with no text hunks (a binary file, a mode
change, a rename) is one unit, id ``<file>.0``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


@dataclass
class Hunk:
    id: str
    path: str
    header: str
    lines: List[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for line in self.lines if line.startswith("+"))

    @property
    def deleted(self) -> int:
        return sum(1 for line in self.lines if line.startswith("-"))

    def to_json(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "path": self.path,
            "header": self.header,
            "added": self.added,
            "deleted": self.deleted,
        }


@dataclass
class FileDiff:
    path: str
    head: List[str]
    hunks: List[Hunk]


def _path_of(head: List[str]) -> str:
    for line in head:
        if line.startswith("+++ b/"):
            return line[6:]
    for line in head:
        if line.startswith("--- a/"):
            return line[6:]
    first = head[0] if head else ""
    match = re.match(r"^diff --git a/(.*) b/(.*)$", first)
    return match.group(2) if match else first


def parse_patch(patch: str) -> List[FileDiff]:
    files: List[FileDiff] = []
    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    current_head: List[str] = []
    current_hunks: List[List[str]] = []

    def close() -> None:
        if not current_head:
            return
        index = len(files)
        path = _path_of(current_head)
        if current_hunks:
            hunks = [
                Hunk(id=f"{index}.{n}", path=path, header=body[0], lines=body[1:])
                for n, body in enumerate(current_hunks)
            ]
            head = list(current_head)
        else:
            # No text hunks: the whole file entry is one unit.
            hunks = [
                Hunk(id=f"{index}.0", path=path, header="", lines=current_head[1:])
            ]
            head = current_head[:1]
        files.append(FileDiff(path=path, head=head, hunks=hunks))

    for line in lines:
        if line.startswith("diff --git "):
            close()
            current_head = [line]
            current_hunks = []
        elif _HUNK.match(line) and current_head:
            current_hunks.append([line])
        elif current_hunks:
            current_hunks[-1].append(line)
        elif current_head:
            current_head.append(line)
    close()
    return files


def all_hunks(files: Iterable[FileDiff]) -> List[Hunk]:
    return [hunk for diff in files for hunk in diff.hunks]


def patch_for(files: Iterable[FileDiff], ids: Iterable[str]) -> str:
    """A patch with only the hunks named, in their original order."""

    wanted = set(ids)
    out: List[str] = []
    for diff in files:
        chosen = [hunk for hunk in diff.hunks if hunk.id in wanted]
        if not chosen:
            continue
        out.extend(diff.head)
        for hunk in chosen:
            if hunk.header:
                out.append(hunk.header)
            out.extend(hunk.lines)
    return "\n".join(out) + "\n" if out else ""
