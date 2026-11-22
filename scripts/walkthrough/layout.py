# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Place a walkthrough on the Wiki: areas, stops, crossings and citations.

Everything here is deterministic. The Wiki's top pages are its areas, each
owning the files its pages cover. A walk's stops are its functions; a
crossing is a hop from a file in one area into a file in another, counted
against the calls the Wiki map records in that direction. Citations say
which Wiki pages cite each function, so a step can link to them.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

AREA_COLORS = [
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#e66767",
]
OTHER_AREA_COLOR = "#80868b"


def base(rel: str) -> str:
    return rel.split("/")[-1]


def short_names(files: list[str]) -> dict[str, str]:
    """Each file's shortest trailing path that no other file shares.

    Usually the base name; ``runtime/mod.rs`` and ``task/mod.rs`` keep a
    folder, so two files never share a zone, a stop label or a link.
    """

    unique = list(dict.fromkeys(files))
    out = {}
    for f in unique:
        parts = f.split("/")
        for k in range(1, len(parts) + 1):
            tail = "/".join(parts[-k:])
            if not any(
                g != f and (g == tail or g.endswith("/" + tail)) for g in unique
            ):
                break
        out[f] = tail
    return out


def files_of(page: dict):
    yield from page.get("files") or []
    for ch in page.get("children") or []:
        yield from files_of(ch)


def outline_areas(outline: dict) -> tuple[list[dict], dict[str, str]]:
    """The Wiki's top pages as areas, and the area that owns each file."""

    tops = [p for p in outline["pages"] if p.get("id") != "overview"]
    area_of_file: dict[str, str] = {}
    areas = []
    for k, p in enumerate(tops):
        for f in sorted(set(files_of(p))):
            area_of_file.setdefault(f, p["id"])
        short = re.split(r",| and | & ", p["title"])[0].strip()
        color = AREA_COLORS[k] if k < len(AREA_COLORS) else OTHER_AREA_COLOR
        areas.append(
            {"id": p["id"], "title": p["title"], "name": short, "color": color}
        )
    return areas, area_of_file


def wiki_pages(pages_tree: list[dict]) -> dict[str, dict]:
    """Every Wiki page but the Overview: its title, top page and depth."""

    pages: dict[str, dict] = {}

    def walk(ps: list[dict], top: str | None = None, depth: int = 0) -> None:
        for p in ps:
            if p["id"] == "overview":
                continue
            t = top or p["id"]
            pages[p["id"]] = {"title": p["title"], "top": t, "depth": depth}
            walk(p.get("children") or [], t, depth + 1)

    walk(pages_tree)
    return pages


@dataclass
class RepoMap:
    """What every walk of one repository shares: areas, map counts, citations."""

    areas: list[dict]
    area_of_file: dict[str, str]
    size: dict[str, list[int]]
    calls: dict[str, int]
    links: list[list]
    pages: dict[str, dict]
    cites: dict[str, set[str]]

    @classmethod
    def build(
        cls, outline: dict, wiki_map: dict, pages: dict[str, dict], cites: dict
    ) -> "RepoMap":
        areas, area_of_file = outline_areas(outline)
        size = {a["id"]: [a["symbols"], a["files"]] for a in wiki_map["areas"]}
        for a in areas:
            a["symbols"], a["files"] = size.get(a["id"], [0, 0])
        calls = {
            f"{lk['source']}>{lk['target']}": lk["calls"] for lk in wiki_map["links"]
        }
        links = [
            [lk["source"], lk["target"], lk["calls"]]
            for lk in wiki_map["links"]
            if lk["calls"] >= 10
        ]
        return cls(areas, area_of_file, size, calls, links, pages, cites)

    def wiki(self) -> dict:
        cites = {k: sorted(v) for k, v in self.cites.items()}
        return {
            "pages": self.pages,
            "cites": cites,
            "size": self.size,
            "calls": self.calls,
        }


def _get(url: str) -> dict:
    return json.load(urllib.request.urlopen(url, timeout=120))


