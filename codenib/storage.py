# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Wiki-only database boundary.

This module exposes the pluggable :class:`WikiStore` contract and its supported
SQLite implementation. Repository indexes remain manifest-bound file artifacts;
this namespace does not provide a generic catalog, CAS, or backend registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._lazy import exported_dir, load_export

if TYPE_CHECKING:  # pragma: no cover - imported only by static analyzers
    from codenib.wiki.sqlite_store import SQLiteWikiStore
    from codenib.wiki.store import (
        WIKI_ENVELOPE_MAX_BYTES,
        WikiStore,
        WikiStoreCorruptionError,
        WikiStoredEntry,
        WikiStoreError,
        WikiStoreSchemaError,
        WikiStoreValidationError,
    )

_STORE_MODULE = "codenib.wiki.store"
_EXPORTS = {
    "WIKI_ENVELOPE_MAX_BYTES": (_STORE_MODULE, "WIKI_ENVELOPE_MAX_BYTES"),
    "SQLiteWikiStore": ("codenib.wiki.sqlite_store", "SQLiteWikiStore"),
    "WikiStore": (_STORE_MODULE, "WikiStore"),
    "WikiStoreCorruptionError": (_STORE_MODULE, "WikiStoreCorruptionError"),
    "WikiStoredEntry": (_STORE_MODULE, "WikiStoredEntry"),
    "WikiStoreError": (_STORE_MODULE, "WikiStoreError"),
    "WikiStoreSchemaError": (_STORE_MODULE, "WikiStoreSchemaError"),
    "WikiStoreValidationError": (_STORE_MODULE, "WikiStoreValidationError"),
}

__all__ = [
    "WIKI_ENVELOPE_MAX_BYTES",
    "SQLiteWikiStore",
    "WikiStore",
    "WikiStoreCorruptionError",
    "WikiStoredEntry",
    "WikiStoreError",
    "WikiStoreSchemaError",
    "WikiStoreValidationError",
]


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
