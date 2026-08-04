# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reproducible CodeNib runner for the released SWE-Explore benchmark."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from codenib._version import package_version
from codenib.agent.runtime.explorer import (
    REPOSITORY_EXPLORER_POLICIES,
    normalize_repository_explorer_policy,
    repository_explorer_build_views,
)
from codenib.cli import detect_languages, index_repository
from codenib.compiler.artifact_fingerprints import (
    bm25_artifact_file_fingerprints,
    bm25_artifact_files_match,
)
from codenib.compiler.index_builders import BM25IndexBuilder
from codenib.compiler.manifest import MANIFEST_FILENAME, RepoManifest
from codenib.integrations.swe_explore import CodeNibSWEExploreExplorer
from codenib.paths import repo_index_dir

from .swe_explore import (
    SWE_EXPLORE_BENCHMARK_SHA256,
    SWE_EXPLORE_DATASET_REVISION,
    SWE_EXPLORE_METRICS,
    SWE_EXPLORE_SOURCE_DATASETS,
    SWE_EXPLORE_UPSTREAM_REVISION,
    SWEExploreCase,
    audit_swe_explore_snapshot,
    flatten_swe_explore_results,
    load_swe_explore_cases,
    load_swe_explore_sources,
    resolve_swe_explore_repo,
    score_swe_explore_regions,
    swe_explore_prediction_record,
)


def run_swe_explore_benchmark(
    *,
    bench_path: str | Path,
    case_set_path: str | Path,
    repos_root: str | Path,
    top_ks: Sequence[int],
    build_indexes: bool = True,
    rebuild: bool = False,
    policy: str = "auto",
    planning_budget: str = "balanced",
    retrieval_level: str = "l2",
    expected_bench_sha256: str | None = SWE_EXPLORE_BENCHMARK_SHA256,
) -> dict[str, Any]:
    """Run a fixed case set without dropping preparation or query failures."""

    normalized_policy = normalize_repository_explorer_policy(policy)
    build_views = repository_explorer_build_views(normalized_policy)
    planning_budget = str(planning_budget).strip().lower()
    retrieval_level = str(retrieval_level).strip().lower()
    if planning_budget not in {"fast", "balanced", "thorough"}:
        raise ValueError("planning_budget must be fast, balanced, or thorough")
    if retrieval_level not in {"l0", "l2"}:
        raise ValueError("retrieval_level must be l0 or l2")
    normalized_ks = tuple(sorted(set(int(value) for value in top_ks)))
    if not normalized_ks or normalized_ks[0] <= 0:
        raise ValueError("SWE-Explore top-k values must be positive")
    benchmark_digest = _file_sha256(bench_path)
    if expected_bench_sha256 is not None and not hmac.compare_digest(
        benchmark_digest, expected_bench_sha256.lower()
    ):
        raise ValueError(
            "SWE-Explore benchmark SHA-256 mismatch: "
            f"expected {expected_bench_sha256.lower()}, observed {benchmark_digest}"
        )
    case_labels, case_set_digest = _load_case_set(case_set_path)
    source_rows = _load_pinned_source_rows()
    sources = load_swe_explore_sources(source_rows)
    all_cases = {
        case.instance_id: case
        for case in load_swe_explore_cases(bench_path, sources=sources)
    }
    missing = [
        instance_id for instance_id in case_labels if instance_id not in all_cases
    ]
    if missing:
        raise ValueError(
            "case set contains unknown SWE-Explore instances: " + ", ".join(missing)
        )

    rows: list[dict[str, Any]] = []
    for instance_id, language_label in case_labels.items():
        case = all_cases[instance_id]
        try:
            rows.append(
                _run_case(
                    case,
                    language_label=language_label,
                    repos_root=repos_root,
                    top_ks=normalized_ks,
                    build_indexes=build_indexes,
                    rebuild=rebuild,
                    policy=normalized_policy,
                    planning_budget=planning_budget,
                    retrieval_level=retrieval_level,
                    build_views=build_views,
                )
            )
        except Exception as exc:
            rows.append(
                {
                    "instance_id": instance_id,
                    "dataset": case.dataset,
                    "language": language_label,
                    "success": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )

    return {
        "schema_version": 3,
        "provenance": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "codenib": _code_provenance(),
            "swe_explore_upstream_revision": SWE_EXPLORE_UPSTREAM_REVISION,
            "swe_explore_dataset_revision": SWE_EXPLORE_DATASET_REVISION,
            "benchmark_sha256": benchmark_digest,
            "expected_benchmark_sha256": expected_bench_sha256,
            "source_datasets": {
                name: {"dataset": dataset, "revision": revision}
                for name, (dataset, revision) in SWE_EXPLORE_SOURCE_DATASETS.items()
            },
            "case_set_sha256": case_set_digest,
            "top_ks": list(normalized_ks),
            "build_indexes": build_indexes,
            "rebuild": rebuild,
            "explorer_policy": normalized_policy,
            "planning_budget": planning_budget,
            "retrieval_level": retrieval_level,
            "materialized_views": list(build_views),
        },
        "summary": _summarize(rows, normalized_ks),
        "cases": rows,
    }