def read_repo_map(outline: dict, api: str, repo_id: str) -> RepoMap:
    """Read the Wiki map and every page's citations from a running backend."""

    root = f"{api.rstrip('/')}/api/repos/{repo_id}"
    wiki_map = _get(root + "/wiki-map")
    pages = wiki_pages(_get(root + "/wiki")["pages"])
    cites: dict[str, set[str]] = {}
    for pid in pages:
        try:
            d = _get(root + "/wiki/" + urllib.parse.quote(pid))
        except Exception:
            continue
        for c in d.get("citations") or []:
            n = (c.get("node_name") or "").split(":")[-1]
            if n:
                cites.setdefault(n, set()).add(pid)
    return RepoMap.build(outline, wiki_map, pages, cites)


def walk_layout(path: list[dict], entry_call: str, rmap: RepoMap) -> dict:
    """A walk's stops, call sites (segs), file zones, crossings, areas and paths.

    ``stops[k][3]`` (the stop's role) is left empty for the step text.
    """

    area_of_file = rmap.area_of_file
    fns = [p["fn"] for p in path]
    short = short_names([f["file"] for f in fns])
    stops = [["", entry_call, None, None, True]]
    for f in fns:
        owner, _, member = f["name"].rpartition(".")
        where = f"{short[f['file']]}:{f['a']}–{f['b']}"
        stops.append([owner, ("." if owner else "") + member, where, ""])
    segs = [["", ""]]
    zones, cross = [], []
    for i, p in enumerate(path[1:], start=1):
        prev = path[i - 1]["fn"]
        via = p["via"]
        site = f"{short[prev['file']]}:{via['line'] or prev['a']}"
        a1, a2 = area_of_file.get(prev["file"]), area_of_file.get(p["fn"]["file"])
        kind = "dash" if via["kind"] != "call" else ("cross" if a1 != a2 else "")
        label = {"subclass": "subclass", "unrecorded": "not in the index"}.get(
            via["kind"], ""
        )
        seg = [(label + " " if label else "") + f":{via['line'] or prev['a']}", site]
        seg += [kind, label]
        if via["kind"] == "unrecorded":
            seg.append(via.get("how", "attr"))
        segs.append(seg)
        if a1 and a2 and a1 != a2:
            cross.append([a1, a2, rmap.calls.get(f"{a1}>{a2}", 0), i + 1, via["kind"]])
    for i, f in enumerate(fns, start=1):
        fb = short[f["file"]]
        area = area_of_file.get(f["file"])
        area_name = next((a["name"] for a in rmap.areas if a["id"] == area), "no area")
        if zones and zones[-1][0] == fb:
            zones[-1][2].append(i)
        else:
            zones.append([fb, area_name, [i]])
    walk_areas = []
    for f in fns:
        a = area_of_file.get(f["file"])
        if a and a not in walk_areas:
            walk_areas.append(a)
    paths = {short[f["file"]]: f["file"] for f in fns}
    return {
        "stops": stops,
        "segs": segs,
        "zones": zones,
        "cross": cross,
        "areas": walk_areas,
        "paths": paths,
        "map": map_sentence(cross, walk_areas, rmap.areas),
    }


def map_sentence(cross: list[list], walk_areas: list[str], areas: list[dict]) -> str:
    """One sentence on where the path runs on the Wiki map."""

    an = {a["id"]: a["name"] for a in areas}
    if not cross:
        if not walk_areas:
            return "The path runs outside the Wiki map."
        return f"Every step stays inside {an.get(walk_areas[0], 'one area')}."
    parts = [f"The path starts in {an[walk_areas[0]]}"]
    for _a1, a2, n, at, how in cross:
        if how == "call":
            counted = f", one of {n} recorded calls in that direction" if n else ""
            parts.append(f"crosses into {an[a2]} at step {at}" + counted)
        else:
            what = (
                "a call the index does not record"
                if how == "unrecorded"
                else "a subclass override"
            )
            counted = (
                f"; the map records {n} other calls in that direction" if n else ""
            )
            parts.append(
                f"and crosses into {an[a2]} at step {at} through {what}" + counted
            )
    return ", ".join(parts) + "."


def walk_source(line, path: list[dict]) -> dict[str, dict[str, str]]:
    """Every line of every function on the path, by file and line number.

    ``line(rel, n)`` reads one source line.
    """

    source: dict[str, dict[str, str]] = {}
    for p in path:
        f = p["fn"]
        source.setdefault(f["file"], {})
        for n in range(f["a"], f["b"] + 1):
            source[f["file"]][str(n)] = line(f["file"], n)
    return source
