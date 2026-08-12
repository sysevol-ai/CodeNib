# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.profiling import profile_scip_mcp_consumer as profiler


def test_abba_schedule_is_balanced_and_reverses_blocks() -> None:
    schedule = profiler._abba_schedule(4)

    assert [entry["arm"] for entry in schedule] == [
        "legacy",
        "candidate",
        "candidate",
        "legacy",
        "candidate",
        "legacy",
        "legacy",
        "candidate",
    ]
    assert sum(entry["arm"] == "legacy" for entry in schedule) == 4
    assert sum(entry["arm"] == "candidate" for entry in schedule) == 4


@pytest.mark.parametrize("samples", [-1, 1, 3])
def test_abba_schedule_rejects_unbalanced_sample_counts(samples: int) -> None:
    with pytest.raises(ValueError, match="even"):
        profiler._abba_schedule(samples)


def test_summary_uses_nearest_rank_p95() -> None:
    summary = profiler._summarize(list(range(1, 21)))

    assert summary == {
        "samples": 20,
        "min": 1.0,
        "p50": 10.5,
        "p95": 19.0,
        "max": 20.0,
    }


@pytest.mark.parametrize("values", [[], [math.nan], [math.inf], [-1], [True]])
def test_summary_rejects_empty_or_malformed_samples(values: list[Any]) -> None:
    with pytest.raises(ValueError):
        profiler._summarize(values)


def test_promotion_exact_p50_and_p95_threshold_passes() -> None:
    decision = profiler._promotion_decision(
        {"p50": 10.0, "p95": 20.0},
        {"p50": 8.0, "p95": 16.0},
        threshold=0.20,
        parity=True,
        graph_free=True,
    )

    assert decision["p50"]["passed"] is True
    assert decision["p95"]["passed"] is True
    assert decision["passed"] is True


def test_promotion_requires_both_percentiles() -> None:
    decision = profiler._promotion_decision(
        {"p50": 10.0, "p95": 20.0},
        {"p50": 8.0, "p95": math.nextafter(16.0, math.inf)},
        threshold=0.20,
        parity=True,
        graph_free=True,
    )

    assert decision["p50"]["passed"] is True
    assert decision["p95"]["passed"] is False
    assert decision["passed"] is False


def test_full_public_metadata_changes_parity_digest() -> None:
    legacy = [
        {
            "node": "f.py:1",
            "lsp_provider": {
                "backend": "persisted-symbol-graph-v1",
                "fallback_reason": "local_source_not_verified",
            },
        }
    ]
    candidate = json.loads(json.dumps(legacy))
    candidate[0]["lsp_provider"]["backend"] = "native-scip-fact-query-v1"

    assert profiler._json_digest(legacy) != profiler._json_digest(candidate)


def test_balanced_sample_validator_rejects_missing_samples() -> None:
    with pytest.raises(RuntimeError, match="imbalance"):
        profiler._require_balanced_samples(
            {"legacy": [{}, {}], "candidate": [{}]}, expected=2
        )


def test_only_the_fixed_protocol_is_promotion_eligible() -> None:
    canonical = {
        "iterations": profiler.DEFAULT_ITERATIONS,
        "warmups": profiler.DEFAULT_WARMUPS,
        "requested_query_workload_size": profiler.DEFAULT_QUERY_WORKLOAD_SIZE,
        "observed_query_workload_size": profiler.DEFAULT_QUERY_WORKLOAD_SIZE,
        "promotion_threshold": profiler.DEFAULT_PROMOTION_THRESHOLD,
        "concurrency_workers": profiler.DEFAULT_CONCURRENCY_WORKERS,
    }

    assert profiler._canonical_protocol(**canonical)["passed"] is True
    for field, value in {
        "iterations": 2,
        "warmups": 0,
        "requested_query_workload_size": 6,
        "observed_query_workload_size": 99,
        "promotion_threshold": 0.0,
        "concurrency_workers": 2,
    }.items():
        changed = dict(canonical)
        changed[field] = value
        assert profiler._canonical_protocol(**changed)["passed"] is False


