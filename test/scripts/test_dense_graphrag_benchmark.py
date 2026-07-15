# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeminer.types import QueriedNode
from scripts.retrieval_ablation import dense_graphrag_benchmark as benchmark
from scripts.retrieval_ablation import run_dense_graphrag_matrix as matrix


def test_ranked_files_deduplicates_without_reordering():
    nodes = [
        SimpleNamespace(file="/repo/src/a.py"),
        SimpleNamespace(file="/repo/src/a.py"),
        SimpleNamespace(file="src/a.py"),
        SimpleNamespace(file="src/b.py"),
        SimpleNamespace(file=None),
    ]

    assert benchmark._ranked_files(nodes) == ["/repo/src/a.py", "src/b.py"]


def test_recall_uses_unique_file_budget_and_suffix_matching():
    ranked = ["/cache/repo/src/a.py", "src/b.py"]

    assert benchmark._recall(ranked, ["src/b.py"], [1, 2]) == {
        "files@1": False,
        "files@2": True,
    }


def test_repo_partitions_are_grouped_stratified_and_deterministic():
    rows = {}
    instance_ids = []
    for language in ("Python", "Rust"):
        for repo_index in range(5):
            for issue_index in range(2):
                instance_id = f"{language.lower()}{repo_index}__issue-{issue_index}"
                instance_ids.append(instance_id)
                rows[instance_id] = {"language_group": language}

    first = benchmark.build_repo_partitions(rows, instance_ids, seed="fixed")
    second = benchmark.build_repo_partitions(rows, instance_ids, seed="fixed")

    assert first == second
    for language in ("python", "rust"):
        test_repos = {
            benchmark._repo_key(instance_id)
            for instance_id in instance_ids
            if instance_id.startswith(language) and first[instance_id] == "test"
        }
        assert len(test_repos) == 2
    for instance_id in instance_ids:
        siblings = [
            sibling
            for sibling in instance_ids
            if benchmark._repo_key(sibling) == benchmark._repo_key(instance_id)
        ]
        assert {first[sibling] for sibling in siblings} == {first[instance_id]}


def test_summarize_keeps_oracle_coverage_separate_from_ranked_recall():
    results = [
        {
            "instance_id": "repo__one",
            "n_targets": 1,
            "recall": {
                "dense": {"files@1": False},
                "dense_graph_fusion": {"files@1": True},
            },
            "coverage": {
                "dense_pool_hit": False,
                "union_pool_hit": True,
                "graph_pool_recovers": ["target.py"],
            },
        },
        {
            "instance_id": "repo__two",
            "n_targets": 1,
            "recall": {
                "dense": {"files@1": True},
                "dense_graph_fusion": {"files@1": False},
            },
            "coverage": {
                "dense_pool_hit": True,
                "union_pool_hit": True,
                "graph_pool_recovers": [],
            },
        },
    ]

    summary = benchmark.summarize(results, [1])

    assert summary["recall"]["dense"]["files@1"]["hits"] == 1
    assert summary["recall"]["dense_graph_fusion"]["files@1"]["hits"] == 1
    assert summary["coverage"] == {
        "dense_pool_hits": 1,
        "union_pool_hits": 2,
        "graph_pool_helps": ["repo__one"],
    }
    assert summary["paired_vs_dense"]["dense_graph_fusion"]["files@1"] == {
        "gains": 1,
        "losses": 1,
        "net_hits": 0,
        "delta_rate": 0.0,
    }


def test_nearest_rank_uses_observed_p90():
    assert benchmark._nearest_rank(list(range(1, 11)), 0.9) == 9


def test_checkpoint_is_atomically_replaced(tmp_path, monkeypatch):
    output = tmp_path / "result.json"
    output.write_text('{"stale": true}', encoding="utf-8")
    replacements = []
    real_replace = Path.replace

    def record_replace(source, target):
        replacements.append((source, target))
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", record_replace)

    benchmark._write_checkpoint(output, {"config": {}}, [], [1])

    assert replacements == [(tmp_path / "result.json.tmp", output)]
    assert not (tmp_path / "result.json.tmp").exists()
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["scored"] == 0


def test_cross_encoder_arm_uses_exact_unique_file_budget():
    nodes = [
        QueriedNode(file="a.py", node_name="a1", content="a first"),
        QueriedNode(file="a.py", node_name="a2", content="a duplicate"),
        QueriedNode(file="b.py", node_name="b", content="b body"),
        QueriedNode(file="c.py", node_name="c", content="c body"),
    ]

    class FakeContext:
        def score(self, query, docs):
            assert query == "query"
            assert docs == ["a first", "b body"]
            return [0.1, 0.9]

    candidates, hydration_ms, dropped = benchmark._prepare_cross_encoder_candidates(
        ranked_nodes=nodes,
        candidate_k=2,
        repo_path="/unused",
        git_commit="unused",
    )
    result, elapsed_ms = benchmark._cross_encoder_rerank(
        query="query",
        candidates=candidates,
        context=FakeContext(),
        candidate_k=2,
    )

    assert [node.file for node in result] == ["b.py", "a.py"]
    assert dropped == []
    assert hydration_ms >= 0
    assert elapsed_ms >= 0


