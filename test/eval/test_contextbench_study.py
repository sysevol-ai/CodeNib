# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the frozen ContextBench external-validity protocol."""

from __future__ import annotations

import json

import pytest

from codeminer.eval.contextbench_study import (
    CONTEXTBENCH_TEST_SHA256,
    SYNTHESIS_DEVELOPMENT_REPOSITORIES,
    build_protocol,
    normalize_gold_context,
    normalize_gold_path,
    protocol_rows,
    repository_identity,
    score_context_candidates,
    summarize_agent_context_policies,
    summarize_preload_retrieval,
)


def test_repository_identity_recovers_owner_for_multilingual_rows():
    assert (
        repository_identity(
            {
                "repo": "core",
                "original_inst_id": "vuejs__core-10289",
            }
        )
        == "vuejs/core"
    )
    assert (
        repository_identity(
            {
                "repo": "jq",
                "original_inst_id": "jqlang__jq-2654",
            }
        )
        == "jqlang/jq"
    )


def test_gold_path_normalization_preserves_dotfiles():
    assert normalize_gold_path("/workspace/org__repo__0.1/src/a.py") == "src/a.py"
    assert normalize_gold_path("/testbed/pkg/b.py") == "pkg/b.py"
    assert normalize_gold_path(".size-limit.js") == ".size-limit.js"


def test_gold_context_removes_exact_duplicate_ranges():
    contexts = [
        {"file": "a.py", "start_line": 3, "end_line": 8},
        {"file": "a.py", "start_line": 3, "end_line": 8},
    ]

    assert normalize_gold_context(contexts) == [
        {"file_path": "a.py", "start_line": 3, "end_line": 8}
    ]


def _record(index: int, repo: str) -> dict:
    return {
        "instance_id": f"ContextBench__python__{index:02d}",
        "original_inst_id": f"owner__repo-{index + 1}",
        "repo": repo,
        "language": "python",
        "base_commit": f"{index + 1:040x}",
        "gold_context": json.dumps(
            [
                {
                    "file": "src/module.py",
                    "start_line": 3,
                    "end_line": 9,
                    "content": "ignored by the protocol",
                }
            ]
        ),
        "patch": f"patch-{index}",
        "test_patch": f"test-patch-{index}",
        "problem_statement": f"Find issue {index}",
        "hints_text": None,
        "source": "Verified",
    }


def test_protocol_freezes_outcome_independent_repository_disjoint_slice():
    selected_repos = [f"external-owner/repo-{index}" for index in range(18)]
    records = [_record(index, selected_repos[index % 18]) for index in range(41)]
    records.extend(
        _record(41 + index, repo)
        for index, repo in enumerate(SYNTHESIS_DEVELOPMENT_REPOSITORIES[:9])
    )

    protocol = build_protocol(
        records,
        dataset_file_sha256=CONTEXTBENCH_TEST_SHA256,
    )

    assert protocol["selection"]["selected_rows"] == 41
    assert protocol["selection"]["selected_repositories"] == 18
    assert len(protocol["selection"]["excluded_rows"]) == 9
    assert len(protocol_rows(protocol)) == 41
    assert protocol["agent_protocol"]["planned_cells"] == 123

    protocol["rows"][0]["query"] = "post-freeze mutation"
    with pytest.raises(ValueError, match="payload hash"):
        protocol_rows(protocol)


def test_protocol_rejects_unpinned_parquet():
    with pytest.raises(ValueError, match="parquet hash"):
        build_protocol([], dataset_file_sha256="0" * 64)


def test_context_candidate_metrics_merge_ranges_and_match_files():
    metrics = score_context_candidates(
        [
            {"file": "a.py", "start": 5, "end": 12},
            {"file": "a.py", "start": 10, "end": 15},
            {"file": "other.py", "start": 1, "end": 5},
        ],
        [
            {"file_path": "a.py", "start_line": 1, "end_line": 10},
            {"file_path": "missing.py", "start_line": 20, "end_line": 24},
        ],
    )

    assert metrics["file_coverage_at_10"] == 0.5
    assert metrics["file_precision_at_10"] == 0.5
    assert metrics["line_coverage_at_10"] == 6 / 15
    assert metrics["line_precision_at_10"] == 6 / 16
    assert metrics["gold_block_recall_at_10"] == 0.5


