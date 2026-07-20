# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from scripts.profiling.aggregate_incremental_maintenance import (
    aggregate_incremental_maintenance,
    render_markdown,
)


def _graph_row(step, *, source=True, symbol_guard=True, file_guard=True):
    fidelity = lambda guard: {  # noqa: E731
        "status": "ok",
        "equivalence_guardrail_pass": guard,
        "behavior_exact": True,
        "behavior": {"exact": True, "equivalent_fraction": 1.0},
        "raw_graph": {
            "whole_exact": guard,
            "changed_exact": guard,
            "metrics": {
                "edges": {"f1": 1.0 if guard else 0.98},
                "reference_edges": {"f1": 1.0 if guard else 0.97},
                "changed_edges": {"f1": 1.0},
            },
        },
    }
    return {
        "instance_id": "repo-1",
        "language": "python",
        "step": step,
        "delta_files": 1 if source else 0,
        "protocol_version": 6,
        "transition_manifest_id": "manifest",
        "fully-rebuild": {"status": "ok", "t_s": 12.0},
        "symbol-level-patch": {
            "status": "ok",
            "t_s": 2.0,
            "amortized_t_s": 3.0,
            "fault_inclusive_amortized_t_s": 5.0 if step == 1 else 3.0,
            "strategy_attempts": 2 if step == 1 else 1,
            "recovery_s": 2.0 if step == 1 else 0.0,
            "attempt_failures": [{"status": "timeout"}] if step == 1 else [],
            "server_start_s": 1.0,
            "workspace_warmup_s": 4.0,
            "phase_cold_start_s": 5.0,
            "fidelity": fidelity(symbol_guard),
        },
        "file-level-patch": {
            "status": "ok",
            "t_s": 4.0,
            "amortized_t_s": 6.0,
            "server_start_s": 1.5,
            "workspace_warmup_s": 3.5,
            "phase_cold_start_s": 5.0,
            "strategy_attempts": 1,
            "recovery_s": 0.0,
            "attempt_failures": [],
            "fidelity": fidelity(file_guard),
        },
    }


def _vector_row(step, *, source=True, guard=True):
    return {
        "instance_id": "repo-1",
        "language": "python",
        "step": step,
        "delta_files_test_excluded": 1 if source else 0,
        "protocol_version": 3,
        "experiment_config_id": "vector-config",
        "transition_manifest_id": "manifest",
        "fully-rebuild": {"status": "ok", "t_s": 20.0},
        "incremental": {
            "status": "ok",
            "t_s": 2.0,
            "metadata": {
                "cache_hit_rate": 0.9,
                "chunks_reembedded": 2,
            },
        },
        "fidelity": {
            "equivalence_guardrail_pass": guard,
            "levels": {
                "l2": {
                    "document_identities_exact": True,
                    "vectors_close": True,
                    "replay": {
                        "ordered_exact_fraction": 1.0 if guard else 0.0,
                        "set_exact_fraction": 1.0,
                    },
                }
            },
        },
    }


def _stability_row():
    return {
        "instance_id": "repo-1",
        "language": "python",
        "step": 1,
        "repeat": 1,
        "status": "ok",
        "transition_manifest_id": "manifest",
        "build": {"t_s": 15.0},
        "comparisons": [
            {
                "reference": "original",
                "whole_exact": False,
                "changed_exact": True,
                "behavior_exact": False,
                "behavior": {"equivalent_fraction": 0.99},
                "metrics": {
                    "edges": {"f1": 0.995},
                    "reference_edges": {"f1": 0.994},
                },
            }
        ],
    }


