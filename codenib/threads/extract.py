# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic reading of thread text: cleaning and code mentions.

Everything here is regular expressions over text that is treated as data. No
model is involved and nothing found in a thread is executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_DETAILS_RE = re.compile(r"</?(details|summary)[^>]*>", re.I)
_QUOTE_INTRO_RE = re.compile(r"^On .{5,120} wrote:\s*$")
_BLANKS_RE = re.compile(r"\n{3,}")
_LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_FAIL_RE = re.compile(
    r"(?i)(\berror\b|\bfail(ed|ure|ures)?\b|exception|traceback|panicked|"
    r"\bassert|##\[error\])"
)

_SOURCE_EXT = (
    "rs|py|pyi|go|c|h|cc|cpp|hpp|js|jsx|ts|tsx|java|kt|rb|php|swift|scala|lua|cs|"
    "toml|yaml|yml|json|md|sh"
)
_PATH_RE = re.compile(
    rf"(?<![\w/.-])((?:[\w.@+-]+/)*[\w.@+-]+\.(?:{_SOURCE_EXT}))"
    r"(?::(\d+)|#L(\d+)|\", line (\d+)| line (\d+))?"
)
_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]{3,})\(\)")
_PY_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\w+)')

_COMMON_WORDS = frozenset(
    """
    true false none null self this super main test tests error errors result
    option some string str int bool list dict vec type types value values
    data name path file line code print return import from with async await
    default new init args kwargs config index item items key keys node
    """.split()
)


@dataclass(frozen=True)
class Mention:
    """A piece of thread text that points at code."""

    kind: str  # path | symbol | frame
    path: str = ""
    line: int = 0
    symbol: str = ""


def clean_body(body: str) -> str:
    """Drop quoted replies, HTML comments and collapsed-section markup."""

    body = _HTML_COMMENT_RE.sub("", body or "")
    body = _DETAILS_RE.sub("", body)
    lines = [
        line.rstrip()
        for line in body.splitlines()
        if not line.lstrip().startswith(">") and not _QUOTE_INTRO_RE.match(line)
    ]
    return _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def _template(line: str) -> str:
    """A line with numbers, paths and quoted text blanked, to spot repetition."""

    line = re.sub(r"[A-Za-z]:[\\/]\S+|(?:[\w.@+-]+/)+[\w.@+-]+", "<path>", line)
    line = re.sub(r"\b[0-9a-f]{7,40}\b", "<hex>", line)
    line = re.sub(r"\d+", "<n>", line)
    words = line.split()
    # Long lines that differ only after a shared opening are one template.
    return " ".join(words[:4])


def failing_lines(log: str, *, context: int = 2, cap: int = 40) -> str:
    """The lines of a CI log around errors, with repetition folded.

    A tool that reports the same kind of error hundreds of times would fill
    the whole excerpt with one message. Lines that share a template are shown
    twice and then counted, so that different errors, and the summary a tool
    prints at the end, all fit. When still too long, the start and the end of
    the excerpt are kept.
    """

    lines = [_ANSI_RE.sub("", _LOG_TIMESTAMP_RE.sub("", ln)) for ln in log.splitlines()]
    keep: set[int] = set()
    for index, line in enumerate(lines):
        if _FAIL_RE.search(line):
            keep.update(
                range(max(0, index - context), min(len(lines), index + context + 1))
            )
    seen: dict[str, int] = {}
    out: list[str] = []
    folded: dict[str, int] = {}
    for index in sorted(keep):
        line = lines[index][:300]
        if not line.strip():
            continue
        template = _template(line)
        seen[template] = seen.get(template, 0) + 1
        if seen[template] <= 2:
            out.append(line)
        else:
            if template not in folded:
                out.append(f"\x00{template}")  # placeholder for the count
            folded[template] = seen[template] - 2
    out = [
        (
            f"[... {folded[item[1:]]} more lines like the two above ...]"
            if item.startswith("\x00")
            else item
        )
        for item in out
    ]
    if len(out) > cap:
        head = cap // 2
        out = (
            out[:head]
            + [f"[... {len(out) - cap} lines omitted ...]"]
            + out[-(cap - head) :]
        )
    return "\n".join(out)


def elide(text: str, max_chars: int) -> str:
    """Keep the head and the tail of a long text."""

    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.65)
    tail = max_chars - head
    dropped = len(text) - max_chars
    return f"{text[:head].rstrip()}\n[... {dropped} chars omitted ...]\n{text[-tail:].lstrip()}"


def paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def mentions(text: str) -> list[Mention]:
    """Paths, stack frames and symbol names in a text, in order of appearance."""

    found: list[Mention] = []
    seen: set[tuple] = set()

    def add(mention: Mention) -> None:
        key = (mention.kind, mention.path, mention.line, mention.symbol)
        if key not in seen:
            seen.add(key)
            found.append(mention)

    for match in _PY_FRAME_RE.finditer(text):
        add(Mention("frame", match.group(1), int(match.group(2)), match.group(3)))
    for match in _PATH_RE.finditer(text):
        path = match.group(1)
        if path.startswith(("http", "www.")) or "/" not in path and path[0].isdigit():
            continue
        line = next((int(g) for g in match.groups()[1:] if g), 0)
        add(Mention("path", path.lstrip("./"), line))
    for match in _BACKTICK_RE.finditer(text):
        span = match.group(1).strip()
        if " " in span.strip("()") or _PATH_RE.fullmatch(span):
            continue
        idents = _IDENT_RE.findall(span)
        if idents and _is_symbol_like(idents[-1]):
            add(Mention("symbol", symbol=idents[-1]))
    for match in _CALL_RE.finditer(text):
        if _is_symbol_like(match.group(1)):
            add(Mention("symbol", symbol=match.group(1)))
    return found


@lru_cache(maxsize=4096)
def _is_symbol_like(name: str) -> bool:
    if len(name) < 4 or name.lower() in _COMMON_WORDS or name.startswith("__"):
        return False
    # snake_case, CamelCase or SCREAMING_CASE; a plain lowercase word is prose.
    return "_" in name or (name != name.lower() and name != name.upper())


__all__ = ["Mention", "clean_body", "elide", "failing_lines", "mentions", "paragraphs"]
