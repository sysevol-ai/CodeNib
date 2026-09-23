# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess
from types import SimpleNamespace

import pytest

from codenib.code_chunker import CodeChunker
from codenib.types import NodeInfo
from scripts.benchmark_jev_base import (
    candidate_coverage,
    digest,
    load_cases,
    metrics,
    paired_bootstrap,
    pool_coverage,
    prefix,
    repeat_consistency,
    score_qwen,
    snapshot_chunks,
    visible_node,
)


def test_git_snapshot_uses_requested_commit_despite_later_and_dirty_source(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    source = tmp_path / "module.py"
    source.write_text("def locate():\n    return 'original'\n")
    git("add", "module.py")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "first",
    )
    commit = git("rev-parse", "HEAD")
    source.write_text("def locate():\n    return 'later'\n")
    git("add", "module.py")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "second",
    )
    source.write_text("def locate():\n    return 'dirty'\n")
    before = git("status", "--porcelain")

    chunks, files = snapshot_chunks(
        tmp_path, commit, CodeChunker(language="python"), {}
    )

    assert [name for name, _oid, _language in files] == ["module.py"]
    assert chunks and all("original" in c.content for c in chunks)
    assert "dirty" in source.read_text()
    assert git("status", "--porcelain") == before
    assert git("rev-parse", "HEAD") != commit


def test_metrics_convert_line_origin_and_deduplicate_overlapping_predictions():
    case = {
        "target_files": ["a.py", "b.py"],
        "target_blocks": [
            {"file": "a.py", "start": 11, "end": 11},
            {"file": "b.py", "start": 1, "end": 1},
        ],
    }
    nodes = [
        NodeInfo(file="a.py", start_line=10, end_line=10),
        NodeInfo(file="a.py", start_line=10, end_line=10),
        NodeInfo(file="b.py", start_line=0, end_line=0),
    ]
    result = metrics(case, nodes)
    assert result["span_recall@1"] == 0.5
    assert result["span_recall@5"] == 1.0
    assert result["file_recall@1"] == 0.5
    assert result["span_mrr"] == 1.0


