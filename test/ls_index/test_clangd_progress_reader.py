# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the clangd $/progress LSP frame parser.

Verifies ``_clangd_progress_reader`` survives split reads, malformed
``Content-Length`` headers, missing JSON-RPC fields, interleaved noise
messages, and oversize unrelated messages — without crashing or losing
the ``backgroundIndexProgress`` begin/end events that the wait loop
relies on for early-exit.
"""

from __future__ import annotations

import io
import json
import threading

from codeminer.ls_index.clangd_indexer import _clangd_progress_reader

# ─── helpers ──────────────────────────────────────────────────────────────


def _frame(body: dict) -> bytes:
    payload = json.dumps(body).encode()
    return f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload


def _state() -> dict:
    return {"saw_begin": False, "active_tokens": set(), "lock": threading.Lock()}


def _begin(token: str = "backgroundIndexProgress") -> bytes:
    return _frame(
        {
            "jsonrpc": "2.0",
            "method": "$/progress",
            "params": {"token": token, "value": {"kind": "begin", "title": "indexing"}},
        }
    )


def _report() -> bytes:
    return _frame(
        {
            "jsonrpc": "2.0",
            "method": "$/progress",
            "params": {
                "token": "backgroundIndexProgress",
                "value": {"kind": "report", "percentage": 50},
            },
        }
    )


def _end() -> bytes:
    return _frame(
        {
            "jsonrpc": "2.0",
            "method": "$/progress",
            "params": {
                "token": "backgroundIndexProgress",
                "value": {"kind": "end", "message": "done"},
            },
        }
    )


class _ChunkyStream:
    """Yield at most ``chunk_size`` bytes per ``read1`` — simulates the
    short reads we get from a real subprocess pipe."""

    def __init__(self, data: bytes, chunk_size: int = 11):
        self.data = data
        self.pos = 0
        self.chunk_size = chunk_size

    def read1(self, n: int) -> bytes:
        if self.pos >= len(self.data):
            return b""
        end = min(self.pos + min(n, self.chunk_size), len(self.data))
        out = self.data[self.pos : end]
        self.pos = end
        return out


# ─── tests ────────────────────────────────────────────────────────────────


def test_full_begin_end_cycle():
    """Single begin/report/end sequence delivered in one chunk."""
    s = _state()
    _clangd_progress_reader(io.BytesIO(_begin() + _report() + _end()), s)
    assert s["saw_begin"] is True
    assert s["active_tokens"] == set()


def test_chunked_reads():
    """Reader must reassemble frames split across multiple short reads."""
    s = _state()
    _clangd_progress_reader(
        _ChunkyStream(_begin() + _report() + _end(), chunk_size=11), s
    )
    assert s["saw_begin"] is True
    assert s["active_tokens"] == set()


def test_malformed_content_length():
    """A non-integer Content-Length must not stall later frames."""
    bad = b"Content-Length: oops\r\n\r\n" + _begin() + _end()
    s = _state()
    _clangd_progress_reader(io.BytesIO(bad), s)
    assert s["saw_begin"] is True


def test_begin_without_end_keeps_token_active():
    """If clangd dies mid-indexing, the wait loop should still see
    ``active_tokens`` populated so it does not falsely return."""
    s = _state()
    _clangd_progress_reader(io.BytesIO(_begin()), s)
    assert s["saw_begin"] is True
    assert s["active_tokens"] == {"backgroundIndexProgress"}


def test_other_progress_tokens_ignored():
    """clangd may use other tokens (e.g. per-file diagnostics) — only
    ``backgroundIndexProgress`` should affect our state."""
    noise = _frame(
        {
            "jsonrpc": "2.0",
            "method": "$/progress",
            "params": {"token": "otherToken", "value": {"kind": "begin"}},
        }
    )
    s = _state()
    _clangd_progress_reader(io.BytesIO(noise + _begin() + _end()), s)
    assert s["saw_begin"] is True
    assert s["active_tokens"] == set()


def test_missing_params_or_value():
    """Malformed $/progress messages must not crash the reader."""
    weird = (
        _frame({"jsonrpc": "2.0", "method": "$/progress"})  # no params
        + _frame(
            {
                "jsonrpc": "2.0",
                "method": "$/progress",
                "params": {"token": "backgroundIndexProgress"},  # no value
            }
        )
        + _begin()
        + _end()
    )
    s = _state()
    _clangd_progress_reader(io.BytesIO(weird), s)
    assert s["saw_begin"] is True


def test_large_intervening_message():
    """A 100KB unrelated message must not stall progress detection."""
    big = _frame(
        {
            "jsonrpc": "2.0",
            "method": "window/showMessage",
            "params": {"type": 3, "message": "x" * 100_000},
        }
    )
    s = _state()
    _clangd_progress_reader(_ChunkyStream(big + _begin() + _end(), chunk_size=17), s)
    assert s["saw_begin"] is True
    assert s["active_tokens"] == set()
