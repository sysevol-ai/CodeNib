# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import copy

import pytest

from codenib.types import NodeInfo
from scripts.analyze_jev_dense import align_cases, require_matched_cases
from scripts.benchmark_jev_base import metrics


def example():
    case = {
        "instance_id": "one",
        "query": "Find the bug",
        "repo": "owner/repo",
        "language_group": "python",
        "base_commit": "base",
        "git_tree": "tree",
        "selected_blobs_sha256": "blobs",
        "corpus_chunks": 10,
        "target_files": ["a.py"],
        "target_blocks": [
            {
                "file": "a.py",
                "symbol": "f",
                "change_type": "modified",
                "start": 30,
                "end": 31,
            }
        ],
    }
    audit = [
        {
            "instance_id": "one",
            "file": "a.py",
            "symbol": "f",
            "change_type": "modified",
            "published": [30, 31],
            "base": [1, 2],
        }
    ]
    return case, audit


def test_all_routes_use_corrected_coordinates_without_mutating_source_labels():
    case, audit = example()
    candidates = [NodeInfo(node_id="a.py:f", file="a.py", start_line=0, end_line=1)]
    aligned = align_cases([case], audit)

    assert metrics(case, candidates)["span_recall@5"] == 0
    assert metrics(aligned[0], candidates)["span_recall@5"] == 1
    assert case["target_blocks"][0]["start"] == 30
    with pytest.raises(ValueError, match="coordinates differ"):
        align_cases(aligned, audit)


@pytest.mark.parametrize(
    "field", ["query", "base_commit", "selected_blobs_sha256", "target_blocks"]
)
def test_route_comparison_rejects_different_queries_sources_or_targets(field):
    case, _ = example()
    other = copy.deepcopy(case)
    other[field] = "different"

    with pytest.raises(ValueError, match=f"Routes differ in {field}"):
        require_matched_cases([case], [other])


def test_audit_cannot_silently_cover_only_a_subset_or_duplicate_a_target():
    case, audit = example()
    with pytest.raises(ValueError, match="targets differ"):
        align_cases([], audit)
    with pytest.raises(ValueError, match="Duplicate audit"):
        align_cases([case], audit + audit)


def test_matching_duplicate_cases_cannot_inflate_the_sample():
    case, _ = example()
    with pytest.raises(ValueError, match="Duplicate instances"):
        require_matched_cases([case, case], [case, case])