def test_changed_frozen_candidates_are_rejected_before_scoring(tmp_path):
    case = tmp_path / "case.json"
    case.write_text("{}")
    manifest = {"cases": [{"file": "case.json", "sha256": digest(case)}]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    case.write_text('{"candidates": []}')
    with pytest.raises(ValueError, match="Frozen case changed"):
        load_cases(tmp_path)


def test_paired_bootstrap_groups_instances_by_repository():
    cases = [
        {"repo": "one", "instance_id": "a"},
        {"repo": "one", "instance_id": "b"},
        {"repo": "two", "instance_id": "c"},
    ]
    left = {key: {"recall": 0.75} for key in "abc"}
    right = {key: {"recall": 0.25} for key in "abc"}
    result = paired_bootstrap(cases, left, right, "recall")
    assert result == {"difference": 0.5, "ci95": [0.5, 0.5], "repositories": 2}


def test_last_position_inference_preserves_yes_no_scores_with_left_padding():
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    config = transformers.Qwen3Config(
        vocab_size=32,
        hidden_size=16,
        intermediate_size=32,
        num_hidden_layers=1,
        num_attention_heads=2,
        num_key_value_heads=1,
        head_dim=8,
        attention_dropout=0.0,
    )
    model = transformers.Qwen3ForCausalLM(config).eval()
    inputs = {
        "input_ids": torch.tensor([[0, 0, 4, 5], [1, 2, 3, 4]]),
        "attention_mask": torch.tensor([[0, 0, 1, 1], [1, 1, 1, 1]]),
    }
    scorer = SimpleNamespace(
        batch_size=2,
        _device="cpu",
        _model=model,
        _yes_id=7,
        _no_id=8,
        _format_pair=lambda query, doc: query + doc,
        _build_input_ids=lambda pairs: inputs,
    )
    with torch.inference_mode():
        logits = model(**inputs, use_cache=False).logits[:, -1, :]
        expected = torch.softmax(logits[:, [7, 8]], dim=-1)[:, 0]
    actual = score_qwen(scorer, "query", ["first", "second"])
    torch.testing.assert_close(torch.tensor(actual), expected)


def test_candidate_ceiling_does_not_depend_on_overlap_deduplication_order():
    nodes = [
        NodeInfo(file="a.py", start_line=0, end_line=5),
        NodeInfo(file="a.py", start_line=5, end_line=10),
    ]
    case = {
        "target_files": ["a.py"],
        "target_blocks": [{"file": "a.py", "start": 11, "end": 11}],
        "candidates": [n.model_dump() for n in nodes],
    }
    assert metrics(case, nodes)["span_recall@50"] == 0
    assert candidate_coverage(case)["span_recall"] == 1
    case["candidates"].reverse()
    assert candidate_coverage(case)["span_recall"] == 1


def test_repeat_statistics_keep_rank_score_and_metric_changes_separate():
    first = [
        {"node_id": str(i), "start_line": 0, "end_line": 0, "score": 10.0 - i}
        for i in range(10)
    ]
    second = [dict(first[5], score=10.5), *first[:5], *first[6:]]
    rows = [
        {
            "instance_id": "case",
            "rep": 0,
            "ranking": first,
            "metrics": {"span_recall@5": 0.0},
        },
        {
            "instance_id": "case",
            "rep": 1,
            "ranking": second,
            "metrics": {"span_recall@5": 1.0},
        },
    ]
    result = repeat_consistency(rows)
    assert result["repeated_instances"] == 1
    assert result["instances_with_rank_changes"] == 1
    assert result["instances_with_score_changes"] == 1
    assert result["instances_with_span_recall5_changes"] == 1
    assert result["mean_raw_top5_set_overlap"] == 0.8
    assert result["mean_rank_spearman"] == pytest.approx(1 - 180 / 990)
    rows[1]["ranking"] = second[:-1]
    with pytest.raises(ValueError, match="candidate identity"):
        repeat_consistency(rows)


def test_truncated_text_cannot_credit_hidden_target_lines():
    chunk = SimpleNamespace(
        node_id="a.py:f",
        file="a.py",
        chunk_type="function",
        start_line=10,
        end_line=12,
        content="first\nsecond\nthird",
    )
    node = visible_node(chunk, 6)
    assert node.end_line == 10
    case = {
        "target_files": ["a.py"],
        "target_blocks": [{"file": "a.py", "start": 12, "end": 12}],
    }
    assert pool_coverage(case, [node]) == {
        "file_recall": 1.0,
        "span_recall": 0.0,
        "span_all": 0.0,
    }
    assert pool_coverage(case, [visible_node(chunk, 3000)])["span_recall"] == 1


def test_prefix_preserves_candidate_order_and_recomputes_baseline(tmp_path):
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    candidates = [
        NodeInfo(node_id="first", file="a.py", start_line=0, end_line=0),
        NodeInfo(node_id="second", file="b.py", start_line=0, end_line=0),
    ]
    case = {
        "instance_id": "example",
        "target_files": ["b.py"],
        "target_blocks": [{"file": "b.py", "start": 1, "end": 1}],
        "candidates": [n.model_dump() for n in candidates],
    }
    case["baseline_metrics"] = metrics(case, candidates)
    case_path = prepared / "case.json"
    case_path.write_text(json.dumps(case))
    manifest = {
        "candidate_count": 2,
        "cases": [{"file": "case.json", "sha256": digest(case_path)}],
    }
    (prepared / "manifest.json").write_text(json.dumps(manifest))
    output = tmp_path / "prefix"
    args = SimpleNamespace(prepared=prepared, output=output, candidates=1)
    prefix(args)
    frozen, cases = load_cases(output)
    assert frozen["parent_manifest_sha256"] == digest(prepared / "manifest.json")
    assert cases[0]["candidates"] == case["candidates"][:1]
    assert cases[0]["baseline_metrics"]["span_recall@5"] == 0
    assert load_cases(prepared)[1][0]["baseline_metrics"]["span_recall@5"] == 1
    args.candidates = 3
    with pytest.raises(ValueError, match="exceeds"):
        prefix(args)
