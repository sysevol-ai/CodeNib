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


def _area_by_folder(file: str, areas: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """The area listing the most files in this file's own folder, if any."""
    folder = file.rsplit("/", 1)[0] if "/" in file else ""
    best, most = None, 0
    for area in areas:
        count = sum(
            1
            for owned in area.get("files") or []  # type: ignore[union-attr]
            if (str(owned).rsplit("/", 1)[0] if "/" in str(owned) else "") == folder
        )
        if count > most:
            best, most = area, count
    return best


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
            # A file no area lists and no page cites goes with the area
            # listing the most files in its own folder: tokio's
            # ``task/spawn.rs`` with the area holding ``task/join_set.rs``.
            place = _area_of(file, areas) or places.get(file) or _area_by_folder(file, areas)
            if place:
                break
        if not place or not place.get("id") or not place.get("title"):
            continue
        title = str(place["title"]).replace("]", "")
        lines[index] = f"{head} · [{title}](?p={place['id']}){colon}{text}"
        changed = True
    return "\n".join(lines) if changed else markdown


_LINKED_STEP = re.compile(r"^\s*\d+\.\s+\*\*`[^`]+`\*\*\s*·\s*\[[^\]]*\]\(\?p=([^)\s]+)\)")


def journey_area_ids(markdown: str, top_of: Mapping[str, str]) -> list[str]:
    """The top-level areas a placed main path enters, in the order it enters them.

    ``top_of`` maps every page id to its top-level area's id. The map gives
    these areas its hues first, so the path is in colour from end to end even
    in a repository with more areas than hues.
    """
    seen: list[str] = []
    for line in markdown.split("\n"):
        match = _LINKED_STEP.match(line)
        if not match:
            continue
        top = top_of.get(match.group(1))
        if top and top not in seen:
            seen.append(top)
    return seen


def place_architecture_areas(
    slots: Sequence[Mapping[str, object]],
    citations: Sequence[Mapping[str, object] | None],
    places: Mapping[str, Mapping[str, object]],
    areas: Sequence[Mapping[str, object]] = (),
) -> list[Mapping[str, object]]:
    """Give each role of the Overview's architecture the area its source is in.

    The architecture is drawn as the main path is, a stair coloured by area,
    so each role takes the area of the first source it cites, found as a
    path step's is. A role citing only the README, or code no area holds,
    stays outside.
    """
    out: list[Mapping[str, object]] = []
    for slot in slots:
        contract = slot.get("render_contract") if isinstance(slot, Mapping) else None
        data = contract.get("data") if isinstance(contract, Mapping) else None
        nodes = data.get("nodes") if isinstance(data, Mapping) else None
        if not (
            isinstance(contract, Mapping)
            and contract.get("adapter") == "architecture"
            and isinstance(nodes, list)
        ):
            out.append(slot)
            continue
        placed = []
        for node in nodes:
            place = None
            for evidence in node.get("evidence") or [] if isinstance(node, Mapping) else []:
                match = re.fullmatch(r"E(\d+)", str(evidence))
                position = int(match.group(1)) - 1 if match else -1
                cite = citations[position] if 0 <= position < len(citations) else None
                file = str((cite or {}).get("file") or "")
                if not file:
                    continue
                place = _area_of(file, areas) or places.get(file) or _area_by_folder(file, areas)
                if place:
                    break
            if isinstance(node, Mapping) and place and place.get("id") and place.get("title"):
                node = {**node, "area": {"id": str(place["id"]), "title": str(place["title"])}}
            placed.append(node)
        out.append({**slot, "render_contract": {**contract, "data": {**data, "nodes": placed}}})
    return out