def test_retrieval_summary_keeps_missing_rows_in_coverage_denominator():
    selected_repos = [f"external-owner/repo-{index}" for index in range(18)]
    records = [_record(index, selected_repos[index % 18]) for index in range(41)]
    records.extend(
        _record(41 + index, repo)
        for index, repo in enumerate(SYNTHESIS_DEVELOPMENT_REPOSITORIES[:9])
    )
    protocol = build_protocol(
        records,
        dataset_file_sha256=CONTEXTBENCH_TEST_SHA256,
    )
    ready_row = protocol["rows"][0]
    ready_metrics = score_context_candidates(
        [{"file": "src/module.py", "start": 3, "end": 9}],
        ready_row["gt_code_blocks"],
    )

    summary = summarize_preload_retrieval(
        protocol,
        [
            {
                "instance_id": ready_row["instance_id"],
                "status": "ready",
                "metrics": ready_metrics,
            }
        ],
        bootstrap_samples=20,
        seed=7,
    )

    assert summary["completion"]["ready_rows"] == 1
    assert summary["completion"]["failed_or_missing_rows"] == 40
    assert summary["estimands"]["macro_task_level"]["file_coverage_at_10"][
        "estimate"
    ] == pytest.approx(1 / 41)
    assert (
        summary["estimands"]["micro_context_weighted"]["file_precision_at_10"][
            "estimate"
        ]
        == 1.0
    )
    assert not summary["repo_resident_gold_sensitivity"]["available"]


def test_retrieval_summary_reports_repo_resident_gold_sensitivity():
    selected_repos = [f"external-owner/repo-{index}" for index in range(18)]
    source = [_record(index, selected_repos[index % 18]) for index in range(41)]
    source[0]["gold_context"] = json.dumps(
        [
            {"file": "src/module.py", "start_line": 3, "end_line": 9},
            {"file": "scratch_test.py", "start_line": 1, "end_line": 5},
        ]
    )
    source.extend(
        _record(41 + index, repo)
        for index, repo in enumerate(SYNTHESIS_DEVELOPMENT_REPOSITORIES[:9])
    )
    protocol = build_protocol(source, dataset_file_sha256=CONTEXTBENCH_TEST_SHA256)
    records = []
    for row in protocol["rows"]:
        candidates = [{"file": "src/module.py", "start": 3, "end": 9}]
        records.append(
            {
                "instance_id": row["instance_id"],
                "status": "ready",
                "candidates": candidates,
                "missing_gold_files": (
                    ["scratch_test.py"] if row is protocol["rows"][0] else []
                ),
                "metrics": score_context_candidates(candidates, row["gt_code_blocks"]),
            }
        )

    summary = summarize_preload_retrieval(
        protocol, records, bootstrap_samples=20, seed=7
    )

    sensitivity = summary["repo_resident_gold_sensitivity"]
    assert sensitivity["available"]
    assert sensitivity["excluded_file_ledger"] == [
        {
            "instance_id": protocol["rows"][0]["instance_id"],
            "files": ["scratch_test.py"],
            "blocks": 1,
        }
    ]
    assert (
        sensitivity["estimands"]["macro_task_level"]["file_coverage_at_10"]["estimate"]
        == 1.0
    )


def test_agent_summary_uses_paired_ratio_and_frozen_quality_denominator():
    selected_repos = [f"external-owner/repo-{index}" for index in range(18)]
    source = [_record(index, selected_repos[index % 18]) for index in range(41)]
    source.extend(
        _record(41 + index, repo)
        for index, repo in enumerate(SYNTHESIS_DEVELOPMENT_REPOSITORIES[:9])
    )
    protocol = build_protocol(source, dataset_file_sha256=CONTEXTBENCH_TEST_SHA256)
    cells = []
    arm_values = {
        "grep_only": (100, 0.5),
        "preinj_eager": (50, 0.5),
        "preinj_eager_compact": (40, 0.6),
    }
    for row in protocol["rows"]:
        for arm, (tokens, recall) in arm_values.items():
            cells.append(
                {
                    "cell_id": f"{row['query_id']}__{arm}__rep1",
                    "query_id": row["query_id"],
                    "subset_id": arm,
                    "success": True,
                    "total_tokens": tokens,
                    "metrics": {"answer_blocks": {"5": {"recall": recall}}},
                }
            )

    summary = summarize_agent_context_policies(
        protocol, cells, bootstrap_samples=20, seed=7
    )

    assert summary["completion"]["complete_matrix"] is True
    eager = summary["arms"]["preinj_eager"]
    compact = summary["arms"]["preinj_eager_compact"]
    assert eager["token_ratio_of_sums_vs_grep"]["estimate"] == 0.5
    assert eager["recall_at_5_delta_vs_grep"]["estimate"] == 0.0
    assert eager["confirmatory_admission"] is True
    assert compact["token_ratio_of_sums_vs_grep"]["estimate"] == 0.4
    assert compact["absolute_recall_at_5"]["estimate"] == pytest.approx(0.6)

    failed = next(cell for cell in cells if cell["subset_id"] == "preinj_eager")
    failed["success"] = False
    failed.pop("total_tokens")
    incomplete = summarize_agent_context_policies(
        protocol, cells, bootstrap_samples=20, seed=7
    )
    assert incomplete["completion"]["successful_cells"] == 122
    assert incomplete["arms"]["preinj_eager"]["paired_token_rows"] == 40
    assert incomplete["arms"]["preinj_eager"]["confirmatory_admission"] is False