def test_validate_worker_rejects_nan_and_snapshot_drift() -> None:
    snapshot = {"snapshot": "fixed"}
    run = _fake_run(
        arm="legacy",
        workload="symbol_only",
        process_id=1,
        snapshot=snapshot,
    )
    run["wall_seconds"] = math.nan
    with pytest.raises(RuntimeError, match="wall_seconds"):
        profiler._validate_worker_run(
            run,
            arm="legacy",
            workload="symbol_only",
            expected_snapshot=snapshot,
        )

    run["wall_seconds"] = 10.0
    run["receipt_after"] = {"snapshot": "changed"}
    with pytest.raises(RuntimeError, match="changed during"):
        profiler._validate_worker_run(
            run,
            arm="legacy",
            workload="symbol_only",
            expected_snapshot=snapshot,
        )


def test_file_receipt_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"artifact")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        profiler._file_receipt(link)


def test_subject_manifest_fixes_python_and_rust_revisions() -> None:
    manifest = Path(profiler.__file__).with_name("scip_mcp_consumer_subjects.json")
    payload, python = profiler._load_subject_manifest(
        manifest, subject_id="python-codenib"
    )
    _, rust = profiler._load_subject_manifest(manifest, subject_id="rust-ruff")

    assert payload["schema_version"] == 1
    assert python == {
        "id": "python-codenib",
        "language": "python",
        "repository": "https://github.com/sysevol-ai/CodeNib.git",
        "revision": "6cf61b08310e165574c52fa217c66f0b6ae2a36d",
    }
    assert rust["revision"] == "75a24bbc67aa31b825b6326cfb6e6afdf3ca90d5"


class _VariantGraph:
    def __init__(self, *, ambiguous: bool = True) -> None:
        self.name_to_vertex = {
            "canonical-alpha": 0,
            "canonical-beta": 1,
            "canonical-shared-left": 2,
            "canonical-shared-right": 3,
        }
        self._info = {
            "canonical-alpha": {
                "type": "function",
                "file": "alpha.py",
                "start_line": 0,
                "end_line": 1,
                "selection_line": 0,
                "unified_name": "module:UniqueAlpha()",
                "has_definition": True,
            },
            "canonical-beta": {
                "type": "function",
                "file": "beta.py",
                "start_line": 2,
                "end_line": 3,
                "selection_line": 2,
                "unified_name": "module:UniqueBeta()",
                "has_definition": True,
            },
            "canonical-shared-left": {
                "type": "function",
                "file": "left.py",
                "start_line": 4,
                "end_line": 5,
                "selection_line": 4,
                "unified_name": "left:Shared()",
                "has_definition": True,
            },
            "canonical-shared-right": {
                "type": "function",
                "file": "right.py",
                "start_line": 6,
                "end_line": 7,
                "selection_line": 6,
                "unified_name": "right:Shared()",
                "has_definition": True,
            },
        }
        self.graph = SimpleNamespace(vcount=lambda: 4, ecount=lambda: 3)
        self._ambiguous = ambiguous

    def get_node_info_by_name(self, name: str) -> dict[str, Any]:
        return dict(self._info[name])

    def resolve_symbol_candidates(self, raw: str, limit: int = 8) -> list[str]:
        seed = raw.strip().strip("`'\"")
        if seed in self.name_to_vertex:
            return [seed]
        aliases = {
            "module:UniqueAlpha()": ["canonical-alpha"],
            "UniqueAlpha": ["canonical-alpha"],
            "module:UniqueBeta()": ["canonical-beta"],
            "UniqueBeta": ["canonical-beta"],
            "left:Shared()": ["canonical-shared-left"],
            "right:Shared()": ["canonical-shared-right"],
            "Shared": (
                ["canonical-shared-left", "canonical-shared-right"]
                if self._ambiguous
                else ["canonical-shared-left"]
            ),
        }
        return aliases.get(seed, [])[:limit]


def test_workload_inputs_cover_all_symbol_variants_and_route_query() -> None:
    inputs = profiler._workload_inputs(_VariantGraph(), query_workload_size=6)

    assert inputs["symbol_query_category_counts"] == {
        category: 1 for category in profiler._SYMBOL_QUERY_CATEGORIES
    }
    assert {query["category"] for query in inputs["symbol_queries"]} == set(
        profiler._SYMBOL_QUERY_CATEGORIES
    )
    assert len({query["symbol"] for query in inputs["symbol_queries"]}) == 6
    assert inputs["route_queries"]


