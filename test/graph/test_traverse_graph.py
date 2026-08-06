# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Test script for traverse_graph functionality using the httpie CLI repository."""

import subprocess
from pathlib import Path

import pytest

from codenib.graph.traverse_graph import RepoDependencySearcher, traverse_tree_structure
from codenib.log_utils import setup_detailed_logging
from codenib.ls_router import LSIndexer
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    NODE_TYPE_CLASS,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
)

pytestmark = pytest.mark.integration

HTTPIE_REPO_URL = "https://github.com/httpie/cli.git"
HTTPIE_REPO_PATH = Path("/tmp/httpie-cli")


def ensure_httpie_repo() -> Path:
    """Clone the httpie/cli repository if needed and return its path."""
    if not HTTPIE_REPO_PATH.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", HTTPIE_REPO_URL, str(HTTPIE_REPO_PATH)],
            check=True,
        )
    return HTTPIE_REPO_PATH


def _nodes_by_type(graph, node_type):
    return [
        vertex.attributes()
        for vertex in graph.graph.vs
        if vertex.attributes().get("type") == node_type
    ]


def run_traverse_graph_smoke(repo_path: Path, output_path: Path) -> None:
    """Exercise indexing and graph traversal against a real Python repository."""
    assert repo_path.is_dir(), f"Test repository not found: {repo_path}"

    graph = LSIndexer(repo_path, output_dir=output_path).run_pipeline(
        project_name="httpie_cli_test", skip_level="graph"
    )
    assert graph is not None, "LSIndexer did not produce a graph"

    file_names = {node["name"] for node in _nodes_by_type(graph, NODE_TYPE_FILE)}
    assert "httpie/core.py" in file_names
    assert _nodes_by_type(graph, NODE_TYPE_CLASS)
    assert _nodes_by_type(graph, NODE_TYPE_FUNCTION)
    assert _nodes_by_type(graph, NODE_TYPE_METHOD)
    assert _nodes_by_type(graph, NODE_TYPE_FIELD)

    root_file = "httpie/core.py"
    target_function = "httpie/core.py:raw_main()"
    downstream = traverse_tree_structure(
        graph,
        root_file,
        direction="downstream",
        hops=2,
        node_type_filter=[NODE_TYPE_CLASS, NODE_TYPE_FUNCTION, NODE_TYPE_METHOD],
    )
    assert downstream.startswith(root_file)
    assert target_function in downstream
    assert EDGE_TYPE_CONTAIN in downstream

    searcher = RepoDependencySearcher(graph)
    contain_nodes, contain_edges = searcher.get_neighbors(
        root_file,
        direction="forward",
        etype_filter=[EDGE_TYPE_CONTAIN],
    )
    assert target_function in contain_nodes
    assert contain_edges
    assert all(edge[3]["type"] == EDGE_TYPE_CONTAIN for edge in contain_edges)

    parent_nodes, parent_edges = searcher.get_neighbors(
        target_function,
        direction="backward",
        ntype_filter=[NODE_TYPE_FILE],
        etype_filter=[EDGE_TYPE_CONTAIN],
    )
    assert root_file in parent_nodes
    assert any(
        edge[0] == root_file and edge[1] == target_function for edge in parent_edges
    )

    upstream = traverse_tree_structure(
        graph,
        target_function,
        direction="upstream",
        hops=1,
        node_type_filter=[NODE_TYPE_FILE],
        edge_type_filter=[EDGE_TYPE_CONTAIN],
    )
    assert upstream.startswith(target_function)
    assert root_file in upstream


def test_transgraph_simple(httpie_cli_repo, tmp_path_factory):
    """Index a pinned checkout and verify traversal contracts end to end."""
    output_path = tmp_path_factory.mktemp("httpie_cli_transgraph")
    run_traverse_graph_smoke(httpie_cli_repo, output_path)


# Example usage
if __name__ == "__main__":
    print("Starting httpie CLI repository traverse test...")
    setup_detailed_logging(
        log_dir="logs", run_name="httpie_cli_test", mode="both", level="scip_debug"
    )
    standalone_output = Path("/tmp") / "httpie_cli_transgraph"
    standalone_output.mkdir(parents=True, exist_ok=True)
    run_traverse_graph_smoke(ensure_httpie_repo(), standalone_output)
    print("\nAll tests completed successfully.")
