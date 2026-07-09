# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from scripts.profiling.aggregate_lsp_replay import aggregate_lsp_replay_reports


def _row(request_id, capability, same_result, verdict):
    return {
        "request": {"request_id": request_id, "capability": capability},
        "same_result": same_result,
        "verdict": verdict,
        "static_call": {"duration_ms": 1.0, "status": "ok"},
        "reference_call": {"duration_ms": 10.0, "status": "ok"},
        "speedup_ratio": 10.0,
        "latency_saved_ms": 9.0,
    }


def test_aggregate_counts_repetitions_once_per_request():
    report = {
        "warmup_summary": {
            "row_count": 2,
            "equivalent_row_count": 1,
            "mismatch_count": 0,
            "error_count": 1,
        },
        "setup": {
            "graph_load_ms": 5.0,
            "static_provider_init_ms": 0.1,
            "live_start_ms": 100.0,
        },
        "comparisons": [
            _row("d1", "definition", True, "equivalent_static_faster"),
            _row("d1", "definition", True, "equivalent_static_faster"),
            _row("r1", "references", False, "mismatch"),
            _row("r1", "references", False, "mismatch"),
        ],
    }

    summary = aggregate_lsp_replay_reports([report])

    assert summary["request_summary"]["overall"] == {
        "requests": 2,
        "equivalent_requests": 1,
        "mismatch_requests": 1,
        "error_or_fallback_requests": 0,
        "equivalence_rate": 0.5,
    }
    assert summary["request_summary"]["definition"]["requests"] == 1
    assert summary["measurements"]["overall"]["equivalent_row_count"] == 2
    assert summary["warmup_summary"] == {
        "row_count": 2,
        "equivalent_row_count": 1,
        "mismatch_count": 0,
        "error_count": 1,
        "reports_with_errors": 1,
    }
