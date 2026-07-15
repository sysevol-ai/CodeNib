# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codeminer.eval.reports.snapshot_amortization import (
    analyze_snapshot_amortization,
    view_construction_seconds,
)


def _profile(repo="owner/repo", commit="abc", seconds=12.0):
    return {
        "repo": repo,
        "base_commit": commit,
        "language_group": "Python",
        "embedding_model": "model",
        "embedding_provider": "huggingface",
        "embedding_dimension": 128,
        "profile_tag": "test",
        "total_duration": 999.0,
        "sections": [
            {"label": "build_vector_store", "total": 500.0},
            {"label": "embedding_encode_l2", "total": seconds - 2.0},
            {"label": "faiss_index_add_l2", "total": 2.0},
        ],
    }


def test_view_construction_seconds_ignores_nested_totals():
    assert view_construction_seconds(_profile(), "l2") == 12.0


def test_view_construction_seconds_prefers_combined_level_timer():
    profile = _profile()
    profile["sections"].append({"label": "vector_store_add_documents_l2", "total": 7.5})

    assert view_construction_seconds(profile, "l2") == 7.5


def test_analyze_snapshot_amortization_uses_exact_snapshot_counts():
    rows = [
        {"repo": "Owner/Repo", "base_commit": "ABC"},
        {"repo": "owner/repo", "base_commit": "abc"},
        {"repo": "other/repo", "base_commit": "def"},
    ]
    profiles = [
        _profile(seconds=12.0),
        _profile(repo="other/repo", commit="def", seconds=6.0),
    ]

    report = analyze_snapshot_amortization(rows, profiles)

    assert report["summary"]["consumer_records"] == 3
    assert report["summary"]["unique_snapshots"] == 2
    assert report["summary"]["avoided_rebuilds"] == 1
    assert report["summary"]["build_once_seconds"] == 18.0
    assert report["summary"]["rebuild_per_consumer_seconds"] == 30.0
    assert report["summary"]["amortized_build_seconds_per_consumer"] == 6.0


def test_analyze_snapshot_amortization_rejects_missing_profile():
    rows = [{"repo": "missing/repo", "base_commit": "abc"}]

    with pytest.raises(ValueError, match="missing build profiles"):
        analyze_snapshot_amortization(rows, [_profile()])


def test_analyze_snapshot_amortization_rejects_mixed_view_identity():
    rows = [
        {"repo": "owner/repo", "base_commit": "abc"},
        {"repo": "other/repo", "base_commit": "def"},
    ]
    other = _profile(repo="other/repo", commit="def")
    other["embedding_model"] = "other-model"

    with pytest.raises(ValueError, match="one view identity"):
        analyze_snapshot_amortization(rows, [_profile(), other])
