# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from codenib.compiler.artifact_fingerprints import bm25_artifact_file_fingerprints
from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.eval.benchmarks import swe_explore_runner
from codenib.eval.benchmarks.swe_explore import SWE_EXPLORE_METRICS
from codenib.repository_source_selection import DEFAULT_REPOSITORY_SOURCE_SELECTION

COMMIT = "a" * 40
SOURCE_FINGERPRINT = "sha256-v2:" + ("b" * 64)
SOURCE_SELECTION_DIGEST = DEFAULT_REPOSITORY_SOURCE_SELECTION.digest


def test_runner_reports_failures_separately_from_quality_metrics(
    monkeypatch, tmp_path
) -> None:
    case_set = tmp_path / "cases.json"
    case_set.write_text(
        json.dumps(
            {
                "cases": [
                    {"instance_id": "ok", "language": "python"},
                    {"instance_id": "failed", "language": "go"},
                ]
            }
        ),
        encoding="utf-8",
    )
    cases = (
        SimpleNamespace(instance_id="ok", dataset="verified"),
        SimpleNamespace(instance_id="failed", dataset="multilingual"),
    )
    monkeypatch.setattr(swe_explore_runner, "_load_pinned_source_rows", lambda: {})
    monkeypatch.setattr(swe_explore_runner, "load_swe_explore_sources", lambda _x: {})
    monkeypatch.setattr(
        swe_explore_runner, "load_swe_explore_cases", lambda *_a, **_kw: cases
    )

    observed_kwargs = []

    def run_case(case, **kwargs):
        observed_kwargs.append(kwargs)
        if case.instance_id == "failed":
            raise RuntimeError("index unavailable")
        metrics = {metric: 1.0 for metric in SWE_EXPLORE_METRICS}
        return {
            "instance_id": case.instance_id,
            "dataset": case.dataset,
            "language": kwargs["language_label"],
            "success": True,
            "timing_seconds": {"build": 2.0, "load": 0.2, "query": 0.01},
            "evaluations": {"5": {"metrics": metrics}},
        }

    monkeypatch.setattr(swe_explore_runner, "_run_case", run_case)
    bench = tmp_path / "bench.jsonl"
    bench.write_text("fixture\n", encoding="utf-8")

    report = swe_explore_runner.run_swe_explore_benchmark(
        bench_path=bench,
        case_set_path=case_set,
        repos_root=tmp_path / "repos",
        top_ks=(5,),
        expected_bench_sha256=hashlib.sha256(bench.read_bytes()).hexdigest(),
    )

    assert report["summary"]["requested_cases"] == 2
    assert report["summary"]["successful_cases"] == 1
    assert report["summary"]["failed_cases"] == 1
    assert report["summary"]["failure_ids"] == ["failed"]
    assert report["summary"]["metric_aggregation"] == {
        "population": "successful_cases",
        "n": 1,
        "failures_reported_separately": True,
    }
    assert report["summary"]["metrics_by_top_k"]["5"]["recall"] == 1.0
    assert report["cases"][1]["error_type"] == "RuntimeError"
    assert report["schema_version"] == 3
    assert report["provenance"]["explorer_policy"] == "auto"
    assert report["provenance"]["materialized_views"] == [
        "bm25",
        "vector",
        "symbol_graph",
    ]
    assert all(kwargs["policy"] == "auto" for kwargs in observed_kwargs)
    assert all(
        kwargs["build_views"] == ("bm25", "vector", "symbol_graph")
        for kwargs in observed_kwargs
    )


def test_runner_rejects_unpinned_benchmark_bytes(tmp_path) -> None:
    bench = tmp_path / "bench.jsonl"
    bench.write_text("modified release\n", encoding="utf-8")

    with pytest.raises(ValueError, match="benchmark SHA-256 mismatch"):
        swe_explore_runner.run_swe_explore_benchmark(
            bench_path=bench,
            case_set_path=tmp_path / "unused.json",
            repos_root=tmp_path / "repos",
            top_ks=(5,),
        )


def test_bm25_profile_validation_rejects_incompatible_artifact(tmp_path) -> None:
    manifest_path = tmp_path / "repo_manifest.json"
    profile = BM25IndexBuilder(languages=["python"]).artifact_identity()
    profile["max_k"] = 15
    manifest = RepoManifest(
        commit=COMMIT,
        last_indexed_commit=COMMIT,
        source_fingerprint=SOURCE_FINGERPRINT,
        last_indexed_source_fingerprint=SOURCE_FINGERPRINT,
        last_indexed_source_selection_digest=SOURCE_SELECTION_DIGEST,
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(tmp_path / "bm25"),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                config=profile,
                commit=COMMIT,
                source_fingerprint=SOURCE_FINGERPRINT,
                source_selection_digest=SOURCE_SELECTION_DIGEST,
            )
        },
    )
    manifest.save(manifest_path)

    with pytest.raises(ValueError, match="BM25 profile mismatch"):
        swe_explore_runner._validated_bm25_profile(
            manifest_path,
            languages=("python",),
            required_top_k=20,
        )


