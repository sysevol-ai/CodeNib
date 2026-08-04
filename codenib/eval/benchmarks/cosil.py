# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Dependency-free parsers for CoSIL file and XML location output."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, Sequence

_FENCED_BLOCK = re.compile(r"```(?:[^\n]*\n)?(.*?)```", re.DOTALL)
_LOCATIONS_XML = re.compile(r"<locations\b[^>]*>.*?</locations>", re.DOTALL)


def _normalize(value: object) -> str:
    text = str(value or "").strip().strip("`'\".,;()[]{} ")
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _match_file(value: object, valid_files: Sequence[str], root_name: str) -> str:
    candidate = _normalize(value)
    prefix = root_name.strip("/") + "/" if root_name.strip("/") else ""
    if prefix and candidate.startswith(prefix):
        candidate = candidate[len(prefix) :]
    return candidate if candidate in valid_files else ""


def parse_cosil_files(
    raw_output: str,
    *,
    valid_files: Iterable[str],
    root_name: str = "",
    limit: int = 5,
) -> tuple[str, ...]:
    """Parse CoSIL's fenced ranked-file response."""

    files = tuple(dict.fromkeys(_normalize(path) for path in valid_files if path))
    if str(raw_output or "").count("```") % 2:
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for block in _FENCED_BLOCK.findall(str(raw_output or "")):
        for line in block.splitlines():
            file_path = _match_file(line.strip(), files, root_name)
            if not file_path or file_path in seen:
                continue
            seen.add(file_path)
            output.append(file_path)
            if len(output) >= max(1, int(limit)):
                return tuple(output)
    return tuple(output)


@dataclass(frozen=True, slots=True)
class CoSILLocation:
    """One source-linked location from CoSIL's final XML summary."""

    file_path: str
    kind: str
    name: str

    def to_record(self) -> dict[str, str]:
        return {
            "file_path": self.file_path,
            "kind": self.kind,
            "name": self.name,
        }


def parse_cosil_locations(
    raw_output: str,
    *,
    valid_files: Iterable[str],
    root_name: str = "",
) -> tuple[CoSILLocation, ...]:
    """Parse a well-formed ``<locations>`` document and reject unknown files."""

    files = tuple(dict.fromkeys(_normalize(path) for path in valid_files if path))
    match = _LOCATIONS_XML.search(str(raw_output or ""))
    if match is None:
        return ()
    try:
        root = ET.fromstring(match.group(0))
    except ET.ParseError:
        return ()

    output: list[CoSILLocation] = []
    seen: set[tuple[str, str, str]] = set()
    for node in root.findall("./location"):
        file_path = _match_file(node.findtext("file") or "", files, root_name)
        name = str(node.findtext("name") or "").strip()
        kind = str(node.findtext("type") or "function").strip().lower()
        if kind not in {"function", "class", "variable"}:
            kind = "function"
        key = (file_path, kind, name)
        if not file_path or not name or key in seen:
            continue
        seen.add(key)
        output.append(CoSILLocation(file_path=file_path, kind=kind, name=name))
    return tuple(output)


__all__ = [
    "CoSILLocation",
    "parse_cosil_files",
    "parse_cosil_locations",
]
