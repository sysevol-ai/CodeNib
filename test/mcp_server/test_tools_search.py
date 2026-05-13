# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codeminer.mcp.tools.search — BM25, regex, zoekt tool impls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codeminer.index.trigram import ZoektUnavailableError
from codeminer.mcp.tools.search import (
    search_bm25_impl,
    search_regex_impl,
    search_zoekt_impl,
)
from codeminer.types import NodeInfo

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_ctx(*, bm25=None, regex_index=None, zoekt=None, errors=None):
    """Create a minimal mock ServerContext."""
    ctx = MagicMock()
    ctx.bm25 = bm25
    ctx.regex_index = regex_index
    ctx.zoekt = zoekt
    ctx.errors = errors or {}
    return ctx


def _sample_nodes() -> list[NodeInfo]:
    return [
        NodeInfo(
            node_name="calculate_tax",
            type="function",
            file="billing/tax.py",
            start_line=10,
            end_line=25,
            content="def calculate_tax(amount): ...",
            score=0.0,
        ),
        NodeInfo(
            node_name="TaxError",
            type="class",
            file="billing/exceptions.py",
            start_line=1,
            end_line=5,
            content="class TaxError(Exception): ...",
            score=0.0,
        ),
    ]


# ------------------------------------------------------------------
# search_bm25
# ------------------------------------------------------------------


class TestSearchBM25:
    def test_basic_search(self) -> None:
        nodes = _sample_nodes()
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = nodes
        ctx = _make_ctx(bm25=mock_bm25)

        results = search_bm25_impl(ctx, query="tax", top_k=10, filter_test=False)

        assert len(results) == 2
        assert results[0]["node_name"] == "calculate_tax"
        assert results[0]["type"] == "function"
        assert results[0]["file"] == "billing/tax.py"
        mock_bm25.search.assert_called_once_with(
            query="tax",
            top_k=10,
            return_code_content=True,
            wrap_with_ln=False,
            filter_test=False,
        )

    def test_forwards_filter_test(self) -> None:
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = []
        ctx = _make_ctx(bm25=mock_bm25)

        search_bm25_impl(ctx, query="test", top_k=5, filter_test=True)

        _, kwargs = mock_bm25.search.call_args
        assert kwargs["filter_test"] is True

    def test_raises_when_index_missing(self) -> None:
        ctx = _make_ctx(bm25=None)
        with pytest.raises(RuntimeError, match="BM25 index is not available"):
            search_bm25_impl(ctx, query="anything")

    def test_none_fields_excluded(self) -> None:
        node = NodeInfo(node_name="foo", type="function")
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = [node]
        ctx = _make_ctx(bm25=mock_bm25)

        results = search_bm25_impl(ctx, query="foo")
        assert "file" not in results[0]
        assert "score" not in results[0]


# ------------------------------------------------------------------
# search_regex
# ------------------------------------------------------------------


class TestSearchRegex:
    def test_basic_search(self) -> None:
        nodes = _sample_nodes()[:1]
        mock_regex = MagicMock()
        mock_regex.search.return_value = nodes
        ctx = _make_ctx(regex_index=mock_regex)

        results = search_regex_impl(ctx, pattern=r"def\s+\w+", top_k=10)

        assert len(results) == 1
        assert results[0]["node_name"] == "calculate_tax"
        mock_regex.search.assert_called_once_with(
            pattern=r"def\s+\w+",
            file_glob=None,
            node_type=None,
            case_sensitive=False,
            use_regex=True,
        )

    def test_forwards_filters(self) -> None:
        mock_regex = MagicMock()
        mock_regex.search.return_value = []
        ctx = _make_ctx(regex_index=mock_regex)

        search_regex_impl(
            ctx,
            pattern="class",
            top_k=5,
            file_glob="*.py",
            node_type="class",
            case_sensitive=True,
        )

        _, kwargs = mock_regex.search.call_args
        assert kwargs["file_glob"] == "*.py"
        assert kwargs["node_type"] == "class"
        assert kwargs["case_sensitive"] is True

    def test_top_k_truncation(self) -> None:
        many_nodes = [
            NodeInfo(node_name=f"func_{i}", type="function", content="x")
            for i in range(50)
        ]
        mock_regex = MagicMock()
        mock_regex.search.return_value = many_nodes
        ctx = _make_ctx(regex_index=mock_regex)

        results = search_regex_impl(ctx, pattern="x", top_k=10)
        assert len(results) == 10

    def test_raises_when_index_missing(self) -> None:
        ctx = _make_ctx(regex_index=None)
        with pytest.raises(RuntimeError, match="Regex index is not available"):
            search_regex_impl(ctx, pattern="anything")

    def test_invalid_regex_translated_to_runtime_error(self) -> None:
        """Invalid regex from RegexNodeIndex.search bubbles up as a friendly RuntimeError."""
        mock_regex = MagicMock()
        mock_regex.search.side_effect = ValueError("bad escape at position 0")
        ctx = _make_ctx(regex_index=mock_regex)

        with pytest.raises(RuntimeError, match="Invalid regex pattern"):
            search_regex_impl(ctx, pattern=r"[")


