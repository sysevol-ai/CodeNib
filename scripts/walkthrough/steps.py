# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Write a walkthrough's text: each step, then the title, overview and recap.

The model writes each step from the function's full source, the call site that
reaches it and the whole path. Each answer is checked: length limits, the
lines it marks (kept only when they fall in the shown excerpt), and every
``file.py:123`` or ``tree.go:87`` it cites (the line must exist in a file on the
path). A failed
answer is sent back once with the errors; the second answer is kept unless it
fails more checks, and the errors left are logged.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Callable

from scripts.walkthrough.progress import say

if TYPE_CHECKING:
    from scripts.walkthrough.model import Model
    from scripts.walkthrough.repo_index import RepoIndex

LineReader = Callable[[str, int], str]

STYLE = (
    "Write for a developer reading the code for the first time.\n"
    "- Plain words. Say what the code does with the thing passing through, "
    'not that it is "responsible for" something.\n'
    '- No hype, no "simply", "seamlessly", "robust", "powerful", "elegant", '
    '"crucial", "key", "core".\n'
    '- Never start with "This function" or "This method". Present tense.\n'
    "- Put identifiers in backticks: `run()`, `self.memory`, `final_answer`.\n"
    "- Only state what the shown source says. If the code only hands something "
    "on, say so.\n"
    "- Cite lines as file:line, e.g. sessions.py:123 or tree.go:87, when a "
    "specific line matters."
)
# A line citation: a file name with an extension, a colon, a line number.
CITE = re.compile(
    r"((?:[\w.-]+/)*[\w-]+(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{0,4}):(\d+)\b"
)


def cited_file(paths: dict[str, str], name: str) -> str | None:
    """The repository path a cited file name means: a name the walk shows, or a
    base name only one file on the path has."""

    if name in paths:
        return paths[name]
    hits = {full for full in paths.values() if full.split("/")[-1] == name}
    return hits.pop() if len(hits) == 1 else None


# How a hop the index did not record was found, as the model is told.
HOW = {
    "attr": "a call through an attribute, which the index does not record; "
    "its type is read from the source",
    "named": "a call by method name on a value whose type the index does not know; "
    "the walk follows the implementation most callers get",
}
NUMS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
}
STEP_SYSTEM = "You write one step of a code walkthrough. Reply in JSON only."
OVERVIEW_SYSTEM = (
    "You write the title, first and last step of a code walkthrough. "
    "Reply in JSON only."
)


def html(s: object) -> str:
    """Escape text for the page; `backticks` become <code>."""

    s = str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)


def plain(h: str | None) -> str:
    """The inverse of :func:`html`: <code> back to backticks, entities decoded."""

    text = re.sub(r"<code>(.*?)</code>", r"`\1`", h or "")
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _retry(prompt: str, errs: list[str], previous: dict) -> str:
    return (
        prompt
        + "\n\nYour previous answer failed these checks; "
        + "fix them and return the full JSON again:\n- "
        + "\n- ".join(errs)
        + "\n\nPrevious answer:\n"
        + json.dumps(previous)
    )


# ---------- one step ----------


def detab(text: str) -> str:
    """Leading tabs as four spaces each, as the page shows them."""

    tabs = len(text) - len(text.lstrip("\t"))
    return "    " * tabs + text[tabs:]


