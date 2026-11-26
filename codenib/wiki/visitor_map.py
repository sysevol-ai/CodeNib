# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""A map of a visitor's repository drawn from the code index, with no model.

Areas are folders, so drawing them needs neither an outline nor prose. Lines
are the reference edges the index recorded from one area's symbols into
another's, counted exactly as the Wiki map counts them
(``codemap.build_area_map``). The main path is the walk the Overview narrates
(``AgentWiki.graph_main_path``), taken here without any prose.

Nothing in this module downloads source, runs an indexer or calls a model;
callers hand it captured paths, a built graph and the source bundle.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

from ..log_utils import get_logger
from ..utils import is_test_file
from .importance import is_peripheral_file

MAX_AREAS = 12
MIN_AREAS = 3
TINY_SHARE = 0.03
DOMINANT_SHARE = 0.7
# The hosted graph indexes Python only. A repository counts as Python when at
# least this share of its mapped files is Python; otherwise a Python-only map
# would draw a minority of the code as if it were the whole.
GRAPH_LANGUAGE_SHARE = 0.5
GRAPH_SUFFIXES = frozenset({".py", ".pyi"})
ROOT_AREA = "root"
OTHER_AREA = "other"
logger = get_logger(__name__)


class ModelCallRefused(BaseException):
    """Raised if anything on the free map path reaches for a model.

    A ``BaseException`` so no ``except Exception`` fallback can swallow it.
    """


class _NoModel:
    """Stands in for AgentWiki's model; every use is refused loudly."""

    cache_identity = "visitor-map-no-model"

    def __getattr__(self, name: str) -> Any:
        raise ModelCallRefused(f"The free map makes no model call ({name}).")

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ModelCallRefused("The free map makes no model call.")


def mapped_file(path: str) -> bool:
    """Whether a source file belongs on the map: the project, not its tests,
    examples, docs, benchmarks, vendored code or tooling."""

    return bool(path) and not is_test_file(path) and not is_peripheral_file(path)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "area"


