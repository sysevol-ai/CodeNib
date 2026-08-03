# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coherent incremental document-symbol reads."""

from __future__ import annotations

import pytest

from codenib.graph.code_graph import CodeGraph
from codenib.graph.incremental.patcher_rust import PatcherRust


class _DocumentSymbolClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def document_symbol(self, _file_path):
        response_index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[response_index]


def _function_symbol(name: str, start_line: int, end_line: int) -> dict:
    location = {
        "start": {"line": start_line, "character": 0},
        "end": {"line": end_line, "character": 1},
    }
    return {
        "name": name,
        "kind": 12,
        "range": location,
        "selectionRange": location,
    }


def _patcher(tmp_path, responses) -> tuple[PatcherRust, _DocumentSymbolClient]:
    patcher = PatcherRust(str(tmp_path), CodeGraph(str(tmp_path)))
    client = _DocumentSymbolClient(responses)
    patcher.lsp_client = client
    patcher.DOCUMENT_SYMBOL_ATTEMPTS = 3
    patcher.DOCUMENT_SYMBOL_RETRY_DELAY_S = 0
    return patcher, client


def test_incremental_symbols_retry_transient_empty_snapshot(tmp_path):
    symbol = _function_symbol("kept", 2, 5)
    patcher, client = _patcher(tmp_path, [[], [symbol]])

    result = patcher._read_incremental_symbols(
        file_path="src/lib.rs",
        abs_file=str(tmp_path / "src/lib.rs"),
        old_symbols={
            "src/lib.rs:kept()": {
                "start_line": 2,
                "end_line": 5,
            }
        },
        hunks=[(10, 10, 10, 10)],
    )

    assert list(result) == ["src/lib.rs:kept()"]
    assert client.calls == 2


def test_incremental_symbols_reject_persistently_empty_snapshot(tmp_path):
    patcher, client = _patcher(tmp_path, [[]])

    with pytest.raises(RuntimeError, match="unchanged indexed definitions remain"):
        patcher._read_incremental_symbols(
            file_path="src/lib.rs",
            abs_file=str(tmp_path / "src/lib.rs"),
            old_symbols={
                "src/lib.rs:kept()": {
                    "start_line": 2,
                    "end_line": 5,
                }
            },
            hunks=[(10, 10, 10, 10)],
        )

    assert client.calls == 3


def test_incremental_symbols_allow_empty_file_after_complete_deletion(tmp_path):
    patcher, client = _patcher(tmp_path, [[]])

    result = patcher._read_incremental_symbols(
        file_path="src/lib.rs",
        abs_file=str(tmp_path / "src/lib.rs"),
        old_symbols={
            "src/lib.rs:removed()": {
                "start_line": 2,
                "end_line": 5,
            }
        },
        hunks=[(2, 5, 2, 2)],
    )

    assert result == {}
    assert client.calls == 1
