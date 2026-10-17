"""Directed compiler navigation preserves actual source and dependency direction."""

import hashlib
import json

import pytest

from codenib.eval.benchmarks.contextbench import contextbench_prediction
from codenib.graph.code_graph import CodeGraph
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
)
from scripts.contextbench_graph_navigation import (
    GraphNavigation,
    load_graph_records,
    load_navigation,
)


@pytest.fixture
def navigation(tmp_path):
    graph = CodeGraph()
    for name, file in (
        ("caller", "tests/test_a.py"),
        ("seed", "a.py"),
        ("target", "b.py"),
        ("unrelated", "c.py"),
    ):
        path = tmp_path / file
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"def {name}():\n    return 1\n")
        graph.graph.add_vertex(
            name=name,
            type=NODE_TYPE_FUNCTION,
            file=file,
            start_line=0,
            end_line=1,
            has_definition=True,
        )
    graph.graph.add_vertex(name="hub", type=NODE_TYPE_FILE, has_definition=False)
    graph.name_to_vertex = {v["name"]: v.index for v in graph.graph.vs}
    for source, target, kind in (
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (4, 1, EDGE_TYPE_CONTAIN),
        (4, 3, EDGE_TYPE_CONTAIN),
    ):
        graph.graph.add_edge(
            source, target, type=kind, anchor_file="a.py", anchor_line=0
        )
    return GraphNavigation(graph, tmp_path), tmp_path


def test_directed_navigation_preserves_tests_and_excludes_file_hubs(navigation):
    tool, repo = navigation
    incoming = json.loads(tool.neighbors("a.py", 1, "incoming"))
    assert [r["node_id"] for r in incoming] == ["caller"]
    assert incoming[0]["file"] == "tests/test_a.py"
    result = tool.neighbors("a.py", 1, "outgoing")
    outgoing = json.loads(result)
    assert [r["node_id"] for r in outgoing] == ["target"]
    assert outgoing[0]["anchor_line"] == 1
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "graph",
                    "function": {"name": "graph_neighbors", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "graph", "content": result},
    ]
    assert contextbench_prediction(
        instance_id="case", messages=messages, repo_path=repo
    )["traj_data"]["pred_spans"] == {"b.py": [{"start": 1, "end": 2}]}


def test_unavailable_or_oversized_source_gets_no_read_credit(navigation):
    tool, repo = navigation
    (repo / "b.py").write_text("def target():\n    return '" + "x" * 20000 + "'\n")
    row = json.loads(tool.neighbors("a.py", 1))[0]
    assert row["status"] == "source_not_returned" and row["content"] == ""
    assert (
        json.loads(GraphNavigation(None, repo).neighbors("a.py", 1))[0]["status"]
        == "graph_unavailable"
    )


def test_navigation_enforces_path_and_scan_bounds(navigation):
    tool, repo = navigation
    with pytest.raises(ValueError, match="escapes"):
        tool.neighbors("../outside.py", 1)
    (repo / "b.py").unlink()
    (repo / "b.py").symlink_to(repo.parent / "secret.py")
    assert all("node_id" not in r for r in json.loads(tool.neighbors("a.py", 1)))
    bounded = GraphNavigation(tool.graph, repo, edge_limit=1)
    assert (
        json.loads(bounded.neighbors("a.py", 1, "incoming"))[0]["node_id"] == "caller"
    )


def test_frozen_graph_bytes_and_source_population_are_checked(navigation):
    tool, repo = navigation
    prepared = repo / "prepared"
    prepared.mkdir()
    (prepared / "manifest.json").write_text("source manifest")
    graphs = repo / "graphs"
    graphs.mkdir()
    path = graphs / "graph.pkl"
    tool.graph.save_graph(path)
    record = {
        "instance_id": "case",
        "available": True,
        "path": "graph.pkl",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    manifest = {
        "source_manifest_sha256": hashlib.sha256(b"source manifest").hexdigest(),
        "implementation": {},
        "cases": [record],
    }
    (graphs / "manifest.json").write_text(json.dumps(manifest))
    loaded = load_graph_records(graphs, prepared, {"case": None})
    assert (
        json.loads(load_navigation(loaded["case"], graphs, repo).neighbors("a.py", 1))[
            0
        ]["node_id"]
        == "target"
    )
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="Frozen graph bytes changed"):
        load_navigation(record, graphs, repo)
    with pytest.raises(ValueError, match="retain all selected"):
        load_graph_records(graphs, prepared, {"case": None, "missing": None})
    (prepared / "manifest.json").write_text("changed")
    with pytest.raises(ValueError, match="different source manifest"):
        load_graph_records(graphs, prepared, {"case": None})
