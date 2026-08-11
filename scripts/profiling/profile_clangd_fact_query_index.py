#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark native clangd symbol queries against the complete CodeGraph.

Both arms start from the same existing project-local .idx directory and run
the same deterministic definition/reference workload. clangd index generation,
position queries, routes, persistence, and serving integration are outside this
baseline gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CORE_BUILD = _PROJECT_ROOT / "build/core"
for candidate in (_CORE_BUILD, _PROJECT_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts.profiling.profile_fact_batch_buffer import (  # noqa: E402
    DEFAULT_ITERATIONS,
    DEFAULT_THRESHOLD,
    DEFAULT_WARMUPS,
    acceleration_decision,
    summarize_samples,
)
from scripts.profiling.profile_fact_query_index import (  # noqa: E402
    DEFAULT_PARITY_SAMPLE_LIMIT,
    DEFAULT_QUERY_WORKLOAD_SIZE,
    _assert_query_parity,
    _definition_symbols,
    _even_sample,
    _parity_seeds,
    _query_workload,
    _reference_count,
    _run_arm,
    _timed,
)


def _idx_digest(idx_directory: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    total_bytes = 0
    files = sorted(idx_directory.glob("*.idx"), key=lambda path: path.name)
    for path in files:
        name = path.name.encode("utf-8")
        digest.update(len(name).to_bytes(8, "little"))
        digest.update(name)
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                total_bytes += len(block)
                digest.update(block)
    return f"sha256:{digest.hexdigest()}", total_bytes, len(files)


def _append_sample(samples: dict[str, list[float]], name: str, value: float) -> None:
    samples.setdefault(name, []).append(float(value))


def profile_clangd_fact_query_index(
    *,
    idx_directory: Path,
    project_root: Path,
    iterations: int = DEFAULT_ITERATIONS,
    warmups: int = DEFAULT_WARMUPS,
    threshold: float = DEFAULT_THRESHOLD,
    external_index_seconds: float | None = None,
    query_workload_size: int = DEFAULT_QUERY_WORKLOAD_SIZE,
    parity_sample_limit: int = DEFAULT_PARITY_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Run alternating graph/native arms and return a reproducible report."""

    if iterations < 1 or warmups < 0:
        raise ValueError("iterations must be positive and warmups non-negative")
    if query_workload_size < 1 or parity_sample_limit < 1:
        raise ValueError("query and parity sample sizes must be positive")
    idx_directory = idx_directory.resolve()
    project_root = project_root.resolve()
    if not idx_directory.is_dir():
        raise FileNotFoundError(idx_directory)
    if not project_root.is_dir():
        raise FileNotFoundError(project_root)
    if not any(idx_directory.glob("*.idx")):
        raise ValueError(f"no .idx files in {idx_directory}")

    import codenib_core

    from codenib.ls_index.clangd_decode import ClangdGraphDecoder

    if not hasattr(codenib_core, "decode_clangd_fact_query_index") or not hasattr(
        codenib_core, "clangd_fact_query_snapshot"
    ):
        raise RuntimeError("compiled core does not expose clangd FactQueryIndex v1")
    contract = codenib_core.clangd_fact_query_contract()
    initial_receipt = dict(
        codenib_core.clangd_fact_query_snapshot(
            idx_directory=str(idx_directory), project_root=str(project_root)
        )
    )
    initial_digest = _idx_digest(idx_directory)

    def legacy_arm() -> tuple[Any, dict[str, float]]:
        decoder = ClangdGraphDecoder(
            idx_directory=str(idx_directory), project_root=str(project_root)
        )
        graph, startup = _timed(decoder.materialize_code_graph)
        _, query = _timed(lambda: _query_workload(graph, workload_symbols))
        return graph, {
            "startup": startup,
            "query_workload": query,
        }

    baseline_decoder = ClangdGraphDecoder(
        idx_directory=str(idx_directory), project_root=str(project_root)
    )
    baseline_graph = baseline_decoder.materialize_code_graph()
    all_symbols = _definition_symbols(baseline_graph)
    if not all_symbols:
        raise ValueError("benchmark index has no definition symbols")
    expected_references = _reference_count(baseline_graph)
    workload_symbols = _even_sample(all_symbols, query_workload_size)
    parity_symbols = _even_sample(all_symbols, parity_sample_limit)
    parity_seeds = _parity_seeds(baseline_graph, parity_symbols)

    def native_arm() -> tuple[Any, dict[str, float]]:
        payload, startup = _timed(
            lambda: codenib_core.decode_clangd_fact_query_index(
                idx_directory=str(idx_directory), project_root=str(project_root)
            )
        )
        if payload.get("graph_materialized") is not False:
            raise AssertionError("candidate payload reports graph materialization")
        if payload.get("snapshot_id") != initial_receipt.get("snapshot_id"):
            raise RuntimeError("clangd index changed during native startup")
        index = payload["index"]
        _, query = _timed(lambda: _query_workload(index, workload_symbols))
        native_decode = payload["native_decode_ns"] / 1_000_000_000
        native_index = payload["native_index_ns"] / 1_000_000_000
        count_fields = {
            "file_count",
            "raw_symbol_count",
            "raw_reference_count",
            "definition_count",
            "reference_count",
            "decoded_record_count",
            "index_bytes",
        }
        stages = {
            name: value / 1_000_000_000
            for name, value in (payload.get("decode_profile_ns") or {}).items()
            if name not in count_fields
        }
        return index, {
            "startup": startup,
            "native_decode": native_decode,
            "native_index": native_index,
            "boundary_residual": max(0.0, startup - native_decode - native_index),
            "query_workload": query,
            **{f"native_stage_{name}": value for name, value in stages.items()},
        }

    for _ in range(warmups):
        _run_arm(legacy_arm)
        _run_arm(native_arm)

    legacy_samples: dict[str, list[float]] = {}
    native_samples: dict[str, list[float]] = {}
    first_arm_by_iteration: list[str] = []
    last_graph = baseline_graph
    last_index = None
    for iteration in range(iterations):
        if iteration % 2 == 0:
            first_arm_by_iteration.append("legacy")
            (last_graph, legacy_timing), legacy_total = _run_arm(legacy_arm)
            (last_index, native_timing), native_total = _run_arm(native_arm)
        else:
            first_arm_by_iteration.append("native")
            (last_index, native_timing), native_total = _run_arm(native_arm)
            (last_graph, legacy_timing), legacy_total = _run_arm(legacy_arm)
        legacy_timing["total"] = legacy_total
        native_timing["total"] = native_total
        for name, value in legacy_timing.items():
            _append_sample(legacy_samples, name, value)
        for name, value in native_timing.items():
            _append_sample(native_samples, name, value)

    parity = True
    parity_error = None
    try:
        _assert_query_parity(
            last_graph,
            last_index,
            seeds=parity_seeds,
            expected_symbol_count=len(all_symbols),
            expected_reference_count=expected_references,
        )
    except AssertionError as exc:
        parity = False
        parity_error = str(exc)

    final_digest = _idx_digest(idx_directory)
    if final_digest != initial_digest:
        raise RuntimeError("clangd index changed during benchmark")
    final_receipt = dict(
        codenib_core.clangd_fact_query_snapshot(
            idx_directory=str(idx_directory), project_root=str(project_root)
        )
    )
    if final_receipt != initial_receipt:
        raise RuntimeError("clangd content receipt changed during benchmark")
    legacy_summary = {
        name: summarize_samples(values) for name, values in legacy_samples.items()
    }
    native_summary = {
        name: summarize_samples(values) for name, values in native_samples.items()
    }
    decision = acceleration_decision(
        legacy_local_seconds=legacy_summary["total"]["median_seconds"],
        candidate_local_seconds=native_summary["total"]["median_seconds"],
        threshold=threshold,
        external_index_seconds=external_index_seconds,
        parity=parity,
        candidate_name="native-clangd-fact-query-index",
    )
    hash_seconds = native_summary["native_stage_hash_index"]["median_seconds"]
    verify_seconds = native_summary["native_stage_verify_snapshot"]["median_seconds"]
    native_startup_seconds = native_summary["startup"]["median_seconds"]
    receipt_seconds = hash_seconds + verify_seconds
    idx_sha256, idx_bytes, idx_files = initial_digest
    return {
        "schema_version": 1,
        "benchmark": "native_clangd_fact_query_index_v1",
        "timer": "time.perf_counter",
        "subject": {
            "idx_directory": str(idx_directory),
            "idx_files": idx_files,
            "idx_bytes": idx_bytes,
            "idx_sha256": idx_sha256,
            "language": "cpp",
            "project_root": str(project_root),
            "definition_symbols": len(all_symbols),
            "references": expected_references,
        },
        "configuration": {
            "iterations": iterations,
            "warmups": warmups,
            "threshold": threshold,
            "alternating_arm_order": True,
            "first_arm_by_iteration": first_arm_by_iteration,
            "query_workload_size": len(workload_symbols),
            "parity_sample_size": len(parity_symbols),
            "parity_seed_count": len(parity_seeds),
            "cyclic_gc_collected_before_each_arm": True,
            "cyclic_gc_disabled_during_timed_regions": True,
            "external_index_seconds": external_index_seconds,
            "clangd_fact_query_contract": contract,
        },
        "capabilities": dict(contract["capabilities"]),
        "parity": {"passed": parity, "error": parity_error},
        "raw_samples": {
            "legacy_clangd_graph": legacy_samples,
            "native_clangd_fact_query_index": native_samples,
        },
        "legacy_clangd_graph": legacy_summary,
        "native_clangd_fact_query_index": native_summary,
        "snapshot_receipt": {
            **initial_receipt,
            "hash_index_median_seconds": hash_seconds,
            "verify_snapshot_median_seconds": verify_seconds,
            "total_median_seconds": receipt_seconds,
            "fraction_of_native_startup": (
                receipt_seconds / native_startup_seconds
                if native_startup_seconds > 0
                else 0.0
            ),
        },
        "decision": decision,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idx-directory", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--external-index-seconds", type=float)
    parser.add_argument(
        "--query-workload-size", type=int, default=DEFAULT_QUERY_WORKLOAD_SIZE
    )
    parser.add_argument(
        "--parity-sample-limit", type=int, default=DEFAULT_PARITY_SAMPLE_LIMIT
    )
    parser.add_argument("--output-json", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = profile_clangd_fact_query_index(
        idx_directory=args.idx_directory,
        project_root=args.project_root,
        iterations=args.iterations,
        warmups=args.warmups,
        threshold=args.threshold,
        external_index_seconds=args.external_index_seconds,
        query_workload_size=args.query_workload_size,
        parity_sample_limit=args.parity_sample_limit,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report["parity"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
