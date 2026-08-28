# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dis
import sys
from pathlib import Path

import pytest

import codenib.web.local_index_service as local_index_service
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


def test_existing_catalog_factory_closes_session_after_store_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_path = tmp_path / "catalog.sqlite3"
    _provision_catalog(catalog_path)
    factory = ExistingLocalIndexCatalogFactory(catalog_path)
    interruption = KeyboardInterrupt("catalog session stored")

    class Session:
        closed = False

        def close(self) -> None:
            self.closed = True

    session = Session()
    monkeypatch.setattr(
        local_index_service,
        "SQLiteCatalog",
        lambda *_args, **_kwargs: session,
    )

    acquire = local_index_service._CatalogSessionOwner.acquire
    instructions = tuple(dis.get_instructions(acquire))
    store_indexes = {
        index
        for index, instruction in enumerate(instructions[:-1])
        if instruction.opname == "STORE_ATTR" and instruction.argval == "_catalog"
    }
    assert len(store_indexes) == 1
    store_index = next(iter(store_indexes))
    opcode_offsets_after_store = {instructions[store_index + 1].offset}
    store_offset = instructions[store_index].offset
    line_offsets_after_store = {
        instruction.offset
        for instruction in instructions
        if instruction.starts_line is not None and instruction.offset > store_offset
    }
    injected = False
    previous_trace = sys.gettrace()

    def trace(frame, event: str, _arg: object):
        nonlocal injected
        if event == "call" and frame.f_code is acquire.__code__:
            frame.f_trace_opcodes = True
            frame.f_trace_lines = True
            return trace
        if (
            not injected
            and frame.f_code is acquire.__code__
            and (
                event == "opcode"
                and frame.f_lasti in opcode_offsets_after_store
                or event == "line"
                and frame.f_lasti in line_offsets_after_store
            )
            and frame.f_locals["self"].closed is False
        ):
            injected = True
            sys.settrace(None)
            raise interruption
        return trace

    def open_session() -> None:
        with factory():
            pytest.fail("interruption must precede the context body")

    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            open_session()
    finally:
        sys.settrace(previous_trace)

    assert injected
    assert raised.value is interruption
    assert session.closed


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
