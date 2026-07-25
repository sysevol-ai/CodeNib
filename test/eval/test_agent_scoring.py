# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for shared agent-runner scoring helpers."""

from __future__ import annotations

from types import SimpleNamespace

from codenib.eval.agent_runner.scoring import evaluate_agent_localization


def _node(**kwargs):
    return SimpleNamespace(**kwargs)


def test_evaluate_agent_localization_scores_answer_and_retrieval_spans():
    evaluation = evaluate_agent_localization(
        answer=(
            "Files: src/foo.py\n"
            "Symbols: src/foo.py:target()\n"
            "Locations: src/foo.py:10-12"
        ),
        file_read_paths=[],
        nodes=[
            _node(
                node_id="src/foo.py:target()",
                file="/tmp/repo/src/foo.py",
                node_name="target",
                start_line=9,
                end_line=12,
                score=0.9,
            )
        ],
        target_files=["src/foo.py"],
        target_symbols=["src/foo.py:target()"],
        gt_blocks=[
            {
                "file": "src/foo.py",
                "start": 10,
                "end": 12,
                "source": "gt",
            }
        ],
        metrics_k=[1],
        repo_path="/tmp/repo",
        symbol_span_index={("src/foo.py", "target"): (10, 12)},
        preload_candidates=[
            {"file": "src/foo.py", "start": 10, "end": 12, "source": "preload"}
        ],
    )

    assert evaluation.metrics["files"][1]["accuracy"] == 1.0
    assert evaluation.metrics["symbols"][1]["accuracy"] == 1.0
    assert evaluation.metrics["answer_blocks"][1]["recall"] == 1.0
    assert evaluation.metrics["retrieval_blocks"][1]["recall"] == 1.0
    assert evaluation.preload_contribution == 1.0
    assert not evaluation.format_failed
    assert evaluation.to_record_fields()["target_files"] == ["src/foo.py"]


def test_evaluate_agent_localization_flags_unformatted_answer_without_spans():
    evaluation = evaluate_agent_localization(
        answer="The relevant code is probably in foo.",
        file_read_paths=[],
        nodes=[],
        target_files=[],
        target_symbols=[],
        gt_blocks=[],
        metrics_k=[1],
        repo_path="",
    )

    assert evaluation.format_failed
    assert not evaluation.metrics_meaningful
    assert evaluation.preload_contribution is None


def test_evaluate_agent_localization_allows_meaningful_override():
    evaluation = evaluate_agent_localization(
        answer="Files: src/foo.py\nSymbols:\nLocations:",
        file_read_paths=[],
        nodes=[],
        target_files=[],
        target_symbols=[],
        gt_blocks=[],
        metrics_k=[1],
        repo_path="",
        metrics_meaningful=True,
    )

    assert evaluation.metrics_meaningful
    assert not evaluation.format_failed