def test_workload_inputs_fail_without_ambiguous_seed() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        profiler._workload_inputs(_VariantGraph(ambiguous=False), query_workload_size=6)


def test_workload_inputs_fail_without_route_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.agent.lsp_graph as lsp_graph

    monkeypatch.setattr(lsp_graph, "_terms", lambda _value: set())
    with pytest.raises(ValueError, match="query-only route"):
        profiler._workload_inputs(_VariantGraph(), query_workload_size=6)


def test_symbol_workload_calls_both_reference_declaration_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.mcp.tools.lsp as lsp_tools

    reference_modes: list[bool] = []
    monkeypatch.setattr(lsp_tools, "lsp_definition_impl", lambda *_a, **_k: [])

    def references(*_args: Any, **kwargs: Any) -> list[Any]:
        reference_modes.append(kwargs["include_declaration"])
        return []

    monkeypatch.setattr(lsp_tools, "lsp_references_impl", references)
    outcome = profiler._run_mcp_workload(
        object(),
        workload="symbol_only",
        symbol_queries=[{"category": "canonical", "symbol": "exact"}],
        positions=[],
        route_symbols=[],
        route_queries=[],
        capture_payload=True,
    )

    assert reference_modes == [False, True]
    assert outcome["calls"] == 3
    assert [row["query_category"] for row in outcome["public_payload"]] == [
        "canonical",
        "canonical",
        "canonical",
    ]


def test_correctness_gate_requires_exact_native_and_fallback_counts() -> None:
    run = _fake_run(
        arm="candidate",
        workload="mixed",
        process_id=1,
        snapshot={"snapshot": "fixed"},
    )
    expected = {
        "native_definition_count": 6,
        "native_references_count": 12,
        "native_symbol_call_count": 18,
        "fallback_call_count": 4,
        "receipt_observation_count": 9,
    }

    assert profiler._correctness_candidate_ready(run, expected=expected) is True
    run["diagnostics"]["native_references_count"] = 11
    assert profiler._correctness_candidate_ready(run, expected=expected) is False


def _candidate_diagnostics(
    *, workload: str, workers: int, symbol_count: int = 6
) -> dict[str, Any]:
    symbol_only = workload == "symbol_only"
    concurrent = workload == "concurrent_first_fallback"
    fallback_calls = {
        "symbol_only": 0,
        "position_first": 2,
        "route_first": 2,
        "mixed": 4,
        "concurrent_first_fallback": workers,
    }[workload]
    load_count = 0 if symbol_only else 1
    symbol_count = 0 if concurrent else symbol_count
    return {
        "state": "NATIVE_READY" if symbol_only else "GRAPH_READY",
        "logical_backend": "persisted-symbol-graph-v1",
        "native_physical_backend": "native-scip-fact-query-v1",
        "fallback_physical_backend": "lazy-persisted-symbol-graph-v1",
        "native_definition_count": symbol_count,
        "native_references_count": symbol_count * 2,
        "native_symbol_call_count": symbol_count * 3,
        "receipt_observation_count": (
            0 if symbol_only else (2 if concurrent else 2 * fallback_calls + 1)
        ),
        "graph_load_attempt_count": load_count,
        "graph_load_count": load_count,
        "graph_publication_count": load_count,
        "fallback_call_count": fallback_calls,
        "graph_fallback_count": fallback_calls,
        "graph_call_count": fallback_calls,
        "graph_materialization_seconds": None if symbol_only else 0.01,
        "terminal_failure": None,
    }


