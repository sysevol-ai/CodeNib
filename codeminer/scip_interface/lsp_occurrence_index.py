# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exact position index for LSP-shaped navigation over decoded SCIP data."""

from __future__ import annotations

import os
import pickle
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .scip_indexer_base import extract_scip_blocks, extract_symbol

LSP_OCCURRENCE_INDEX_SCHEMA_VERSION = 1
_DEFINITION_ROLE = 1
_RANGE_RE = re.compile(r"range:\s*(\d+)")
_SYMBOL_ROLES_RE = re.compile(r"symbol_roles:\s*(\d+)")


@dataclass(frozen=True, slots=True)
class SCIPOccurrence:
    """One SCIP symbol occurrence with an exact half-open source range."""

    file_path: str
    start_line: int
    start_character: int
    end_line: int
    end_character: int
    symbol: str
    symbol_roles: int = 0

    @property
    def is_definition(self) -> bool:
        return bool(self.symbol_roles & _DEFINITION_ROLE)

    def contains(self, line: int, character: int) -> bool:
        position = (int(line), int(character))
        return (
            (self.start_line, self.start_character)
            <= position
            < (
                self.end_line,
                self.end_character,
            )
        )

    @property
    def span_key(self) -> tuple[int, int, int, int]:
        return (
            self.end_line - self.start_line,
            self.end_character - self.start_character,
            self.start_line,
            self.start_character,
        )


@dataclass(frozen=True, slots=True)
class SCIPLocation:
    """Repository-relative location returned by the occurrence index."""

    file_path: str
    start_line: int
    start_character: int
    end_line: int
    end_character: int


class SCIPOccurrenceIndex:
    """Resolve exact source positions to SCIP definitions and references."""

    def __init__(
        self,
        occurrences: Iterable[SCIPOccurrence],
        *,
        position_encoding: str = "UTF8",
    ) -> None:
        self.position_encoding = str(position_encoding or "UTF8")
        self.occurrences = tuple(occurrences)
        by_file: dict[str, list[int]] = {}
        by_symbol: dict[tuple[str, str], list[int]] = {}
        for index, occurrence in enumerate(self.occurrences):
            by_file.setdefault(occurrence.file_path, []).append(index)
            key = self._symbol_key(occurrence.file_path, occurrence.symbol)
            by_symbol.setdefault(key, []).append(index)
        self._by_file = {
            file_path: tuple(
                sorted(
                    indexes,
                    key=lambda item: self.occurrences[item].span_key,
                )
            )
            for file_path, indexes in by_file.items()
        }
        self._by_symbol = {key: tuple(indexes) for key, indexes in by_symbol.items()}

    @classmethod
    def from_decoded_file(
        cls,
        path: str | Path,
        *,
        path_filter: Optional[Callable[[str], bool]] = None,
    ) -> SCIPOccurrenceIndex:
        decoded = Path(path).read_text(encoding="utf-8", errors="replace")
        return cls.from_decoded_text(decoded, path_filter=path_filter)

    @classmethod
    def from_decoded_text(
        cls,
        decoded: str,
        *,
        path_filter: Optional[Callable[[str], bool]] = None,
    ) -> SCIPOccurrenceIndex:
        encoding_match = re.search(
            r"text_document_encoding:\s*([A-Za-z0-9_]+)", decoded
        )
        position_encoding = encoding_match.group(1) if encoding_match else "UTF8"
        occurrences = []
        for document in extract_scip_blocks(decoded, "documents"):
            file_path = _extract_quoted_field(document, "relative_path")
            if not file_path or (
                path_filter is not None and not path_filter(file_path)
            ):
                continue
            for raw_occurrence in extract_scip_blocks(document, "occurrences"):
                occurrence = _parse_occurrence(raw_occurrence, file_path=file_path)
                if occurrence is not None:
                    occurrences.append(occurrence)
        return cls(occurrences, position_encoding=position_encoding)

    @classmethod
    def load(cls, path: str | Path) -> SCIPOccurrenceIndex:
        with Path(path).open("rb") as handle:
            payload = pickle.load(handle)
        if not isinstance(payload, Mapping):
            raise ValueError(f"invalid LSP occurrence index at {path}")
        version = payload.get("schema_version")
        if version != LSP_OCCURRENCE_INDEX_SCHEMA_VERSION:
            raise ValueError(
                f"LSP occurrence index at {path} has schema_version={version!r}, "
                f"expected {LSP_OCCURRENCE_INDEX_SCHEMA_VERSION}"
            )
        rows = payload.get("occurrences") or ()
        occurrences = tuple(
            row if isinstance(row, SCIPOccurrence) else SCIPOccurrence(**row)
            for row in rows
        )
        by_file = payload.get("by_file")
        by_symbol = payload.get("by_symbol")
        if isinstance(by_file, Mapping) and isinstance(by_symbol, Mapping):
            index = cls.__new__(cls)
            index.position_encoding = str(payload.get("position_encoding") or "UTF8")
            index.occurrences = occurrences
            index._by_file = {str(key): tuple(value) for key, value in by_file.items()}
            index._by_symbol = {
                tuple(key): tuple(value) for key, value in by_symbol.items()
            }
            return index
        return cls(occurrences, position_encoding=payload.get("position_encoding"))

    def save(self, path: str | Path) -> None:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": LSP_OCCURRENCE_INDEX_SCHEMA_VERSION,
            "position_encoding": self.position_encoding,
            "occurrences": self.occurrences,
            "by_file": self._by_file,
            "by_symbol": self._by_symbol,
        }
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temporary, output)

    def occurrence_at(
        self, file_path: str, line: int, character: int
    ) -> Optional[SCIPOccurrence]:
        normalized_path = _normalize_path(file_path)
        for index in self._by_file.get(normalized_path, ()):
            occurrence = self.occurrences[index]
            if occurrence.contains(line, character):
                return occurrence
        return None

    def definitions(
        self,
        *,
        file_path: str,
        line: int,
        character: int,
        top_k: int = 8,
    ) -> list[SCIPLocation]:
        occurrence = self._require_occurrence(file_path, line, character)
        matches = [
            self.occurrences[index]
            for index in self._by_symbol.get(
                self._symbol_key(occurrence.file_path, occurrence.symbol), ()
            )
            if self.occurrences[index].is_definition
        ]
        return _locations(matches, limit=top_k)

    def references(
        self,
        *,
        file_path: str,
        line: int,
        character: int,
        include_declaration: bool = True,
        top_k: int = 40,
    ) -> list[SCIPLocation]:
        occurrence = self._require_occurrence(file_path, line, character)
        matches = [
            self.occurrences[index]
            for index in self._by_symbol.get(
                self._symbol_key(occurrence.file_path, occurrence.symbol), ()
            )
            if include_declaration or not self.occurrences[index].is_definition
        ]
        return _locations(matches, limit=top_k)

    def _require_occurrence(
        self, file_path: str, line: int, character: int
    ) -> SCIPOccurrence:
        occurrence = self.occurrence_at(file_path, line, character)
        if occurrence is None:
            raise ValueError(
                "no SCIP occurrence at "
                f"{_normalize_path(file_path)}:{int(line) + 1}:{int(character)}"
            )
        return occurrence

    @staticmethod
    def _symbol_key(file_path: str, symbol: str) -> tuple[str, str]:
        scope = _normalize_path(file_path) if symbol.startswith("local ") else ""
        return scope, symbol


