# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Choose a walkthrough's path: the chain of functions from the start to the answer.

A curated question names its start, its answer and optionally functions the
walk must pass on the way (``via``). The path is then the cheapest route
through them along the index's calls, with no model call: a recorded call
or a subclass implementation costs 1, an attribute call resolved from the
source costs 1.6, and a call matched only by method name costs 2.6 and is
tried only when the recorded calls do not get there.

When the answer is out of reach (or a question names none), the model picks
up to three goals among the reachable functions, a hop-by-hop walk is added,
and the model judges the candidate paths whole.
"""

from __future__ import annotations

import heapq
import re
from typing import TYPE_CHECKING

from scripts.walkthrough.progress import say

if TYPE_CHECKING:
    from scripts.walkthrough.model import Model
    from scripts.walkthrough.repo_index import RepoIndex

COST = {"call": 1.0, "subclass": 1.0, "unrecorded": 1.6}
STOP = set(
    "a an the of to in on for and or how does do is are what when why which with by"
    " from it its this that i my get set".split()
)
CURATED = (
    "question and answer function chosen by hand; "
    "the path is the shortest chain of recorded calls between them"
)
JUDGED = (
    "the best of several candidate paths through recorded calls, "
    "judged whole by the model"
)


def is_test(f: dict) -> bool:
    parts = f["file"].lower().split("/")
    return any(
        x in ("test", "tests", "testing", "benchmarks", "examples", "docs")
        or x.startswith("test_")
        or x.endswith("_test.go")
        for x in parts
    )


def words(t: str) -> list[str]:
    """Word stems (first five letters) of a name or question, without stop words."""

    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
    return [
        w[:5]
        for w in re.split(r"[^A-Za-z0-9]+", t.lower())
        if len(w) > 2 and w not in STOP
    ]


def first_doc(ix: RepoIndex, f: dict) -> str:
    """The first line inside a function that reads like a description."""

    for n in range(f["a"] + 1, min(f["b"], f["a"] + 8) + 1):
        t = ix.line(f["file"], n).strip().strip("\"'").strip()
        if t and not t.startswith(("#", "//", ")", "self", "*")) and len(t) > 12:
            return t[:90]
    return ix.line(f["file"], f["a"]).strip()[:90]


def path_names(path: list[dict]) -> list[str]:
    """Each stop's name, with how it was reached when that was not a call."""

    return [
        p["fn"]["name"]
        + (
            ""
            if not p["via"] or p["via"]["kind"] == "call"
            else f" [{p['via']['kind']}]"
        )
        for p in path
    ]


# ---------- curated: the cheapest route through named functions ----------


def named_calls(ix: RepoIndex, u: int, dsts: set[int]) -> list[tuple[int, int]]:
    """Lines of u that call one of dsts by its method name on a value whose type
    the index does not know.

    Only for functions a curator named: the name in the question is the evidence
    the hop is meant.
    """

    f = ix.info(u)
    leaves: dict[str, list[int]] = {}
    for v in dsts:
        leaf = ix.info(v)["name"].split(".")[-1].rstrip("()")
        leaves.setdefault(leaf, []).append(v)
    out = []
    for n in range(f["a"] + 1, f["b"] + 1):
        text = ix.line(f["file"], n)
        for leaf, vs in leaves.items():
            if re.search(r"\.\s*" + re.escape(leaf) + r"\s*\(", text):
                out += [(v, n) for v in vs]
    return out


