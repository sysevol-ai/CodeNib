# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from codeminer.compiler.manifest import RepoManifest
from codeminer.eval.agent_runner.lsp_provider_validation import (
    fingerprint_lsp_start_locations,
)
from codeminer.eval.materialization_trace import (
    TraceRequest,
    attach_materialization_cost,
    replay_materialized_trace,
)


class _Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1_000_000
        return self.value


def _manifest(tmp_path):
    path = tmp_path / "repo_manifest.json"
    RepoManifest(
        repo_path="/repo",
        commit="abc",
        languages=["python"],
        capabilities={"sparse_search": True},
    ).save(path)
    return path


def test_replay_materialized_trace_guards_outputs(monkeypatch, tmp_path):
    requests = [TraceRequest("q1", "bm25", {"query": "needle", "top_k": 2})]
    context = SimpleNamespace(
        bm25=object(),
        vector=None,
        regex_index=None,
        symbol_graph=None,
        errors={},
    )
    calls = []

    def fake_search(ctx, **params):
        calls.append(params)
        return [{"file": "a.py", "start_line": 1, "score": 0.5}]

    monkeypatch.setattr(
        "codeminer.eval.materialization_trace.search_bm25_impl", fake_search
    )
    report = asyncio.run(
        replay_materialized_trace(
            _manifest(tmp_path),
            requests,
            warmup_repetitions=1,
            measured_repetitions=2,
            context_loader=lambda _: context,
            clock_ns=_Clock(),
        )
    )

    assert len(calls) == 3
    assert report["phases"]["load_seconds"] == 0.001
    assert report["phases"]["measured_request_seconds"] == 0.002
    assert report["operations"][0]["measurements"] == 2
    assert len({row["response_fingerprint"] for row in report["rows"]}) == 1


def test_replay_rejects_missing_required_view(tmp_path):
    request = TraceRequest("q1", "dense", {"query": "needle"})
    context = SimpleNamespace(
        bm25=None,
        vector=None,
        regex_index=None,
        symbol_graph=None,
        errors={"vector": "load failed"},
    )

    with pytest.raises(ValueError, match="vector: load failed"):
        asyncio.run(
            replay_materialized_trace(
                _manifest(tmp_path),
                [request],
                context_loader=lambda _: context,
            )
        )


def test_replay_clears_dense_cache_between_independent_requests(monkeypatch, tmp_path):
    vector = SimpleNamespace(clear_query_cache=lambda: clears.append(True))
    context = SimpleNamespace(
        bm25=None,
        vector=vector,
        regex_index=None,
        symbol_graph=None,
        errors={},
    )
    clears = []

    async def fake_search(ctx, **params):
        return [{"file": "a.py", "score": 0.5}]

    monkeypatch.setattr(
        "codeminer.eval.materialization_trace.search_semantic", fake_search
    )
    report = asyncio.run(
        replay_materialized_trace(
            _manifest(tmp_path),
            [TraceRequest("q1", "dense", {"query": "needle"})],
            warmup_repetitions=1,
            measured_repetitions=2,
            context_loader=lambda _: context,
        )
    )

    assert len(clears) == 3
    assert report["protocol"]["dense_query_cache"].startswith("cleared")


def test_replay_enforces_preregistered_lsp_location_contract(monkeypatch, tmp_path):
    expected = fingerprint_lsp_start_locations(
        [{"file": "src/example.py", "start_line": 4}]
    )
    request = TraceRequest.from_dict(
        {
            "request_id": "definition-1",
            "operation": "definition",
            "params": {
                "file_path": "src/caller.py",
                "line": 10,
                "character": 2,
            },
            "expected_result": {
                "contract": "ordered_file_start_line",
                "fingerprint": expected,
                "result_count": 1,
            },
        }
    )
    context = SimpleNamespace(
        bm25=None,
        vector=None,
        regex_index=None,
        symbol_graph=object(),
        errors={},
    )

    def fake_definition(ctx, **params):
        return [{"file": "src/example.py", "start_line": 5}]

    monkeypatch.setattr(
        "codeminer.eval.materialization_trace.lsp_definition_impl", fake_definition
    )
    report = asyncio.run(
        replay_materialized_trace(
            _manifest(tmp_path),
            [request],
            warmup_repetitions=0,
            measured_repetitions=2,
            context_loader=lambda _: context,
        )
    )

    assert report["protocol"]["semantic_guarded_request_count"] == 1
    assert {row["contract_fingerprint"] for row in report["rows"]} == {expected}


def test_replay_rejects_lsp_contract_drift(monkeypatch, tmp_path):
    request = TraceRequest.from_dict(
        {
            "request_id": "definition-1",
            "operation": "definition",
            "params": {"file_path": "src/caller.py", "line": 10},
            "expected_result": {
                "contract": "ordered_file_start_line",
                "fingerprint": "a" * 64,
                "result_count": 1,
            },
        }
    )
    context = SimpleNamespace(
        bm25=None,
        vector=None,
        regex_index=None,
        symbol_graph=object(),
        errors={},
    )
    monkeypatch.setattr(
        "codeminer.eval.materialization_trace.lsp_definition_impl",
        lambda ctx, **params: [{"file": "src/example.py", "start_line": 5}],
    )

    with pytest.raises(ValueError, match="pre-registered"):
        asyncio.run(
            replay_materialized_trace(
                _manifest(tmp_path),
                [request],
                warmup_repetitions=0,
                measured_repetitions=1,
                context_loader=lambda _: context,
            )
        )


def test_attach_materialization_cost_reports_accounting_curve():
    replay = {
        "protocol": {"request_count": 4, "measured_repetitions": 2},
        "phases": {"load_seconds": 1.0, "measured_request_seconds": 8.0},
    }

    report = attach_materialization_cost(
        replay,
        materialization_seconds=20.0,
        per_index_seconds={"vector": 12.0, "bm25": 8.0},
        artifact_bytes=100,
        consumer_counts=[1, 5],
    )

    assert report["trace_repetition_curve"] == [
        {
            "trace_repetitions": 1,
            "request_count": 4,
            "materialization_seconds_per_trace": 20.0,
            "materialization_seconds_per_request": 5.0,
            "deployment_models": {
                "artifact_shared_process_isolated": {
                    "projected_total_seconds": 25.0,
                    "load_seconds_per_trace": 1.0,
                },
                "shared_resident_runtime": {
                    "projected_total_seconds": 25.0,
                    "load_seconds_per_trace": 1.0,
                },
            },
        },
        {
            "trace_repetitions": 5,
            "request_count": 20,
            "materialization_seconds_per_trace": 4.0,
            "materialization_seconds_per_request": 1.0,
            "deployment_models": {
                "artifact_shared_process_isolated": {
                    "projected_total_seconds": 45.0,
                    "load_seconds_per_trace": 1.0,
                },
                "shared_resident_runtime": {
                    "projected_total_seconds": 41.0,
                    "load_seconds_per_trace": 0.2,
                },
            },
        },
    ]
