#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark legacy pybind objects against FactBatchBuffer v1.

The benchmark deliberately alternates arm order. It reports native SCIP decode
stages, FactBatch encoding, boundary-copy residual, Python validation, and
igraph materialization separately, then applies an explicit end-to-end
promotion threshold. An optional external-index duration folds the unchanged
SCIP tool cost into the same Amdahl-style decision.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CORE_BUILD = _PROJECT_ROOT / "build/core"
for candidate in (_CORE_BUILD, _PROJECT_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

DEFAULT_THRESHOLD = 0.20
DEFAULT_ITERATIONS = 5
DEFAULT_WARMUPS = 1
REPORT_SCHEMA_VERSION = 1


def summarize_samples(values: Sequence[float]) -> dict[str, float | int]:
    """Return stable distribution fields for a non-empty timing sample."""

    if not values:
        raise ValueError("at least one timing sample is required")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) or value < 0 for value in ordered):
        raise ValueError("timing samples must be finite and non-negative")
    p95_index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) - 1e-12)))
    return {
        "samples": len(ordered),
        "min_seconds": ordered[0],
        "median_seconds": statistics.median(ordered),
        "p95_seconds": ordered[p95_index],
        "max_seconds": ordered[-1],
    }


def acceleration_decision(
    *,
    legacy_local_seconds: float,
    candidate_local_seconds: float,
    threshold: float = DEFAULT_THRESHOLD,
    external_index_seconds: float | None = None,
    parity: bool = True,
    candidate_name: str = "fact-buffer",
) -> dict[str, Any]:
    """Apply the promotion gate to local and optional cold-start timings."""

    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("threshold must be between 0 and 1")
    for name, value in (
        ("legacy_local_seconds", legacy_local_seconds),
        ("candidate_local_seconds", candidate_local_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if external_index_seconds is not None and (
        not math.isfinite(external_index_seconds) or external_index_seconds < 0
    ):
        raise ValueError("external_index_seconds must be finite and non-negative")
    if not candidate_name or not candidate_name.strip():
        raise ValueError("candidate_name must be non-empty")

    local_improvement = (
        legacy_local_seconds - candidate_local_seconds
    ) / legacy_local_seconds
    result: dict[str, Any] = {
        "threshold": threshold,
        "parity": parity,
        "legacy_local_seconds": legacy_local_seconds,
        "candidate_local_seconds": candidate_local_seconds,
        "local_improvement_fraction": local_improvement,
    }
    gate_improvement = local_improvement
    gate_name = "local_decode_end_to_end"
    if external_index_seconds is not None:
        legacy_cold = external_index_seconds + legacy_local_seconds
        candidate_cold = external_index_seconds + candidate_local_seconds
        cold_improvement = (legacy_cold - candidate_cold) / legacy_cold
        result.update(
            {
                "external_index_seconds": external_index_seconds,
                "legacy_cold_start_seconds": legacy_cold,
                "candidate_cold_start_seconds": candidate_cold,
                "cold_start_improvement_fraction": cold_improvement,
            }
        )
        gate_improvement = cold_improvement
        gate_name = "cold_start_end_to_end"
    promoted = parity and gate_improvement >= threshold
    result.update(
        {
            "gate": gate_name,
            "gate_improvement_fraction": gate_improvement,
            "promoted": promoted,
            "recommendation": (
                f"promote-{candidate_name}" if promoted else "keep-experimental"
            ),
            "reason": (
                "semantic parity and end-to-end threshold passed"
                if promoted
                else (
                    "semantic parity failed"
                    if not parity
                    else "end-to-end improvement is below threshold"
                )
            ),
        }
    )
    return result


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


def _run_arm(call: Callable[[], Any]) -> Any:
    """Collect cyclic garbage outside the timed region, then run one arm."""

    gc.collect()
    return call()


def _assert_graph_parity(reference: Any, candidate: Any) -> None:
    vertex_fields = (
        "name",
        "type",
        "file",
        "start_line",
        "end_line",
        "selection_line",
        "unified_name",
        "symbol_kind",
        "has_definition",
    )
    edge_fields = ("type", "anchor_file", "anchor_line")
    if reference.project_root != candidate.project_root:
        raise AssertionError(
            f"project root differs: {reference.project_root!r} != "
            f"{candidate.project_root!r}"
        )
    if reference.graph.vcount() != candidate.graph.vcount():
        raise AssertionError(
            f"vertex count differs: {reference.graph.vcount()} != "
            f"{candidate.graph.vcount()}"
        )
    if reference.graph.ecount() != candidate.graph.ecount():
        raise AssertionError(
            f"edge count differs: {reference.graph.ecount()} != "
            f"{candidate.graph.ecount()}"
        )
    for index, (left, right) in enumerate(
        zip(reference.graph.vs, candidate.graph.vs, strict=True)
    ):
        left_row = tuple(left.attributes().get(name) for name in vertex_fields)
        right_row = tuple(right.attributes().get(name) for name in vertex_fields)
        if left_row != right_row:
            raise AssertionError(
                f"vertex {index} differs: {left_row!r} != {right_row!r}"
            )
    for index, (left, right) in enumerate(
        zip(reference.graph.es, candidate.graph.es, strict=True)
    ):
        left_row = (
            left.source,
            left.target,
            *(left.attributes().get(name) for name in edge_fields),
        )
        right_row = (
            right.source,
            right.target,
            *(right.attributes().get(name) for name in edge_fields),
        )
        if left_row != right_row:
            raise AssertionError(f"edge {index} differs: {left_row!r} != {right_row!r}")
    if reference.name_to_vertex != candidate.name_to_vertex:
        raise AssertionError("name_to_vertex indexes differ")
    if reference.symbol_ranges != candidate.symbol_ranges:
        raise AssertionError("symbol_ranges indexes differ")


def _fact_file_paths(graph: Any) -> set[str]:
    attributes = [vertex.attributes() for vertex in graph.graph.vs]
    file_paths = {str(item["file"]) for item in attributes if item.get("file")}
    file_paths.update(
        str(item["name"]) for item in attributes if item.get("type") == "file"
    )
    file_paths.update(
        str(edge.attributes()["anchor_file"])
        for edge in graph.graph.es
        if edge.attributes().get("anchor_file")
    )
    return file_paths


def _append_sample(samples: dict[str, list[float]], name: str, value: float) -> None:
    samples.setdefault(name, []).append(float(value))


def profile_fact_batch_buffer(
    *,
    index_path: Path,
    language: str,
    project_root: Path | None = None,
    iterations: int = DEFAULT_ITERATIONS,
    warmups: int = DEFAULT_WARMUPS,
    threshold: float = DEFAULT_THRESHOLD,
    external_index_seconds: float | None = None,
    include_semantic_consumer: bool = False,
) -> dict[str, Any]:
    """Run alternating legacy/buffer arms and return a JSON-ready report."""

    if iterations < 1 or warmups < 0:
        raise ValueError("iterations must be positive and warmups non-negative")
    index_path = index_path.resolve()
    if not index_path.is_file():
        raise FileNotFoundError(index_path)
    root = str(project_root.resolve()) if project_root is not None else None

    import codenib_core

    from codenib.scip_interface.fact_batch_buffer import (
        FactBatchBufferView,
        validate_compiled_contract,
    )
    from codenib.scip_interface.scip_decode_core import _build_code_graph

    validate_compiled_contract(codenib_core.fact_batch_buffer_contract())
    profile_digest = (
        "sha256:"
        + hashlib.sha256(f"fact-buffer-profile:{language}".encode("utf-8")).hexdigest()
    )

    def legacy_arm() -> tuple[Any, dict[str, float]]:
        result, binding = _timed(
            lambda: codenib_core.decode_scip(
                index_file=str(index_path), project_root=root, language=language
            )
        )
        graph, materialize = _timed(
            lambda: _build_code_graph(
                result["vertices"], result["edges"], project_root=root
            )
        )
        return graph, {
            "binding": binding,
            "materialize": materialize,
            "total": binding + materialize,
        }

    def buffer_arm() -> tuple[Any, dict[str, float], int]:
        payload, binding = _timed(
            lambda: codenib_core.decode_scip_fact_buffer(
                index_file=str(index_path),
                profile_digest=profile_digest,
                project_root=root,
                language=language,
                copy_buffers=False,
            )
        )
        view, validation = _timed(lambda: FactBatchBufferView(payload))

        def materialize_graph():
            graph = view.materialize_code_graph(project_root=root)
            graph.project_root = root
            return graph

        graph, materialize = _timed(materialize_graph)
        native_decode = (view.meta.native_decode_ns or 0) / 1_000_000_000
        native_encode = (view.meta.native_encode_ns or 0) / 1_000_000_000
        stage_values = {
            name: value / 1_000_000_000
            for name, value in (view.meta.decode_profile_ns or {}).items()
            if name not in {"worker_count", "document_count"}
        }
        timings = {
            "binding": binding,
            "native_decode_outer": native_decode,
            "native_encode": native_encode,
            "boundary_transport_residual": max(
                0.0, binding - native_decode - native_encode
            ),
            "validation": validation,
            "materialize": materialize,
            "total": binding + validation + materialize,
            **{f"native_stage_{name}": value for name, value in stage_values.items()},
        }
        buffer_bytes = sum(
            len(payload[name])
            for name in (
                "meta",
                "batches",
                "symbols",
                "occurrences",
                "edges",
                "diagnostics",
                "graph_vertices",
                "graph_edges",
                "arena",
            )
        )
        return graph, timings, buffer_bytes

    for _ in range(warmups):
        _run_arm(legacy_arm)
        _run_arm(buffer_arm)

    legacy_samples: dict[str, list[float]] = {}
    buffer_samples: dict[str, list[float]] = {}
    parity = True
    parity_error = None
    buffer_sizes: list[int] = []
    semantic_file_paths: set[str] = set()
    for iteration in range(iterations):
        if iteration % 2 == 0:
            legacy_graph, legacy_timing = _run_arm(legacy_arm)
            buffer_graph, buffer_timing, buffer_size = _run_arm(buffer_arm)
        else:
            buffer_graph, buffer_timing, buffer_size = _run_arm(buffer_arm)
            legacy_graph, legacy_timing = _run_arm(legacy_arm)
        if parity:
            try:
                _assert_graph_parity(legacy_graph, buffer_graph)
            except AssertionError as exc:
                parity = False
                parity_error = str(exc)
        for name, value in legacy_timing.items():
            _append_sample(legacy_samples, name, value)
        for name, value in buffer_timing.items():
            _append_sample(buffer_samples, name, value)
        buffer_sizes.append(buffer_size)
        if include_semantic_consumer and not semantic_file_paths:
            semantic_file_paths = _fact_file_paths(legacy_graph)
        del legacy_graph, buffer_graph

    legacy_summary = {
        name: summarize_samples(values) for name, values in legacy_samples.items()
    }
    buffer_summary = {
        name: summarize_samples(values) for name, values in buffer_samples.items()
    }
    decision = acceleration_decision(
        legacy_local_seconds=legacy_summary["total"]["median_seconds"],
        candidate_local_seconds=buffer_summary["total"]["median_seconds"],
        threshold=threshold,
        external_index_seconds=external_index_seconds,
        parity=parity,
    )
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark": "fact_batch_buffer_v1",
        "subject": {
            "index_path": str(index_path),
            "index_bytes": index_path.stat().st_size,
            "index_sha256": _sha256(index_path),
            "language": language,
            "project_root": root,
        },
        "configuration": {
            "iterations": iterations,
            "warmups": warmups,
            "threshold": threshold,
            "alternating_arm_order": True,
            "copy_buffers": False,
            "include_semantic_consumer": include_semantic_consumer,
            "promotion_metric": "median_total_seconds",
            "timer": "time.perf_counter",
            "garbage_collection": "disabled-during-timing; collected-before-arms",
            "first_arm_by_iteration": [
                "legacy" if iteration % 2 == 0 else "fact_buffer"
                for iteration in range(iterations)
            ],
            "fact_batch_buffer_contract": dict(
                codenib_core.fact_batch_buffer_contract()
            ),
        },
        "parity": {"passed": parity, "error": parity_error},
        "raw_samples": {
            "legacy": legacy_samples,
            "fact_buffer": buffer_samples,
        },
        "legacy": legacy_summary,
        "fact_buffer": {
            **buffer_summary,
            "wire_bytes": {
                "min": min(buffer_sizes),
                "median": statistics.median(buffer_sizes),
                "max": max(buffer_sizes),
            },
        },
        "decision": decision,
    }

    if include_semantic_consumer:
        from codenib.facts import fact_batches_from_code_graph, sha256_digest

        content_digests = {
            path: sha256_digest(f"benchmark:{path}") for path in semantic_file_paths
        }

        def legacy_fact_arm() -> tuple[Any, dict[str, float]]:
            graph, graph_timing = legacy_arm()
            facts, adapt = _timed(
                lambda: fact_batches_from_code_graph(
                    graph,
                    language=language,
                    content_digests=content_digests,
                    profile_digest=profile_digest,
                    provider="scip-core",
                )
            )
            return facts, {
                "graph_ready": graph_timing["total"],
                "fact_materialize": adapt,
                "total": graph_timing["total"] + adapt,
            }

        def native_fact_arm() -> tuple[Any, dict[str, float]]:
            payload, binding = _timed(
                lambda: codenib_core.decode_scip_fact_buffer(
                    index_file=str(index_path),
                    profile_digest=profile_digest,
                    project_root=root,
                    language=language,
                    content_digests=content_digests,
                    include_graph_compat=False,
                    copy_buffers=False,
                )
            )
            view, validation = _timed(lambda: FactBatchBufferView(payload))
            facts, materialize = _timed(
                lambda: view.to_fact_batches(content_digests=content_digests)
            )
            native_decode = (view.meta.native_decode_ns or 0) / 1_000_000_000
            native_encode = (view.meta.native_encode_ns or 0) / 1_000_000_000
            return facts, {
                "binding": binding,
                "native_decode": native_decode,
                "native_encode": native_encode,
                "boundary_residual": max(0.0, binding - native_decode - native_encode),
                "validation": validation,
                "fact_materialize": materialize,
                "total": binding + validation + materialize,
            }

        for _ in range(warmups):
            _run_arm(legacy_fact_arm)
            _run_arm(native_fact_arm)

        legacy_fact_samples: dict[str, list[float]] = {}
        native_fact_samples: dict[str, list[float]] = {}
        semantic_parity = True
        semantic_error = None
        semantic_batch_count = 0
        for iteration in range(iterations):
            if iteration % 2 == 0:
                legacy_facts, legacy_fact_timing = _run_arm(legacy_fact_arm)
                native_facts, native_fact_timing = _run_arm(native_fact_arm)
            else:
                native_facts, native_fact_timing = _run_arm(native_fact_arm)
                legacy_facts, legacy_fact_timing = _run_arm(legacy_fact_arm)
            if semantic_parity and native_facts != legacy_facts:
                semantic_parity = False
                semantic_error = "logical FactBatch tuples differ"
            semantic_batch_count = len(legacy_facts)
            for name, value in legacy_fact_timing.items():
                _append_sample(legacy_fact_samples, name, value)
            for name, value in native_fact_timing.items():
                _append_sample(native_fact_samples, name, value)
            del legacy_facts, native_facts

        legacy_fact_summary = {
            name: summarize_samples(values)
            for name, values in legacy_fact_samples.items()
        }
        native_fact_summary = {
            name: summarize_samples(values)
            for name, values in native_fact_samples.items()
        }
        semantic_decision = acceleration_decision(
            legacy_local_seconds=legacy_fact_summary["total"]["median_seconds"],
            candidate_local_seconds=native_fact_summary["total"]["median_seconds"],
            threshold=threshold,
            external_index_seconds=external_index_seconds,
            parity=semantic_parity,
            candidate_name="native-fact-batches",
        )
        report["semantic_fact_consumer"] = {
            "content_digest_source": "synthetic-path-digests-for-timing",
            "batch_count": semantic_batch_count,
            "parity": {"passed": semantic_parity, "error": semantic_error},
            "raw_samples": {
                "legacy_graph_adapter": legacy_fact_samples,
                "native_fact_buffer": native_fact_samples,
            },
            "legacy_graph_adapter": legacy_fact_summary,
            "native_fact_buffer": native_fact_summary,
            "decision": semantic_decision,
        }

    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--external-index-seconds", type=float)
    parser.add_argument("--include-semantic-consumer", action="store_true")
    parser.add_argument("--output-json", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = profile_fact_batch_buffer(
        index_path=args.index,
        language=args.language,
        project_root=args.project_root,
        iterations=args.iterations,
        warmups=args.warmups,
        threshold=args.threshold,
        external_index_seconds=args.external_index_seconds,
        include_semantic_consumer=args.include_semantic_consumer,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    parity_passed = report["parity"]["passed"]
    semantic = report.get("semantic_fact_consumer")
    if semantic is not None:
        parity_passed = parity_passed and semantic["parity"]["passed"]
    return 0 if parity_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