def test_bm25_profile_validation_records_exact_artifact(tmp_path) -> None:
    manifest_path = tmp_path / "repo_manifest.json"
    artifact_path = tmp_path / "bm25"
    artifact_path.mkdir()
    (artifact_path / "documents.json").write_text("[]", encoding="utf-8")
    (artifact_path / "bm25_metadata.json").write_text(
        '{"max_k": 128}', encoding="utf-8"
    )
    artifact_files = bm25_artifact_file_fingerprints(artifact_path)
    profile = BM25IndexBuilder(languages=["python", "go"]).artifact_identity()
    manifest = RepoManifest(
        commit=COMMIT,
        last_indexed_commit=COMMIT,
        source_fingerprint=SOURCE_FINGERPRINT,
        last_indexed_source_fingerprint=SOURCE_FINGERPRINT,
        last_indexed_source_selection_digest=SOURCE_SELECTION_DIGEST,
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(artifact_path),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                config={
                    **profile,
                    "artifact_file_fingerprints": artifact_files,
                },
                commit=COMMIT,
                source_fingerprint=SOURCE_FINGERPRINT,
                source_selection_digest=SOURCE_SELECTION_DIGEST,
            )
        },
    )
    manifest.save(manifest_path)

    observed = swe_explore_runner._validated_bm25_profile(
        manifest_path,
        languages=("python", "go"),
        required_top_k=20,
    )

    assert observed == {
        "config": profile,
        "commit": COMMIT,
        "source_fingerprint": SOURCE_FINGERPRINT,
        "artifact_path": str(artifact_path),
        "artifact_file_fingerprints": artifact_files,
    }

    (artifact_path / "documents.json").write_text("[{}]", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact files do not match"):
        swe_explore_runner._validated_bm25_profile(
            manifest_path,
            languages=("python", "go"),
            required_top_k=20,
        )


def test_view_profile_validation_records_native_policy_artifacts(tmp_path) -> None:
    manifest_path = tmp_path / "repo_manifest.json"
    vector_path = tmp_path / "vector"
    graph_path = tmp_path / "graph.pkl"
    vector_path.mkdir()
    graph_path.write_bytes(b"graph")
    manifest = RepoManifest(
        commit=COMMIT,
        last_indexed_commit=COMMIT,
        source_fingerprint=SOURCE_FINGERPRINT,
        last_indexed_source_fingerprint=SOURCE_FINGERPRINT,
        last_indexed_source_selection_digest=SOURCE_SELECTION_DIGEST,
        indexes={
            "vector": IndexEntry(
                index_type="vector",
                path=str(vector_path),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                config={"embedding_model": "fixture"},
                commit=COMMIT,
                source_fingerprint=SOURCE_FINGERPRINT,
                source_selection_digest=SOURCE_SELECTION_DIGEST,
            ),
            "symbol_graph": IndexEntry(
                index_type="symbol_graph",
                path=str(graph_path),
                built_at="2026-08-04T00:00:00+00:00",
                built_at_epoch=0.0,
                status="fresh",
                config={"builder_schema": 3},
                commit=COMMIT,
                source_fingerprint=SOURCE_FINGERPRINT,
                source_selection_digest=SOURCE_SELECTION_DIGEST,
            ),
        },
    )
    manifest.save(manifest_path)

    profiles = swe_explore_runner._validated_view_profiles(
        manifest_path,
        views=("vector", "symbol_graph"),
        languages=("python",),
        required_top_k=20,
    )

    assert profiles["vector"]["config"] == {"embedding_model": "fixture"}
    assert profiles["symbol_graph"]["artifact_path"] == str(graph_path)


def test_runner_normalizes_explicit_bm25_control(monkeypatch, tmp_path) -> None:
    case_set = tmp_path / "cases.json"
    case_set.write_text(
        json.dumps({"cases": [{"instance_id": "case", "language": "python"}]}),
        encoding="utf-8",
    )
    bench = tmp_path / "bench.jsonl"
    bench.write_text("fixture\n", encoding="utf-8")
    case = SimpleNamespace(instance_id="case", dataset="verified")
    monkeypatch.setattr(swe_explore_runner, "_load_pinned_source_rows", lambda: {})
    monkeypatch.setattr(swe_explore_runner, "load_swe_explore_sources", lambda _x: {})
    monkeypatch.setattr(
        swe_explore_runner, "load_swe_explore_cases", lambda *_a, **_kw: (case,)
    )
    observed = {}

    def run_case(_case, **kwargs):
        observed.update(kwargs)
        metrics = {metric: 0.0 for metric in SWE_EXPLORE_METRICS}
        return {
            "instance_id": "case",
            "dataset": "verified",
            "language": "python",
            "success": True,
            "timing_seconds": {"build": 0.0, "load": 0.0, "query": 0.0},
            "evaluations": {"5": {"metrics": metrics}},
        }

    monkeypatch.setattr(swe_explore_runner, "_run_case", run_case)

    report = swe_explore_runner.run_swe_explore_benchmark(
        bench_path=bench,
        case_set_path=case_set,
        repos_root=tmp_path / "repos",
        top_ks=(5,),
        policy="BM25",
        expected_bench_sha256=hashlib.sha256(bench.read_bytes()).hexdigest(),
    )

    assert observed["policy"] == "bm25"
    assert observed["build_views"] == ("bm25",)
    assert report["provenance"]["explorer_policy"] == "bm25"


def test_runner_rejects_duplicate_case_ids(tmp_path) -> None:
    case_set = tmp_path / "cases.json"
    case_set.write_text(
        json.dumps(
            {
                "cases": [
                    {"instance_id": "same", "language": "python"},
                    {"instance_id": "same", "language": "python"},
                ]
            }
        ),
        encoding="utf-8",
    )

    try:
        swe_explore_runner._load_case_set(case_set)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate case IDs must fail")