def _run_case(
    case: SWEExploreCase,
    *,
    language_label: str,
    repos_root: str | Path,
    top_ks: Sequence[int],
    build_indexes: bool,
    rebuild: bool,
    policy: str,
    planning_budget: str,
    retrieval_level: str,
    build_views: Sequence[str],
) -> dict[str, Any]:
    snapshot = audit_swe_explore_snapshot(case, repos_root)
    if not snapshot.valid:
        raise ValueError(f"snapshot audit failed: {snapshot.to_record()}")
    repo = resolve_swe_explore_repo(case, repos_root)
    detected_languages = detect_languages(repo)
    if not detected_languages:
        raise ValueError("CodeNib detected no supported source language")
    expected = "cpp" if language_label == "c" else language_label
    if expected and expected not in detected_languages:
        raise ValueError(
            f"declared language {language_label!r} not detected in {detected_languages!r}"
        )

    build_seconds = 0.0
    if build_indexes:
        started = time.perf_counter()
        _manifest, failed = index_repository(
            repo,
            languages=detected_languages,
            views=build_views,
            rebuild=rebuild,
        )
        build_seconds = time.perf_counter() - started
        if failed:
            raise RuntimeError("required view builds failed: " + ", ".join(failed))

    manifest_path = repo_index_dir(repo) / MANIFEST_FILENAME
    view_profiles = _validated_view_profiles(
        manifest_path,
        views=build_views,
        languages=detected_languages,
        required_top_k=max(top_ks),
    )
    load_started = time.perf_counter()
    explorer = CodeNibSWEExploreExplorer.from_repository(
        repo,
        manifest_path=manifest_path,
        policy=policy,
        budget=planning_budget,
        level=retrieval_level,
    )
    try:
        preparation = explorer.prepare(case.query)
        load_seconds = time.perf_counter() - load_started
        query_started = time.perf_counter()
        results = explorer.explore(
            instance_id=case.instance_id,
            query=case.query,
            top_k=max(top_ks),
        )
        query_seconds = time.perf_counter() - query_started
        query_trace = (
            explorer.last_trace.to_record() if explorer.last_trace is not None else None
        )
    finally:
        explorer.close()
    all_regions = flatten_swe_explore_results(results)

    evaluations: dict[str, Any] = {}
    for top_k in top_ks:
        regions = all_regions[:top_k]
        metrics = score_swe_explore_regions(
            regions,
            case.ground_truth,
            repo_path=repo,
        )
        evaluations[str(top_k)] = swe_explore_prediction_record(
            instance_id=case.instance_id,
            regions=regions,
            explorer=f"codenib-{policy.replace('_', '-')}",
            metrics=metrics,
        )

    return {
        "instance_id": case.instance_id,
        "dataset": case.dataset,
        "language": language_label,
        "success": True,
        "snapshot": snapshot.to_record(),
        "detected_languages": detected_languages,
        "manifest_path": str(manifest_path),
        "policy": policy,
        "view_profiles": view_profiles,
        "selected_plan": preparation.plan.name,
        "query_trace": query_trace,
        "timing_seconds": {
            "build": build_seconds,
            "load": load_seconds,
            "query": query_seconds,
        },
        "evaluations": evaluations,
    }


