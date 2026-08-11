#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark graph-free FactQueryIndex v1 against query-ready CodeGraph.

Both arms decode the same SCIP artifact and execute the same deterministic
symbol definition/reference workload. The candidate must preserve public
results and errors, own no graph, and clear the configured end-to-end gate.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

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

DEFAULT_QUERY_WORKLOAD_SIZE = 100
DEFAULT_PARITY_SAMPLE_LIMIT = 300


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    gc_was_enabled = gc.isenabled()
    gc.disable()
    started = time.perf_counter()
    try:
        result = call()
        elapsed = time.perf_counter() - started
    finally:
        if gc_was_enabled:
            gc.enable()
        else:
            gc.disable()
    return result, elapsed


def _run_arm(call: Callable[[], Any]) -> tuple[Any, float]:
    gc.collect()
    return _timed(call)


def _even_sample(values: Sequence[str], limit: int) -> list[str]:
    if limit < 1:
        raise ValueError("sample limit must be positive")
    if len(values) <= limit:
        return list(values)
    stride = max(1, len(values) // limit)
    return list(values[::stride][:limit])


def _definition_symbols(graph: Any) -> list[str]:
    from codenib.types import node_has_definition

    return [
        name
        for name in graph.name_to_vertex
        if node_has_definition(graph.get_node_info_by_name(name) or {})
    ]


def _reference_count(graph: Any) -> int:
    from codenib.types import EDGE_TYPE_REFERENCE

    return sum(
        edge.attributes().get("type") == EDGE_TYPE_REFERENCE for edge in graph.graph.es
    )


def _query_workload(index: Any, symbols: Sequence[str]) -> None:
    from codenib.agent.lsp_graph import lsp_definition, lsp_references

    for symbol in symbols:
        lsp_definition(index, symbol=symbol, top_k=10_000)
        lsp_references(index, symbol=symbol, top_k=10_000)


def _parity_seeds(graph: Any, symbols: Sequence[str]) -> list[str]:
    seeds: list[str] = []
    for symbol in symbols:
        info = graph.get_node_info_by_name(symbol) or {}
        display = str(info.get("unified_name") or symbol)
        bare = display.split(":")[-1].split(".")[-1].split("#")[-1]
        bare = bare.rstrip("()").strip()
        seeds.extend((symbol, display, bare, f"`{bare}`"))
    seeds.append("definitely-missing-fact-query-symbol")
    return list(dict.fromkeys(seed for seed in seeds if seed))


def _query_outcome(call: Callable[[], Any]) -> tuple[Any, ...]:
    try:
        return "ok", call()
    except Exception as exc:  # noqa: BLE001 - parity includes public failures
        return "error", type(exc).__name__, str(exc)


def _assert_query_parity(
    graph: Any,
    index: Any,
    *,
    seeds: Sequence[str],
    expected_symbol_count: int,
    expected_reference_count: int,
    expected_capabilities: Mapping[str, bool] | None = None,
) -> None:
    from codenib.agent.lsp_graph import lsp_definition, lsp_references

    if getattr(index, "materializes_graph", None) is not False or hasattr(
        index, "graph"
    ):
        raise AssertionError("candidate materialized a graph")
    capabilities = expected_capabilities or {
        "definition_by_symbol": True,
        "references_by_symbol": True,
        "position_queries": False,
        "route_queries": False,
    }
    if getattr(index, "capabilities", None) != dict(capabilities):
        raise AssertionError("candidate capabilities differ from expected contract")
    if index.symbol_count != expected_symbol_count:
        raise AssertionError(
            f"definition symbol count differs: {expected_symbol_count} != "
            f"{index.symbol_count}"
        )
    if index.reference_count != expected_reference_count:
        raise AssertionError(
            f"reference count differs: {expected_reference_count} != "
            f"{index.reference_count}"
        )

    for symbol in seeds:
        expected = _query_outcome(
            lambda symbol=symbol: lsp_definition(graph, symbol=symbol, top_k=10_000)
        )
        actual = _query_outcome(
            lambda symbol=symbol: lsp_definition(index, symbol=symbol, top_k=10_000)
        )
        if actual != expected:
            raise AssertionError(f"definition result differs for {symbol!r}")

        for include_declaration in (False, True):
            expected = _query_outcome(
                lambda symbol=symbol, include_declaration=include_declaration: (
                    lsp_references(
                        graph,
                        symbol=symbol,
                        include_declaration=include_declaration,
                        top_k=10_000,
                    )
                )
            )
            actual = _query_outcome(
                lambda symbol=symbol, include_declaration=include_declaration: (
                    lsp_references(
                        index,
                        symbol=symbol,
                        include_declaration=include_declaration,
                        top_k=10_000,
                    )
                )
            )
            if actual != expected:
                raise AssertionError(
                    f"reference result differs for {symbol!r} "
                    f"(include_declaration={include_declaration})"
                )


def _append_sample(samples: dict[str, list[float]], name: str, value: float) -> None:
    samples.setdefault(name, []).append(float(value))


def profile_fact_query_index(
    *,
    index_path: Path,
    language: str,
    project_root: Path | None = None,
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
    index_path = index_path.resolve()
    if not index_path.is_file():
        raise FileNotFoundError(index_path)
    root = str(project_root.resolve()) if project_root is not None else None

    import codenib_core

    from codenib.scip_interface.scip_decode_core import _build_code_graph

    if not hasattr(codenib_core, "decode_scip_fact_query_index"):
        raise RuntimeError("compiled core does not expose FactQueryIndex v1")
    contract = codenib_core.fact_query_index_contract()

    def legacy_arm() -> tuple[Any, dict[str, float]]:
        payload, binding = _timed(
            lambda: codenib_core.decode_scip(
                index_file=str(index_path), project_root=root, language=language
            )
        )
        graph, materialize = _timed(
            lambda: _build_code_graph(
                payload["vertices"], payload["edges"], project_root=root
            )
        )
        _, query = _timed(lambda: _query_workload(graph, workload_symbols))
        return graph, {
            "binding": binding,
            "graph_materialize": materialize,
            "query_workload": query,
        }

    # Establish deterministic workload and parity seeds outside measured arms.
    baseline_payload = codenib_core.decode_scip(
        index_file=str(index_path), project_root=root, language=language
    )
    baseline_graph = _build_code_graph(
        baseline_payload["vertices"], baseline_payload["edges"], project_root=root
    )
    all_symbols = _definition_symbols(baseline_graph)
    if not all_symbols:
        raise ValueError("benchmark index has no definition symbols")
    expected_references = _reference_count(baseline_graph)
    workload_symbols = _even_sample(all_symbols, query_workload_size)
    parity_symbols = _even_sample(all_symbols, parity_sample_limit)
    parity_seeds = _parity_seeds(baseline_graph, parity_symbols)

    def native_arm() -> tuple[Any, dict[str, float]]:
        payload, binding = _timed(
            lambda: codenib_core.decode_scip_fact_query_index(
                index_file=str(index_path), project_root=root, language=language
            )
        )
        if payload.get("graph_materialized") is not False:
            raise AssertionError("candidate payload reports graph materialization")
        index = payload["index"]
        _, query = _timed(lambda: _query_workload(index, workload_symbols))
        native_decode = payload["native_decode_ns"] / 1_000_000_000
        native_index = payload["native_index_ns"] / 1_000_000_000
        profile = payload.get("decode_profile_ns") or {}
        stage_values = {
            name: value / 1_000_000_000
            for name, value in profile.items()
            if name not in {"worker_count", "document_count"}
        }
        return index, {
            "binding": binding,
            "native_decode": native_decode,
            "native_index": native_index,
            "boundary_residual": max(0.0, binding - native_decode - native_index),
            "query_workload": query,
            **{f"native_stage_{name}": value for name, value in stage_values.items()},
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
        candidate_name="native-fact-query-index",
    )

    return {
        "schema_version": 1,
        "benchmark": "native_fact_query_index_v1",
        "timer": "time.perf_counter",
        "subject": {
            "index_path": str(index_path),
            "index_bytes": index_path.stat().st_size,
            "index_sha256": _sha256(index_path),
            "language": language,
            "project_root": root,
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
            "fact_query_index_contract": contract,
        },
        "capabilities": dict(contract["capabilities"]),
        "parity": {"passed": parity, "error": parity_error},
        "raw_samples": {
            "legacy_code_graph": legacy_samples,
            "native_fact_query_index": native_samples,
        },
        "legacy_code_graph": legacy_summary,
        "native_fact_query_index": native_summary,
        "decision": decision,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--project-root", type=Path)
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
    report = profile_fact_query_index(
        index_path=args.index,
        language=args.language,
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