def leg(
    ix: RepoIndex, src: int, dsts: set[int], banned: set[int], by_name: bool = False
) -> list[dict] | None:
    """Cheapest route from src to the first of dsts it reaches, searched until it
    gets there.

    With by_name, a function may also reach a named target through a call by the
    target's method name (`transformer.fit_transform(...)` on a parameter). Only
    as a fallback: `DataArray.sel()` calls `.sel(` on a dataset, and a name match
    there would skip the recorded chain to `PandasIndex.sel()`.
    """

    d, pv, hp = {src: 0.0}, {src: None}, {src: 0}
    hq, seen, hit = [(0.0, src)], set(), None
    while hq and len(seen) < 40000:
        d0, u = heapq.heappop(hq)
        if u in seen:
            continue
        seen.add(u)
        if u in dsts and u != src:
            hit = u
            break
        if hp[u] >= 9:
            continue
        edges = [(v, ln, kind, None) for v, ln, kind in ix.neighbors(u)]
        recorded = {v for v, _, _, _ in edges}
        if by_name:
            edges += [
                (v, ln, "unrecorded", "named")
                for v, ln in named_calls(ix, u, dsts)
                if v not in recorded
            ]
        for v, ln, kind, how in edges:
            if v in banned or is_test(ix.info(v)):
                continue
            nd = d0 + COST[kind] + (1.0 if how == "named" else 0.0)
            if nd < d.get(v, 1e9):
                d[v], pv[v], hp[v] = nd, (u, ln, kind, how), hp[u] + 1
                heapq.heappush(hq, (nd, v))
    if hit is None:
        return None
    chain, v = [], hit
    while pv[v] is not None:
        u, ln, kind, how = pv[v]
        via = {"kind": kind, "line": ln}
        if kind == "unrecorded":
            via["how"] = how or "attr"
        chain.append({"fn": ix.info(v), "via": via})
        v = u
    return list(reversed(chain))


def curated_route(ix: RepoIndex, start: dict, stops: list[str]) -> list[dict] | None:
    """The start, then the cheapest route through each named function in turn;
    None if one is out of reach."""

    path, cur = [{"fn": start, "via": None}], start["vid"]
    for name in stops:
        dsts = set(ix.by_name.get(name, []))
        if not dsts:
            say(f"WARNING: no function named {name}")
            return None
        banned = {p["fn"]["vid"] for p in path}
        chain = leg(ix, cur, dsts, banned) or leg(ix, cur, dsts, banned, by_name=True)
        if chain is None:
            say(f"WARNING: {name} is not reachable from {ix.info(cur)['name']}")
            return None
        path += chain
        cur = chain[-1]["fn"]["vid"]
    return path


# ---------- fallback: goals the model names, a hop-by-hop walk, a judge ----------


class Reach:
    """Every function the start reaches by the cheapest calls, at most 9 hops."""

    def __init__(self, ix: RepoIndex, start: dict) -> None:
        self.start = start
        dist, prev, hops = {start["vid"]: 0.0}, {start["vid"]: None}, {start["vid"]: 0}
        heap, done = [(0.0, start["vid"])], set()
        while heap and len(done) < 900:
            d0, u = heapq.heappop(heap)
            if u in done:
                continue
            done.add(u)
            if hops[u] >= 9:
                continue
            for v, ln, kind in ix.neighbors(u):
                if is_test(ix.info(v)):
                    continue
                nd = d0 + COST[kind]
                if nd < dist.get(v, 1e9):
                    dist[v], prev[v], hops[v] = nd, (u, ln, kind), hops[u] + 1
                    heapq.heappush(heap, (nd, v))
        self.ix, self.dist, self.prev, self.hops = ix, dist, prev, hops
        self.reach = [v for v in dist if v != start["vid"]]

    def route(self, goal: int) -> list[dict]:
        chain, v = [], goal
        while self.prev[v] is not None:
            u, ln, kind = self.prev[v]
            chain.append({"fn": self.ix.info(v), "via": {"kind": kind, "line": ln}})
            v = u
        return [{"fn": self.start, "via": None}] + list(reversed(chain))

    def ranked(self, question: str, entry_call: str) -> list[int]:
        """The 40 reachable functions whose names and code best match the question."""

        ix, qn = self.ix, set(words(question + " " + entry_call))

        def relevance(v: int) -> float:
            f = ix.info(v)
            name = set(words(f["name"]))
            head = range(f["a"], min(f["b"], f["a"] + 40) + 1)
            body = set(words(" ".join(ix.line(f["file"], n) for n in head)))
            size = f["b"] - f["a"] + 1
            return (
                3 * len(qn & name)
                + len(qn & body)
                + (0.5 if size >= 8 else 0)
                - 0.15 * self.hops[v]
                - (2 if ix.is_abstract(f) else 0)
            )

        return sorted(self.reach, key=relevance, reverse=True)[:40]


