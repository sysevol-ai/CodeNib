# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Source excerpts that keep their definition in view.

An excerpt cropped to the lines a section talks about starts in the middle of
a function: the reader sees a body with no signature and cannot tell where it
lives. Every excerpt here carries real (1-based) file line numbers and, when
the window starts inside a definition, that definition's signature followed
by a gap row for the skipped lines.

Fence format::

    ```go at=288,300 hl=301,303
    func (n *node) insertChild(path string, fullPath string) {
    ⋯
    <lines 300.. of the file>
    ```

``at=`` lists the first file line of each segment, and segments are separated
by a line holding only :data:`GAP`. When ``at=`` is present, ``hl=`` names
absolute file lines.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, Optional

GAP = "⋯"
EXCERPT_VERSION = 1
# A gap this small is shown rather than elided: "⋯ 1 line" hides nothing.
MERGE_GAP = 2
_MAX_SIGNATURE_LINES = 12
# A longer signature shows only its first line, which names the function.
_MAX_SHOWN_SIGNATURE = 4
_PREAMBLE = ("//", "#", "/*", "*", "--")
# Evidence about a whole file has no signature to pin.
_NOT_DEFINITIONS = {"", "file", "module", "document", "section", "readme"}
_FENCE = re.compile(
    r"^(?P<marker>`{3,})(?P<info>[^\n`]*)\n(?P<body>.*?)\n(?P=marker)[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_CITE = re.compile(r"\[(E\d+)\]")


def signature_span(lines: Sequence[str]) -> tuple[int, int]:
    """Return ``(offset, length)`` of the signature in a definition's lines.

    Leading comments and blank lines are skipped. The signature runs to the
    first line ending in ``:`` or ``{`` (Python, and brace languages whose
    brace sits on the signature or the following line). One longer than four
    lines, or without such an end, is represented by its first line.
    """

    offset = 0
    while offset < len(lines) and (
        not lines[offset].strip() or lines[offset].strip().startswith(_PREAMBLE)
    ):
        offset += 1
    if offset >= len(lines):
        return 0, 0
    name_line = offset
    if lines[offset].lstrip().startswith("@"):
        indentation = len(lines[offset]) - len(lines[offset].lstrip())
        for index in range(offset + 1, len(lines)):
            line = lines[index]
            if len(line) - len(
                line.lstrip()
            ) == indentation and line.lstrip().startswith(
                ("def ", "async def ", "class ")
            ):
                name_line = index
                break
    for index in range(offset, min(len(lines), offset + _MAX_SIGNATURE_LINES)):
        if lines[index].rstrip().endswith((":", "{")):
            length = index - offset + 1
            if length <= _MAX_SHOWN_SIGNATURE:
                return offset, length
            # A collapsed decorated signature must still show the definition.
            return name_line, 1
    return name_line, 1


def segments(
    lines: Sequence[str],
    first_line: int,
    begin: int,
    end: int,
    *,
    definition: bool = True,
) -> list[tuple[int, list[str]]]:
    """Segments showing the signature of *lines*, then ``lines[begin:end]``.

    *lines* is a definition's source starting at file line *first_line*;
    *begin*/*end* index the window inside it. Returns ``(start_line, lines)``
    pairs, one when the window already reaches the signature.
    """

    while end > begin and not lines[end - 1].strip():
        end -= 1
    offset, length = signature_span(lines) if definition else (begin, 0)
    if length == 0 or begin <= offset + length + MERGE_GAP:
        start = min(offset, begin) if length else begin
        return [(first_line + start, list(lines[start:end]))]
    return [
        (first_line + offset, list(lines[offset : offset + length])),
        (first_line + begin, list(lines[begin:end])),
    ]


def fence_body(parts: Sequence[tuple[int, Sequence[str]]]) -> tuple[str, str]:
    """Return the ``at=`` value and the fence body for *parts*."""

    body: list[str] = []
    for index, (_start, part) in enumerate(parts):
        if index:
            body.append(GAP)
        body.extend(line.rstrip() for line in part)
    return ",".join(str(start) for start, _part in parts), "\n".join(body)


def _locate(window: Sequence[str], source: Sequence[str]) -> Optional[list[int]]:
    """Indices in *source* of the non-blank *window* lines, matched in order.

    Excerpts drop blank lines and the evidence text lost its first line's
    indentation, so lines compare stripped and blank source lines are skipped.
    """

    want = [line.strip() for line in window if line.strip()]
    if not want:
        return None
    for first, line in enumerate(source):
        if line.strip() != want[0]:
            continue
        found, cursor = [], first
        while cursor < len(source) and len(found) < len(want):
            text = source[cursor].strip()
            if text:
                if text != want[len(found)]:
                    break
                found.append(cursor)
            cursor += 1
        if len(found) == len(want):
            return found
    return None


def _highlights(info: str, line_count: int) -> list[int]:
    match = re.search(r"(?:^|\s)hl=([\d,-]+)", info)
    lines: list[int] = []
    for part in (match.group(1) if match else "").split(","):
        try:
            bounds = [int(value) for value in part.split("-") if value.isdigit()]
        except ValueError:
            continue
        if bounds:
            lines.extend(range(max(1, bounds[0]), min(line_count, bounds[-1]) + 1))
    return lines


def anchor_excerpts(
    markdown: str,
    evidence: Sequence[dict[str, Any]],
    read_lines: Callable[[str, int, int], Optional[Sequence[str]]],
) -> str:
    """Give each cited code excerpt in *markdown* real lines and its signature.

    An excerpt fence is followed by a caption citing one evidence item
    (``Source excerpt from `f`. [E2]``). Its window is located in that item's
    source, read through ``read_lines(file, start_line, end_line)`` (1-based,
    inclusive). Fences already anchored (``at=``), diagrams, and windows that
    cannot be located are left unchanged.
    """

    items = {str(item.get("id")): item for item in evidence if item.get("id")}
    out, cursor = [], 0
    for match in _FENCE.finditer(markdown):
        info = match.group("info").strip()
        language = info.split()[0] if info else ""
        after = markdown[match.end() :].lstrip("\n").split("\n\n", 1)[0]
        cited = _CITE.search(after)
        item = items.get(cited.group(1)) if cited else None
        if (
            language in {"", "mermaid"}
            or " at=" in f" {info}"
            or item is None
            or not item.get("file")
            or not isinstance(item.get("start_line"), int)
        ):
            continue
        first_line = item["start_line"]
        last_line = item.get("end_line") or first_line
        source = read_lines(item["file"], first_line, last_line)
        window = match.group("body").split("\n")
        found = _locate(window, source) if source else None
        if not found:
            continue
        parts = segments(
            source,
            first_line,
            found[0],
            found[-1] + 1,
            definition=str(item.get("kind") or "").lower() not in _NOT_DEFINITIONS,
        )
        at, body = fence_body(parts)
        non_blank = [line for line in window if line.strip()]
        marked = [
            first_line + found[index - 1]
            for index in _highlights(info, len(non_blank))
            if 0 < index <= len(non_blank)
        ]
        head = f"{language} at={at}"
        if marked:
            head += " hl=" + ",".join(str(line) for line in marked)
        marker = match.group("marker")
        out.append(markdown[cursor : match.start()])
        out.append(f"{marker}{head}\n{body}\n{marker}")
        cursor = match.end()
    out.append(markdown[cursor:])
    return "".join(out)
