# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free parsing for classic Agentless localization output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

_FIELD = re.compile(
    r"^(?:[-*]\s*)?"
    r"(lines?|class(?:es)?|functions?|methods?|variables?)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_LINE_RANGE = re.compile(r"(-?\d+)(?:\s*(?:-|:|to)\s*(-?\d+))?", re.IGNORECASE)
_LIST_PREFIX = re.compile(r"^(?:[-*]\s+|\d+[.)]\s+)")


def _normalize(value: object) -> str:
    text = _LIST_PREFIX.sub("", str(value or "").strip())
    text = text.strip("`'\"()[]{} ").rstrip(",;").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _match_file(line: str, valid_files: Sequence[str], root_name: str) -> str:
    candidate = _normalize(line).strip("# ")
    if candidate.lower().startswith("file:"):
        candidate = _normalize(candidate.split(":", 1)[1]).strip("# ")
    roots = tuple(filter(None, (root_name.strip("/"),)))
    for file_path in sorted(valid_files, key=len, reverse=True):
        accepted = {file_path}
        accepted.update(f"{root}/{file_path}" for root in roots)
        if candidate in accepted:
            return file_path
        if any(candidate.startswith(value + ":") for value in accepted):
            return file_path
    suffix_matches = tuple(
        file_path for file_path in valid_files if file_path.endswith("/" + candidate)
    )
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return ""


def _symbols(value: str) -> list[str]:
    output: list[str] = []
    for raw in re.split(r"\s*[,;]\s*", value):
        symbol = raw.strip().strip("`'\" ")
        if symbol:
            output.append(symbol)
    return output


@dataclass(frozen=True, slots=True)
class AgentlessLocation:
    """One file, symbol, variable, or line emitted by Agentless."""

    file_path: str
    kind: str = "file"
    name: str = ""
    line_start: int | None = None
    line_end: int | None = None

    def to_record(self) -> dict[str, str | int | None]:
        return {
            "file_path": self.file_path,
            "kind": self.kind,
            "name": self.name,
            "line_start": self.line_start,
            "line_end": self.line_end,
        }


def parse_agentless_files(
    raw_output: str,
    *,
    valid_files: Iterable[str],
    root_name: str = "",
    limit: int | None = None,
) -> tuple[str, ...]:
    """Parse the ranked file-only response used by Agentless stage one."""

    files = tuple(dict.fromkeys(_normalize(path) for path in valid_files if path))
    output: list[str] = []
    seen: set[str] = set()
    for line in str(raw_output or "").splitlines():
        file_path = _match_file(line, files, root_name)
        if not file_path or file_path in seen:
            continue
        seen.add(file_path)
        output.append(file_path)
        if limit is not None and len(output) >= limit:
            break
    return tuple(output)


def parse_agentless_locations(
    raw_output: str,
    *,
    valid_files: Iterable[str],
    root_name: str = "",
) -> tuple[AgentlessLocation, ...]:
    """Parse Agentless's fenced file/class/function/variable/line blocks."""

    files = tuple(dict.fromkeys(_normalize(path) for path in valid_files if path))
    output: list[AgentlessLocation] = []
    current_file = ""
    current_has_detail = False

    def flush_file() -> None:
        nonlocal current_has_detail
        if current_file and not current_has_detail:
            output.append(AgentlessLocation(file_path=current_file))
        current_has_detail = False

    for raw_line in str(raw_output or "").splitlines():
        line = raw_line.strip().strip("`").strip()
        if not line:
            continue
        file_path = _match_file(line, files, root_name)
        if file_path:
            flush_file()
            current_file = file_path
            continue
        if not current_file:
            continue
        match = _FIELD.match(line)
        if match is None:
            continue
        field = match.group(1).lower()
        value = match.group(2)
        current_has_detail = True
        if field.startswith("line"):
            for line_match in _LINE_RANGE.finditer(value):
                start = int(line_match.group(1))
                end = int(line_match.group(2) or start)
                if start < 1 or end < start:
                    continue
                output.append(
                    AgentlessLocation(
                        file_path=current_file,
                        kind="line",
                        line_start=start,
                        line_end=end,
                    )
                )
            continue
        if field.startswith("class"):
            kind = "class"
        elif field.startswith("variable"):
            kind = "variable"
        elif field.startswith("method"):
            kind = "method"
        else:
            kind = "function"
        symbols = _symbols(value)
        if kind == "variable":
            symbols = [name for symbol in symbols for name in symbol.split()]
        output.extend(
            AgentlessLocation(file_path=current_file, kind=kind, name=symbol)
            for symbol in symbols
        )
    flush_file()

    deduplicated: list[AgentlessLocation] = []
    seen: set[tuple[object, ...]] = set()
    for location in output:
        key = (
            location.file_path,
            location.kind,
            location.name,
            location.line_start,
            location.line_end,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(location)
    return tuple(deduplicated)


__all__ = [
    "AgentlessLocation",
    "parse_agentless_files",
    "parse_agentless_locations",
]