def _hop_options(
    ix: RepoIndex, cur: dict, on_path: set[int]
) -> tuple[list[dict], list[str], list[dict]]:
    """The hops offered at one function, the unresolved calls left to name, and
    the recorded calls."""

    calls = ix.out_calls(cur["vid"])
    subs = ix.overrides(cur["vid"]) if ix.is_abstract(cur) else []
    unres = ix.unresolved(cur, {c["line"] for c in calls})
    opts = []
    for c in calls:
        t = ix.info(c["vid"])
        if c["vid"] in on_path:
            continue
        code = ix.line(cur["file"], c["line"]).strip()[:90]
        where = f"{t['file']}:{t['a']}-{t['b']}; {ix.ahead(c['vid'])}"
        opts.append(
            {
                "key": f"C{len(opts) + 1}",
                "kind": "call",
                "line": c["line"],
                "fn": t,
                "text": f"line {c['line']}: `{code}`  ->  {t['name']}  ({where})",
            }
        )
    for i in subs:
        t = ix.info(i)
        if i in on_path:
            continue
        where = f"{t['file']}:{t['a']}-{t['b']}; {ix.ahead(i)}"
        opts.append(
            {
                "key": f"S{len(opts) + 1}",
                "kind": "subclass",
                "line": None,
                "fn": t,
                "text": f"subclass implementation: {t['name']}  ({where})",
            }
        )
    recorded = {c["line"] for c in calls}
    for n in range(cur["a"] + 1, cur["b"] + 1):
        if n in recorded:
            continue
        vid = ix.resolve_attr_call(cur, n)
        if vid is None or vid in on_path or any(o["fn"]["vid"] == vid for o in opts):
            continue
        t = ix.info(vid)
        code = ix.line(cur["file"], n).strip()[:90]
        where = (
            f"{t['file']}:{t['a']}-{t['b']}; {ix.ahead(vid)}; "
            "resolved from the attribute's type, not recorded by the index"
        )
        opts.append(
            {
                "key": f"A{len(opts) + 1}",
                "kind": "unrecorded",
                "line": n,
                "fn": t,
                "text": f"line {n}: `{code}`  ->  {t['name']}  ({where})",
            }
        )
        unres = [u for u in unres if not u.startswith(f"line {n}:")]
    return opts, unres, calls


def _hop_prompt(
    ix: RepoIndex, question: str, path: list[dict], cur: dict, opts: list, unres: list
) -> str:
    sofar = " -> ".join(p["fn"]["name"] for p in path)
    source = ix.numbered(cur["file"], cur["a"], cur["b"])
    options = "\n".join(o["key"] + ": " + o["text"] for o in opts) or "(none)"
    missing = "\n".join(unres) or "(none)"
    not_yet = "" if len(path) >= 3 else " (not yet: at least 4 functions)"
    return (
        f"Question the walkthrough answers: {question}\n"
        f"    Path so far: {sofar}\n"
        "\n"
        f"    Current function {cur['name']} ({cur['file']}:{cur['a']}-{cur['b']}):\n"
        f"    {source}\n"
        "\n"
        "    Recorded next hops:\n"
        f"    {options}\n"
        "\n"
        '    Calls in the source the index did not resolve (usable only as "unrecorded"):\n'
        f"    {missing}\n"
        "\n"
        "    Choose the hop that best continues the answer: the main path a typical "
        "call takes, not logging, not error handling,\n"
        "    not an optional branch. Skip trivial helpers. Prefer the hop into code "
        "that does this library's own distinctive work, and that goes on to do more "
        "of it\n"
        "    (see what each option calls next): a leaf helper that calls nothing ends "
        "the walk early. A call into an abstract\n"
        "    base method continues through its implementations. If an important call "
        "in the source is missing from the list\n"
        "    (for example a call through an attribute such as self.executor(...)), you "
        'may name it as "unrecorded" with its line\n'
        "    and the exact function it reaches. Stop when the question is answered"
        f"{not_yet}.\n"
        "\n"
        '    Return JSON: {"choice": "C1" | "S2" | "unrecorded" | "stop", '
        '"line": <line for unrecorded>, "target": "<Class.method() for unrecorded>", '
        '"why": "one sentence"}'
    )


