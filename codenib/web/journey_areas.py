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

from ..ops.filter import is_test_path
from ..wiki.importance import is_detail_step, is_side_road

_STEP = re.compile(r"^(\s*\d+\.\s+\*\*`[^`]+`\*\*)(\s*:\s*)(.*)$")
_EVIDENCE = re.compile(r"\(#evidence-E(\d+)\)")


def _area_of(
    file: str, areas: Sequence[Mapping[str, object]]
) -> Mapping[str, object] | None:
    """The top-level area whose outline holds the file, or a folder above it."""
    for area in areas:
        for owned in area.get("files") or []:  # type: ignore[union-attr]
            owned = str(owned).rstrip("/")
            if owned and (file == owned or file.startswith(owned + "/")):
                return area
    return None


def _area_by_folder(
    file: str, areas: Sequence[Mapping[str, object]]
) -> Mapping[str, object] | None:
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
            place = (
                _area_of(file, areas)
                or places.get(file)
                or _area_by_folder(file, areas)
            )
            if place:
                break
        if not place or not place.get("id") or not place.get("title"):
            continue
        title = str(place["title"]).replace("]", "")
        lines[index] = f"{head} · [{title}](?p={place['id']}){colon}{text}"
        changed = True
    return "\n".join(lines) if changed else markdown


_LINKED_STEP = re.compile(
    r"^\s*\d+\.\s+\*\*`[^`]+`\*\*\s*·\s*\[[^\]]*\]\(\?p=([^)\s]+)\)"
)


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


_TITLE_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "api",
        "code",
        "core",
        "data",
        "for",
        "from",
        "in",
        "into",
        "layer",
        "model",
        "of",
        "on",
        "or",
        "system",
        "the",
        "to",
        "user",
        "with",
    }
)
_SUFFIXES = ("ings", "ing", "ers", "er", "ions", "ion", "ed", "es", "s")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _title_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {_stem(word) for word in words if word not in _TITLE_STOP and len(word) > 2}


def _area_by_title(
    label: str, areas: Sequence[Mapping[str, object]]
) -> Mapping[str, object] | None:
    """The one area a role is named after.

    A role and an area match when they share two words, or when one's words
    all appear in the other's: ``Renderer`` and Rendering Backends,
    ``Persistence and Replication`` and Replication, ``Local Time Series
    Storage`` and Time Series Database. One shared word is not enough
    (``Loss and Gradient Computation`` is not Gradient Boosting), and a tie
    names none: no colour is better than a guess.
    """
    own = _title_words(label)
    if not own:
        return None
    scored = []
    for index, area in enumerate(areas):
        best = 0
        # The area's own title and the titles of the pages inside it.
        for text in [area.get("title"), *(area.get("titles") or [])]:  # type: ignore[misc]
            title = _title_words(str(text or ""))
            shared = own & title
            if len(shared) >= 2 or (shared and (own <= title or title <= own)):
                best = max(best, len(shared))
        if best:
            scored.append((best, index))
    scored.sort(reverse=True)
    if not scored or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return areas[scored[0][1]]


def _area_by_folder_word(
    label: str, areas: Sequence[Mapping[str, object]]
) -> Mapping[str, object] | None:
    """The area holding the folder a role is named after.

    A role that cites no code is often named for a folder all the same:
    prometheus' Rule Evaluation cites only the README, and ``rules/`` holds
    the rule manager. The area holding the shallowest folder named with one
    of the role's words takes it, so terraform's Providers is
    ``internal/providers``, not ``internal/builtin/providers``; a tie names
    none.
    """
    own = _title_words(label)
    if not own:
        return None
    depths: list[tuple[int, int]] = []
    for index, area in enumerate(areas):
        best = None
        for owned in area.get("files") or []:  # type: ignore[union-attr]
            parts = str(owned).strip("/").split("/")
            folders = parts[:-1] if "." in parts[-1] else parts
            for depth, folder in enumerate(folders):
                if own & _title_words(folder):
                    best = depth if best is None else min(best, depth)
                    break
        if best is not None:
            depths.append((best, index))
    depths.sort()
    if not depths or (len(depths) > 1 and depths[1][0] == depths[0][0]):
        return None
    return areas[depths[0][1]]


def _cited_files(
    node: Mapping[str, object], citations: Sequence[Mapping[str, object] | None]
) -> list[tuple[str, str]]:
    """A role's cited sources as (evidence id, file), tests and READMEs last."""
    out = []
    for evidence in node.get("evidence") or []:  # type: ignore[union-attr]
        match = re.fullmatch(r"E(\d+)", str(evidence))
        position = int(match.group(1)) - 1 if match else -1
        cite = citations[position] if 0 <= position < len(citations) else None
        file = str((cite or {}).get("file") or "")
        if file:
            out.append((str(evidence), file))
    return sorted(out, key=lambda item: (is_test_path(item[1]) or _is_readme(item[1])))


def _is_readme(file: str) -> bool:
    return bool(re.search(r"(^|/)readme[^/]*$", file, re.IGNORECASE))


