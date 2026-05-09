"""Unit tests for ZoektSearcher — HTTP query composition and result mapping."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from codeminer.index.trigram.zoekt_searcher import (
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
    def test_no_filter(self) -> None:
        assert _compose_query("foo", None) == "foo"
        assert _compose_query("foo", "") == "foo"

    def test_simple_filter(self) -> None:
        assert _compose_query("foo", "*.py") == "foo file:*.py"

    def test_filter_with_spaces_quoted(self) -> None:
        assert _compose_query("foo", "src dir") == 'foo file:"src dir"'


# ------------------------------------------------------------------
# _file_match_to_node
# ------------------------------------------------------------------


class TestFileMatchMapping:
    def test_matches_collapse_to_node(self) -> None:
        fm = {
            "FileName": "src/auth.py",
            "Repo": "repo",
            "Language": "Python",
            "Matches": [
                {
                    "LineNum": 10,
                    "Fragments": [
                        {"Pre": "def ", "Match": "login", "Post": "(user):"},
                    ],
                },
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
            ],
        }
        node = _file_match_to_node(fm)
        assert node.type == "file"
        assert node.file == "src/auth.py"
        assert node.node_name == "src/auth.py"
        assert node.start_line == 10
        assert node.end_line == 30
        assert node.node_id == "Python"
        assert "InvalidTokenError" in (node.content or "")
        assert "def login" in (node.content or "")
        # Score is not exposed by the JSON endpoint -- caller relies on order.
        assert node.score is None

    def test_unsorted_matches_min_max_aggregation(self) -> None:
        """``start_line`` and ``end_line`` aggregate the union of LineNum values."""
        fm = {
            "FileName": "x.py",
            "Matches": [
                {"LineNum": 30, "Fragments": [{"Match": "a"}]},
                {"LineNum": 5, "Fragments": [{"Match": "b"}]},
                {"LineNum": 12, "Fragments": [{"Match": "c"}]},
            ],
        }
        node = _file_match_to_node(fm)
        assert node.start_line == 5
        assert node.end_line == 30

    def test_empty_file_match_safe(self) -> None:
        node = _file_match_to_node({"FileName": "x.py"})
        assert node.node_name == "x.py"
        assert node.type == "file"
        assert node.start_line is None
        assert node.end_line is None
        assert node.content is None

    def test_fragment_pre_match_post_concatenated(self) -> None:
        """Per-fragment snippet preserves surrounding context for the agent."""
        fm = {
            "FileName": "main.go",
            "Matches": [
                {
                    "LineNum": 5,
                    "Fragments": [
                        {"Pre": "package ", "Match": "main", "Post": ""},
                    ],
                }
            ],
        }
        node = _file_match_to_node(fm)
        assert "package main" in (node.content or "")


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

        with patch.object(searcher._session, "get", return_value=mock_response) as mock_get:
            results = searcher.search("foo", top_k=10)

        assert len(results) == 1
        assert results[0].file == "a.py"
        assert results[0].start_line == 3

        # GET /search with format=json query string (no JSON body POST).
        assert mock_get.call_args.args[0].endswith("/search")
        params = mock_get.call_args.kwargs["params"]
        assert params["q"] == "foo"
        assert params["format"] == "json"
        assert params["num"] == "10"

    def test_search_passes_file_filter_into_query(self, tmp_path) -> None:
        searcher = _running_searcher(str(tmp_path))
        mock_response = MagicMock()
        mock_response.json.return_value = _zoekt_response([])
        mock_response.raise_for_status.return_value = None

        with patch.object(searcher._session, "get", return_value=mock_response) as mock_get:
            searcher.search("bar", top_k=5, file_filter="*.go")

        params = mock_get.call_args.kwargs["params"]
        assert params["q"] == "bar file:*.go"
        assert params["num"] == "5"

    def test_top_k_truncation(self, tmp_path) -> None:
        searcher = _running_searcher(str(tmp_path))
        files = [{"FileName": f"f{i}.py", "Matches": []} for i in range(50)]
        mock_response = MagicMock()
        mock_response.json.return_value = _zoekt_response(files)
        mock_response.raise_for_status.return_value = None

        with patch.object(searcher._session, "get", return_value=mock_response):
            results = searcher.search("anything", top_k=7)

        assert len(results) == 7

    def test_repo_only_response_returns_empty(self, tmp_path) -> None:
        """Zoekt returns ``{"repos": ...}`` for repo-listing queries; treat as no hits."""
        searcher = _running_searcher(str(tmp_path))
        mock_response = MagicMock()
        mock_response.json.return_value = {"repos": {"Repos": []}}
        mock_response.raise_for_status.return_value = None

        with patch.object(searcher._session, "get", return_value=mock_response):
            assert searcher.search("anything") == []

    def test_http_failure_raises_zoekt_unavailable(self, tmp_path) -> None:
        import requests

        searcher = _running_searcher(str(tmp_path))
        with patch.object(
            searcher._session,
            "get",
            side_effect=requests.ConnectionError("boom"),
        ):
            with pytest.raises(ZoektUnavailableError, match="search request failed"):
                searcher.search("x")

    def test_non_json_response_raises_zoekt_unavailable(self, tmp_path) -> None:
        searcher = _running_searcher(str(tmp_path))
        bad_response = MagicMock()
        bad_response.json.side_effect = ValueError("not json")
        bad_response.raise_for_status.return_value = None

        with patch.object(searcher._session, "get", return_value=bad_response):
            with pytest.raises(ZoektUnavailableError, match="non-JSON response"):
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
        with pytest.raises(ZoektUnavailableError, match="index directory does not exist"):
            s.start()

    def test_idempotent_start_when_already_running(self, tmp_path) -> None:
        s = _running_searcher(str(tmp_path))
        s.start()  # second call should be a no-op, not raise
        assert s.is_running

    def test_stop_is_safe_when_never_started(self, tmp_path) -> None:
        s = ZoektSearcher(index_dir=str(tmp_path))
        s.stop()  # must not raise
        assert s._proc is None
