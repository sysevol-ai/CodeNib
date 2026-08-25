# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cancellation-boundary tests for the restartable SQLite transaction owner."""

from __future__ import annotations

import dis
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import CodeType, FrameType
from typing import Callable, Literal

import pytest

from codenib.storage import sqlite_catalog as sqlite_catalog_module
from codenib.storage.sqlite_catalog import SQLiteCatalog

_Boundary = Literal["load", "call", "after"]
_InjectedType = type[KeyboardInterrupt] | type[SystemExit]


def _instructions(function: Callable[..., object]) -> tuple[dis.Instruction, ...]:
    # The default omits interpreter caches on supported CPython releases and
    # works on the repository's full Python 3.10+ compatibility range.
    return tuple(dis.get_instructions(function))


def _opcode_call_boundary(
    function: Callable[..., object],
    attribute: str,
    boundary: _Boundary,
    *,
    occurrence: int = 0,
) -> int:
    """Resolve one attribute call boundary from live bytecode, never a line."""

    instructions = _instructions(function)
    loads = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
        and instruction.argval == attribute
    ]
    assert len(loads) > occurrence, (function, attribute, occurrence)
    load_index = loads[occurrence]
    if boundary == "load":
        return instructions[load_index].offset
    call_index = next(
        index
        for index in range(load_index + 1, len(instructions))
        if instructions[index].opname.startswith("CALL")
    )
    if boundary == "call":
        return instructions[call_index].offset
    assert call_index + 1 < len(instructions)
    return instructions[call_index + 1].offset


def _opcode_attribute_boundary(
    function: Callable[..., object],
    attribute: str,
    boundary: Literal["load", "after"],
    *,
    occurrence: int = 0,
) -> int:
    instructions = _instructions(function)
    matches = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}
        and instruction.argval == attribute
    ]
    assert len(matches) > occurrence, (function, attribute, occurrence)
    index = matches[occurrence]
    return instructions[index if boundary == "load" else index + 1].offset


def _opcode_global_call_boundary(
    function: Callable[..., object],
    global_name: str,
    boundary: Literal["call", "after", "after_discard"],
    *,
    occurrence: int = 0,
) -> int:
    instructions = _instructions(function)
    loads = [
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "LOAD_GLOBAL" and instruction.argval == global_name
    ]
    assert len(loads) > occurrence, (function, global_name, occurrence)
    load_index = loads[occurrence]
    call_index = next(
        index
        for index in range(load_index + 1, len(instructions))
        if instructions[index].opname.startswith("CALL")
    )
    if boundary == "call":
        return instructions[call_index].offset
    if boundary == "after":
        return instructions[call_index + 1].offset
    assert instructions[call_index + 1].opname == "POP_TOP"
    return instructions[call_index + 2].offset


def _exception_handler_entry(function: Callable[..., object]) -> int:
    instructions = _instructions(function)
    yield_index = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opname == "YIELD_VALUE"
    )
    yield_offset = instructions[yield_index].offset
    exception_entries = getattr(dis.Bytecode(function), "exception_entries", ())
    containing = [
        entry for entry in exception_entries if entry.start <= yield_offset < entry.end
    ]
    if containing:
        return min(containing, key=lambda entry: entry.end - entry.start).target
    # CPython 3.10 encodes handlers with SETUP_FINALLY and enters the nearest
    # matching handler at DUP_TOP; 3.11+ uses the exception table above.
    return next(
        instruction.offset
        for instruction in instructions[yield_index + 1 :]
        if instruction.opname == "DUP_TOP"
    )


@dataclass
class _OpcodeInjection:
    code: CodeType
    offset: int
    error: BaseException
    fired: bool = False

    def trace(self, frame: FrameType, event: str, _arg: object):
        if event == "call" and frame.f_code is self.code:
            frame.f_trace_opcodes = True
            return self.trace
        if (
            event == "opcode"
            and frame.f_code is self.code
            and frame.f_lasti == self.offset
            and not self.fired
        ):
            self.fired = True
            raise self.error
        return self.trace


@contextmanager
def _inject_opcode(
    function: Callable[..., object],
    offset: int,
    error: BaseException,
) -> Iterator[None]:
    injection = _OpcodeInjection(function.__code__, offset, error)
    previous = sys.gettrace()
    sys.settrace(injection.trace)
    try:
        yield
    finally:
        sys.settrace(previous)
        assert injection.fired, (function, offset)


def _warm_transaction_opcode_tracing(catalog: SQLiteCatalog) -> None:
    """Warm traced generator/settlement paths before a one-shot injection."""

    def trace(frame: FrameType, event: str, _arg: object):
        if event == "call":
            frame.f_trace_opcodes = True
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        with catalog._transaction(immediate=False):
            catalog._connection.execute("SELECT 1").fetchone()
    finally:
        sys.settrace(previous)