def _validated_view_profiles(
    manifest_path: str | Path,
    *,
    views: Sequence[str],
    languages: Sequence[str],
    required_top_k: int,
) -> dict[str, Any]:
    """Validate and record every view required by one explorer policy."""

    manifest = RepoManifest.load(manifest_path)
    profiles: dict[str, Any] = {}
    for view in views:
        if view == "bm25":
            profiles[view] = _validated_bm25_profile(
                manifest_path,
                languages=languages,
                required_top_k=required_top_k,
            )
            continue
        entry = manifest.indexes.get(view)
        if entry is None or not manifest.index_is_current(view):
            raise ValueError(f"SWE-Explore requires a fresh {view} manifest entry")
        artifact_path = Path(entry.path).expanduser()
        if not artifact_path.exists():
            raise ValueError(f"SWE-Explore {view} artifact is missing: {artifact_path}")
        profiles[view] = {
            "config": dict(entry.config),
            "commit": entry.commit,
            "source_fingerprint": entry.source_fingerprint,
            "artifact_path": entry.path,
        }
    return profiles


def _validated_bm25_profile(
    manifest_path: str | Path,
    *,
    languages: Sequence[str],
    required_top_k: int,
) -> dict[str, Any]:
    """Validate and record the physical BM25 contract used by a run."""

    manifest = RepoManifest.load(manifest_path)
    entry = manifest.indexes.get("bm25")
    if entry is None or not manifest.index_is_current("bm25"):
        raise ValueError("SWE-Explore requires a fresh BM25 manifest entry")

    expected = BM25IndexBuilder(languages=list(languages)).artifact_identity()
    actual = {key: entry.config.get(key) for key in expected}
    mismatches = {
        key: {"expected": expected[key], "observed": actual[key]}
        for key in expected
        if actual[key] != expected[key]
    }
    if mismatches:
        raise ValueError(f"SWE-Explore BM25 profile mismatch: {mismatches}")
    if int(actual["max_k"]) < required_top_k:
        raise ValueError(
            "SWE-Explore BM25 max_k is smaller than the requested region cutoff: "
            f"{actual['max_k']} < {required_top_k}"
        )
    artifact_files = entry.metadata.get(
        "artifact_file_fingerprints",
        entry.config.get("artifact_file_fingerprints"),
    )
    if not bm25_artifact_files_match(
        entry.path,
        expected_fingerprints=artifact_files,
    ):
        raise ValueError("SWE-Explore BM25 artifact files do not match the manifest")
    return {
        "config": actual,
        "commit": entry.commit,
        "source_fingerprint": entry.source_fingerprint,
        "artifact_path": entry.path,
        "artifact_file_fingerprints": bm25_artifact_file_fingerprints(entry.path),
    }


def _load_case_set(path: str | Path) -> tuple[dict[str, str], str]:
    case_set_path = Path(path).expanduser()
    content = case_set_path.read_bytes()
    value = json.loads(content)
    raw_cases = value.get("cases") if isinstance(value, Mapping) else value
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        raise ValueError("SWE-Explore case set must contain a cases sequence")

    cases: dict[str, str] = {}
    for item in raw_cases:
        if isinstance(item, str):
            instance_id = item.strip()
            language = ""
        elif isinstance(item, Mapping):
            instance_id = str(item.get("instance_id") or "").strip()
            language = str(item.get("language") or "").strip().lower()
        else:
            raise ValueError("invalid SWE-Explore case-set entry")
        if not instance_id:
            raise ValueError("SWE-Explore case-set entry has no instance_id")
        if instance_id in cases:
            raise ValueError(f"duplicate SWE-Explore case-set entry: {instance_id}")
        cases[instance_id] = language
    if not cases:
        raise ValueError("SWE-Explore case set is empty")
    return cases, hashlib.sha256(content).hexdigest()