def _fake_run(
    *,
    arm: str,
    workload: str,
    process_id: int,
    snapshot: dict[str, Any],
    workers: int = 16,
    symbol_count: int = 6,
    symbol_query_category_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    wall = 10.0 if arm == "legacy" else 8.0
    fallback_calls = {
        "symbol_only": 0,
        "position_first": 2,
        "route_first": 2,
        "mixed": 4,
        "concurrent_first_fallback": workers,
    }[workload]
    calls = (
        workers
        if workload == "concurrent_first_fallback"
        else symbol_count * 3 + fallback_calls
    )
    result = {
        "arm": arm,
        "workload": workload,
        "process_id": process_id,
        "wall_seconds": wall,
        "process_wall_seconds": wall + 0.1,
        "cpu_seconds": wall / 2,
        "startup_wall_seconds": wall / 2,
        "workload_wall_seconds": wall / 2,
        "rss_start_bytes": 100,
        "peak_rss_start_bytes": 100,
        "peak_rss_bytes": 200,
        "peak_rss_growth_bytes": 100,
        "calls": calls,
        "results": calls,
        "public_errors": 0,
        "serialized_bytes": 1000,
        "public_payload_sha256": profiler._json_digest({"workload": workload}),
        "provider_selection": {"backend": "persisted-symbol-graph-v1"},
        "diagnostics": (
            _candidate_diagnostics(
                workload=workload,
                workers=workers,
                symbol_count=symbol_count,
            )
            if arm == "candidate"
            else None
        ),
        "receipt_before": dict(snapshot),
        "receipt_after": dict(snapshot),
        "igraph_imported": arm == "legacy",
        "code_graph_imported": arm == "legacy",
    }
    if workload != "concurrent_first_fallback":
        result["symbol_query_category_counts"] = symbol_query_category_counts or {
            category: 1 for category in profiler._SYMBOL_QUERY_CATEGORIES
        }
    if workload == "concurrent_first_fallback":
        result.update(
            {
                "thread_results_identical": True,
                "thread_payload_sha256": ["sha256:" + "a" * 64],
                "public_payload": [],
            }
        )
    return result


def _prepared_subject(tmp_path: Path) -> dict[str, Any]:
    snapshot = {"snapshot_id": "fixed"}
    artifacts = {
        "manifest": {"sha256": "1" * 64},
        "index": {"sha256": "2" * 64},
        "graph": {"sha256": "3" * 64},
    }
    git = {
        "commit": "6cf61b08310e165574c52fa217c66f0b6ae2a36d",
        "dirty": False,
        "remote": "https://github.com/sysevol-ai/CodeNib.git",
    }
    benchmark_sources = {
        "harness": profiler._file_receipt(Path(profiler.__file__)),
        "provider": profiler._file_receipt(
            profiler._PROJECT_ROOT / "codenib/scip_interface/scip_query_provider.py"
        ),
        "snapshot_observer": profiler._file_receipt(
            profiler._PROJECT_ROOT / "codenib/scip_interface/scip_query.py"
        ),
    }
    return {
        "manifest_path": str(tmp_path / "repo_manifest.json"),
        "project_root": str(tmp_path),
        "subject_manifest": {
            "path": str(tmp_path / "subjects.json"),
            "sha256": "4" * 64,
            "payload": {"schema_version": 1, "subjects": []},
            "entry": {
                "id": "python-codenib",
                "language": "python",
                "repository": git["remote"],
                "revision": git["commit"],
            },
        },
        "language": "python",
        "candidate_receipt": {"receipt_sha256": "5" * 64},
        "initial_snapshot": snapshot,
        "artifact_before": artifacts,
        "git_before": git,
        "benchmark_git_before": git,
        "benchmark_sources_before": benchmark_sources,
        "public_fallback_reason": "local_source_not_verified",
        "public_provider_selection": {
            "provider": "static",
            "backend": "persisted-symbol-graph-v1",
            "status": "ok",
            "index_snapshot": "symbol_graph:fixed:4:3",
            "fallback_reason": "local_source_not_verified",
        },
        "inputs": {
            "symbol_queries": [
                {"category": category, "symbol": f"{category}-seed"}
                for category in profiler._SYMBOL_QUERY_CATEGORIES
            ],
            "symbol_query_category_counts": {
                category: 1 for category in profiler._SYMBOL_QUERY_CATEGORIES
            },
            "positions": [{"file_path": "src/a.py", "line": 1}],
            "route_symbols": ["src/a.py:f()"],
            "route_queries": ["route query"],
            "definition_symbol_count": 1,
            "graph_vertex_count": 4,
            "graph_edge_count": 3,
        },
    }


def test_controller_uses_fake_isolated_workers_and_gates_only_symbols(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepared = _prepared_subject(tmp_path)
    symbol_queries = [
        {
            "category": profiler._SYMBOL_QUERY_CATEGORIES[
                index % len(profiler._SYMBOL_QUERY_CATEGORIES)
            ],
            "symbol": f"seed-{index}",
        }
        for index in range(profiler.DEFAULT_QUERY_WORKLOAD_SIZE)
    ]
    category_counts = {
        category: sum(query["category"] == category for query in symbol_queries)
        for category in profiler._SYMBOL_QUERY_CATEGORIES
    }
    prepared["inputs"]["symbol_queries"] = symbol_queries
    prepared["inputs"]["symbol_query_category_counts"] = category_counts
    process_ids = iter(range(1000, 2000))
    observed_configs: list[dict[str, Any]] = []

    monkeypatch.setattr(profiler, "_prepare_subject", lambda **_kwargs: prepared)
    monkeypatch.setattr(
        profiler,
        "_observe_receipt",
        lambda _receipt: dict(prepared["initial_snapshot"]),
    )
    monkeypatch.setattr(
        profiler,
        "_artifact_receipts",
        lambda _receipt: dict(prepared["artifact_before"]),
    )
    monkeypatch.setattr(
        profiler,
        "_git_identity",
        lambda _root: dict(prepared["git_before"]),
    )

    def fake_worker(
        config: dict[str, Any], *, timeout_seconds: float
    ) -> dict[str, Any]:
        assert timeout_seconds == 5.0
        observed_configs.append(dict(config))
        return _fake_run(
            arm=config["arm"],
            workload=config["workload"],
            process_id=next(process_ids),
            snapshot=prepared["initial_snapshot"],
            workers=config["concurrency_workers"],
            symbol_count=len(config["symbol_queries"]),
            symbol_query_category_counts=category_counts,
        )

    monkeypatch.setattr(profiler, "_run_isolated_worker", fake_worker)

    report = profiler.profile_scip_mcp_consumer(
        manifest_path=tmp_path / "repo_manifest.json",
        project_root=tmp_path,
        subject_id="python-codenib",
        worker_timeout_seconds=5.0,
    )

    assert report["passed"] is True
    assert report["symbol_only"]["promotion"]["passed"] is True
    assert report["gates"]["canonical_protocol"] is True
    assert report["symbol_only"]["legacy"]["wall_seconds"]["samples"] == 20
    assert report["symbol_only"]["candidate"]["wall_seconds"]["samples"] == 20
    assert all(
        observation["performance_gated"] is False
        for observation in report["correctness"].values()
    )
    assert report["concurrent_first_fallback"]["passed"] is True
    assert report["configuration"]["process_isolation_observed"] is True
    assert report["configuration"]["symbol_query_category_counts"] == category_counts
    assert len(observed_configs) == 55  # 48 performance + 6 correctness + 1 race
    assert sum(config["workload"] == "symbol_only" for config in observed_configs) == 48
    assert all(config["capture_payload"] is False for config in observed_configs)

    smoke_report = profiler.profile_scip_mcp_consumer(
        manifest_path=tmp_path / "repo_manifest.json",
        project_root=tmp_path,
        subject_id="python-codenib",
        iterations=2,
        warmups=0,
        query_workload_size=6,
        worker_timeout_seconds=5.0,
    )

    assert smoke_report["symbol_only"]["promotion"]["passed"] is True
    assert smoke_report["gates"]["canonical_protocol"] is False
    assert smoke_report["passed"] is False


def test_controller_fails_when_fake_workers_reuse_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepared = _prepared_subject(tmp_path)
    monkeypatch.setattr(profiler, "_prepare_subject", lambda **_kwargs: prepared)
    monkeypatch.setattr(
        profiler,
        "_observe_receipt",
        lambda _receipt: dict(prepared["initial_snapshot"]),
    )
    monkeypatch.setattr(
        profiler,
        "_artifact_receipts",
        lambda _receipt: dict(prepared["artifact_before"]),
    )
    monkeypatch.setattr(
        profiler,
        "_git_identity",
        lambda _root: dict(prepared["git_before"]),
    )
    monkeypatch.setattr(
        profiler,
        "_run_isolated_worker",
        lambda config, **_kwargs: _fake_run(
            arm=config["arm"],
            workload=config["workload"],
            process_id=123,
            snapshot=prepared["initial_snapshot"],
            workers=config["concurrency_workers"],
        ),
    )

    report = profiler.profile_scip_mcp_consumer(
        manifest_path=tmp_path / "repo_manifest.json",
        project_root=tmp_path,
        subject_id="python-codenib",
        iterations=2,
        warmups=0,
    )

    assert report["configuration"]["process_isolation_observed"] is False
    assert report["gates"]["process_isolation"] is False
    assert report["passed"] is False


def test_controller_fails_when_benchmark_checkout_is_dirty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepared = _prepared_subject(tmp_path)
    process_ids = iter(range(2000, 3000))
    monkeypatch.setattr(profiler, "_prepare_subject", lambda **_kwargs: prepared)
    monkeypatch.setattr(
        profiler,
        "_observe_receipt",
        lambda _receipt: dict(prepared["initial_snapshot"]),
    )
    monkeypatch.setattr(
        profiler,
        "_artifact_receipts",
        lambda _receipt: dict(prepared["artifact_before"]),
    )

    def git_identity(root: Path) -> dict[str, Any]:
        observed = dict(prepared["git_before"])
        if root == profiler._PROJECT_ROOT:
            observed["dirty"] = True
        return observed

    monkeypatch.setattr(profiler, "_git_identity", git_identity)
    monkeypatch.setattr(
        profiler,
        "_run_isolated_worker",
        lambda config, **_kwargs: _fake_run(
            arm=config["arm"],
            workload=config["workload"],
            process_id=next(process_ids),
            snapshot=prepared["initial_snapshot"],
            workers=config["concurrency_workers"],
        ),
    )

    report = profiler.profile_scip_mcp_consumer(
        manifest_path=tmp_path / "repo_manifest.json",
        project_root=tmp_path,
        subject_id="python-codenib",
        iterations=2,
        warmups=0,
        query_workload_size=6,
    )

    assert report["gates"]["immutable_benchmark"] is False
    assert report["passed"] is False


def test_isolated_worker_reraises_structured_memory_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        profiler.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=70,
            stdout="",
            stderr=json.dumps(
                {"error_type": "MemoryError", "message": "allocation failed"}
            ),
        ),
    )

    with pytest.raises(MemoryError, match="allocation failed"):
        profiler._run_isolated_worker({}, timeout_seconds=1.0)