def folder_areas(
    paths: Iterable[str],
    *,
    repository: str = "",
    weights: Mapping[str, int] | None = None,
    pinned: Iterable[str] = (),
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Group source files into folder areas (three to twelve when possible).

    The rules, in order:

    1. Tests, examples, docs, benchmarks, vendored code and build files
       (``is_test_file``, ``is_peripheral_file``) belong to no area, so they
       draw no box and no line.
    2. A leading ``src/`` is ignored for grouping (``src/flask`` is ``flask``).
    3. While one folder holds at least 70% of the files below the current
       level, the map opens it: ``requests/``, ``packages/``, a chain of
       single folders. Files outside the opened folder are grouped by their
       own top-level folder.
    4. Each subfolder of the opened folder is one area. Files directly inside
       the opened folder form one more, titled with its path; at the
       repository root that area is ``root``, titled with the repository name.
    5. With fewer than three folder areas the package is mostly flat files
       (``requests/*.py``, ``flask/*.py``): each of its direct files becomes an
       area instead, the most weighted first, and those past twelve areas
       share one ``Other`` area. ``weights`` counts each file's definitions
       (one each when omitted); a file with none, such as an ``__init__.py``
       of imports, draws no box of its own and belongs to no area. Files in
       ``pinned`` (the main path's stops) come first, so the path does not
       disappear into ``Other``.
    6. With more than twelve areas, folders under 3% of the files merge into
       ``Other``, then the smallest, until twelve remain.

    Returns ``(file_area, areas)``: the area id of every mapped file, and the
    areas largest first as ``{id, title, path, kind, files}`` where ``kind`` is
    ``folder``, ``file``, ``root`` or ``other``, ``path`` is the folder or file
    as it appears in the repository (``None`` for ``Other``) and ``files``
    lists the member files.
    """

    def weight(file: str) -> int:
        return weights.get(file, 0) if weights else 1

    first = {path.replace("\\", "/").strip("/") for path in pinned}

    files = sorted(
        {
            path.replace("\\", "/").strip("/")
            for path in paths
            if path and mapped_file(path.replace("\\", "/"))
        }
    )
    if not files:
        return {}, []

    def grouping(path: str) -> tuple[str, ...]:
        parts = tuple(PurePosixPath(path).parts)
        return parts[1:] if parts[0] == "src" and len(parts) > 1 else parts

    parts_of = {file: grouping(file) for file in files}
    prefix: tuple[str, ...] = ()
    while True:
        inside = [
            parts for parts in parts_of.values() if parts[: len(prefix)] == prefix
        ]
        nested = Counter(
            parts[len(prefix)] for parts in inside if len(parts) > len(prefix) + 1
        )
        if not nested:
            break
        name, count = min(nested.items(), key=lambda item: (-item[1], item[0]))
        if count < DOMINANT_SHARE * len(inside):
            break
        prefix += (name,)

    def group_of(parts: tuple[str, ...]) -> tuple[str, ...]:
        depth = len(prefix)
        if parts[:depth] == prefix:
            return parts[: depth + 1] if len(parts) > depth + 1 else prefix
        return parts[:1] if len(parts) > 1 else ()

    groups: dict[tuple[str, ...], list[str]] = {}
    for file in files:
        groups.setdefault(group_of(parts_of[file]), []).append(file)

    def real_path(key: tuple[str, ...], members: list[str]) -> str:
        # Grouping dropped a leading ``src/``; the repository path keeps it.
        first = members[0]
        stripped = parts_of[first] != tuple(PurePosixPath(first).parts)
        return ("src/" if stripped else "") + "/".join(key)

    root_title = repository.replace("\\", "/").rsplit("/", 1)[-1] or "(root)"
    rows: list[dict[str, Any]] = []
    folders = [key for key in groups if key and key != prefix]
    if len(folders) < MIN_AREAS and prefix in groups:
        # A flat package: its files are the areas.
        direct = sorted(
            (file for file in groups.pop(prefix) if file in first or weight(file)),
            key=lambda file: (file not in first, -weight(file), file),
        )
        room = MAX_AREAS - len(groups)
        keep = direct if len(direct) <= room else direct[: max(0, room - 1)]
        for file in keep:
            rows.append(
                {
                    "title": "/".join(parts_of[file]),
                    "path": file,
                    "kind": "file",
                    "files": [file],
                }
            )
        rest = direct[len(keep) :]
        if rest:
            rows.append(
                {"title": "Other", "path": None, "kind": "other", "files": rest}
            )
    for key, members in groups.items():
        rows.append(
            {
                "title": "/".join(key) if key else root_title,
                "path": real_path(key, members) if key else "",
                "kind": "folder" if key else "root",
                "files": members,
            }
        )
    rows.sort(
        key=lambda row: (
            row["kind"] == "other",
            -len(row["files"]),
            -sum(weight(file) for file in row["files"]),
            row["title"],
        )
    )

    if len(rows) > MAX_AREAS:
        total = len(files)
        named = [row for row in rows if row["kind"] != "other"]
        merged = [row for row in rows if row["kind"] == "other"]
        keep = [row for row in named if len(row["files"]) >= TINY_SHARE * total]
        merged += [row for row in named if len(row["files"]) < TINY_SHARE * total]
        if len(keep) + 1 > MAX_AREAS:
            merged += keep[MAX_AREAS - 1 :]
            keep = keep[: MAX_AREAS - 1]
        other = sorted(file for row in merged for file in row["files"])
        rows = [
            *keep,
            {"title": "Other", "path": None, "kind": "other", "files": other},
        ]

    # A folder named ``other`` or ``root`` must not take the special ids.
    special = {"root": ROOT_AREA, "other": OTHER_AREA}
    used: set[str] = set(special.values())
    file_area: dict[str, str] = {}
    areas: list[dict[str, Any]] = []
    for row in rows:
        area_id = special.get(row["kind"])
        if area_id is None:
            base = _slug(row["title"])
            area_id, suffix = base, 2
            while area_id in used:
                area_id, suffix = f"{base}-{suffix}", suffix + 1
            used.add(area_id)
        for file in row["files"]:
            file_area[file] = area_id
        areas.append({"id": area_id, **row})
    return file_area, areas


def graph_language_share(file_area: Mapping[str, str]) -> float:
    """The share of mapped files the hosted graph can index (Python)."""

    if not file_area:
        return 0.0
    graphed = sum(PurePosixPath(file).suffix in GRAPH_SUFFIXES for file in file_area)
    return graphed / len(file_area)


def outline_map(
    areas: list[dict[str, Any]],
    reason: str,
    weights: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The folder areas alone, for a map the graph cannot draw lines on.

    ``symbols`` here counts the definitions the parser found in each area's
    files (``weights``), since there is no index to count from.
    """

    weights = weights or {}
    return {
        "available": False,
        "reason": reason,
        "areas": [
            {
                "id": area["id"],
                "title": area["title"],
                "path": area["path"],
                "kind": area["kind"],
                "symbols": sum(weights.get(file, 0) for file in area["files"]),
                "files": len(area["files"]),
            }
            for area in areas
        ],
        "links": [],
    }


def area_links(
    graph: Any,
    file_area: Mapping[str, str],
    areas: list[dict[str, Any]] | None = None,
    *,
    repo_dir: str | None = None,
    source_reader: Any = None,
) -> dict[str, Any]:
    """Lines between folder areas: the references the index recorded.

    The counting is the Wiki map's own (``codemap.build_area_map``): a link's
    ``weight`` is the reference edges from the source area's symbols into the
    target area, ``calls`` the distinct caller/callee pairs, self-links and
    test files are left out, and each link carries one example with a 1-based
    anchor line. Areas keep their ``path`` and ``kind``.
    """

    from ..web.codemap import build_area_map

    rows = list(areas or [])
    if not rows:
        ids = list(dict.fromkeys(file_area.values()))
        rows = [{"id": area_id, "title": area_id} for area_id in ids]
    members: dict[str, list[str]] = {}
    for file, area_id in file_area.items():
        members.setdefault(area_id, []).append(file)
    result = build_area_map(
        graph,
        [
            {
                "id": row["id"],
                "title": row.get("title") or row["id"],
                "files": sorted(members.get(row["id"], [])),
            }
            for row in rows
        ],
        repo_dir=repo_dir,
        source_reader=source_reader,
    )
    extra = {row["id"]: row for row in rows}
    for area in result.get("areas", []):
        row = extra.get(area["id"], {})
        area["path"] = row.get("path")
        area["kind"] = row.get("kind", "folder")
    return result


def readable_symbol(label: str, file: str = "") -> str:
    """``requests/sessions.py:Session.send()`` -> ``Session.send()``."""

    text = str(label or "")
    if file and text.startswith(f"{file}:"):
        return text[len(file) + 1 :]
    head, separator, rest = text.partition(":")
    if separator and rest and not rest.startswith(":") and ("/" in head or "." in head):
        return rest
    return text


def _call_site(anchor: str) -> dict[str, Any] | None:
    file, separator, line = str(anchor or "").rpartition(":")
    if not separator or not file or not line.isdigit() or int(line) < 1:
        return None
    return {"file": file, "line": int(line)}


def main_path(
    bundle: Any, file_area: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """The main call path the graph records, with no model call.

    The Overview's own walk (``AgentWiki.graph_main_path``): start where the
    README, the manifests or a program point, then follow the recorded call
    that reaches furthest, up to five hops. Each stop is ``{symbol, kind, file,
    line, area, call}`` with a 1-based ``line``; ``call`` is the 1-based call
    site in the previous stop that leads to it (``None`` for the first stop or
    when unknown) and ``area`` is ``None`` for a file outside every area.
    Returns None when the graph gives no start or the path is shorter than
    three stops.
    """

    from .agent_wiki import AgentWiki

    try:
        wiki = AgentWiki(bundle, "none", llm=_NoModel())
        items, relations, path = wiki.graph_main_path()
    except Exception as exc:  # noqa: BLE001 - the map stands without a path
        logger.warning("Visitor map main path unavailable (%s)", type(exc).__name__)
        return None
    if not path:
        return None
    file_area = file_area or {}
    evidence = {item.id: item for item in items}
    calls = {relation.id: relation for relation in relations}
    stops = []
    for step in path:
        item = evidence.get(step.get("evidence"))
        if item is None or not isinstance(item.start_line, int):
            return None
        relation = calls.get(step.get("relation"))
        stops.append(
            {
                "symbol": readable_symbol(item.symbol, item.file),
                "kind": item.kind,
                "file": item.file,
                "line": item.start_line,
                "area": file_area.get(item.file),
                "call": (
                    _call_site(relation.anchors[0])
                    if relation is not None and relation.anchors
                    else None
                ),
            }
        )
    return {
        "title": f"From {stops[0]['symbol']} to {stops[-1]['symbol']}",
        "stops": stops,
    }
