"""Directed compiler navigation preserves actual source and dependency direction."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from codenib.eval.benchmarks.contextbench import contextbench_prediction
from codenib.graph.code_graph import CodeGraph
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_SYMBOL,
)
from scripts.benchmark_context_agent import observed_nodes
from scripts.contextbench_graph_navigation import (
    GraphNavigation,
    load_graph_records,
    load_navigation,
    prepare_graphs,
)


def read_receipt(file, content):
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "read",
                    "function": {
                        "name": "read",
                        "arguments": json.dumps({"file_path": file, "limit": 999}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "read", "content": content},
    ]


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


def test_local_definition_does_not_hide_enclosing_callable_references(navigation):
    tool, repo = navigation
    graph = tool.graph
    graph.graph.add_vertex(
        name="local",
        type=NODE_TYPE_SYMBOL,
        file="a.py",
        start_line=0,
        end_line=0,
        has_definition=True,
    )
    graph.name_to_vertex["local"] = graph.graph.vcount() - 1
    rebound = GraphNavigation(graph, repo)
    assert json.loads(rebound.neighbors("a.py", 1))[0]["node_id"] == "target"


@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
def test_callable_neighbors_precede_fields_within_raw_edge_budget(tmp_path, direction):
    graph = CodeGraph()
    for name, kind in (
        ("seed", NODE_TYPE_FUNCTION),
        ("field", NODE_TYPE_SYMBOL),
        ("callee", NODE_TYPE_FUNCTION),
    ):
        (tmp_path / f"{name}.py").write_text(f"{name} = 1\n")
        graph.graph.add_vertex(
            name=name,
            type=kind,
            file=f"{name}.py",
            start_line=0,
            end_line=0,
            has_definition=True,
        )
    graph.name_to_vertex = {v["name"]: v.index for v in graph.graph.vs}
    for target in (1, 2):
        source, destination = (0, target) if direction == "outgoing" else (target, 0)
        graph.graph.add_edge(
            source,
            destination,
            type=EDGE_TYPE_REFERENCE,
            anchor_file="seed.py",
            anchor_line=0,
        )
    tool = GraphNavigation(graph, tmp_path)
    result = json.loads(tool.neighbors("seed.py", 1, direction, limit=1))
    assert [record["node_id"] for record in result] == ["callee"]
    assert result[0]["direction"] == direction
    assert result[0]["relationship"] == EDGE_TYPE_REFERENCE
    assert result[0]["content"] == "1 | callee = 1"
    # A callable beyond the raw scan boundary cannot displace the visible field.
    bounded = GraphNavigation(graph, tmp_path, edge_limit=1)
    result = json.loads(bounded.neighbors("seed.py", 1, direction, limit=1))
    assert [record["node_id"] for record in result] == ["field"]


def test_unseen_navigation_skips_known_neighbors_without_using_result_limit(navigation):
    tool, repo = navigation
    graph = tool.graph
    graph.graph.add_edge(
        1, 3, type=EDGE_TYPE_REFERENCE, anchor_file="a.py", anchor_line=0
    )
    messages = read_receipt("b.py", "1 | def target():\n2 |     return 1")
    unseen = GraphNavigation(
        graph,
        repo,
        source_policy="unseen",
        observed_source=lambda: observed_nodes(messages, repo),
    )
    assert json.loads(tool.neighbors("a.py", 1, limit=1))[0]["node_id"] == "target"
    result = json.loads(unseen.neighbors("a.py", 1, limit=1))
    assert [record["node_id"] for record in result] == ["unrelated"]
    assert result[0]["status"] == "source_returned"


def test_unseen_navigation_counts_only_actual_source_and_preserves_gaps(navigation):
    tool, repo = navigation
    messages = read_receipt("b.py", "1 | def target():")
    unseen = GraphNavigation(
        tool.graph,
        repo,
        source_policy="unseen",
        observed_source=lambda: observed_nodes(messages, repo),
    )
    result = unseen.neighbors("a.py", 1)
    record = json.loads(result)[0]
    assert record["definition_start"] == 1 and record["definition_end"] == 2
    assert record["content"] == "2 |     return 1"
    graph_messages = [
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
    prediction = contextbench_prediction(
        instance_id="case", messages=graph_messages, repo_path=repo
    )
    assert prediction["traj_data"]["pred_spans"] == {"b.py": [{"start": 2, "end": 2}]}
    messages.extend(graph_messages)
    assert json.loads(unseen.neighbors("a.py", 1)) == [
        {"status": "no_unseen_neighbors", "content": ""}
    ]
    messages.clear()
    # A request without its returned source must not suppress any graph lines.
    messages.extend(read_receipt("b.py", "")[0:1])
    assert json.loads(unseen.neighbors("a.py", 1))[0]["content"].startswith("1 |")


def test_unseen_navigation_can_fit_missing_lines_but_retains_scan_budget(navigation):
    tool, repo = navigation
    header = "def target(" + ", ".join(f"arg{i}" for i in range(200)) + "):"
    (repo / "b.py").write_text(header + "\n    return 1\n")
    messages = read_receipt("b.py", "1 | " + header)
    unseen = GraphNavigation(
        tool.graph,
        repo,
        token_budget=200,
        source_policy="unseen",
        observed_source=lambda: observed_nodes(messages, repo),
    )
    control = GraphNavigation(tool.graph, repo, token_budget=200)
    assert (
        json.loads(control.neighbors("a.py", 1))[0]["status"] == "source_not_returned"
    )
    assert json.loads(unseen.neighbors("a.py", 1))[0]["content"] == "2 |     return 1"
    with pytest.raises(ValueError, match="requires observed source"):
        GraphNavigation(tool.graph, repo, source_policy="unseen")
    # A known first edge does not remove the independent raw-edge bound.
    messages[-1]["content"] += "\n2 |     return 1"
    tool.graph.graph.add_edge(
        1, 3, type=EDGE_TYPE_REFERENCE, anchor_file="a.py", anchor_line=0
    )
    bounded = GraphNavigation(
        tool.graph,
        repo,
        edge_limit=1,
        source_policy="unseen",
        observed_source=lambda: observed_nodes(messages, repo),
    )
    assert all(
        "node_id" not in record for record in json.loads(bounded.neighbors("a.py", 1))
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


def test_project_preparation_cannot_relabel_reused_graphs(tmp_path):
    with pytest.raises(ValueError, match="requires fresh graph builds"):
        prepare_graphs(
            SimpleNamespace(allow_project_preparation=True, reuse_cohort=tmp_path)
        )
