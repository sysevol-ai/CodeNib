# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import codenib.storage as storage
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

EXPECTED_EXPORTS = [
    "WIKI_ENVELOPE_MAX_BYTES",
    "SQLiteWikiStore",
    "WikiStore",
    "WikiStoreCorruptionError",
    "WikiStoredEntry",
    "WikiStoreError",
    "WikiStoreSchemaError",
    "WikiStoreValidationError",
]

REPO_ROOT = Path(__file__).parents[1]


def test_storage_is_a_wiki_only_module() -> None:
    assert storage.__all__ == EXPECTED_EXPORTS
    assert not hasattr(storage, "__path__")
    assert storage.WIKI_ENVELOPE_MAX_BYTES is WIKI_ENVELOPE_MAX_BYTES
    assert storage.SQLiteWikiStore is SQLiteWikiStore
    assert storage.WikiStore is WikiStore
    assert storage.WikiStoreCorruptionError is WikiStoreCorruptionError
    assert storage.WikiStoredEntry is WikiStoredEntry
    assert storage.WikiStoreError is WikiStoreError
    assert storage.WikiStoreSchemaError is WikiStoreSchemaError
    assert storage.WikiStoreValidationError is WikiStoreValidationError


def test_storage_import_and_resolution_stay_lightweight() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    "import codenib.storage as storage",
                    "wiki_modules = lambda: sorted(name for name in sys.modules "
                    "if name == 'codenib.wiki' or "
                    "name.startswith('codenib.wiki.'))",
                    "before = wiki_modules()",
                    "for name in storage.__all__: getattr(storage, name)",
                    "print(json.dumps({'before': before, 'after': wiki_modules()}))",
                )
            ),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    loaded = json.loads(result.stdout)
    assert loaded == {
        "before": [],
        "after": [
            "codenib.wiki",
            "codenib.wiki.sqlite_store",
            "codenib.wiki.store",
        ],
    }


@pytest.mark.parametrize(
    "retired_name",
    ("IndexCatalog", "LocalCAS", "ObjectStore", "SQLiteCatalog", "StorageError"),
)
def test_storage_does_not_restore_generic_exports(retired_name: str) -> None:
    assert retired_name not in dir(storage)
    with pytest.raises(AttributeError):
        getattr(storage, retired_name)


@pytest.mark.parametrize(
    "retired_module",
    (
        "codenib.storage.cas",
        "codenib.storage.models",
        "codenib.storage.protocols",
        "codenib.storage.sqlite_catalog",
        "codenib.storage.view_bundle",
    ),
)
def test_storage_does_not_restore_generic_submodules(retired_module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(retired_module)