def _catalog_with_probe(path) -> SQLiteCatalog:
    catalog = SQLiteCatalog(path, busy_timeout_ms=100)
    catalog._connection.execute(
        "CREATE TABLE transaction_owner_probe(value TEXT PRIMARY KEY)"
    )
    _warm_transaction_opcode_tracing(catalog)
    return catalog


def _assert_connection_settled(catalog: SQLiteCatalog) -> None:
    assert catalog._transaction_owner is None
    try:
        active = catalog._connection.in_transaction
    except sqlite3.ProgrammingError:
        return
    assert active is False


def _assert_second_writer_unlocked(path) -> None:
    connection = sqlite3.connect(path, isolation_level=None, timeout=0.2)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO transaction_owner_probe(value) VALUES ('writer')"
        )
        connection.rollback()
    finally:
        connection.close()


def _probe_values(path) -> tuple[str, ...]:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        return tuple(
            row[0]
            for row in connection.execute(
                "SELECT value FROM transaction_owner_probe ORDER BY value"
            )
        )
    finally:
        connection.close()


def _injected(kind: _InjectedType, label: str) -> BaseException:
    return kind(label)


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_cancellation_immediately_after_begin_is_settled_and_propagated(
    tmp_path,
    kind: _InjectedType,
) -> None:
    path = tmp_path / f"post-begin-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    error = _injected(kind, "post-BEGIN cancellation")
    offset = _opcode_call_boundary(
        sqlite_catalog_module._SQLiteTransactionOwner.begin,
        "execute",
        "after",
    )
    try:
        with pytest.raises(kind) as caught:
            with _inject_opcode(
                sqlite_catalog_module._SQLiteTransactionOwner.begin,
                offset,
                error,
            ):
                with catalog._transaction():
                    raise AssertionError("the transaction body must not start")
        assert caught.value is error
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_handler_entry_cancellation_preserves_the_original_body_error(
    tmp_path,
    kind: _InjectedType,
) -> None:
    path = tmp_path / f"handler-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = RuntimeError("original transaction body failure")
    injected = _injected(kind, "handler cancellation")
    transaction = SQLiteCatalog._transaction.__wrapped__
    offset = _exception_handler_entry(transaction)
    try:
        with pytest.raises(RuntimeError) as caught:
            with _inject_opcode(transaction, offset, injected):
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
                    raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize("boundary", ("load", "call", "after"))
def test_retain_boundaries_preserve_primary_and_finish_rollback(
    tmp_path,
    kind: _InjectedType,
    boundary: _Boundary,
) -> None:
    path = tmp_path / f"retain-{boundary}-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = RuntimeError("original transaction body failure")
    injected = _injected(kind, f"retain {boundary} cancellation")
    transaction = SQLiteCatalog._transaction.__wrapped__
    offset = _opcode_call_boundary(transaction, "retain", boundary)
    try:
        with pytest.raises(RuntimeError) as caught:
            with _inject_opcode(transaction, offset, injected):
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
                    raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize("boundary", ("load", "after"))
