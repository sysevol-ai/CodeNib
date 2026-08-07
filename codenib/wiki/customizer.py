# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rewrite a rendered wiki page to a reader's stated prior.

This is a *presentation layer* over the grounded wiki: it takes the markdown a
page already renders and rewrites it to how the reader asked to read it — a
style instruction, an optional section structure, or both. It never touches the
grounded generation pipeline, and it is fail-soft: any problem returns the
original markdown unchanged, so a customization failure degrades to the default
rather than an error.

The transform is instructed to preserve every citation marker and add no new
facts, but its output is NOT re-run through the wiki's grounding guards. Callers
must therefore present a customized page as a reader lens, not as
grounding-verified prose.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You restyle an existing software wiki page to match a reader's stated "
    "preference. You never add, remove, or alter facts, and you never invent "
    "citations."
)

# Bounds enforced here too: a public endpoint must not trust the client's limits.
MAX_INSTRUCTION_CHARS = 2000
MAX_STRUCTURE_SECTIONS = 24
MAX_SECTION_CHARS = 120


def clean_instruction(text: Optional[str]) -> str:
    """Trim and clip a free-text instruction."""
    return (text or "").strip()[:MAX_INSTRUCTION_CHARS]


def clean_structure(sections: Optional[Sequence[str]]) -> List[str]:
    """Normalize a structure template: trim, drop blanks, clip count and length."""
    if not sections:
        return []
    out: List[str] = []
    for raw in sections:
        name = str(raw).strip()[:MAX_SECTION_CHARS]
        if name:
            out.append(name)
        if len(out) >= MAX_STRUCTURE_SECTIONS:
            break
    return out


class Customizer:
    """Applies a reader prior to page markdown via one LLM pass."""

    def __init__(self, llm: Any) -> None:
        """*llm* is any client exposing ``complete(messages, **overrides) -> str``."""
        self._llm = llm

    def apply(
        self,
        markdown: str,
        *,
        instruction: str = "",
        structure: Optional[Sequence[str]] = None,
    ) -> str:
        """Return *markdown* rewritten to the prior, or *markdown* unchanged.

        A no-op (empty markdown or empty prior) returns the input directly
        without calling the model.
        """
        instruction = clean_instruction(instruction)
        structure = clean_structure(structure)
        if not markdown or not (instruction or structure):
            return markdown

        prompt = self._build_prompt(markdown, instruction, structure)
        try:
            out = self._llm.complete(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2048,
                temperature=0.3,
                timeout=90,
            )
        except Exception as exc:  # noqa: BLE001 - any failure falls back to default
            logger.warning("wiki customizer call failed: %s", exc)
            return markdown

        out = (out or "").strip()
        # Strip a wrapping ```markdown fence if the model added one.
        if out.startswith("```"):
            out = out.split("\n", 1)[-1]
            if out.rstrip().endswith("```"):
                out = out.rstrip()[:-3].rstrip()
        return out or markdown

    @staticmethod
    def _build_prompt(markdown: str, instruction: str, structure: List[str]) -> str:
        parts = [
            "Rewrite the wiki page below to match the reader's preference.",
            "",
            "Hard rules:",
            "- Preserve every citation marker exactly as written (tokens such as "
            "[1], [`path/file.py`], or footnote-style references). Do not add or "
            "remove citations.",
            "- Do not introduce any fact, symbol, or claim that is not already in "
            "the page. You are restyling, not researching.",
            "- Keep it valid Markdown. Return only the rewritten page — no "
            "preamble, no explanation.",
        ]
        if instruction:
            parts += ["", f"Reader instruction: {instruction}"]
        if structure:
            listed = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(structure))
            parts += [
                "",
                "Organize the page under exactly these sections, in order, "
                "filling each only with facts already present in the page (omit a "
                "section if the page truly has nothing for it):",
                listed,
            ]
        parts += ["", "--- PAGE START ---", markdown, "--- PAGE END ---"]
        return "\n".join(parts)
