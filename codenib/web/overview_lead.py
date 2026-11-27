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


_WORD = re.compile(r"[a-z][a-z0-9]+")
_COMMON = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "with",
        "its",
        "into",
        "from",
        "a",
        "an",
        "is",
        "are",
        "of",
        "to",
        "in",
        "on",
        "as",
        "by",
        "it",
        "this",
    }
)
_SENTENCE = re.compile(r"(?<=[.!?])(?:\s*\[E\d+\]\(#evidence-E\d+\))*\s+")


def _content(sentence: str) -> list[str]:
    plain = _CITATION.sub("", sentence).replace("`", "").lower()
    return [word for word in _WORD.findall(plain) if word not in _COMMON]


def _without_restatement(block: str) -> str:
    """Drop a lead sentence that says again what the one beside it says.

    jq's lead said what jq is twice: "jq is a command-line JSON processor
    akin to sed ..." and "jq is a portable C command-line JSON processor
    that applies a filter program ...". Two sentences with the same subject
    that share three more words are one statement; the fuller one stays.
    """
    parts = [
        part
        for part in re.split(r"(?<=\))\s+(?=[A-Z`])|(?<=[.!?])\s+(?=[A-Z`])", block)
        if part.strip()
    ]
    if len(parts) < 2:
        return block
    kept = [parts[0]]
    for part in parts[1:]:
        previous = kept[-1]
        a, b = _content(previous), _content(part)
        if a and b and a[0] == b[0] and len((set(a) & set(b)) - {a[0]}) >= 3:
            if len(b) > len(a):
                kept[-1] = part
            continue
        kept.append(part)
    return " ".join(kept) if len(kept) < len(parts) else block


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
            blocks[index] = _without_restatement(block)
            return "\n\n".join(blocks)
        line = purpose.strip()
        if line and _says_what_it_is(line):
            blocks[index] = line if line.endswith((".", "!", "?")) else f"{line}."
        else:
            del blocks[index]
        return "\n\n".join(blocks)
    return markdown
