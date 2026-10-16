# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deterministic boundaries for the source-only graph/decisions experiment."""

import json
from types import SimpleNamespace

import pytest
import requests
import tiktoken

from codenib.code_chunking.base import CodeChunk
from codenib.graph.code_graph import CodeGraph
from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_CLASS,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
    NodeInfo,
)
from scripts.benchmark_graph_decisions import (
    expand_candidates,
    graph_bindings,
    grep_payload,
    grep_pools,
    pack_context,
    prediction,
    strict_rerank,
    visible_node,
)
from scripts.benchmark_jev_retrieval import MeasuredDecisions


def node(name, line, content="def f():\n    pass\n", file="code.py"):
    return NodeInfo(
        node_id=name,
        node_name=name,
        type=NODE_TYPE_FUNCTION,
        file=file,
        start_line=line,
        end_line=line + len(content.splitlines()) - 1,
        content=content,
    )


def graph_fixture():
    graph = CodeGraph()
    nodes = [
        node("a", 0),
        node("b", 5),
        node("c", 10, file="test_code.py"),
        node("d", 20),
    ]
    for n in nodes:
        graph.graph.add_vertex(
            name=n.node_id,
            type=n.type,
            file=n.file,
            start_line=n.start_line,
            end_line=n.end_line,
            has_definition=True,
        )
    graph.graph.add_vertex(name="file", type=NODE_TYPE_FILE, has_definition=False)
    graph.name_to_vertex = {v["name"]: v.index for v in graph.graph.vs}
    # Cycle, parallel edge and a file hub leading to an unrelated definition.
    for a, b, kind in [
        (0, 1, EDGE_TYPE_REFERENCE),
        (0, 1, EDGE_TYPE_REFERENCE),
        (1, 2, EDGE_TYPE_REFERENCE),
        (2, 1, EDGE_TYPE_REFERENCE),
        (4, 0, EDGE_TYPE_CONTAIN),
        (4, 3, EDGE_TYPE_CONTAIN),
    ]:
        graph.graph.add_edge(a, b, type=kind)
    return graph, nodes


def test_hops_cycles_and_file_hubs_preserve_real_reachability():
    graph, nodes = graph_fixture()
    first, _ = expand_candidates(graph, nodes, [0, 3, 2, 1], seeds=1, hops=1)
    second, info = expand_candidates(graph, nodes, [0, 3, 2, 1], seeds=1, hops=2)
    assert first == [0, 1]
    assert second == [0, 1, 2]  # Test definitions are legitimate context.
    assert info["selected_depths"] == [0, 1, 2]
    assert 3 not in second  # No file -> sibling shortcut.
    zero, _ = expand_candidates(graph, nodes, [0, 3, 2, 1], seeds=1, hops=0)
    assert zero == [0]


def test_edge_and_candidate_budgets_do_not_silently_widen():
    graph, nodes = graph_fixture()
    selected, info = expand_candidates(
        graph,
        nodes,
        [0, 1, 2, 3],
        seeds=1,
        hops=2,
        edge_limit=1,
    )
    assert info["raw_edges_inspected"] == 1
    assert info["edge_cap_hit"]
    assert len(selected) <= 2
    selected, info = expand_candidates(
        graph,
        nodes,
        [0, 1, 2, 3],
        seeds=1,
        hops=2,
        candidate_limit=2,
    )
    assert selected == [0, 1]
    assert info["reachable_chunks"] == 3
    assert info["candidate_cap_hit"]


def test_explicit_grep_seeds_replace_bm25_roots_without_refill():
    graph, nodes = graph_fixture()
    # BM25 would seed a; grep picks isolated d, which must stay isolated.
    found, trace = expand_candidates(
        graph,
        nodes,
        [0, 1, 2, 3],
        seeds=1,
        hops=2,
        seed_indices=[3],
    )
    assert found == [3]
    assert trace["seed_symbols"] == 1
    empty, trace = expand_candidates(
        graph,
        nodes,
        [0, 1, 2, 3],
        seeds=5,
        seed_indices=[],
    )
    assert empty == []
    assert trace["seed_symbols"] == 0


def test_grep_seed_sweep_counts_available_seeds_and_keeps_disconnected_roots():
    graph, nodes = graph_fixture()
    pools, traces, _ = grep_pools(graph, nodes, [0, 1, 2, 3], [3, 0], [0, 3], 40)
    assert pools["grep_graph2_s1"] == [3]
    assert set(pools["grep_graph2_s5"]) == {0, 1, 2, 3}
    assert pools["grep_seed20"] == [3, 0]
    assert traces["grep_graph2_s20"]["seed_chunks"] == 2
    assert pools["grep_wide"] == [0, 3]
    empty, _, _ = grep_pools(graph, nodes, [0, 1, 2, 3], [], [], 40)
    assert all(not pool for pool in empty.values())


def test_grep_planner_payload_excludes_labels_and_code(monkeypatch):
    from types import SimpleNamespace

    from scripts import benchmark_graph_decisions as experiment

    case = SimpleNamespace(
        repo="owner/repo",
        base_commit="a" * 40,
        problem_statement="Find the issue",
        gold_context="GOLD_SECRET",
    )
    source = {
        "repo": "/irrelevant",
        "nodes": [{"file": "src/a.py", "content": "CODE_SECRET"}],
    }
    monkeypatch.setattr(
        experiment,
        "snapshot_files",
        lambda *_: [
            ("src/a.py", "b" * 40, "python"),
            ("docs/unindexed.py", "c" * 40, "python"),
        ],
    )
    payload, files = grep_payload(case, source)
    assert set(payload) == {
        "repo",
        "issue",
        "source_file_count",
        "source_directories",
        "directory_overview_truncated",
        "note",
    }
    assert files == [("src/a.py", "b" * 40, "python")]
    assert "SECRET" not in json.dumps(payload)