def greedy_path(
    ix: RepoIndex, question: str, start: dict, model: Model, log: dict
) -> list[dict]:
    """Walk hop by hop, the model choosing each next call, at most 7 stops."""

    path = [{"fn": start, "via": None}]
    max_stops = 7
    while len(path) < max_stops:
        cur = path[-1]["fn"]
        on_path = {p["fn"]["vid"] for p in path}
        opts, unres, calls = _hop_options(ix, cur, on_path)
        pick = model.ask(
            "You choose the next hop of a code walkthrough. Each hop must be a call "
            "the index recorded. Reply in JSON only.",
            _hop_prompt(ix, question, path, cur, opts, unres),
            max_tokens=300,
            temperature=0,
            plan=True,
            calls=log["calls"],
        )
        ch = str(pick.get("choice", "stop")).strip()
        say(
            f"  at {cur['name']}: {ch} {pick.get('target', '')} — {pick.get('why', '')}"
        )
        if ch == "stop" and len(path) >= 4:
            break
        opt = next((o for o in opts if o["key"] == ch), None)
        if opt:
            path.append(
                {"fn": opt["fn"], "via": {"kind": opt["kind"], "line": opt["line"]}}
            )
            continue
        if ch == "unrecorded":
            vid = ix.find_symbol(str(pick.get("target", "")))
            ln = int(pick.get("line") or 0)
            # A call the index did record is taken as recorded, whatever the model
            # called it.
            match = next(
                (
                    o
                    for o in opts
                    if vid is not None
                    and o["fn"]["vid"] == vid
                    and o["kind"] != "unrecorded"
                ),
                None,
            )
            if match:
                via = {"kind": match["kind"], "line": match["line"]}
                path.append({"fn": match["fn"], "via": via})
                continue
            ok = (
                vid is not None
                and cur["a"] <= ln <= cur["b"]
                and "(" in ix.line(cur["file"], ln)
                and ln not in {c["line"] for c in calls}
            )
            log["checks"].append(
                {
                    "hop": "unrecorded",
                    "target": pick.get("target"),
                    "line": ln,
                    "ok": ok,
                }
            )
            if ok:
                path.append(
                    {"fn": ix.info(vid), "via": {"kind": "unrecorded", "line": ln}}
                )
                continue
        break
    return path


def _goals_prompt(
    ix: RepoIndex, question: str, start: dict, reach: Reach, ranked: list[int]
) -> str:
    rows = []
    for k, v in enumerate(ranked):
        f = ix.info(v)
        rows.append(
            f"G{k + 1}: {f['name']}  ({f['file']}:{f['a']}-{f['b']}) "
            f"[{reach.hops[v]}]  {first_doc(ix, f)}"
        )
    listing = "\n".join(rows)
    return (
        f"Question the walkthrough answers: {question}\n"
        f"It starts at {start['name']} ({start['file']}:{start['a']}-{start['b']}).\n"
        "\n"
        "Functions reachable from the start through calls CodeNib recorded "
        "(hops from the start in brackets):\n"
        f"{listing}\n"
        "\n"
        "Name up to three functions where the question could be answered: functions "
        "whose own code does the thing the\n"
        "question asks about. Not validation, logging, configuration or formatting "
        "helpers, and not an abstract method when\n"
        "an implementation is listed. Best first.\n"
        "\n"
        'Return JSON: {"goals": ["G1", ...], "why": "one sentence"}'
    )


def describe(ix: RepoIndex, pth: list[dict]) -> str:
    """One candidate path as the judge reads it: a numbered list of stops."""

    rows = []
    hows = {"call": "", "subclass": " (subclass)", "unrecorded": " (attribute call)"}
    for k, p in enumerate(pth):
        via = "" if not p["via"] else hows[p["via"]["kind"]]
        size = p["fn"]["b"] - p["fn"]["a"] + 1
        rows.append(
            f"   {k + 1}. {p['fn']['name']}{via}  {size} lines  {first_doc(ix, p['fn'])}"
        )
    return "\n".join(rows)


def _judge_prompt(ix: RepoIndex, question: str, unique: list) -> str:
    listing = "\n".join(
        f"P{k + 1}:\n{describe(ix, pth)}" for k, (label, pth) in enumerate(unique)
    )
    return (
        f"Question the walkthrough answers: {question}\n"
        "\n"
        "Candidate paths, each a chain of calls CodeNib recorded or resolved, "
        "from the user's call:\n"
        f"{listing}\n"
        "\n"
        "Pick the path that best answers the question for someone new to the code: "
        "it follows the main steps from the call\n"
        "to where the answer is decided, has no detour into helpers, and is long "
        "enough to explain the answer. A path that\n"
        "reaches the answer and then keeps going into solver internals, parsing or "
        "conversion helpers is worse than the same\n"
        "path ending at the answer.\n"
        "\n"
        f'Return JSON: {{"path": "P1".."P{len(unique)}", "why": "one sentence"}}'
    )