def place_architecture_areas(
    slots: Sequence[Mapping[str, object]],
    citations: Sequence[Mapping[str, object] | None],
    places: Mapping[str, Mapping[str, object]],
    areas: Sequence[Mapping[str, object]] = (),
) -> list[Mapping[str, object]]:
    """Give each role of the Overview's architecture its area on the map.

    The architecture is drawn as the main path is, a stair coloured by area.
    A role whose name matches one area's title takes that area (the plan
    names roles after the parts the map draws); otherwise it takes the area
    of the first code it cites, a test's or a README's only when nothing
    else is cited. The source that placed it is recorded, so the step shows
    the citation its colour comes from. A role citing only the README, or
    code no area holds, takes the area holding the folder it is named after
    (``rules/`` for Rule Evaluation); otherwise, or when outside the code,
    it stays outside.
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

        # Each step shows a source of its own. Steps list the sources that
        # could place them; the steps with fewest claim first, so one with a
        # single source keeps it (vue's Renderer keeps renderer.ts:347 and
        # Host Adapters, which also cites it, shows runtime-dom).
        def area_of_file(file: str):
            return (
                _area_of(file, areas)
                or places.get(file)
                or _area_by_folder(file, areas)
            )

        plans = []
        for node in nodes:
            if not isinstance(node, Mapping):
                plans.append(None)
                continue
            usable = [
                (evidence, file)
                for evidence, file in _cited_files(node, citations)
                if not (is_test_path(file) or _is_readme(file))
            ]
            # A part outside the code (the user's program, a client) is
            # never placed by its name.
            named = (
                None
                if node.get("kind") == "external"
                else _area_by_title(str(node.get("label") or ""), areas)
            )
            if named is not None:
                candidates = [
                    (evidence, named)
                    for evidence, file in usable
                    if (_area_of(file, areas) or _area_by_folder(file, areas)) is named
                ]
            else:
                candidates = [
                    (evidence, place)
                    for evidence, file in usable
                    if (place := area_of_file(file))
                ]
            plans.append((named, candidates))
        assigned: dict[int, tuple[str, Mapping[str, object]]] = {}
        used: set[str] = set()
        # The steps the waterfall draws claim before the parts listed beside
        # it (vue's VNode Tree must not take the Renderer's one source).
        on_path = {str(item) for item in (data.get("primary_path") or [])}

        def affinity(index: int) -> int:
            # How well a step's name fits the code it cites: valkey's Command
            # Dispatcher, not Client Connections, takes processCommand.
            label = _title_words(str((nodes[index] or {}).get("label") or ""))
            best = 0
            for evidence, _place in plans[index][1]:
                match = re.fullmatch(r"E(\d+)", evidence)
                position = int(match.group(1)) - 1 if match else -1
                cite = citations[position] if 0 <= position < len(citations) else None
                name = str((cite or {}).get("node_name") or "")
                words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ")
                best = max(best, len(label & _title_words(words)))
            return best

        order = sorted(
            (index for index, plan in enumerate(plans) if plan and plan[1]),
            key=lambda index: (
                str((nodes[index] or {}).get("id")) not in on_path,
                len(plans[index][1]),
                -affinity(index),
                index,
            ),
        )
        for index in order:
            for evidence, place in plans[index][1]:
                if evidence not in used:
                    assigned[index] = (evidence, place)
                    used.add(evidence)
                    break
        placed = []
        for index, node in enumerate(nodes):
            plan = plans[index]
            if plan is None:
                placed.append(node)
                continue
            named, candidates = plan
            source, place = assigned.get(index, (None, None))
            if named is not None:
                place = named
            elif place is None and candidates:
                # Every source it could show is shown by another step: the
                # colour holds, the citation is not repeated.
                place = candidates[0][1]
            elif place is None and node.get("kind") != "external":
                # No code it cites places it: the folder it is named after.
                place = _area_by_folder_word(str(node.get("label") or ""), areas)
            if place and place.get("id") and place.get("title"):
                area = {"id": str(place["id"]), "title": str(place["title"])}
                if source:
                    area["evidence"] = source
                node = {**node, "area": area}
            placed.append(node)
        out.append(
            {**slot, "render_contract": {**contract, "data": {**data, "nodes": placed}}}
        )
    return out


_JOURNEY_TITLE = re.compile(r"^## From `[^`]+` to `[^`]+`\s*$")
_STAGE_ENTITY = re.compile(r"^\s*\d+\.\s+\*\*`([^`]+)`\*\*")


def sync_journey_title(markdown: str) -> str:
    """Make a "From `a` to `b`" heading name the first and last steps under it.

    The title is written before the path's tail is cut, so a cached page can
    name a step its list no longer shows (smolagents' title ended on a
    method the path had dropped).
    """
    lines = markdown.split("\n")
    for index, line in enumerate(lines):
        if not _JOURNEY_TITLE.match(line):
            continue
        entities = []
        for following in lines[index + 1 :]:
            if following.startswith("## "):
                break
            match = _STAGE_ENTITY.match(following)
            if match:
                entities.append(match.group(1))
        if len(entities) >= 2:
            lines[index] = f"## From `{entities[0]}` to `{entities[-1]}`"
        break
    return "\n".join(lines)


def trim_path_tail(markdown: str) -> str:
    """Drop a cached path's trailing getter, check or set-up step.

    A path written before the path rule cut such tails ends on a detail
    (OrcaLoca's ``SearchInput.get_content()``); the step before it is
    where the work lands. Three steps always stay.
    """
    lines = markdown.split("\n")
    for index, line in enumerate(lines):
        if not _JOURNEY_TITLE.match(line):
            continue
        steps = []
        for position in range(index + 1, len(lines)):
            if lines[position].startswith("## "):
                break
            match = _STAGE_ENTITY.match(lines[position])
            if match:
                steps.append((position, match.group(1)))
        dropped = set()
        while len(steps) > 3 and (
            is_detail_step(steps[-1][1]) or is_side_road(steps[-1][1])
        ):
            dropped.add(steps.pop()[0])
        if dropped:
            lines = [
                line for position, line in enumerate(lines) if position not in dropped
            ]
        break
    return "\n".join(lines)
