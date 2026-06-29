# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for candidate filter ops."""

from __future__ import annotations

from codeminer.ops.filter import (
    build_candidate_filters,
    drop_test_files,
    exclude_paths,
    filter_queried_nodes,
    has_content,
    has_span,
    include_extensions,
    is_symbol_candidate,
    is_test_path,
)
from codeminer.types import QueriedNode


def _node(**kwargs):
    data = {
        "node_name": "n",
        "type": "function",
        "file": "src/a.py",
        "start_line": 1,
        "end_line": 2,
        "content": "code",
    }
    data.update(kwargs)
    return QueriedNode(**data)


def test_filter_queried_nodes_preserves_order_and_limit():
    nodes = [_node(node_name="a"), _node(node_name="b"), _node(node_name="c")]

    result = filter_queried_nodes(
        nodes,
        [lambda node: node.node_name != "b"],
        limit=1,
    )

    assert [node.node_name for node in result] == ["a"]


def test_basic_predicates_are_explicit():
    assert has_span(_node())
    assert not has_span(_node(file=None))
    assert has_content(_node(content=" x "))
    assert not has_content(_node(content=" "))
    assert is_symbol_candidate(_node(type="method"))
    assert not is_symbol_candidate(_node(type="file"))


def test_path_predicates():
    nodes = [
        _node(node_name="py", file="src/a.py"),
        _node(node_name="ts", file="src/a.ts"),
        _node(node_name="test", file="tests/test_a.py"),
    ]

    result = filter_queried_nodes(
        nodes,
        [include_extensions(["py"]), exclude_paths(["tests/*"]), drop_test_files],
    )

    assert [node.node_name for node in result] == ["py"]


def test_is_test_path_heuristic():
    assert is_test_path("tests/test_user.py")
    assert is_test_path("src/user.test.ts")
    assert is_test_path("src/UserServiceTest.java")
    assert not is_test_path("src/user.py")
    assert not is_test_path("src/contest.py")


def test_build_candidate_filters_combines_explicit_options():
    nodes = [
        _node(node_name="keep", type="function", file="src/a.py", content="code"),
        _node(node_name="file", type="file", file="src/a.py", content="code"),
        _node(node_name="empty", type="function", file="src/a.py", content=""),
        _node(
            node_name="test", type="function", file="tests/test_a.py", content="code"
        ),
    ]

    predicates = build_candidate_filters(
        require_span=True,
        require_content=True,
        symbol_only=True,
        drop_tests=True,
    )
    result = filter_queried_nodes(nodes, predicates)

    assert [node.node_name for node in result] == ["keep"]