def test_cross_encoder_arm_rejects_incomplete_candidate_budget():
    nodes = [QueriedNode(file="a.py", node_name="a", content="a body")]

    with pytest.raises(RuntimeError, match="expected exactly 2"):
        benchmark._cross_encoder_rerank(
            query="query",
            candidates=nodes,
            context=SimpleNamespace(),
            candidate_k=2,
        )


def test_cross_encoder_arm_rejects_missing_content():
    nodes = [QueriedNode(file="a.py", node_name="a")]

    with pytest.raises(RuntimeError, match="missing content"):
        benchmark._cross_encoder_rerank(
            query="query",
            candidates=nodes,
            context=SimpleNamespace(),
            candidate_k=1,
        )


def test_cross_encoder_candidate_preparation_drops_and_backfills(monkeypatch):
    nodes = [
        QueriedNode(file="generated.d.ts", node_name="generated"),
        QueriedNode(file="source.ts", node_name="source"),
        QueriedNode(file="ready.ts", node_name="ready", content="ready body"),
    ]

    def fake_hydrate(candidates, *, repo_path, git_commit):
        assert repo_path == "/repo"
        assert git_commit == "base"
        candidate = candidates[0]
        if candidate.file == "source.ts":
            return [candidate.model_copy(update={"content": "source body"})]
        return [candidate]

    monkeypatch.setattr(
        "codeminer.ops.expand.hydrate_candidate_contents",
        fake_hydrate,
    )

    eligible, hydration_ms, dropped = benchmark._prepare_cross_encoder_candidates(
        ranked_nodes=nodes,
        candidate_k=2,
        repo_path="/repo",
        git_commit="base",
    )

    assert [node.file for node in eligible] == ["source.ts", "ready.ts"]
    assert dropped == ["generated.d.ts"]
    assert hydration_ms >= 0


def test_paired_cross_encoder_budget_caps_both_arms_to_available_files():
    dense = [SimpleNamespace(file=f"dense-{index}.py") for index in range(29)]
    graph = [SimpleNamespace(file=f"graph-{index}.py") for index in range(80)]

    actual, available = benchmark._paired_cross_encoder_budget(
        requested=50,
        minimum=10,
        eligible_rankings={"dense": dense, "graph": graph},
    )

    assert actual == 29
    assert available == {"dense": 29, "graph": 80}


def test_paired_cross_encoder_budget_rejects_less_than_metric_budget():
    dense = [SimpleNamespace(file=f"dense-{index}.py") for index in range(9)]

    with pytest.raises(RuntimeError, match="files@10"):
        benchmark._paired_cross_encoder_budget(
            requested=50,
            minimum=10,
            eligible_rankings={"dense": dense, "graph": dense},
        )


def test_matrix_command_locks_the_controlled_protocol(tmp_path):
    cell = matrix.EMBEDDINGS[0]

    command = matrix._command(
        cell=cell,
        output=tmp_path / "result.json",
        partition="all",
        prebuilt_dir=tmp_path,
        cross_encoder_model="Qwen/Qwen3-Reranker-4B",
        cross_encoder_candidate_k=50,
        cross_encoder_batch_size=8,
        limit=2,
    )

    assert command[1:3] == [
        "-m",
        "scripts.retrieval_ablation.dense_graphrag_benchmark",
    ]
    assert command[command.index("--embedding-model") + 1] == cell.model
    assert command[command.index("--embedding-dim") + 1] == str(cell.dimension)
    assert command[command.index("--graph-weight") + 1] == "0.5"
    assert command[command.index("--cross-encoder-candidate-k") + 1] == "50"
    assert "--resume" in command
    assert command[-2:] == ["--limit", "2"]


def test_matrix_complete_requires_all_error_free_rows(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(
        '{"instance_ids":["a","b"],"results":[{},{}],'
        '"summary":{"scored":2,"errors":0}}',
        encoding="utf-8",
    )

    assert matrix._is_complete(result_path)

    result_path.write_text(
        '{"instance_ids":["a","b"],"results":[{}],'
        '"summary":{"scored":1,"errors":0}}',
        encoding="utf-8",
    )
    assert not matrix._is_complete(result_path)
