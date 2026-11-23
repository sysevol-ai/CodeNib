# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A walkthrough of the path a Wiki's Overview already shows.

The Overview traces one main path through the code index, from where a user's
code enters the repository to where the work is done. That path needs no
curated question: the walk follows the same recorded path (traced again from
the index, so a stage the Overview could not narrate is still a step), and
each hop is the recorded call between two of its functions.
The model then writes the question the path answers, what the user's own code
writes to start it, and a short tab label; those are checked, and replaced by
plain wording if they fail twice.

Only a path the index records gets a walkthrough. When the index has no main
path, the Overview's journey (if any) was chosen by the model, and when the
Overview starts somewhere else than the recorded path does, the page predates
the path it should show; either way the repository is skipped.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import TYPE_CHECKING

from scripts.walkthrough.graph_paths import leg
from scripts.walkthrough.progress import say

if TYPE_CHECKING:
    from scripts.walkthrough.model import Model
    from scripts.walkthrough.repo_index import RepoIndex

WALK_ID = "main"
HOP_SEARCH = 3000  # functions searched for each hop
CHOSEN = (
    "the main path the Wiki's Overview traces through the code index; "
    "the question is written by the model from that path"
)
QUESTION_SYSTEM = (
    "You name the question a code walkthrough answers. Reply in JSON only."
)


def read_overview_stages(api: str, repo_id: str) -> tuple[str, list[dict]]:
    """The Overview path's title and its functions: ``{"name", "file"}``.

    ``file`` comes from the evidence a stage cites and is None when it cites
    none; an Overview with no path gives no stages.
    """

    url = f"{api.rstrip('/')}/api/repos/{repo_id}/wiki/overview"
    page = json.load(urllib.request.urlopen(url, timeout=120))
    return overview_stages(page)


def overview_stages(page: dict) -> tuple[str, list[dict]]:
    journey = page.get("journey") or {}
    evidence = page.get("evidence") or {}
    items = evidence.get("items") if isinstance(evidence, dict) else evidence
    by_id = {e.get("id"): e for e in items or [] if isinstance(e, dict)}
    stages = []
    for st in journey.get("stages") or []:
        name = str(st.get("stage") or "").strip().strip("`")
        if name and not name.endswith(")"):
            name += "()"  # the index names functions with their parentheses
        file = None
        for eid in st.get("evidence") or []:
            e = by_id.get(eid) or {}
            if e.get("kind") == "function" and e.get("file"):
                if (e.get("symbol") or "").split(":")[-1] == name or file is None:
                    file = e["file"]
        if name:
            stages.append({"name": name, "file": file})
    return str(journey.get("title") or ""), stages


def _plain_name(name: str) -> str:
    """``Arc<Handle><Schedule>.schedule()`` -> ``Arc.schedule``."""

    return re.sub(r"<[^>]*>", "", name.split(":")[-1]).replace("()", "").strip()


def recorded_main_path(ix: RepoIndex) -> list[int]:
    """The main path the index records, as vertices; [] when there is none.

    It is the path the Overview is built from (``AgentWiki._entry_path``),
    traced again with no page evidence and no model, so the walk follows
    exactly the functions the Overview's journey was narrated from.
    """

    _items, _relations, path = ix.wiki._entry_path([], [])
    vids = [ix.names.get(str(step["symbol"])) for step in path]
    return [vid for vid in vids if vid is not None] if None not in vids else []


def check_recorded(stages: list[dict], recorded: list[str]) -> None:
    """Raise unless the Overview's journey is the recorded main path.

    The journey may leave out stages it could not narrate (a private
    method, a second function of the same name), so its stages need only
    appear in the recorded path in order, starting where it starts.
    """

    if len(recorded) < 3:
        raise ValueError(
            "the index records no main path, so the Overview's journey "
            "(if any) is the model's"
        )
    names = [_plain_name(name) for name in recorded]
    if _plain_name(stages[0]["name"]) != names[0]:
        raise ValueError(
            f"the Overview starts at {stages[0]['name']}, but the recorded path "
            f"starts at {recorded[0]}; regenerate the Overview first"
        )
    remaining = iter(names)
    missing = [
        stage["name"]
        for stage in stages
        if not any(_plain_name(stage["name"]) == name for name in remaining)
    ]
    if missing:
        raise ValueError(
            f"the Overview's {missing[0]} is not on the recorded path; "
            "regenerate the Overview first"
        )


def overview_route(ix: RepoIndex, vids: list[int]) -> tuple[list[dict], int]:
    """The walk's path through the Overview's functions, and how many functions
    were added between two of them (0 when every hop is one recorded call)."""

    path = [{"fn": ix.info(vids[0]), "via": None}]
    added = 0
    for vid in vids[1:]:
        cur = path[-1]["fn"]["vid"]
        banned = {p["fn"]["vid"] for p in path}
        # The Overview drew one call here, so a short search finds it; a long
        # one on a large repository would only find a detour.
        chain = leg(ix, cur, {vid}, banned, limit=HOP_SEARCH) or leg(
            ix, cur, {vid}, banned, by_name=True, limit=HOP_SEARCH
        )
        if chain is None:
            raise ValueError(
                f"{ix.info(vid)['name']} is not reachable from {ix.info(cur)['name']}"
            )
        added += len(chain) - 1
        path += chain
    return path, added