def test_in_transaction_observation_boundaries_preserve_primary(
    tmp_path,
    kind: _InjectedType,
    boundary: Literal["load", "after"],
) -> None:
    path = tmp_path / f"observe-{boundary}-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = RuntimeError("original transaction body failure")
    injected = _injected(kind, f"in_transaction {boundary} cancellation")
    offset = _opcode_attribute_boundary(
        sqlite_catalog_module._sqlite_transaction_pass,
        "in_transaction",
        boundary,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            with _inject_opcode(
                sqlite_catalog_module._sqlite_transaction_pass,
                offset,
                injected,
            ):
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
                    raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize("boundary", ("call", "after"))
def test_rollback_call_boundaries_preserve_primary_and_unlock_writer(
    tmp_path,
    kind: _InjectedType,
    boundary: Literal["call", "after"],
) -> None:
    path = tmp_path / f"rollback-{boundary}-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = RuntimeError("original transaction body failure")
    injected = _injected(kind, f"rollback {boundary} cancellation")
    offset = _opcode_call_boundary(
        sqlite_catalog_module._sqlite_transaction_pass,
        "rollback",
        boundary,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            with _inject_opcode(
                sqlite_catalog_module._sqlite_transaction_pass,
                offset,
                injected,
            ):
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
                    raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize(
    ("boundary", "committed"),
    (("call", False), ("after", True)),
)
def test_commit_call_boundaries_expose_exact_later_state(
    tmp_path,
    kind: _InjectedType,
    boundary: Literal["call", "after"],
    committed: bool,
) -> None:
    path = tmp_path / f"commit-{boundary}-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    injected = _injected(kind, f"commit {boundary} cancellation")
    offset = _opcode_call_boundary(
        sqlite_catalog_module._sqlite_transaction_pass,
        "commit",
        boundary,
    )
    try:
        with pytest.raises(kind) as caught:
            with _inject_opcode(
                sqlite_catalog_module._sqlite_transaction_pass,
                offset,
                injected,
            ):
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
        assert caught.value is injected
        _assert_connection_settled(catalog)
        assert _probe_values(path) == (("candidate",) if committed else ())
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
@pytest.mark.parametrize("boundary", ("call", "after"))
def test_force_close_call_boundaries_preserve_primary_and_unlock_writer(
    tmp_path,
    kind: _InjectedType,
    boundary: Literal["call", "after"],
) -> None:
    path = tmp_path / f"close-{boundary}-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = RuntimeError("original transaction body failure")
    injected = _injected(kind, f"close {boundary} cancellation")
    offset = _opcode_call_boundary(
        sqlite_catalog_module._sqlite_transaction_pass,
        "close",
        boundary,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            with _inject_opcode(
                sqlite_catalog_module._sqlite_transaction_pass,
                offset,
                injected,
            ):
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
                    assert catalog._transaction_owner is not None
                    catalog._transaction_owner.force_close = True
                    raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


def test_ordinary_rollback_denial_force_closes_and_preserves_primary(tmp_path) -> None:
    path = tmp_path / "rollback-denied.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = RuntimeError("original transaction body failure")

    def deny_rollback(
        action: int,
        first: str | None,
        _second: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_TRANSACTION and first == "ROLLBACK":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    catalog._connection.set_authorizer(deny_rollback)
    try:
        with pytest.raises(RuntimeError) as caught:
            with catalog._transaction():
                catalog._connection.execute(
                    "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                )
                raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.timeout(5)
def test_body_closes_active_connection_then_primary_is_preserved_and_settled(
    tmp_path,
) -> None:
    path = tmp_path / "body-close.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = RuntimeError("original failure after active connection close")
    try:
        with pytest.raises(RuntimeError) as caught:
            with catalog._transaction():
                catalog._connection.execute(
                    "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                )
                catalog._connection.close()
                raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_cancellation_after_final_settlement_preserves_original_identity(
    tmp_path,
    kind: _InjectedType,
) -> None:
    path = tmp_path / f"post-settle-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = RuntimeError("original transaction body failure")
    injected = _injected(kind, "cancellation after settlement returned")
    transaction = SQLiteCatalog._transaction.__wrapped__
    offset = _opcode_global_call_boundary(
        transaction,
        "_settle_sqlite_transaction",
        "after",
        occurrence=0,
    )
    try:
        with pytest.raises(RuntimeError) as caught:
            with _inject_opcode(transaction, offset, injected):
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
                    raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_one_shot_post_settlement_successor_preserves_original_identity(
    tmp_path,
    kind: _InjectedType,
) -> None:
    path = tmp_path / f"post-settle-discard-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    primary = ValueError("exact original transaction body failure")
    injected = _injected(kind, "cancellation after settlement result discard")
    transaction = SQLiteCatalog._transaction.__wrapped__
    offset = _opcode_global_call_boundary(
        transaction,
        "_settle_sqlite_transaction",
        "after_discard",
        occurrence=0,
    )
    try:
        with pytest.raises(ValueError) as caught:
            with _inject_opcode(transaction, offset, injected):
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
                    raise primary
        assert caught.value is primary
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()


@pytest.mark.parametrize("kind", (KeyboardInterrupt, SystemExit))
def test_body_baseexception_does_not_collapse_to_ambient_exception_context(
    tmp_path,
    kind: _InjectedType,
) -> None:
    path = tmp_path / f"ambient-{kind.__name__}.sqlite3"
    catalog = _catalog_with_probe(path)
    ambient = ValueError("ambient exception outside the transaction")
    body_error = _injected(kind, "exact transaction body BaseException")
    try:
        try:
            raise ambient
        except ValueError as caught_ambient:
            assert caught_ambient is ambient
            with pytest.raises(kind) as caught:
                with catalog._transaction():
                    catalog._connection.execute(
                        "INSERT INTO transaction_owner_probe VALUES ('candidate')"
                    )
                    raise body_error
        assert caught.value is body_error
        _assert_connection_settled(catalog)
        assert _probe_values(path) == ()
        _assert_second_writer_unlocked(path)
    finally:
        catalog.close()
