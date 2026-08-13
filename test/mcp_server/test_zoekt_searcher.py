# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ZoektSearcher — HTTP query composition and result mapping."""

from __future__ import annotations

import dis
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from codenib.index.trigram.zoekt_searcher import (
    ZoektSearcher,
    ZoektUnavailableError,
    _compose_query,
    _file_match_to_node,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _running_searcher(index_dir: str, *, port: int = 12345) -> ZoektSearcher:
    """Build a ZoektSearcher and pretend its subprocess is alive.

    Skips the real ``start()`` path so tests can hit ``search()`` without
    invoking a binary.
    """
    s = ZoektSearcher(index_dir=index_dir, port=port)
    s._port = port
    s._proc = MagicMock()
    s._proc.poll.return_value = None  # process "is running"
    return s


def _zoekt_response(files: list[dict]) -> dict:
    return {"result": {"FileMatches": files}}


# ------------------------------------------------------------------
# _compose_query
# ------------------------------------------------------------------


class TestComposeQuery:
    @pytest.mark.parametrize(
        "query, file_filter, expected",
        [
            ("foo", None, "foo"),
            ("foo", "", "foo"),
            ("foo", "*.py", "foo file:*.py"),
            ("foo", "src dir", 'foo file:"src dir"'),
        ],
    )
    def test_compose(self, query: str, file_filter, expected: str) -> None:
        assert _compose_query(query, file_filter) == expected


# ------------------------------------------------------------------
# _file_match_to_node
# ------------------------------------------------------------------


class TestFileMatchMapping:
    def test_matches_collapse_to_node(self) -> None:
        """Exercises Pre+Match+Post concatenation and min/max line aggregation
        across out-of-order LineNum entries in a single FileMatch."""
        fm = {
            "FileName": "src/auth.py",
            "Repo": "repo",
            "Language": "Python",
            "Matches": [
                # Intentionally out-of-order to verify start/end aggregate
                # via min/max rather than first/last seen.
                {
                    "LineNum": 30,
                    "Fragments": [
                        {
                            "Pre": "    raise ",
                            "Match": "InvalidTokenError",
                            "Post": "()",
                        },
                    ],
                },
                {
                    "LineNum": 10,
                    "Fragments": [
                        {"Pre": "def ", "Match": "login", "Post": "(user):"},
                    ],
                },
            ],
        }
        node = _file_match_to_node(fm)
        assert node.type == "file"
        assert node.file == "src/auth.py"
        assert node.node_name == "src/auth.py"
        assert node.start_line == 9
        assert node.end_line == 29
        assert node.node_id == "Python"
        # Pre + Match + Post are concatenated per fragment so the agent sees
        # the matched line in context.
        assert "def login(user):" in (node.content or "")
        assert "raise InvalidTokenError()" in (node.content or "")
        assert "L10: def login(user):" in (node.content or "")
        assert "L30:     raise InvalidTokenError()" in (node.content or "")
        # Score is not exposed by the JSON endpoint -- caller relies on order.
        assert node.score is None

    def test_filename_match_sentinel_does_not_become_source_line(self) -> None:
        node = _file_match_to_node(
            {
                "FileName": "src/auth.py",
                "Matches": [
                    {
                        "LineNum": 0,
                        "Fragments": [{"Pre": "", "Match": "auth.py", "Post": ""}],
                    }
                ],
            }
        )

        assert node.start_line is None
        assert node.end_line is None
        assert node.content == "auth.py"


# ------------------------------------------------------------------
# ZoektSearcher.search (HTTP layer mocked)
# ------------------------------------------------------------------


class TestSearcherSearch:
    def test_search_gets_json_endpoint_and_returns_nodes(self, tmp_path) -> None:
        searcher = _running_searcher(str(tmp_path))
        mock_response = MagicMock()
        mock_response.json.return_value = _zoekt_response(
            [
                {
                    "FileName": "a.py",
                    "Matches": [
                        {
                            "LineNum": 3,
                            "Fragments": [{"Pre": "", "Match": "x = 1", "Post": ""}],
                        }
                    ],
                }
            ]
        )
        mock_response.raise_for_status.return_value = None

        with patch.object(
            searcher._session, "get", return_value=mock_response
        ) as mock_get:
            results = searcher.search("foo", top_k=10)

        assert len(results) == 1
        assert results[0].file == "a.py"
        assert results[0].start_line == 2

        # GET /search with format=json query string (no JSON body POST).
        assert mock_get.call_args.args[0].endswith("/search")
        params = mock_get.call_args.kwargs["params"]
        assert params["q"] == "foo"
        assert params["format"] == "json"
        assert params["num"] == "10"

    def test_top_k_truncation(self, tmp_path) -> None:
        searcher = _running_searcher(str(tmp_path))
        files = [{"FileName": f"f{i}.py", "Matches": []} for i in range(50)]
        mock_response = MagicMock()
        mock_response.json.return_value = _zoekt_response(files)
        mock_response.raise_for_status.return_value = None

        with patch.object(searcher._session, "get", return_value=mock_response):
            results = searcher.search("anything", top_k=7)

        assert len(results) == 7

    @pytest.mark.parametrize(
        "fault, error_match",
        [
            ("connection_error", "search request failed"),
            ("invalid_json", "non-JSON response"),
        ],
    )
    def test_search_failure_raises_zoekt_unavailable(
        self, tmp_path, fault: str, error_match: str
    ) -> None:
        """Both transport errors and malformed responses surface as
        ``ZoektUnavailableError`` with a descriptive message."""
        import requests

        searcher = _running_searcher(str(tmp_path))
        if fault == "connection_error":
            patcher = patch.object(
                searcher._session,
                "get",
                side_effect=requests.ConnectionError("boom"),
            )
        else:
            bad_response = MagicMock()
            bad_response.json.side_effect = ValueError("not json")
            bad_response.raise_for_status.return_value = None
            patcher = patch.object(searcher._session, "get", return_value=bad_response)

        with patcher, pytest.raises(ZoektUnavailableError, match=error_match):
            searcher.search("x")


# ------------------------------------------------------------------
# ZoektSearcher.start preconditions
# ------------------------------------------------------------------


class TestSearcherStartPreconditions:
    def test_missing_binary_raises(self, tmp_path) -> None:
        s = ZoektSearcher(
            index_dir=str(tmp_path),
            binary="this-binary-does-not-exist-12345",
        )
        with pytest.raises(ZoektUnavailableError, match="binary not found"):
            s.start()

    def test_missing_index_dir_raises(self, tmp_path) -> None:
        # Build a fake binary that exists on disk so the binary check passes.
        fake = tmp_path / "fake-zoekt"
        fake.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)

        s = ZoektSearcher(index_dir=str(tmp_path / "missing"), binary=str(fake))
        with pytest.raises(
            ZoektUnavailableError, match="index directory does not exist"
        ):
            s.start()