def _rows(text: str, width: int = 50) -> int:
    """Rows a shown line takes: wrapped rows hang by its indentation plus four,
    as the page wraps them."""

    if len(text) <= width:
        return 1
    hang = min(len(text) - len(text.lstrip(" ")) + 4, width - 10)
    return 1 + -(-(len(text) - width) // (width - hang))


def excerpt(
    line: LineReader, f: dict, marks: list[int], next_line: int | None = None
) -> list[list[int]]:
    """The def line plus each marked line with context, merged, as line ranges.

    The excerpt must fit 14 rows of 50 characters (after the common indent,
    with tabs as four spaces and wrapped rows hanging), one more row between
    ranges. The call that leads on gets two lines of
    context, others one; context shrinks, then marks drop, last first.
    """

    def build(ms: list[int], near: int, far: int) -> tuple[list[list[int]], int]:
        want = {f["a"]}
        for m in ms:
            ctx = near if m == next_line else far
            for n in range(m - ctx, m + ctx + 1):
                if f["a"] <= n <= f["b"] and (n == m or line(f["file"], n).strip()):
                    want.add(n)
        ranges: list[list[int]] = []
        for n in sorted(want):
            if ranges and n == ranges[-1][1] + 1:
                ranges[-1][1] = n
            else:
                ranges.append([n, n])
        numbers = [n for a, b in ranges for n in range(a, b + 1)]
        shown = [detab(line(f["file"], n)).rstrip() for n in numbers]
        cut = min((len(x) - len(x.lstrip(" ")) for x in shown if x.strip()), default=0)
        rows = sum(_rows(x[cut:]) for x in shown)
        return ranges, rows + len(ranges) - 1

    ms = list(marks)
    while ms:
        for near, far in ((2, 1), (1, 1), (1, 0), (0, 0)):
            ranges, rows = build(ms, near, far)
            if rows <= 14:
                return ranges
        if len(ms) > 1:
            lead = [m for m in ms if m == next_line]
            ms = lead + [m for m in ms if m != next_line][:-1]
        else:
            ms = ms[:0]
    return [[f["a"], f["a"]]]


def check_step(
    line: LineReader,
    st: dict,
    f: dict,
    next_line: int | None,
    paths: dict[str, str],
) -> list[str]:
    """The problems with one step's answer; also sets its ``show`` and ``mark``.

    ``paths`` maps each file name on the path to its repository path.
    """

    errs = []
    marks: list[int] = []
    for m in st.get("mark") or []:
        if (
            str(m).lstrip("-").isdigit()
            and f["a"] <= int(m) <= f["b"]
            and int(m) not in marks
        ):
            marks.append(int(m))
    if next_line:
        marks = [next_line] + [m for m in marks if m != next_line]
    marks = marks[:4]
    st["show"] = excerpt(line, f, marks, next_line)
    shown = {n for a, b in st["show"] for n in range(a, b + 1)}
    st["mark"] = sorted(m for m in marks if m in shown)
    caps = (
        ("title", 56),
        ("lead", 160),
        ("idea", 110),
        ("what", 32),
        ("packet", 8),
        ("role", 26),
    )
    for key, cap in caps:
        if len(str(st.get(key) or "")) > cap:
            errs.append(f"{key} is {len(str(st[key]))} characters; the limit is {cap}")
    body = " ".join(st.get("body") or [])
    if len(body) > 220:
        errs.append(f"body is {len(body)} characters; the limit is 220")
    total = len(str(st.get("lead") or "")) + len(body) + len(str(st.get("idea") or ""))
    if total > 430:
        errs.append(
            f"lead, body and idea together are {total} characters; the limit is 430"
        )
    for key in ("title", "lead", "what", "role"):
        if not str(st.get(key) or "").strip():
            errs.append(f"{key} is empty")
    if not st["mark"]:
        errs.append("mark at least one line")
    # Line citations in the prose must exist in a file on the path.
    prose = " ".join(str(st.get(k) or "") for k in ("lead", "idea")) + " " + body
    for m in CITE.finditer(prose):
        fname, n = m.group(1), int(m.group(2))
        full = cited_file(paths, fname)
        if not full or not line(full, n).strip():
            errs.append(f"{m.group(0)} is not a source line on this path")
    return errs


def path_text(path: list[dict]) -> str:
    """The whole path as the model is shown it, one numbered line per stop."""

    rows = []
    for i, p in enumerate(path):
        fn = p["fn"]
        row = f"{i + 1}. {fn['name']} ({fn['file']}:{fn['a']}-{fn['b']})"
        if p["via"]:
            how = HOW.get(p["via"].get("how"), p["via"]["kind"])
            row += f" reached from line {p['via']['line']} ({how})"
        else:
            row += " (start)"
        rows.append(row)
    return "\n".join(rows)


def step_prompt(
    ix: RepoIndex, question: str, path: list[dict], whole: str, i: int
) -> tuple[str, int | None]:
    """The prompt for step i, and the line where it calls the next step."""

    f = path[i]["fn"]
    p = path[i]
    nxt = path[i + 1] if i + 1 < len(path) else None
    next_line = nxt["via"]["line"] if nxt and nxt["via"]["kind"] != "subclass" else None
    came = ""
    if p["via"] and p["via"]["line"]:
        prev = path[i - 1]["fn"]
        n = p["via"]["line"]
        site = ix.numbered(prev["file"], max(prev["a"], n - 2), min(prev["b"], n + 2))
        came = f"It is reached from {prev['name']} at {prev['file']}:{n}:\n{site}"
    elif p["via"] and p["via"]["kind"] == "subclass":
        came = (
            f"{path[i - 1]['fn']['name']} is a base method; "
            "the call dispatches to this subclass implementation."
        )
    if next_line:
        leads = f"It leads to the next step, {nxt['fn']['name']}, at line {next_line}."
    elif nxt:
        leads = "It dispatches to the subclass " + nxt["fn"]["name"] + "."
    else:
        leads = "It is the last step."
    source = ix.numbered(f["file"], f["a"], f["b"])
    include = next_line if next_line else "the line that matters most"
    user = (
        f"Question the walkthrough answers: {question}\n"
        "The whole path:\n"
        f"{whole}\n"
        "\n"
        f"This step is function {i + 1}: {f['name']} ({f['file']}:{f['a']}-{f['b']}).\n"
        f"{came}\n"
        f"{leads}\n"
        "\n"
        "Source:\n"
        f"{source}\n"
        "\n"
        f"{STYLE}\n"
        "\n"
        "Return JSON:\n"
        '{"role": "what this function does on the path, 2-4 words, lowercase, '
        "e.g. 'fixes the method'\",\n"
        '  "title": "a claim about this step, at most 56 characters, '
        'naming the function",\n'
        '  "lead": "1-2 sentences, at most 160 characters: '
        'what it does with what passes through",\n'
        '  "body": ["1-2 sentences, at most 220 characters in total: '
        'the detail that matters, with a line citation if useful"],\n'
        '  "idea": "optional, at most 110 characters: '
        'the one non-obvious point, or empty",\n'
        "  (lead, body and idea together at most 430 characters)\n"
        '  "what": "what is travelling after this step, 1-4 words, '
        "e.g. 'a PreparedRequest'\",\n"
        '  "packet": "one short word for the moving token, at most 8 characters, '
        "e.g. 'code' or 'result'\",\n"
        '  "mark": [2 to 4 line numbers of this function that the text talks about, '
        f"most important first; include {include}]}}"
    )
    return user, next_line


def write_steps(
    ix: RepoIndex,
    question: str,
    path: list[dict],
    paths: dict[str, str],
    model: Model,
    log: dict,
) -> list[dict]:
    """One checked answer per function on the path, in order."""

    whole = path_text(path)
    steps_out = []
    for i, p in enumerate(path):
        f = p["fn"]
        user, next_line = step_prompt(ix, question, path, whole, i)
        st = model.ask(STEP_SYSTEM, user, calls=log["calls"])
        errs = check_step(ix.line, st, f, next_line, paths)
        tries = 1
        if errs:
            st2 = model.ask(STEP_SYSTEM, _retry(user, errs, st), calls=log["calls"])
            errs2 = check_step(ix.line, st2, f, next_line, paths)
            tries = 2
            if len(errs2) <= len(errs):
                st, errs = st2, errs2
        if len(str(st.get("packet") or "")) > 8 or not st.get("packet"):
            ws = [
                x
                for x in re.split(r"[^A-Za-z]+", str(st.get("what") or ""))
                if 0 < len(x) <= 8 and x.lower() not in ("a", "an", "the")
            ]
            st["packet"] = ws[-1] if ws else "data"
        log["checks"].append({"step": f["name"], "tries": tries, "errors": errs})
        steps_out.append(st)
        say(f"  step {i + 1} {f['name']}: tries {tries}, errors {errs}")
    return steps_out


# ---------- title, overview and recap ----------


def _field(o: object, keys: tuple[str, ...]) -> object:
    v = o
    for k in keys:
        v = (v or {}).get(k) if isinstance(v, dict) else None
    return v


def check_overview(o: dict, n_fns: int, n_files: int) -> list[str]:
    """The problems with the title/overview/recap answer: lengths and counts.

    A count in the title, dek, overview title or lead must be the path's own
    number of functions or files (or "one").
    """

    errs = []
    caps = {
        ("title",): 48,
        ("dek",): 150,
        ("overview", "title"): 48,
        ("overview", "lead"): 160,
        ("overview", "idea"): 110,
        ("recap", "title"): 48,
        ("recap", "lead"): 160,
    }
    for keys, cap in caps.items():
        v = _field(o, keys)
        if not str(v or "").strip():
            errs.append(".".join(keys) + " is empty")
        elif len(str(v)) > cap:
            errs.append(
                f"{'.'.join(keys)} is {len(str(v))} characters; the limit is {cap}"
            )
    for part, cap in (("overview", 160), ("recap", 180)):
        body = " ".join((o.get(part) or {}).get("body") or [])
        if len(body) > cap:
            errs.append(
                f"{part}.body is {len(body)} characters in total; the limit is {cap}"
            )
    allowed = {
        str(n_fns),
        NUMS.get(n_fns, ""),
        str(n_files),
        NUMS.get(n_files, ""),
        "one",
    }
    counted = r"\b(\d+|one|two|three|four|five|six|seven|eight|nine)\b"
    for keys in (("title",), ("dek",), ("overview", "title"), ("overview", "lead")):
        v = _field(o, keys)
        for tok in re.findall(counted, str(v or "").lower()):
            cites = {m.group(2) for m in CITE.finditer(str(v))}
            if tok not in allowed and tok not in cites:
                errs.append(
                    f"{'.'.join(keys)} says {tok!r}, but the path has {n_fns} "
                    f"functions in {n_files} files"
                )
    return errs


def overview_prompt(
    question: str,
    entry_call: str,
    path: list[dict],
    whole: str,
    steps_out: list[dict],
) -> str:
    n_fns, n_files = len(path), len({p["fn"]["file"] for p in path})
    written = "\n".join(
        f"{k + 1}. {s0.get('title')}: {s0.get('lead')} {' '.join(s0.get('body') or [])}"
        for k, s0 in enumerate(steps_out)
    )
    return (
        f"Question: {question}\n"
        f"What the user writes: {entry_call}\n"
        f"The path has {n_fns} functions in {n_files} files:\n"
        f"{whole}\n"
        "The steps, as written:\n"
        f"{written}\n"
        "\n"
        "Use only facts stated in these steps. "
        "Do not add a claim about the code that no step makes.\n"
        f"{STYLE}\n"
        "\n"
        "Return JSON:\n"
        '{"title": "page title, at most 48 characters: '
        'a plain statement of what the page traces",\n'
        '  "dek": "one sentence under the title, at most 150 characters, '
        'naming only functions on this path",\n'
        '  "overview": {"title": "at most 48 characters; '
        f'if it gives counts they must be {n_fns} functions and {n_files} files",\n'
        '               "lead": "at most 160 characters: '
        "what the user's call turns into underneath\",\n"
        '               "body": ["at most 160 characters in total: '
        'how to read the steps; do not list every function"],\n'
        '               "idea": "at most 110 characters: '
        'the one thing to watch for along the path"},\n'
        '  "recap": {"title": "at most 48 characters",\n'
        '            "lead": "at most 160 characters: what comes back, and how",\n'
        '            "body": ["at most 180 characters in total"]}}'
    )


def write_overview(
    question: str,
    entry_call: str,
    path: list[dict],
    steps_out: list[dict],
    model: Model,
    log: dict,
) -> dict:
    """The page title and dek, the overview step and the recap, checked."""

    n_fns, n_files = len(path), len({p["fn"]["file"] for p in path})
    prompt = overview_prompt(question, entry_call, path, path_text(path), steps_out)
    ov = model.ask(OVERVIEW_SYSTEM, prompt, max_tokens=900, calls=log["calls"])
    errs = check_overview(ov, n_fns, n_files)
    tries = 1
    if errs:
        # The retry has always used the default answer length, not 900.
        ov2 = model.ask(OVERVIEW_SYSTEM, _retry(prompt, errs, ov), calls=log["calls"])
        errs2 = check_overview(ov2, n_fns, n_files)
        tries = 2
        if len(errs2) <= len(errs):
            ov, errs = ov2, errs2
    log["checks"].append({"step": "overview+recap", "tries": tries, "errors": errs})
    say("  overview+recap: tries", tries, "errors", errs)
    return ov


def page_steps(
    path: list[dict], steps_out: list[dict], ov: dict, area_of_file: dict[str, str]
) -> list[dict]:
    """The walk's steps as the page shows them: overview, one per function, recap."""

    over, recap = ov["overview"], ov["recap"]
    steps = [
        {
            "rail": "Overview",
            "title": html(over["title"]),
            "at": None,
            "what": html(steps_out[0].get("what", "")),
            "runs": [],
            "lead": html(over["lead"]),
            "body": [html(b) for b in over.get("body") or []],
            "idea": html(over.get("idea")),
            "table": "stops",
        }
    ]
    for i, (p, st) in enumerate(zip(path, steps_out, strict=True)):
        k = i + 1
        steps.append(
            {
                "rail": p["fn"]["name"].split(".")[-1],
                "title": html(st["title"]),
                "at": k,
                "area": area_of_file.get(p["fn"]["file"]),
                "what": html(st.get("what")),
                "runs": [k - 1],
                "packet": str(st.get("packet") or "")[:9],
                "lead": html(st["lead"]),
                "body": [html(b) for b in st.get("body") or []],
                "idea": html(st.get("idea")),
                "code": [p["fn"]["file"], st["show"], st["mark"]],
            }
        )
    steps.append(
        {
            "rail": "Recap",
            "title": html(recap["title"]),
            "at": 0,
            "what": html(steps_out[-1].get("what", "")),
            "runs": list(range(len(path) - 1, -1, -1)),
            "reverse": True,
            "packet": "result",
            "recap": True,
            "lead": html(recap["lead"]),
            "body": [html(b) for b in recap.get("body") or []],
            "table": "sites",
        }
    )
    return steps
