# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for codenib.mcp.tools.search — BM25, regex, zoekt tool impls."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from codenib.compiler.manifest import RepoManifest
from codenib.index.trigram import ZoektUnavailableError
from codenib.index.trigram.zoekt_searcher import _file_match_to_node
from codenib.mcp.tools.search import (
    search_bm25_impl,
    search_context_impl,
    search_regex_impl,
    search_zoekt_impl,
)
from codenib.types import NodeInfo

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_ctx(
    *,
    bm25=None,
    vector=None,
    symbol_graph=None,
    regex_index=None,
    zoekt=None,
    errors=None,
    artifact=None,
):
    """Create a minimal mock ServerContext."""
    ctx = MagicMock()
    ctx.manifest = RepoManifest(
        repo_path="/repo",
        commit="abc123",
        source_fingerprint="sha256:source",
        languages=["python"],
    )
    ctx.bm25 = bm25
    ctx.vector = vector
    ctx.symbol_graph = symbol_graph
    ctx.regex_index = regex_index
    ctx.zoekt = zoekt
    ctx.errors = errors or {}
    ctx.artifact = artifact
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


class TestSearchContext:
    def test_sparse_plan_returns_route_and_source_provenance(self) -> None:
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = _sample_nodes()
        ctx = _make_ctx(
            bm25=mock_bm25,
            artifact={"repository": "sysevol-ai/CodeNib"},
        )

        response = search_context_impl(ctx, query="calculate_tax", top_k=1)

        assert response["plan"]["name"] == "fast_lexical"
        assert response["plan"]["stages"] == [
            {"engine": "sparse", "weight": 1.0, "top_k": 100}
        ]
        assert response["source"] == {
            "repository": "sysevol-ai/CodeNib",
            "commit": "abc123",
            "source_fingerprint": "sha256:source",
        }
        assert response["results"][0]["node_name"] == "calculate_tax"
        assert response["results"][0]["start_line"] == 11

    def test_sparse_plan_does_not_load_optional_graph_runtime(
        self, monkeypatch
    ) -> None:
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = _sample_nodes()
        monkeypatch.setitem(sys.modules, "codenib.ops.expand", None)

        response = search_context_impl(
            _make_ctx(bm25=mock_bm25), query="calculate_tax", top_k=1
        )

        assert response["plan"]["name"] == "fast_lexical"
        assert response["results"][0]["node_name"] == "calculate_tax"

    def test_hybrid_plan_uses_shared_rrf_execution(self) -> None:
        mock_vector = MagicMock()
        mock_vector.search_with_content.return_value = [
            NodeInfo(
                node_name="shared",
                node_id="src/shared.py:shared",
                file="src/shared.py",
                score=0.9,
            ),
            NodeInfo(node_name="dense", node_id="dense", score=0.8),
        ]
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = [
            NodeInfo(
                node_name="shared",
                node_id="src/shared.py:shared",
                file="src/shared.py",
                score=10.0,
            ),
            NodeInfo(node_name="sparse", node_id="sparse", score=9.0),
        ]

        response = search_context_impl(
            _make_ctx(vector=mock_vector, bm25=mock_bm25),
            query="explain `retry_handler` behavior",
            top_k=3,
        )

        assert response["plan"]["name"] == "hybrid_fusion"
        assert response["plan"]["fusion"] == "rrf"
        assert [item["node_id"] for item in response["results"]] == [
            "src/shared.py:shared",
            "dense",
            "sparse",
        ]
        mock_vector.search_with_content.assert_called_once()
        mock_bm25.search.assert_called_once()

    def test_structural_plan_delegates_graph_expansion(self, monkeypatch) -> None:
        mock_bm25 = MagicMock()
        mock_bm25.search.return_value = _sample_nodes()[:1]
        expanded = NodeInfo(
            node_name="caller", node_id="src/caller.py:caller", file="src/caller.py"
        )
        calls = []

        def fake_expand(context, seeds, **kwargs):
            calls.append((context.code_graph, seeds, kwargs))
            from codenib.ops.retrieve import to_queried_nodes

            return [*seeds, *to_queried_nodes([expanded])]

        monkeypatch.setattr(
            "codenib.ops.expand.expand_retrieval_candidates", fake_expand
        )
        graph = object()

        response = search_context_impl(
            _make_ctx(bm25=mock_bm25, symbol_graph=graph),
            query="who calls calculate_tax",
            top_k=5,
        )

        assert response["plan"]["name"] == "structural_graph"
        assert response["plan"]["graph"]["hops"] == 2
        assert [item["node_name"] for item in response["results"]] == [
            "calculate_tax",
            "caller",
        ]
        assert calls[0][0] is graph

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"query": ""}, "query must not be empty"),
            ({"query": "x", "top_k": 0}, "top_k must be between"),
            ({"query": "x", "level": "l1"}, "level must be"),
        ],
    )
    def test_validates_request(self, kwargs, message) -> None:
        with pytest.raises(ValueError, match=message):
            search_context_impl(_make_ctx(bm25=MagicMock()), **kwargs)

    def test_requires_a_ranked_retrieval_view(self) -> None:
        with pytest.raises(ValueError, match="No retrieval backend"):
            search_context_impl(_make_ctx(), query="find parser")


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
        assert results[0]["start_line"] == 11
        assert results[0]["end_line"] == 26
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

    @pytest.mark.parametrize(
        ("query", "top_k", "message"),
        [
            ("", 10, "query must not be empty"),
            ("tax", 0, "top_k must be between"),
            ("tax", 101, "top_k must be between"),
        ],
    )
    def test_validates_bounded_request(self, query, top_k, message) -> None:
        mock_bm25 = MagicMock()
        with pytest.raises(ValueError, match=message):
            search_bm25_impl(_make_ctx(bm25=mock_bm25), query=query, top_k=top_k)
        mock_bm25.search.assert_not_called()


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
        assert results[0]["start_line"] == 11
        assert results[0]["end_line"] == 26
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

    @pytest.mark.parametrize(
        ("pattern", "top_k", "message"),
        [
            ("", 10, "pattern must not be empty"),
            ("x", -1, "top_k must be between"),
            ("x", 101, "top_k must be between"),
        ],
    )
    def test_validates_bounded_request(self, pattern, top_k, message) -> None:
        mock_regex = MagicMock()
        with pytest.raises(ValueError, match=message):
            search_regex_impl(
                _make_ctx(regex_index=mock_regex),
                pattern=pattern,
                top_k=top_k,
            )
        mock_regex.search.assert_not_called()


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
        assert results[0]["start_line"] == 11
        assert results[0]["end_line"] == 13
        mock_zoekt.search.assert_called_once_with(
            query="InvalidTokenError",
            top_k=10,
            file_filter=None,
        )

    def test_normalizes_real_zoekt_line_number_at_agent_boundary(self) -> None:
        node = _file_match_to_node(
            {
                "FileName": "first.py",
                "Matches": [
                    {
                        "LineNum": 1,
                        "Fragments": [{"Pre": "", "Match": "first_line", "Post": ""}],
                    }
                ],
            }
        )
        assert node.start_line == 0

        mock_zoekt = MagicMock()
        mock_zoekt.search.return_value = [node]
        results = search_zoekt_impl(
            _make_ctx(zoekt=mock_zoekt),
            query="first_line",
        )

        assert results[0]["start_line"] == 1
        assert results[0]["end_line"] == 1
        assert results[0]["content"] == "L1: first_line"

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

    @pytest.mark.parametrize(
        ("query", "top_k", "message"),
        [
            ("", 10, "query must not be empty"),
            ("token", False, "top_k must be between"),
            ("token", 101, "top_k must be between"),
        ],
    )
    def test_validates_bounded_request(self, query, top_k, message) -> None:
        mock_zoekt = MagicMock()
        with pytest.raises(ValueError, match=message):
            search_zoekt_impl(
                _make_ctx(zoekt=mock_zoekt),
                query=query,
                top_k=top_k,
            )
        mock_zoekt.search.assert_not_called()