def test_binding_prefers_method_to_containing_class_and_retains_split_chunks():
    graph, nodes = graph_fixture()
    graph.graph.add_vertex(
        name="class",
        type=NODE_TYPE_CLASS,
        file="code.py",
        start_line=0,
        end_line=30,
        has_definition=True,
    )
    nodes.append(node("split_a", 1, "    pass\n"))
    forward, reverse = graph_bindings(graph, nodes)
    assert forward[0] == "a"
    assert forward[4] == "a"
    assert reverse["a"] == [0, 4]


def test_decorated_definition_matches_identity_before_parent_scope():
    graph, nodes = graph_fixture()
    graph.graph.vs[0]["start_line"] = 1
    graph.graph.vs[0]["unified_name"] = "a"
    graph.graph.add_vertex(
        name="parent",
        type=NODE_TYPE_CLASS,
        file="code.py",
        start_line=0,
        end_line=30,
        has_definition=True,
    )
    forward, _ = graph_bindings(graph, nodes)
    assert forward[0] == "a"


def test_visible_content_uses_source_not_synthetic_headers_and_drops_partial_lines():
    chunk = CodeChunk(
        "a synthetic scope header\nabc\nlong second line\n",
        5,
        6,
        NODE_TYPE_FUNCTION,
        "a",
        "code.py",
        "a",
    )
    source = ["before\n"] * 5 + ["abc\n", "long second line\n"]
    clipped = visible_node(chunk, source, max_chars=8)
    assert clipped.content == "abc\n"
    assert clipped.start_line == clipped.end_line == 5


def test_context_budget_and_export_count_only_unique_delivered_lines(tmp_path):
    encoder = tiktoken.get_encoding("cl100k_base")
    (tmp_path / "code.py").write_text("zero\none\ntwo\nthree\n")
    a = node("a", 0, "zero\none\n")
    b = node("b", 1, "one\ntwo\nthree\n")
    text, count = pack_context([a, b], top_k=2, token_budget=4096, encoder=encoder)
    assert text.count("2 | one") == 1
    assert count == len(encoder.encode(text))
    exported = prediction("case", text, tmp_path)
    assert exported["traj_data"]["pred_spans"] == {"code.py": [{"start": 1, "end": 4}]}
    # A smaller budget cannot claim unseen suffix lines in the exported span.
    small, tokens = pack_context(
        [a, b], top_k=2, token_budget=count - 9, encoder=encoder
    )
    assert tokens <= count - 9
    assert "4 | three" not in small
    assert json.loads(small)


def test_failed_decision_fallback_is_not_scored_as_a_success(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")

    def fail(*args, **kwargs):
        raise requests.ConnectionError("unavailable")

    monkeypatch.setattr(requests, "post", fail)
    client = MeasuredDecisions(model="perplexity/pplx-decider-v1.1-27b", max_cost_usd=1)
    with pytest.raises(RuntimeError, match="fallback"):
        strict_rerank(client, "find f", [node("a", 0)])
    assert "error" in client.calls[0]


def test_blank_source_gaps_do_not_invalidate_complete_decision_scoring():
    class Decisions:
        model = "recorded-test"

        def __init__(self):
            self.calls = []

        def decide(self, *, state, questions):
            assert list(questions) == ["node_0", "node_2"]
            self.calls.append({"question_count": 2, "usage": {"cost": 0.001}})
            return SimpleNamespace(
                answers={
                    "node_0": SimpleNamespace(score=1.5),
                    "node_2": SimpleNamespace(score=3.0),
                }
            )

    ranked = strict_rerank(
        Decisions(), "find f", [node("a", 0), node("gap", 2, "\n"), node("b", 3)]
    )
    assert [n.node_id for n in ranked] == ["b", "a", "gap"]
    assert [n.score for n in ranked] == [1.0, 0.5, 0.0]


def test_split_seed_function_includes_all_root_chunks_before_hopping():
    graph, nodes = graph_fixture()
    graph.graph.vs[graph.name_to_vertex["a"]]["end_line"] = 4
    nodes.append(node("a-tail", 2))
    selected, trace = expand_candidates(graph, nodes, [0, 3, 2, 1, 4], seeds=1, hops=0)
    assert selected == [0, 4]
    assert trace["selected_depths"] == [0, 0]
    selected, trace = expand_candidates(
        graph, nodes, [0, 3, 2, 1, 4], seeds=1, hops=0, candidate_limit=1
    )
    assert selected == [0]
    assert trace["candidate_cap_hit"]


@pytest.mark.parametrize("command", ["run", "prepare-grep"])
@pytest.mark.parametrize("budget", ["nan", "inf", "-inf", "0", "-1"])
def test_graph_cli_rejects_invalid_budget_before_live_work(
    monkeypatch, command, budget
):
    from scripts import benchmark_graph_decisions as experiment

    monkeypatch.setattr(
        experiment, "run", lambda _args: pytest.fail("billed work started")
    )
    monkeypatch.setattr(
        experiment, "prepare_grep", lambda _args: pytest.fail("billed work started")
    )
    with pytest.raises(SystemExit) as exc:
        experiment.main(
            [
                command,
                "--prepared",
                "unused",
                "--output",
                "unused",
                "--live",
                f"--max-cost-usd={budget}",
            ]
        )
    assert exc.value.code == 2