def test_aggregate_uses_source_population_and_recomputes_guarded_ratios():
    graph_rows = [
        _graph_row(1),
        _graph_row(2, symbol_guard=False),
        _graph_row(3, source=False, symbol_guard=False, file_guard=False),
    ]
    vector_rows = [_vector_row(1), _vector_row(2, source=False)]

    summary = aggregate_incremental_maintenance(
        graph_rows=graph_rows,
        graph_stability_rows=[_stability_row()],
        vector_rows=vector_rows,
        expected={("repo-1", 1), ("repo-1", 2), ("repo-1", 3)},
        expected_manifest_id="manifest",
    )

    graph = summary["graph"]["overall"]
    assert graph["source_changing_rows"] == 2
    assert graph["symbol"]["admitted"] == 1
    assert graph["symbol"]["admission_rate"] == 0.5
    assert graph["symbol"]["observed_amortized_speedup_vs_full"]["p50"] == 4.0
    assert graph["symbol"]["amortized_speedup_vs_full"]["p50"] == 4.0
    assert graph["file"]["admitted"] == 2
    assert graph["paired"]["symbol_speedup_vs_file"]["p50"] == 2.0
    assert graph["symbol"]["fault_inclusive_update_s"]["p50"] == 5.0
    assert graph["fidelity"]["symbol-level-patch"]["behavior_exact"] == 2
    assert graph["fidelity"]["symbol-level-patch"]["raw_whole_exact"] == 1
    assert graph["fidelity"]["symbol-level-patch"]["edge_f1"]["p50"] == 0.99
    assert graph["reliability"]["symbol-level-patch"]["recovered_rows"] == 1
    assert graph["reliability"]["symbol-level-patch"]["failed_attempts"] == 1
    assert summary["graph"]["backend_setup"]["symbol-level-patch"]["instances"] == 1
    assert summary["graph"]["provider_stability"]["edge_f1"]["p50"] == 0.995
    assert summary["graph"]["completeness"]["complete"] is True

    vector = summary["vector"]["overall"]
    assert vector["source_changing_rows"] == 1
    assert vector["fidelity"]["artifact_exact"] == 1
    assert vector["fidelity"]["ordered_replay_exact"] == 1
    assert vector["fidelity"]["set_replay_exact"] == 1
    assert vector["incremental"]["admitted"] == 1
    assert vector["incremental"]["speedup_vs_full"]["p50"] == 10.0
    assert vector["incremental"]["cache_hit_rate"]["p50"] == 0.9
    markdown = render_markdown(summary)
    assert "Graph maintenance" in markdown
    assert "99.50%" in markdown


def test_aggregate_rejects_cross_manifest_pooling():
    row = _graph_row(1)
    row["transition_manifest_id"] = "other"

    try:
        aggregate_incremental_maintenance(
            graph_rows=[row], expected_manifest_id="manifest"
        )
    except ValueError as exc:
        assert "do not match" in str(exc)
    else:
        raise AssertionError("expected manifest mismatch to fail")


def test_aggregate_rejects_duplicate_transitions():
    row = _graph_row(1)

    try:
        aggregate_incremental_maintenance(
            graph_rows=[row, dict(row)],
            expected={("repo-1", 1)},
            expected_manifest_id="manifest",
        )
    except ValueError as exc:
        assert "duplicate transitions" in str(exc)
    else:
        raise AssertionError("expected duplicate transition to fail")


def test_aggregate_rejects_mixed_protocols_and_vector_configs():
    first_graph = _graph_row(1)
    second_graph = _graph_row(2)
    second_graph["protocol_version"] += 1

    try:
        aggregate_incremental_maintenance(
            graph_rows=[first_graph, second_graph],
            expected_manifest_id="manifest",
        )
    except ValueError as exc:
        assert "mix protocol versions" in str(exc)
    else:
        raise AssertionError("expected mixed graph protocols to fail")

    first_vector = _vector_row(1)
    second_vector = _vector_row(2)
    second_vector["experiment_config_id"] = "other-config"
    try:
        aggregate_incremental_maintenance(
            vector_rows=[first_vector, second_vector],
            expected_manifest_id="manifest",
        )
    except ValueError as exc:
        assert "one experiment_config_id" in str(exc)
    else:
        raise AssertionError("expected mixed vector configurations to fail")