# ------------------------------------------------------------------
# search_zoekt
# ------------------------------------------------------------------


class TestSearchZoekt:
    def _zoekt_results(self) -> list[NodeInfo]:
        return [
            NodeInfo(
                node_name="src/auth.py",
                type="file",
                file="src/auth.py",
                start_line=10,
                end_line=12,
                content="def login(user):\n    raise InvalidTokenError",
                score=42.0,
                node_id="Python",
            ),
        ]

    def test_basic_search(self) -> None:
        mock_zoekt = MagicMock()
        mock_zoekt.search.return_value = self._zoekt_results()
        ctx = _make_ctx(zoekt=mock_zoekt)

        results = search_zoekt_impl(ctx, query="InvalidTokenError", top_k=10)

        assert len(results) == 1
        assert results[0]["type"] == "file"
        assert results[0]["file"] == "src/auth.py"
        assert results[0]["start_line"] == 10
        mock_zoekt.search.assert_called_once_with(
            query="InvalidTokenError",
            top_k=10,
            file_filter=None,
        )

    def test_forwards_file_filter(self) -> None:
        mock_zoekt = MagicMock()
        mock_zoekt.search.return_value = []
        ctx = _make_ctx(zoekt=mock_zoekt)

        search_zoekt_impl(ctx, query="TODO", top_k=5, file_filter="*.py")

        _, kwargs = mock_zoekt.search.call_args
        assert kwargs["file_filter"] == "*.py"
        assert kwargs["top_k"] == 5

    def test_empty_file_filter_normalized_to_none(self) -> None:
        """Passing the empty string for file_filter should be treated as no filter."""
        mock_zoekt = MagicMock()
        mock_zoekt.search.return_value = []
        ctx = _make_ctx(zoekt=mock_zoekt)

        search_zoekt_impl(ctx, query="x", file_filter="")

        _, kwargs = mock_zoekt.search.call_args
        assert kwargs["file_filter"] is None

    def test_raises_when_zoekt_missing(self) -> None:
        ctx = _make_ctx(zoekt=None)
        with pytest.raises(RuntimeError, match="Zoekt index is not available"):
            search_zoekt_impl(ctx, query="anything")

    def test_uses_error_message_from_ctx(self) -> None:
        ctx = _make_ctx(
            zoekt=None, errors={"zoekt": "binary not found at /usr/bin/zoekt"}
        )
        with pytest.raises(RuntimeError, match="binary not found"):
            search_zoekt_impl(ctx, query="anything")

    def test_zoekt_unavailable_translated_to_runtime_error(self) -> None:
        mock_zoekt = MagicMock()
        mock_zoekt.search.side_effect = ZoektUnavailableError("connection refused")
        ctx = _make_ctx(zoekt=mock_zoekt)

        with pytest.raises(
            RuntimeError, match="Zoekt search failed.*connection refused"
        ):
            search_zoekt_impl(ctx, query="x")
