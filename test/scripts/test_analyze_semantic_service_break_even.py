# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from scripts.profiling.analyze_semantic_service_break_even import analyze_break_even


def test_break_even_matches_snapshots_and_excludes_request_latency():
    build = {
        "instance_id": "org__repo-1",
        "repo": "org/repo",
        "base_commit": "a" * 40,
        "language": "go",
        "success": True,
        "wall_seconds": 10.0,
    }
    replay = {
        "subject": {"repository": "org/repo", "git_commit": "a" * 40},
        "setup": {
            "graph_load_ms": 100.0,
            "live_start_ms": 900.0,
            "warmup_wall_ms": 4200.0,
        },
    }

    result = analyze_break_even([build], [replay])

    row = result["rows"][0]
    assert row["live_session_setup_seconds"] == pytest.approx(5.1)
    assert row["avoided_seconds_per_session"] == pytest.approx(5.0)
    assert row["break_even_sessions"] == 2
    assert result["summary"]["break_even_sessions_median"] == 2
    assert result["method"]["request_latency_included"] is False