def _file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()


def _load_pinned_source_rows() -> dict[str, Any]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError(
            'SWE-Explore source joins require `pip install "codenib[datasets]"`'
        ) from exc

    return {
        label: load_dataset(dataset, revision=revision, split="test")
        for label, (dataset, revision) in SWE_EXPLORE_SOURCE_DATASETS.items()
    }


def _code_provenance() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    revision = _git_command(root, "rev-parse", "HEAD")
    status = _git_command(root, "status", "--porcelain")
    return {
        "package_version": package_version(),
        "revision": revision,
        "dirty": bool(status) if status is not None else None,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _git_command(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _summarize(
    rows: Sequence[Mapping[str, Any]], top_ks: Sequence[int]
) -> dict[str, Any]:
    successful = [row for row in rows if row.get("success") is True]
    summary: dict[str, Any] = {
        "requested_cases": len(rows),
        "successful_cases": len(successful),
        "failed_cases": len(rows) - len(successful),
        "failure_ids": [
            str(row.get("instance_id"))
            for row in rows
            if row.get("success") is not True
        ],
        "timing_seconds": {},
        "metric_aggregation": {
            "population": "successful_cases",
            "n": len(successful),
            "failures_reported_separately": True,
        },
        "metrics_by_top_k": {},
    }
    for stage in ("build", "load", "query"):
        values = [
            float(row["timing_seconds"][stage])
            for row in successful
            if isinstance(row.get("timing_seconds"), Mapping)
        ]
        summary["timing_seconds"][stage] = _distribution(values)

    for top_k in top_ks:
        metric_summary: dict[str, float | None] = {}
        for metric in SWE_EXPLORE_METRICS:
            values = [
                float(row["evaluations"][str(top_k)]["metrics"][metric])
                for row in successful
            ]
            metric_summary[metric] = statistics.fmean(values) if values else None
        summary["metrics_by_top_k"][str(top_k)] = metric_summary
    return summary


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "max": None}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def _parse_top_ks(value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "top-k must be comma-separated integers"
        ) from exc
    if not values or min(values) <= 0:
        raise argparse.ArgumentTypeError("top-k values must be positive")
    return values


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run CodeNib on a revision-pinned SWE-Explore case set."
    )
    parser.add_argument("--bench", required=True, type=Path)
    parser.add_argument("--case-set", required=True, type=Path)
    parser.add_argument("--repos-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--top-k", default=(5, 10, 20), type=_parse_top_ks)
    parser.add_argument(
        "--bench-sha256",
        default=SWE_EXPLORE_BENCHMARK_SHA256,
        help="expected SHA-256 of --bench (defaults to the pinned public release)",
    )
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument(
        "--policy",
        choices=tuple(sorted(REPOSITORY_EXPLORER_POLICIES)),
        default="auto",
        help="native CodeNib exploration policy (bm25 is the compatibility control)",
    )
    parser.add_argument(
        "--planning-budget",
        choices=("fast", "balanced", "thorough"),
        default="balanced",
    )
    parser.add_argument("--retrieval-level", choices=("l0", "l2"), default="l2")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run_swe_explore_benchmark(
        bench_path=args.bench,
        case_set_path=args.case_set,
        repos_root=args.repos_root,
        top_ks=args.top_k,
        build_indexes=not args.no_build,
        rebuild=args.rebuild,
        policy=args.policy,
        planning_budget=args.planning_budget,
        retrieval_level=args.retrieval_level,
        expected_bench_sha256=args.bench_sha256,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, args.output)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0 if report["summary"]["failed_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_swe_explore_benchmark"]
