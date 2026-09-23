# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The one-sentence thesis a generated Overview opens with.

The landing page needs a line that says what a repository is for. A README's
first paragraph is often a chat invitation or a port-binding warning; the
Overview's opening thesis was written for exactly this and checked against
the source, so the card reuses it.
"""

from __future__ import annotations

import re
from typing import Optional

_CITATION = re.compile(r"\s*\[[ER]\d+\]\(#evidence-[ER]\d+\)")
_MAX_CHARS = 220


def overview_lead(markdown: str) -> Optional[str]:
    """Return the first prose paragraph of *markdown*, without citations.

    Headings, callouts, lists, tables, and fenced code are skipped. A long
    paragraph is cut at its last sentence end within the budget.
    """

    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    paragraph: list[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if paragraph:
                break
            continue
        if in_fence:
            continue
        if not stripped:
            if paragraph:
                break
            continue
        if re.match(r"^(#|>|[-*+] |\d+\. |\||<)", stripped):
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    text = _CITATION.sub("", " ".join(paragraph))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) > _MAX_CHARS:
        cut = max(text.rfind(". ", 0, _MAX_CHARS), text.rfind(".", 0, _MAX_CHARS))
        text = text[: cut + 1] if cut > 40 else text[: _MAX_CHARS - 1].rstrip() + "…"
    return text


__all__ = ["overview_lead"]
