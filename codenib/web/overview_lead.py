# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Keep an Overview from opening on a README's support note.

An Overview's first paragraph says what the project is. Some READMEs open
their prose on where to ask questions or how to install, and a page written
from one opened the same way: sympy's on "We have a community chat at
Gitter", vue's on "For questions and support please use the official forum".
Such a lead gives way to the repository's own one-line purpose (its
manifest's description, as its card shows), or to nothing.
"""

from __future__ import annotations

import re

from .card_summary import is_purpose_sentence

_CITATION = re.compile(r"\s*\[E\d+\]\(#evidence-E\d+\)")
# A feature bullet set as prose ("✨ Simplicity: the logic fits in ..."), or a
# README title line with its emoji ("AMA-Bench is an 🧠 AMA-Bench: ..."),
# is not a statement of what the project is.
_EMOJI = re.compile("[\u2600-\u27bf\U0001f300-\U0001faff]")
_LABEL = re.compile(r"^\W*[A-Z][\w-]*:\s")
_NOT_PROSE = ("#", "<", "|", "-", "*", ">", "```", "!")


def _says_what_it_is(sentence: str) -> bool:
    return (
        is_purpose_sentence(sentence)
        and not _EMOJI.search(sentence)
        and not _LABEL.match(sentence)
    )


def replace_unfit_lead(markdown: str, purpose: str = "") -> str:
    """Replace or drop an opening paragraph that does not say what the project is."""
    blocks = markdown.split("\n\n")
    for index, block in enumerate(blocks):
        text = block.strip()
        if not text or text.startswith(_NOT_PROSE) or re.match(r"^\d+\.\s", text):
            continue
        plain = _CITATION.sub("", text).strip()
        first = re.split(r"(?<=[.!?])\s+", plain, maxsplit=1)[0]
        if _says_what_it_is(first):
            return markdown
        line = purpose.strip()
        if line and _says_what_it_is(line):
            blocks[index] = line if line.endswith((".", "!", "?")) else f"{line}."
        else:
            del blocks[index]
        return "\n\n".join(blocks)
    return markdown
