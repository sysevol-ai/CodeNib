# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from codenib.storage import SQLiteCatalog
from codenib.web.local_index_service import (
    ExistingLocalIndexCatalogFactory,
    LocalIndexServiceError,
)


def _provision_catalog(path: Path) -> None:
    with SQLiteCatalog(path):
        pass


def test_existing_catalog_factory_opens_fresh_exact_sessions(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    _provision_catalog(catalog_path)
    factory = ExistingLocalIndexCatalogFactory(catalog_path, busy_timeout_ms=250)

    with factory() as first:
        assert first.schema_version > 0
    with factory() as second:
        assert second.schema_version > 0
        assert second is not first

    assert factory.catalog_identity == (
        catalog_path.stat().st_dev,
        catalog_path.stat().st_ino,
        1,
    )


def test_existing_catalog_factory_rejects_symbolic_path_components(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    _provision_catalog(catalog_path)
    alias.symlink_to(catalog_path)

    with pytest.raises(LocalIndexServiceError, match="single-linked file"):
        ExistingLocalIndexCatalogFactory(alias)


def test_existing_catalog_factory_rejects_replaced_catalog(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    displaced = tmp_path / "displaced.sqlite3"
    _provision_catalog(catalog_path)
    factory = ExistingLocalIndexCatalogFactory(catalog_path)
    catalog_path.rename(displaced)
    _provision_catalog(catalog_path)

    with pytest.raises(LocalIndexServiceError, match="binding changed"):
        with factory():
            pass


def test_existing_catalog_factory_revalidates_after_session_body(
    tmp_path: Path,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    displaced = tmp_path / "displaced.sqlite3"
    _provision_catalog(catalog_path)
    factory = ExistingLocalIndexCatalogFactory(catalog_path)

    with pytest.raises(LocalIndexServiceError, match="cannot be inspected safely"):
        with factory() as catalog:
            assert catalog.schema_version > 0
            catalog_path.rename(displaced)

    with SQLiteCatalog(displaced, create=False) as reopened:
        assert reopened.schema_version > 0


@pytest.mark.parametrize("timeout", (True, -1, 86_400_001))
def test_existing_catalog_factory_rejects_invalid_timeout(
    tmp_path: Path,
    timeout: object,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    _provision_catalog(catalog_path)

    with pytest.raises(ValueError, match="busy timeout"):
        ExistingLocalIndexCatalogFactory(
            catalog_path,
            busy_timeout_ms=timeout,  # type: ignore[arg-type]
        )
