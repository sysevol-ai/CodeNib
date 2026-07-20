# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import argparse

import pytest

from scripts.profiling import profile_graph_rebuild_stability as stability
from scripts.profiling.profile_graph_rebuild_stability import (
    compare_rebuilds,
    exactness,
    parse_case,
)


def test_parse_case_preserves_instance_id():
    assert parse_case("owner__repo-123:5") == ("owner__repo-123", 5)


@pytest.mark.parametrize("value", ["repo", "repo:0", ":1", "repo:not-a-step"])
def test_parse_case_rejects_invalid_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_case(value)


def test_exactness_keeps_whole_and_changed_scope_separate():
    exact = {"precision": 1.0, "recall": 1.0}
    mismatch = {"precision": 0.9, "recall": 1.0}
    metrics = {
        "vertices": exact,
        "edges": mismatch,
        "changed_vertices": exact,
        "changed_edges": exact,
    }

    assert exactness(metrics) == {"whole_exact": False, "changed_exact": True}


def test_compare_rebuilds_passes_behavior_contract_as_keywords(monkeypatch, tmp_path):
    exact = {"precision": 1.0, "recall": 1.0}
    metrics = {
        "vertices": exact,
        "edges": exact,
        "changed_vertices": exact,
        "changed_edges": exact,
    }
    candidate = object()
    reference = object()
    changed_files = {"src/lib.rs"}

    monkeypatch.setattr(stability, "compare_graphs", lambda *args: metrics)

    def fake_behavior(predicted, fresh, **kwargs):
        assert predicted is candidate
        assert fresh is reference
        assert kwargs == {
            "project_root": tmp_path,
            "changed_files": changed_files,
            "max_per_capability": 17,
        }
        return {"exact": True, "request_count": 2}

    monkeypatch.setattr(stability, "compare_graph_behavior", fake_behavior)

    result = compare_rebuilds(
        candidate,
        reference,
        project_root=tmp_path,
        changed_files=changed_files,
        max_behavior_requests=17,
    )

    assert result["whole_exact"] is True
    assert result["changed_exact"] is True
    assert result["behavior_exact"] is True
    assert result["behavior"]["request_count"] == 2
