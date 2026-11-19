# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Source excerpts that keep their definition in view.

An excerpt cropped to the lines a section talks about starts in the middle of
a function: the reader sees a body with no signature and cannot tell where it
lives. Every excerpt here carries real (1-based) file line numbers and, when
the window starts inside a definition, that definition's signature followed
by a gap row for the skipped lines.

Fence format::

    ```go at=288,300 hl=301,303 cite=E2
    func (n *node) insertChild(path string, fullPath string) {
    ⋯
    <lines 300.. of the file>
    ```

``at=`` lists the first file line of each segment, and segments are separated
by a line holding only :data:`GAP`. When ``at=`` is present, ``hl=`` names
absolute file lines. ``cite=`` names the evidence item the excerpt was cut
from, so a reader can widen it to the rest of that definition.

What the window shows and what it marks follow the caption. A *What to
notice* caption that names identifiers (`` `MissingSchema` ``) marks the code
lines using them and centres the window there; comments and docstrings are
never marked. Without such names nothing is marked, and a window that is
mostly docstring moves to the code below it. Either way the window does not
stop inside an open bracket.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, Optional

GAP = "⋯"
# 2: windows follow the caption, marks only name lines it mentions, ``cite=``.
EXCERPT_VERSION = 2
# A gap this small is shown rather than elided: "⋯ 1 line" hides nothing.
MERGE_GAP = 2
# Lines of a focused window before the reader asks for more.
_MAX_WINDOW = 16
# Context kept around the first and last marked line.
_CONTEXT = 3
# How far a window may grow to close a bracket it opened, or open one it closes.
_MAX_BALANCE = 8
_MAX_MARKS = 6
# A caption name used on more lines than this is not a pointer to any one.
_MAX_TERM_LINES = 4
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
_NOTICE = re.compile(r"^\s*\*What to notice:\*")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`")
_TRIPLE = re.compile(r"\"\"\"|'''")
# C preprocessor lines and Rust attributes start with ``#`` but are code.
_DIRECTIVE = re.compile(
    r"#(?:!?\[|\s*(?:include|define|undef|if|ifdef|ifndef|elif|else|endif|pragma|error)\b)"
)
_OPENERS, _CLOSERS = "([{", ")]}"
_CLOSER_LINE = re.compile(r"[)\]}]+[;,]?")


def notice_terms(caption: str) -> list[str]:
    """Code spans a *What to notice* caption names, as text to find in code.

    Each span yields itself without call parentheses (``self.url``) and its
    last identifier (``url``); ``a.b()`` and ``a::b`` both end in ``b``. Any
    other caption names nothing: "Source excerpt from `f`" is about the whole
    excerpt, not a line of it.
    """

    if not _NOTICE.match(caption or ""):
        return []
    terms: list[str] = []
    for span in _CODE_SPAN.findall(caption):
        text = re.sub(r"\([^()]*\)$", "", span.strip()).strip()
        names = _IDENTIFIER.findall(text)
        for term in (text, names[-1] if names else ""):
            if len(term) >= 3 and term not in terms:
                terms.append(term)
    return terms


def _is_comment(text: str) -> bool:
    if text.startswith(("//", "/*", "-- ")) or text in {"*", "#"}:
        return True
    # A block comment's continuation, not ``**kwargs`` or ``*ptr = 0;``.
    if text.startswith(("* ", "*/")):
        return True
    return text.startswith("#") and not _DIRECTIVE.match(text)


def prose_lines(lines: Sequence[str]) -> list[bool]:
    """Which lines are comments or docstring text rather than code."""

    flags: list[bool] = []
    in_doc = False
    for line in lines:
        text = line.strip()
        quotes = len(_TRIPLE.findall(text))
        starts_doc = text.startswith(('"""', "'''", 'r"""', "r'''"))
        flags.append(in_doc or starts_doc or _is_comment(text))
        if (in_doc or starts_doc) and quotes % 2:
            in_doc = not in_doc
    return flags


def signature_end(lines: Sequence[str]) -> int:
    """Index of the first line after a definition's whole signature.

    Unlike :func:`signature_span`, a long signature counts in full: a window
    that moves must not start among its parameters.
    """

    offset, length = signature_span(lines)
    if length == 0:
        return 0
    for index in range(offset, min(len(lines), offset + _MAX_SIGNATURE_LINES)):
        if lines[index].rstrip().endswith((":", "{")):
            return max(index + 1, offset + length)
    return offset + length


def _depth_change(line: str, *, braces: bool = False) -> int:
    """Net brackets a line opens: parentheses and square brackets, and braces
    only when they hold expressions (*braces*, Python's dict and set
    literals). A brace language's ``{`` opens a block that a window may
    rightly leave open."""

    code = _STRING.sub("", line)
    for marker in ("#", "//"):
        # Only a marker that starts the remaining text or follows a space is a
        # comment; ``a//b`` and ``"#"`` (already blanked) are not.
        at = code.find(f" {marker}")
        if code.lstrip().startswith(marker):
            code = ""
        elif at >= 0:
            code = code[:at]
    openers, closers = (_OPENERS, _CLOSERS) if braces else ("([", ")]")
    return sum(code.count(c) for c in openers) - sum(code.count(c) for c in closers)


def balanced(
    lines: Sequence[str],
    begin: int,
    end: int,
    *,
    floor: int = 0,
    braces: bool = False,
) -> tuple[int, int]:
    """Grow ``lines[begin:end]`` so it neither opens nor closes a bracket alone.

    A window ending in ``raise Error(`` gains the lines up to its ``)``; one
    starting on the arguments of a call gains the line that opens it. A side
    that a few lines cannot balance is left as it was, and the window never
    reaches above *floor*.
    """

    def scan(first: int, last: int) -> tuple[int, int]:
        lowest = depth = 0
        for line in lines[first:last]:
            depth += _depth_change(line, braces=braces)
            lowest = min(lowest, depth)
        return lowest, depth

    if scan(begin, end)[0] < 0:
        for step in range(1, _MAX_BALANCE + 1):
            if begin - step < floor:
                break
            if scan(begin - step, end)[0] >= 0:
                begin -= step
                break
    if scan(begin, end)[1] > 0:
        for step in range(1, _MAX_BALANCE + 1):
            if end + step > len(lines):
                break
            if scan(begin, end + step)[1] <= 0:
                end += step
                break
    return begin, end


def focus_window(
    lines: Sequence[str],
    begin: int,
    end: int,
    terms: Sequence[str],
    *,
    body: int = 0,
    braces: bool = False,
    skip_docstring: bool = True,
) -> tuple[int, int, list[int]]:
    """Return the window to show in a definition and the indices to mark.

    *begin*/*end* is the window the generator chose. Lines using a caption
    *term* are marked and the window moves to the densest run of them;
    without one, a window that is mostly docstring moves to the code after it.
    *body* is the first line after the signature, the lowest a window that
    moves may start. *skip_docstring* is off for a caption that may be about
    the docstring itself; *braces* says braces hold expressions.
    """

    prose = prose_lines(lines)

    def uses(term: str, line: str) -> bool:
        if _IDENTIFIER.fullmatch(term):
            # A whole word: `scheme` is not a mark on `schemes`.
            return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", line) is not None
        return term in line

    code = [
        index
        for index in range(body, len(lines))
        if lines[index].strip() and not prose[index]
    ]
    targets: set[int] = set()
    for term in terms:
        hits = [index for index in code if uses(term, lines[index])]
        # A name on many lines (`path`, `self`) points at none of them.
        if len(hits) <= _MAX_TERM_LINES:
            targets.update(hits)
    targets = sorted(targets)
    if targets:
        best: list[int] = []
        for first in targets:
            run = [
                t for t in targets if first <= t < first + _MAX_WINDOW - 2 * _CONTEXT
            ]
            if len(run) > len(best):
                best = run
        begin = max(body, best[0] - _CONTEXT)
        end = min(len(lines), best[-1] + _CONTEXT + 1)
    else:
        shown = range(begin, end)
        if skip_docstring and shown and sum(prose[i] for i in shown) * 2 > len(shown):
            code = next(
                (
                    i
                    for i in range(max(begin, body), len(lines))
                    if lines[i].strip() and not prose[i]
                ),
                None,
            )
            if code is not None:
                begin, end = code, min(len(lines), code + _MAX_WINDOW)
    begin, end = balanced(lines, begin, end, floor=body, braces=braces)
    # Nor does it open on blank lines or on the `}` that ends the block above.
    while begin < end - 1 and (
        not lines[begin].strip() or _CLOSER_LINE.fullmatch(lines[begin].strip())
    ):
        begin += 1
    shown = [t for t in targets if begin <= t < end]
    # Trailing comments introduce code the window does not show.
    while end > begin + 1 and (
        not lines[end - 1].strip() or _is_comment(lines[end - 1].strip())
    ):
        if shown and end - 1 <= shown[-1]:
            break
        end -= 1
    marks = shown[:_MAX_MARKS]
    return begin, end, marks


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


def _anchored_window(
    info: str, body: str, source: Sequence[str], first_line: int
) -> Optional[tuple[int, int]]:
    """The focus window of an already anchored fence, as indices in *source*.

    The last segment is the window; earlier ones are the signature. ``None``
    when the fence no longer matches the source it claims to show.
    """

    match = re.search(r"(?:^|\s)at=([\d,]+)", info)
    starts = [
        int(value) for value in (match.group(1) if match else "").split(",") if value
    ]
    parts: list[list[str]] = [[]]
    for row in body.split("\n"):
        if row.strip() == GAP:
            parts.append([])
        else:
            parts[-1].append(row)
    if not starts or len(parts) != len(starts):
        return None
    begin = starts[-1] - first_line
    end = begin + len(parts[-1])
    if begin < 0 or end > len(source) or end <= begin:
        return None
    if [line.strip() for line in source[begin:end]] != [
        line.strip() for line in parts[-1]
    ]:
        return None
    return begin, end


def anchor_excerpts(
    markdown: str,
    evidence: Sequence[dict[str, Any]],
    read_lines: Callable[[str, int, int], Optional[Sequence[str]]],
) -> str:
    """Give each cited code excerpt in *markdown* real lines and its signature.

    An excerpt fence is followed by a caption citing one evidence item
    (``Source excerpt from `f`. [E2]``). Its window is located in that item's
    source, read through ``read_lines(file, start_line, end_line)`` (1-based,
    inclusive), then focused on what the caption names (see
    :func:`focus_window`). Fences anchored by an earlier version are focused
    again from their own lines. Diagrams and windows that cannot be located
    are left unchanged.
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
            or item is None
            or not item.get("file")
            or not isinstance(item.get("start_line"), int)
        ):
            continue
        first_line = item["start_line"]
        last_line = item.get("end_line") or first_line
        source = read_lines(item["file"], first_line, last_line)
        if not source:
            continue
        if " at=" in f" {info}":
            window = _anchored_window(info, match.group("body"), source, first_line)
        else:
            found = _locate(match.group("body").split("\n"), source)
            window = (found[0], found[-1] + 1) if found else None
        if window is None:
            continue
        definition = str(item.get("kind") or "").lower() not in _NOT_DEFINITIONS
        begin, end, marks = focus_window(
            source,
            *window,
            notice_terms(after),
            body=signature_end(source) if definition else 0,
            braces=language in {"python", "py"},
            # A note may point at the docstring; only a bare excerpt skips it.
            skip_docstring=not _NOTICE.match(after),
        )
        at, body = fence_body(
            segments(source, first_line, begin, end, definition=definition)
        )
        head = f"{language} at={at}"
        if marks:
            head += " hl=" + ",".join(str(first_line + index) for index in marks)
        head += f" cite={cited.group(1)}"
        marker = match.group("marker")
        out.append(markdown[cursor : match.start()])
        out.append(f"{marker}{head}\n{body}\n{marker}")
        cursor = match.end()
    out.append(markdown[cursor:])
    return "".join(out)