@pytest.mark.parametrize(("passed", "exit_code"), [(True, 0), (False, 1)])
def test_controller_exit_code_reflects_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    passed: bool,
    exit_code: int,
) -> None:
    monkeypatch.setattr(
        profiler,
        "profile_scip_mcp_consumer",
        lambda **_kwargs: {"passed": passed},
    )

    observed = profiler.main(
        [
            "--manifest",
            "manifest.json",
            "--project-root",
            ".",
            "--subject-id",
            "python-codenib",
        ]
    )

    assert observed == exit_code
    assert json.loads(capsys.readouterr().out) == {"passed": passed}


def test_controller_creates_output_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        profiler,
        "profile_scip_mcp_consumer",
        lambda **_kwargs: {"passed": True},
    )
    output = tmp_path / "nested" / "report.json"

    exit_code = profiler.main(
        [
            "--manifest",
            "manifest.json",
            "--project-root",
            ".",
            "--subject-id",
            "python-codenib",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {"passed": True}


def test_worker_cli_reports_memory_error_without_swallowing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(profiler.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(
        profiler,
        "_worker",
        lambda _config: (_ for _ in ()).throw(MemoryError("out of memory")),
    )

    assert profiler.main(["--worker"]) == 70
    failure = json.loads(capsys.readouterr().err)
    assert failure == {"error_type": "MemoryError", "message": "out of memory"}
