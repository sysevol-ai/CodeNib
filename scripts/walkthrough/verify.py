# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Check each written step against the source it describes, and correct it.

A second model call reads one step's text next to the code it is about and
lists each claim the code contradicts or does not establish, with a rewrite
that keeps only supported claims. A function step is checked against its
own lines and its caller around the call site (through a subclass hop, the
function that called the base method), so a claim about consequences can be
checked. The overview and recap are checked against the start function's
source and the corrected step texts.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from scripts.walkthrough.progress import say
from scripts.walkthrough.steps import html, plain

if TYPE_CHECKING:
    from scripts.walkthrough.model import Model

SYSTEM = "You check a code walkthrough against source code. Reply in JSON only."
RULES = (
    "Check every claim in the text against the source shown. A claim is wrong if "
    "the source contradicts it, and unsupported\n"
    "if the shown source does not establish it (including claims about what "
    "callers or later code do).\n"
    "Three more checks, each easy to miss:\n"
    "- subject: every action is credited to the function or object the source shows "
    'doing it. "X calls Y" is a problem\n'
    "  when the shown code has Z call Y, or when X only passes Y along.\n"
    "- condition: an action the source takes only under a condition (an if, a flag "
    "such as reset=True, one branch of a\n"
    "  try, a loop that may not run) must say so. Stating it as if it always happens "
    "is a problem.\n"
    '- idea: the "idea" field must be one plain sentence about why the code is '
    "shaped this way. A list of line numbers,\n"
    "  names or steps is a problem.\n"
    "Report only real problems of these kinds; do not ask for parameter lists or "
    "extra detail.\n"
    'Return JSON: {"problems": [{"claim": "...", "verdict": "wrong" | "unsupported" '
    '| "subject" | "condition" | "idea", "why": "..."}],\n'
    ' "fixed": {same keys as the text, rewritten so every claim is supported; '
    "keep length within 10% and keep backticks}}\n"
    'If there are no problems, return {"problems": [], "fixed": null}.'
)


def _numbered(source: dict, rel: str, a: int, b: int) -> str:
    """Lines a..b of a file on the path, as the build recorded them."""

    lines = source.get(rel, {})
    return "\n".join(
        f"{n:5d}  {lines[str(n)]}" for n in range(a, b + 1) if str(n) in lines
    )


def _fn_range(walk: dict, k: int) -> tuple[str, int, int]:
    st = walk["steps"][k]
    file = st["code"][0]
    a, b = walk["stops"][st["at"]][2].split(":")[1].split("–")
    return file, int(a), int(b)


def _body(fixed: dict) -> list:
    body = fixed.get("body")
    if isinstance(body, list):
        return body
    return [fixed["body"]] if body else []


def _report(problems: list[dict]) -> str:
    return "".join(
        f"\n   - [{p.get('verdict')}] {p.get('claim')} :: {p.get('why')}"
        for p in problems
    )


def verify_walk(walk: dict, source: dict, model: Model) -> list[dict]:
    """Check and correct a walk's steps in place; the problems found, per step.

    ``source`` is the build's ``{file: {line: text}}`` for the walk's functions.
    """

    report = []
    fn_steps = [k for k, s in enumerate(walk["steps"]) if s.get("code")]
    stops, segs = walk["stops"], walk["segs"]
    for k in fn_steps:
        st = walk["steps"][k]
        file, a, b = _fn_range(walk, k)
        ctx = [f"The function, {file}:{a}-{b}:\n{_numbered(source, file, a, b)}"]
        # The real caller: through a subclass hop, the function that called the
        # base method.
        at = st["at"]
        while (
            at > 1 and (segs[at - 1][3] if len(segs[at - 1]) > 3 else "") == "subclass"
        ):
            at -= 1
        seg = segs[at - 1] if at > 0 else None
        caller = next((j for j in fn_steps if walk["steps"][j]["at"] == at - 1), None)
        if caller is not None:
            pf, pa, pb = _fn_range(walk, caller)
            site = re.search(r":(\d+)", seg[1]) if seg else None
            n = int(site.group(1)) if site else pa
            around = _numbered(source, pf, max(pa, n - 6), min(pb, n + 30))
            ctx.append(f"Its caller around the call ({pf}:{n}):\n{around}")
        text = {
            "lead": plain(st["lead"]),
            "body": [plain(x) for x in st.get("body") or []],
            "idea": plain(st.get("idea")),
        }
        about = stops[st["at"]][0] + stops[st["at"]][1]
        out = model.check(
            SYSTEM,
            f"Text of the step about {about}:\n{json.dumps(text, indent=1)}\n\n"
            + "\n\n".join(ctx)
            + "\n\n"
            + RULES,
        )
        probs = out.get("problems") or []
        if probs and isinstance(out.get("fixed"), dict):
            f = out["fixed"]
            st["lead"] = html(f.get("lead") or text["lead"])
            st["body"] = [html(x) for x in _body(f)]
            st["idea"] = html(f.get("idea") or "")
        report.append({"step": k, "problems": probs})
        say(f"step {k}: {len(probs)} problem(s)" + _report(probs))

    # The overview and recap, against the start function and the corrected steps.
    sf, sa, sb = _fn_range(walk, fn_steps[0])
    steps_text = "\n".join(
        f"{k}. {plain(walk['steps'][k]['title'])}: {plain(walk['steps'][k]['lead'])} "
        + " ".join(plain(x) for x in walk["steps"][k].get("body") or [])
        for k in fn_steps
    )
    for k in (0, len(walk["steps"]) - 1):
        st = walk["steps"][k]
        text = {
            "title": plain(st["title"]),
            "lead": plain(st["lead"]),
            "body": [plain(x) for x in st.get("body") or []],
            "idea": plain(st.get("idea")),
        }
        part = "overview" if k == 0 else "recap"
        out = model.check(
            SYSTEM,
            f"Text of the walkthrough's {part}:\n{json.dumps(text, indent=1)}\n\n"
            f"The steps it summarises:\n{steps_text}\n\n"
            f"The start function, {sf}:{sa}-{sb}:\n{_numbered(source, sf, sa, sb)}\n\n"
            + RULES,
        )
        probs = out.get("problems") or []
        if probs and isinstance(out.get("fixed"), dict):
            f = out["fixed"]
            st["title"] = html(f.get("title") or text["title"])
            st["lead"] = html(f.get("lead") or text["lead"])
            st["body"] = [html(x) for x in _body(f)]
            st["idea"] = html(f.get("idea") or "")
        report.append({"step": k, "problems": probs})
        say(f"{part}: {len(probs)} problem(s)" + _report(probs))
    return report
