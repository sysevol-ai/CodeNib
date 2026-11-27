# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Name the area of each Overview main-path step that the writer left unnamed.

The Overview lists its main path as numbered steps, ``N. **`symbol`** ·
[Area](?p=id): sentence``. The writer names a step's area only sometimes, so
the reader saw a path half in colour and half grey. A step's citation names
the file its code is in, and the Wiki's own pages cite files: the page that
cites the step's file is where that code is told. That page is named here,
only where the writer named none.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

_STEP = re.compile(r"^(\s*\d+\.\s+\*\*`[^`]+`\*\*)(\s*:\s*)(.*)$")
_EVIDENCE = re.compile(r"\(#evidence-E(\d+)\)")


def _area_of(file: str, areas: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """The top-level area whose outline holds the file, or a folder above it."""
    for area in areas:
        for owned in area.get("files") or []:  # type: ignore[union-attr]
            owned = str(owned).rstrip("/")
            if owned and (file == owned or file.startswith(owned + "/")):
                return area
    return None


def place_journey_areas(
    markdown: str,
    citations: Sequence[Mapping[str, object] | None],
    places: Mapping[str, Mapping[str, object]],
    areas: Sequence[Mapping[str, object]] = (),
) -> str:
    """Add ``· [Title](?p=id)`` to unnamed steps from their cited file.

    ``areas`` are the outline's top-level areas with the files each holds, as
    the map draws them; ``places`` maps a file to the first page, in outline
    order, that cites it, for files no area holds. A step that already names
    its area, or whose sources neither knows, is left as written.
    """
    if not places and not areas:
        return markdown
    lines = markdown.split("\n")
    changed = False
    for index, line in enumerate(lines):
        match = _STEP.match(line)
        if not match:
            continue
        head, colon, text = match.groups()
        place = None
        for number in _EVIDENCE.findall(text):
            position = int(number) - 1
            cite = citations[position] if 0 <= position < len(citations) else None
            file = str((cite or {}).get("file") or "")
            if not file:
                continue
            place = _area_of(file, areas) or places.get(file)
            if place:
                break
        if not place or not place.get("id") or not place.get("title"):
            continue
        title = str(place["title"]).replace("]", "")
        lines[index] = f"{head} · [{title}](?p={place['id']}){colon}{text}"
        changed = True
    return "\n".join(lines) if changed else markdown
