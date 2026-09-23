# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Fenced-code handling shared by the Wiki renderer and its quality gates.

A ``re.sub(r"```[\\s\\S]*?```", ...)`` stops at the first triple backtick it
meets, so a fenced excerpt whose *source* contains ``` (a Rust doc comment,
a Markdown fixture) leaks half its lines into the prose the gates read, and
an evidence id quoted inside code reads as narration.  Fences open with three
or more backticks or tildes and close only on a line made of at least that
many of the same mark, which is also how the renderer escapes such sources.
"""

from __future__ import annotations

import re
from typing import List, Tuple

_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _fence_spans(lines: List[str]) -> List[Tuple[int, int]]:
    """Return ``(start, end)`` line spans (end exclusive) of fenced blocks."""

    spans: List[Tuple[int, int]] = []
    index = 0
    while index < len(lines):
        match = _FENCE_OPEN.match(lines[index])
        if not match:
            index += 1
            continue
        marker = match.group(1)
        closer = re.compile(
            rf"^[ \t]{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$"
        )
        end = index + 1
        while end < len(lines) and not closer.match(lines[end]):
            end += 1
        spans.append((index, min(end + 1, len(lines))))
        index = end + 1
    return spans


def strip_code_fences(markdown: str) -> str:
    """Drop fenced code, matching each opener with a closer of its own length."""

    lines = (markdown or "").split("\n")
    spans = _fence_spans(lines)
    if not spans:
        return markdown or ""
    kept: List[str] = []
    cursor = 0
    for start, end in spans:
        kept.extend(lines[cursor:start])
        cursor = end
    kept.extend(lines[cursor:])
    return "\n".join(kept)


def count_code_fences(markdown: str) -> int:
    """Count fenced code blocks the way ``strip_code_fences`` delimits them."""

    return len(_fence_spans((markdown or "").split("\n")))


def fence_marker(lines, minimum: int = 3) -> str:
    """Return a backtick run long enough to fence ``lines`` unambiguously."""

    longest = 0
    for line in lines:
        for run in re.findall(r"`+", line):
            longest = max(longest, len(run))
    return "`" * max(minimum, longest + 1)


__all__ = ["count_code_fences", "fence_marker", "strip_code_fences"]