def _parse_occurrence(text: str, *, file_path: str) -> Optional[SCIPOccurrence]:
    values = [int(value) for value in _RANGE_RE.findall(text)]
    if len(values) == 3:
        start_line, start_character, end_character = values
        end_line = start_line
    elif len(values) == 4:
        start_line, start_character, end_line, end_character = values
    else:
        return None
    symbol = extract_symbol(text)
    if not symbol:
        return None
    roles_match = _SYMBOL_ROLES_RE.search(text)
    roles = int(roles_match.group(1)) if roles_match else 0
    return SCIPOccurrence(
        file_path=_normalize_path(file_path),
        start_line=start_line,
        start_character=start_character,
        end_line=end_line,
        end_character=end_character,
        symbol=symbol,
        symbol_roles=roles,
    )


def _locations(
    occurrences: Sequence[SCIPOccurrence], *, limit: int
) -> list[SCIPLocation]:
    unique: dict[tuple[Any, ...], SCIPLocation] = {}
    for occurrence in occurrences:
        key = (
            occurrence.file_path,
            occurrence.start_line,
            occurrence.start_character,
            occurrence.end_line,
            occurrence.end_character,
        )
        unique[key] = SCIPLocation(*key)
    ordered = [unique[key] for key in sorted(unique)]
    return ordered[: max(1, int(limit))]


def _extract_quoted_field(text: str, field: str) -> Optional[str]:
    marker = f"{field}:"
    position = text.find(marker)
    if position < 0:
        return None
    index = position + len(marker)
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != '"':
        return None
    index += 1
    value = []
    while index < len(text):
        character = text[index]
        if character == '"':
            return "".join(value)
        if character == "\\" and index + 1 < len(text):
            index += 1
            escaped = text[index]
            value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
        else:
            value.append(character)
        index += 1
    return None


def _normalize_path(path: str | Path) -> str:
    normalized = Path(str(path).replace("\\", "/")).as_posix()
    return normalized[2:] if normalized.startswith("./") else normalized


__all__ = [
    "LSP_OCCURRENCE_INDEX_SCHEMA_VERSION",
    "SCIPLocation",
    "SCIPOccurrence",
    "SCIPOccurrenceIndex",
]