class TestSearcherLifecycle:
    def test_stop_retains_failed_process_and_visits_all_cleanup(self, tmp_path) -> None:
        searcher = ZoektSearcher(index_dir=str(tmp_path))
        proc = MagicMock()
        proc.poll.return_value = None
        terminate_failure = RuntimeError("terminate failed")
        proc.terminate.side_effect = terminate_failure
        proc.wait.side_effect = [
            RuntimeError("wait failed"),
            RuntimeError("wait failed"),
        ]
        proc.kill.side_effect = RuntimeError("kill failed")
        searcher._proc = proc

        with pytest.raises(RuntimeError) as caught:
            searcher.stop()

        assert caught.value is terminate_failure
        assert caught.value.zoekt_cleanup_owner is searcher
        assert searcher._proc is proc
        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()
        assert proc.wait.call_count == 2

        proc.terminate.side_effect = None
        proc.wait.side_effect = None
        proc.wait.return_value = 0
        proc.kill.side_effect = None
        searcher.stop()
        assert searcher._proc is None

    def test_start_retains_popen_result_at_first_python_handoff(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import codenib.index.trigram.zoekt_searcher as zoekt_module

        binary = tmp_path / "zoekt-webserver"
        binary.write_bytes(b"")
        searcher = ZoektSearcher(index_dir=str(tmp_path), binary=str(binary))
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0

        def spawn(owner, *_args, **_kwargs):
            owner.retain(proc)
            return proc

        monkeypatch.setattr(zoekt_module, "_OwnedZoektProcess", spawn)

        instructions = list(dis.get_instructions(ZoektSearcher.start))
        load_index = next(
            index
            for index, instruction in enumerate(instructions)
            if instruction.opname == "LOAD_GLOBAL"
            and instruction.argval == "_OwnedZoektProcess"
        )
        handoff_offset = next(
            instruction.offset
            for instruction in instructions[load_index:]
            if instruction.opname == "POP_TOP"
        )
        interruption = KeyboardInterrupt("injected after Popen return")
        injected = False

        def trace(frame, event, _arg):
            nonlocal injected
            if frame.f_code is ZoektSearcher.start.__code__ and event == "call":
                frame.f_trace_opcodes = True
            if (
                not injected
                and frame.f_code is ZoektSearcher.start.__code__
                and event == "opcode"
                and frame.f_lasti == handoff_offset
            ):
                injected = True
                raise interruption
            return trace

        sys.settrace(trace)
        try:
            with pytest.raises(KeyboardInterrupt) as caught:
                searcher.start()
        finally:
            sys.settrace(None)

        assert caught.value is interruption
        assert injected
        proc.terminate.assert_called_once_with()
        proc.wait.assert_called_once_with(timeout=5)
        assert searcher._proc is None
