# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Domain contract for persisted AgentWiki cache entries."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, ContextManager, Literal, Protocol

WikiEntryKind = Literal["outline", "page", "evidence"]


class WikiStoreError(RuntimeError):
    """Base error for a Wiki store operation."""


class WikiStoreValidationError(WikiStoreError, ValueError):
    """The caller supplied an invalid Wiki entry or query."""


class WikiStoreSchemaError(WikiStoreError):
    """The database does not implement the supported Wiki schema."""


class WikiStoreCorruptionError(WikiStoreError):
    """Persisted Wiki data failed an integrity or decoding check."""


@dataclass(frozen=True, slots=True)
class WikiStoredEntry:
    """One complete Wiki cache envelope and its stable identity."""

    entry_id: str
    repository_id: str
    kind: WikiEntryKind
    page_id: str | None
    envelope: Mapping[str, Any]


class WikiStore(Protocol):
    """Minimal persistence boundary consumed by AgentWiki."""

    def read(self, entry_id: str) -> WikiStoredEntry | None:
        """Read one entry, returning ``None`` when it is absent."""

    def publish(
        self,
        *,
        entry_id: str,
        repository_id: str,
        kind: WikiEntryKind,
        page_id: str | None,
        envelope: Mapping[str, Any],
        if_absent: bool = False,
    ) -> WikiStoredEntry:
        """Atomically insert or replace one complete entry."""

    def scan(
        self,
        *,
        repository_ids: Collection[str] | None = None,
    ) -> tuple[WikiStoredEntry, ...]:
        """Return entries in stable ID order, optionally filtered by repository."""

    def generation_guard(self, entry_id: str) -> ContextManager[None]:
        """Serialize generation of the same entry across processes."""


__all__ = [
    "WikiEntryKind",
    "WikiStore",
    "WikiStoreCorruptionError",
    "WikiStoreError",
    "WikiStoreSchemaError",
    "WikiStoredEntry",
    "WikiStoreValidationError",
]
