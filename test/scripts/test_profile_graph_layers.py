# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for graph-layer classifier profiling."""

from __future__ import annotations

import pytest

from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_IMPORT,
    EDGE_TYPE_REFERENCE,
    EDGE_TYPE_TYPE_USE,
)
from scripts.profiling import profile_graph_layers


def test_build_edge_types_is_deterministic():
    assert profile_graph_layers.build_edge_types(7) == [
        EDGE_TYPE_CONTAIN,
        EDGE_TYPE_REFERENCE,
        EDGE_TYPE_IMPORT,
        EDGE_TYPE_TYPE_USE,
        "custom",
        EDGE_TYPE_CONTAIN,
        EDGE_TYPE_REFERENCE,
    ]


def test_build_edge_types_rejects_negative_count():
    with pytest.raises(ValueError, match="edge_count"):
        profile_graph_layers.build_edge_types(-1)


def test_profile_layers_reports_parity_and_layer_counts():
    result = profile_graph_layers.profile_layers(edge_count=10, reps=1)

    assert result["edges"] == 10
    assert result["reps"] == 1
    assert result["parity"] is True
    assert result["python_seconds_min"] >= 0
    assert result["layers"]["all"] == 10
    assert result["layers"]["containment"] == 2
    assert result["layers"]["dependency"] == 6


def test_profile_layers_rejects_non_positive_reps():
    with pytest.raises(ValueError, match="reps"):
        profile_graph_layers.profile_layers(edge_count=10, reps=0)
