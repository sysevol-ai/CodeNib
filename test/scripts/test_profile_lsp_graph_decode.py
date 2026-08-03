# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the synthetic LSP graph decode profiler."""

from __future__ import annotations

from scripts.profiling.profile_lsp_graph_decode import build_payload, profile_decode


def test_profile_lsp_graph_decode_builds_expected_shape(tmp_path):
    payload = build_payload(
        tmp_path,
        files=2,
        methods_per_file=2,
        include_references=True,
    )

    report = profile_decode(payload, tmp_path)

    assert report["files"] == 2
    assert report["vertices"] > 0
    assert report["edges"] > 0
    assert report["decode_seconds"] >= 0