def _model_candidates(
    ix: RepoIndex, q: dict, start: dict, model: Model, log: dict
) -> list[tuple[str, list[dict]]]:
    """Candidate paths when no answer is reached: routes to model goals, hop by hop."""

    question = q["question"]
    reach = Reach(ix, start)
    say(f"reachable from {start['name']}: {len(reach.reach)} functions")
    ranked = reach.ranked(question, q.get("entry_call", ""))
    candidates = []
    if ranked:
        goals = model.ask(
            "You choose where a code walkthrough could end. Reply in JSON only.",
            _goals_prompt(ix, question, start, reach, ranked),
            max_tokens=300,
            temperature=0,
            plan=True,
            calls=log["calls"],
        )
        for goal_key in goals.get("goals") or []:
            k = int(re.sub(r"\D", "", str(goal_key)) or 0)
            if 1 <= k <= len(ranked):
                goal = ranked[k - 1]
                candidates.append(("goal " + ix.info(goal)["name"], reach.route(goal)))
    walked = greedy_path(ix, question, start, model, log)
    candidates.append(("hop by hop", walked))
    named = {
        pth[-1]["fn"]["vid"]: label
        for label, pth in candidates
        if label.startswith("goal ")
    }
    for k, p in enumerate(walked[:-1]):
        if p["fn"]["vid"] in named and k >= 1:
            candidates.append(
                ("hop by hop, ending at " + p["fn"]["name"], walked[: k + 1])
            )
    return candidates


def choose_path(
    ix: RepoIndex,
    q: dict,
    start: dict,
    model: Model,
    log: dict,
) -> tuple[list[dict], str]:
    """The walk's path and the sentence saying how it was chosen.

    Each stop is ``{"fn": info, "via": None | {"kind", "line"[, "how"]}}``;
    ``how`` is "attr" or "named" on an unrecorded hop. ``q`` is the curated
    question (``question``, ``entry_call``, ``answer``, optional ``via``).
    """

    candidates = []
    answer = q.get("answer")
    avid = None
    if answer:
        fixed_path = curated_route(ix, start, list(q.get("via") or []) + [answer])
        if fixed_path is None:
            say(
                f"WARNING: answer {answer} is not reachable from {start['name']}; "
                "falling back to the judge"
            )
            log["checks"].append({"answer": answer, "reached": False})
        else:
            avid = fixed_path[-1]["fn"]["vid"]
            label = "answer " + ix.info(avid)["name"]
            candidates.append((label, fixed_path))
            log["checks"].append(
                {
                    "answer": answer,
                    "reached": True,
                    "candidates": [label],
                    "picked": label,
                }
            )
    if avid is None:
        candidates += _model_candidates(ix, q, start, model, log)
    seen_paths, unique = set(), []
    for label, pth in candidates:
        key = tuple(p["fn"]["vid"] for p in pth)
        if key not in seen_paths and len(pth) >= 2:
            seen_paths.add(key)
            unique.append((label, pth))
    if not unique:
        raise ValueError(f"no candidate path from {start['name']}")

    # The model judges whole paths: easier than one hop at a time.
    if len(unique) > 1:
        judge = model.ask(
            "You choose the call path for a code walkthrough. Reply in JSON only.",
            _judge_prompt(ix, q["question"], unique),
            max_tokens=300,
            temperature=0,
            plan=True,
            calls=log["calls"],
        )
        pk = int(re.sub(r"\D", "", str(judge.get("path", ""))) or 1)
        label, path = unique[pk - 1] if 1 <= pk <= len(unique) else unique[0]
        say(f"judge: P{pk} ({label}) — {judge.get('why', '')}")
        log["checks"].append(
            {
                "candidates": [lb for lb, _ in unique],
                "picked": label,
                "why": judge.get("why"),
            }
        )
    else:
        label, path = unique[0]
    for k, (lb, pth) in enumerate(unique):
        say(f"  P{k + 1} {lb}: " + " -> ".join(p["fn"]["name"] for p in pth))
    chosen = CURATED if avid is not None else "a question chosen by hand; " + JUDGED
    return path, chosen