def _bare(name: str) -> str:
    """``Session.request()`` -> ``request``."""

    return name.split(".")[-1].split("(")[0].strip()


def check_question(q: dict, start_name: str) -> list[str]:
    errs = []
    caps = (("question", 80), ("entry_call", 36), ("tab", 28))
    for key, cap in caps:
        v = str(q.get(key) or "").strip()
        if not v:
            errs.append(f"{key} is empty")
        elif len(v) > cap:
            errs.append(f"{key} is {len(v)} characters; the limit is {cap}")
    if not str(q.get("question") or "").rstrip().endswith("?"):
        errs.append("question must end with a question mark")
    bare = _bare(start_name)
    call = str(q.get("entry_call") or "")
    # main() is a command line, and __call__() is called as agent(task).
    named = bare != "main" and not bare.startswith("__")
    if named and not re.search(r"\b" + re.escape(bare) + r"\b", call):
        errs.append(f"entry_call must call {bare}")
    if "`" in call:
        errs.append("entry_call is code, without backticks")
    return errs


def plain_question(path: list[dict]) -> dict:
    """Wording that needs no model: the start's call and the end's name."""

    start, end = path[0]["fn"]["name"], path[-1]["fn"]["name"]
    bare = _bare(start)
    call = "the program" if bare == "main" else f"{bare}()"
    return {
        "question": f"What happens between {start} and {end}?",
        "entry_call": call,
        "tab": "The main path",
    }


def question_prompt(ix: RepoIndex, repo_id: str, title: str, path: list[dict]) -> str:
    rows = []
    for i, p in enumerate(path):
        f = p["fn"]
        head = ix.line(f["file"], f["a"]).strip()
        rows.append(f"{i + 1}. {f['name']} ({f['file']}:{f['a']}): {head}")
    start = path[0]["fn"]["name"]
    return (
        f"Repository: {repo_id.replace('__', '/')}\n"
        f"The Wiki's Overview follows this main path ({title}):\n"
        + "\n".join(rows)
        + "\n\n"
        "A walkthrough will follow this path one function per step. Name:\n"
        "- the question a developer new to this repository would ask that this "
        "path answers, at most 80 characters, ending with '?'. Ask about what "
        "the user does or gets (sending a request, starting the server), not "
        "about function names;\n"
        f"- entry_call: the code or command a user writes that reaches {start}, "
        "at most 36 characters, without backticks, e.g. 'requests.get(url)', "
        "'r.GET(\"/ping\", h)' or, for a program's main(), its command line such "
        "as 'redis-server redis.conf'. Use the repository's public API as its "
        f"README would; it must call {_bare(start)} unless that is main() or a "
        "special method such as __call__();\n"
        "- tab: a 2-4 word label for the walkthrough, at most 28 characters, "
        "e.g. 'Sending a request'.\n"
        'Return JSON: {"question": "...", "entry_call": "...", "tab": "..."}'
    )


def write_question(
    ix: RepoIndex, repo_id: str, title: str, path: list[dict], model: Model, log: dict
) -> dict:
    """The question, entry call and tab for the path, checked; plain wording if
    two answers fail."""

    user = question_prompt(ix, repo_id, title, path)
    start = path[0]["fn"]["name"]
    q = model.ask(QUESTION_SYSTEM, user, max_tokens=300, calls=log["calls"])
    errs = check_question(q, start)
    if errs:
        retry = (
            user
            + "\n\nYour previous answer failed these checks; fix them:\n- "
            + "\n- ".join(errs)
            + "\nPrevious answer: "
            + json.dumps(q)
        )
        q = model.ask(QUESTION_SYSTEM, retry, max_tokens=300, calls=log["calls"])
        errs = check_question(q, start)
    log["checks"].append({"question": {k: q.get(k) for k in q}, "errors": errs})
    if errs:
        say("question failed its checks, using plain wording:", errs)
        return plain_question(path)
    return {k: str(q[k]).strip() for k in ("question", "entry_call", "tab")}


def overview_walk_input(
    ix: RepoIndex, api: str, model: Model, log: dict
) -> tuple[dict, list[dict]]:
    """The question dict and path for a repository's Overview walk.

    Raises ValueError when the Overview has no path, its path is not the one
    the index records, or its functions cannot be found and joined in the
    index.
    """

    title, stages = read_overview_stages(api, ix.repo_id)
    if len(stages) < 2:
        raise ValueError("the Overview has no main path")
    vids = recorded_main_path(ix)
    check_recorded(stages, [ix.info(vid)["name"] for vid in vids])
    path, added = overview_route(ix, vids)
    log["checks"].append(
        {
            "overview": [s["name"] for s in stages],
            "path": [p["fn"]["name"] for p in path],
            "added": added,
        }
    )
    if added:
        say(f"overview path: {added} function(s) added between the Overview's own")
    q = write_question(ix, ix.repo_id, title, path, model, log)
    q["id"] = WALK_ID
    return q, path
