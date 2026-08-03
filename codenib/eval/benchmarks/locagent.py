# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free parser for LocAgent's ranked localization output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

_PATH_TOKEN = re.compile(r"(?:[A-Za-z]:)?(?:[\w.@+-]+[\\/])*[\w.@+-]+\.[A-Za-z0-9]+")
_FIELD = re.compile(
    r"^(?:[-*]\s*)?" r"(lines?|class(?:es)?|functions?|methods?)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
_LINE_RANGE = re.compile(r"(\d+)(?:\s*(?:-|:|to)\s*(\d+))?", re.IGNORECASE)
_SOURCE_EXTENSIONS = frozenset(
    {
        "c",
        "cc",
        "cpp",
        "cs",
        "go",
        "h",
        "hpp",
        "java",
        "js",
        "jsx",
        "kt",
        "php",
        "py",
        "rb",
        "rs",
        "scala",
        "swift",
        "ts",
        "tsx",
    }
)


def _normalize_path(value: object) -> str:
    path = str(value or "").strip().strip("`'\".,:;()[]{}")
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _match_valid_path(candidate: str, valid_files: Sequence[str]) -> str:
    normalized = _normalize_path(candidate)
    if not valid_files:
        extension = normalized.rsplit(".", 1)[-1].lower()
        return normalized if extension in _SOURCE_EXTENSIONS else ""
    exact = {path: path for path in valid_files}
    if normalized in exact:
        return exact[normalized]
    suffixes = [
        path
        for path in valid_files
        if normalized == path or normalized.endswith("/" + path)
    ]
    return max(suffixes, key=len) if suffixes else ""


def _extract_path(line: str, valid_files: Sequence[str]) -> tuple[bool, str, str]:
    if valid_files:
        candidate_line = line.strip().strip("`").strip()
        if candidate_line.startswith(("- ", "* ")):
            candidate_line = candidate_line[2:].strip()
        candidate_line = candidate_line.strip("`'\" ")
        normalized_line = candidate_line.replace("\\", "/")
        for valid_path in sorted(valid_files, key=len, reverse=True):
            start = normalized_line.find(valid_path)
            while start >= 0:
                prefix_ok = start == 0 or normalized_line[start - 1] == "/"
                remainder = normalized_line[start + len(valid_path) :].strip()
                suffix_ok = not remainder or remainder.startswith(":")
                if prefix_ok and suffix_ok:
                    tail = remainder[1:].strip() if remainder.startswith(":") else ""
                    return True, valid_path, tail
                start = normalized_line.find(valid_path, start + 1)

    for match in _PATH_TOKEN.finditer(line):
        candidate = _normalize_path(match.group(0))
        extension = candidate.rsplit(".", 1)[-1].lower()
        file_path = _match_valid_path(match.group(0), valid_files)
        if extension not in _SOURCE_EXTENSIONS and not file_path:
            continue
        tail = line[match.end() :].strip()
        if tail.startswith(":"):
            tail = tail[1:].strip()
        else:
            tail = ""
        return True, file_path, tail
    return False, "", ""


def _parse_ranges(value: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _LINE_RANGE.finditer(value):
        start = max(1, int(match.group(1)))
        end = max(start, int(match.group(2) or start))
        ranges.append((start, end))
    return ranges


def _parse_symbols(value: str) -> list[str]:
    symbols: list[str] = []
    for raw in re.split(r"\s*[,;]\s*", value):
        symbol = raw.strip().strip("`'\" ")
        symbol = re.sub(r"\s*\([^)]*\)\s*$", "", symbol)
        if symbol:
            symbols.append(symbol)
    return symbols


@dataclass(frozen=True, slots=True)
class LocAgentLocation:
    """One ranked file, class, function, or line location from LocAgent."""

    file_path: str
    class_name: str = ""
    function_name: str = ""
    line_start: int | None = None
    line_end: int | None = None

    def to_record(self) -> dict[str, str | int | None]:
        """Return a stable JSON-ready representation."""

        return {
            "file_path": self.file_path,
            "class_name": self.class_name,
            "function_name": self.function_name,
            "line_start": self.line_start,
            "line_end": self.line_end,
        }


@dataclass(slots=True)
class _LocationBlock:
    file_path: str
    class_name: str = ""
    functions: list[str] = field(default_factory=list)
    ranges: list[tuple[int, int]] = field(default_factory=list)


def _flush_block(
    block: _LocationBlock | None,
    output: list[LocAgentLocation],
) -> None:
    if block is None:
        return
    functions = block.functions
    ranges = block.ranges
    if functions:
        for index, function_name in enumerate(functions):
            line_range = ranges[min(index, len(ranges) - 1)] if ranges else None
            output.append(
                LocAgentLocation(
                    file_path=block.file_path,
                    class_name=block.class_name,
                    function_name=function_name,
                    line_start=line_range[0] if line_range else None,
                    line_end=line_range[1] if line_range else None,
                )
            )
        for start, end in ranges[len(functions) :]:
            output.append(
                LocAgentLocation(
                    file_path=block.file_path,
                    line_start=start,
                    line_end=end,
                )
            )
    elif block.class_name:
        line_range = ranges[0] if ranges else None
        output.append(
            LocAgentLocation(
                file_path=block.file_path,
                class_name=block.class_name,
                line_start=line_range[0] if line_range else None,
                line_end=line_range[1] if line_range else None,
            )
        )
        for start, end in ranges[1:]:
            output.append(
                LocAgentLocation(
                    file_path=block.file_path,
                    line_start=start,
                    line_end=end,
                )
            )
    elif ranges:
        output.extend(
            LocAgentLocation(
                file_path=block.file_path,
                line_start=start,
                line_end=end,
            )
            for start, end in ranges
        )
    else:
        output.append(LocAgentLocation(file_path=block.file_path))


def parse_locagent_locations(
    raw_output: str,
    *,
    valid_files: Iterable[str] = (),
) -> tuple[LocAgentLocation, ...]:
    """Parse LocAgent's fenced file/class/function/line response.

    ``valid_files`` makes absolute-path stripping and hallucinated-path rejection
    deterministic. The parser does not read source files or assume Python.
    """

    files = tuple(
        dict.fromkeys(
            path for path in (_normalize_path(value) for value in valid_files) if path
        )
    )
    output: list[LocAgentLocation] = []
    block: _LocationBlock | None = None

    for raw_line in str(raw_output or "").splitlines():
        line = raw_line.strip().strip("`").strip()
        if not line:
            continue

        field_match = _FIELD.match(line)
        if field_match is not None:
            if block is None:
                continue
            field = field_match.group(1).lower()
            value = field_match.group(2)
            if field.startswith("line"):
                block.ranges.extend(_parse_ranges(value))
            elif field.startswith("class"):
                classes = _parse_symbols(value)
                if classes:
                    block.class_name = classes[0]
            else:
                block.functions.extend(_parse_symbols(value))
            continue

        has_path, file_path, inline_symbol = _extract_path(line, files)
        if has_path:
            _flush_block(block, output)
            block = _LocationBlock(file_path=file_path) if file_path else None
            if block is not None and inline_symbol:
                block.functions.extend(_parse_symbols(inline_symbol))
            continue

    _flush_block(block, output)

    deduplicated: list[LocAgentLocation] = []
    seen: set[tuple[object, ...]] = set()
    for location in output:
        key = (
            location.file_path,
            location.class_name,
            location.function_name,
            location.line_start,
            location.line_end,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(location)
    return tuple(deduplicated)


def locagent_regions(
    raw_output: str,
    *,
    valid_files: Iterable[str] = (),
) -> tuple[dict[str, int | str], ...]:
    """Convert parsed locations to deduplicated benchmark source regions."""

    regions: list[dict[str, int | str]] = []
    seen: set[tuple[str, int, int]] = set()
    for location in parse_locagent_locations(raw_output, valid_files=valid_files):
        start = location.line_start or 1
        end = location.line_end if location.line_end is not None else -1
        key = (location.file_path, start, end)
        if key in seen:
            continue
        seen.add(key)
        regions.append({"path": location.file_path, "start": start, "end": end})
    return tuple(regions)


__all__ = [
    "LocAgentLocation",
    "locagent_regions",
    "parse_locagent_locations",
]
