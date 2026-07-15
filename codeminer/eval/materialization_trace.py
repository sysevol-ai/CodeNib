# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Replay a fixed mixed-operation trace over one materialized manifest."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from codeminer.agent.boundary import from_agent_repr
from codeminer.compiler.manifest import RepoManifest
from codeminer.eval.agent_runner.lsp_provider_validation import (
    fingerprint_lsp_start_location_set,
    fingerprint_lsp_start_locations,
)
from codeminer.mcp.context import ServerContext
from codeminer.mcp.tools.lsp import (
    lsp_definition_impl,
    lsp_references_impl,
    lsp_route_impl,
)
from codeminer.mcp.tools.search import (
    search_bm25_impl,
    search_regex_impl,
    search_semantic,
)

_OPERATIONS = {
    "dense",
    "bm25",
    "regex",
    "definition",
    "references",
    "route",
}
_LSP_RESULT_CONTRACTS = {
    "ordered_file_start_line": "definition",
    "unordered_file_start_line_set": "references",
}


@dataclass(frozen=True, slots=True)
class TraceRequest:
    """One agent-visible repository operation with fixed parameters."""

    request_id: str
    operation: str
    params: Mapping[str, Any]
    expected_result: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TraceRequest:
        request_id = str(payload.get("request_id") or "").strip()
        operation = str(payload.get("operation") or "").strip().lower()
        params = payload.get("params")
        expected_result = payload.get("expected_result")
        if not request_id:
            raise ValueError("trace request is missing request_id")
        if operation not in _OPERATIONS:
            raise ValueError(f"unsupported trace operation {operation!r}")
        if not isinstance(params, Mapping):
            raise ValueError(f"trace request {request_id!r} has invalid params")
        if expected_result is not None:
            if not isinstance(expected_result, Mapping):
                raise ValueError(
                    f"trace request {request_id!r} has invalid expected_result"
                )
            _validate_expected_result(request_id, operation, expected_result)
        return cls(
            request_id=request_id,
            operation=operation,
            params=dict(params),
            expected_result=(
                dict(expected_result) if expected_result is not None else None
            ),
        )


def load_trace(path: str | Path) -> list[TraceRequest]:
    """Load a JSON array or JSONL request trace."""

    trace_path = Path(path)
    if trace_path.suffix == ".jsonl":
        payloads = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        payloads = json.loads(trace_path.read_text(encoding="utf-8"))
    if not isinstance(payloads, list):
        raise ValueError("trace must be a JSON array or JSONL records")
    requests = [TraceRequest.from_dict(payload) for payload in payloads]
    _validate_trace(requests)
    return requests


async def replay_materialized_trace(
    manifest_path: str | Path,
    requests: Iterable[TraceRequest],
    *,
    warmup_repetitions: int = 1,
    measured_repetitions: int = 5,
    reuse_dense_query_embedding: bool = False,
    context_loader: Callable[[str | Path], Any] = ServerContext.load,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, Any]:
    """Load one manifest and replay a deterministic mixed-operation trace."""

    trace = list(requests)
    _validate_trace(trace)
    if warmup_repetitions < 0 or measured_repetitions <= 0:
        raise ValueError("repetition counts must be nonnegative, with measurements > 0")

    manifest = RepoManifest.load(manifest_path)
    load_start = clock_ns()
    context = context_loader(manifest_path)
    load_seconds = (clock_ns() - load_start) / 1e9
    _validate_context(context, trace)

    for _ in range(warmup_repetitions):
        for request in trace:
            _assert_response(
                request,
                await _execute(
                    context,
                    request,
                    reuse_dense_query_embedding=reuse_dense_query_embedding,
                ),
            )

    rows = []
    fingerprints: dict[str, set[str]] = {request.request_id: set() for request in trace}
    for repetition in range(measured_repetitions):
        for request in trace:
            start = clock_ns()
            response = await _execute(
                context,
                request,
                reuse_dense_query_embedding=reuse_dense_query_embedding,
            )
            elapsed_seconds = (clock_ns() - start) / 1e9
            contract_fingerprint = _assert_response(request, response)
            fingerprint = response_fingerprint(response)
            fingerprints[request.request_id].add(fingerprint)
            row = {
                "request_id": request.request_id,
                "operation": request.operation,
                "repetition": repetition,
                "elapsed_seconds": elapsed_seconds,
                "response_fingerprint": fingerprint,
                "result_count": _result_count(response),
            }
            if contract_fingerprint is not None:
                row["contract_fingerprint"] = contract_fingerprint
            rows.append(row)

    unstable = sorted(key for key, values in fingerprints.items() if len(values) != 1)
    if unstable:
        raise ValueError(
            "response fingerprint changed across repetitions for " + ", ".join(unstable)
        )

    request_seconds = sum(row["elapsed_seconds"] for row in rows)
    return {
        "schema_version": 1,
        "manifest": {
            "path": str(Path(manifest_path).resolve()),
            "repository": manifest.repo_path,
            "commit": manifest.commit,
            "languages": list(manifest.languages),
            "capabilities": dict(manifest.capabilities),
        },
        "protocol": {
            "warmup_repetitions": warmup_repetitions,
            "measured_repetitions": measured_repetitions,
            "request_count": len(trace),
            "output_guard": "canonical JSON SHA-256 stable across repetitions",
            "semantic_guarded_request_count": sum(
                request.expected_result is not None for request in trace
            ),
            "semantic_guard": (
                "pre-registered provider-equivalent file/start-line fingerprint"
            ),
            "timing_boundary": "manifest hydration and MCP operation execution",
            "dense_query_cache": (
                "retained across requests"
                if reuse_dense_query_embedding
                else "cleared before each independent dense request"
            ),
        },
        "phases": {
            "load_seconds": load_seconds,
            "measured_request_seconds": request_seconds,
        },
        "operations": _operation_summary(rows),
        "rows": rows,
    }


def attach_materialization_cost(
    replay_report: Mapping[str, Any],
    *,
    materialization_seconds: float,
    per_index_seconds: Mapping[str, float],
    artifact_bytes: int,
    consumer_counts: Sequence[int] = (1, 2, 5, 10, 20, 50, 100),
) -> dict[str, Any]:
    """Attach explicit build/load/service costs and two deployment projections."""

    if not math.isfinite(materialization_seconds) or materialization_seconds < 0:
        raise ValueError("materialization_seconds must be finite and nonnegative")
    counts = sorted({int(value) for value in consumer_counts})
    if not counts or counts[0] <= 0:
        raise ValueError("consumer_counts must contain positive integers")
    request_count = int(replay_report["protocol"]["request_count"])
    if request_count <= 0:
        raise ValueError("replay report has no requests")
    request_seconds = float(replay_report["phases"]["measured_request_seconds"])
    repetitions = int(replay_report["protocol"]["measured_repetitions"])
    one_trace_seconds = request_seconds / repetitions
    load_seconds = float(replay_report["phases"]["load_seconds"])

    report = dict(replay_report)
    report["materialization"] = {
        "wall_seconds": materialization_seconds,
        "per_index_seconds": {
            key: float(value) for key, value in sorted(per_index_seconds.items())
        },
        "artifact_bytes": int(artifact_bytes),
        "accounting_boundary": (
            "materialization, manifest hydration, and one mixed trace are measured "
            "separately"
        ),
    }
    report["trace_repetition_curve"] = [
        {
            "trace_repetitions": count,
            "request_count": count * request_count,
            "materialization_seconds_per_trace": materialization_seconds / count,
            "materialization_seconds_per_request": (
                materialization_seconds / (count * request_count)
            ),
            "deployment_models": {
                "artifact_shared_process_isolated": {
                    "projected_total_seconds": (
                        materialization_seconds
                        + count * load_seconds
                        + count * one_trace_seconds
                    ),
                    "load_seconds_per_trace": load_seconds,
                },
                "shared_resident_runtime": {
                    "projected_total_seconds": (
                        materialization_seconds
                        + load_seconds
                        + count * one_trace_seconds
                    ),
                    "load_seconds_per_trace": load_seconds / count,
                },
            },
        }
        for count in counts
    ]
    return report


def response_fingerprint(response: Any) -> str:
    """Hash the complete JSON-visible response for replay stability."""

    canonical = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _execute(
    context: Any,
    request: TraceRequest,
    *,
    reuse_dense_query_embedding: bool,
) -> Any:
    params = dict(request.params)
    if request.operation == "dense":
        if not reuse_dense_query_embedding:
            context.vector.clear_query_cache()
        return await search_semantic(context, **params)
    if request.operation == "bm25":
        return await asyncio.to_thread(search_bm25_impl, context, **params)
    if request.operation == "regex":
        return await asyncio.to_thread(search_regex_impl, context, **params)
    if request.operation == "definition":
        return await asyncio.to_thread(lsp_definition_impl, context, **params)
    if request.operation == "references":
        return await asyncio.to_thread(lsp_references_impl, context, **params)
    return await asyncio.to_thread(lsp_route_impl, context, **params)


def _validate_trace(requests: Sequence[TraceRequest]) -> None:
    if not requests:
        raise ValueError("trace has no requests")
    ids = [request.request_id for request in requests]
    duplicates = sorted(key for key, count in _counts(ids).items() if count > 1)
    if duplicates:
        raise ValueError("duplicate trace request ids: " + ", ".join(duplicates))


def _validate_context(context: Any, requests: Sequence[TraceRequest]) -> None:
    requirements = {
        "dense": "vector",
        "bm25": "bm25",
        "regex": "regex_index",
        "definition": "symbol_graph",
        "references": "symbol_graph",
        "route": "symbol_graph",
    }
    missing = sorted(
        {
            requirements[request.operation]
            for request in requests
            if getattr(context, requirements[request.operation], None) is None
        }
    )
    if missing:
        errors = getattr(context, "errors", {}) or {}
        details = "; ".join(
            f"{key}: {errors.get(key, 'not loaded')}" for key in missing
        )
        raise ValueError(f"manifest cannot serve trace: {details}")


def _assert_response(request: TraceRequest, response: Any) -> str | None:
    if isinstance(response, Mapping) and "error" in response:
        raise ValueError(f"request {request.request_id!r} failed: {response['error']}")
    response_fingerprint(response)
    if request.expected_result is None:
        return None

    expected_count = int(request.expected_result["result_count"])
    actual_count = _result_count(response)
    if actual_count != expected_count:
        raise ValueError(
            f"request {request.request_id!r} returned {actual_count} results; "
            f"expected {expected_count}"
        )
    actual_fingerprint = _contract_fingerprint(request, response)
    expected_fingerprint = str(request.expected_result["fingerprint"])
    if actual_fingerprint != expected_fingerprint:
        raise ValueError(
            f"request {request.request_id!r} violated its pre-registered "
            "file/start-line result contract"
        )
    return actual_fingerprint


def _contract_fingerprint(request: TraceRequest, response: Any) -> str:
    if not isinstance(response, Sequence) or isinstance(response, (str, bytes)):
        raise ValueError(
            f"request {request.request_id!r} did not return a result sequence"
        )
    nodes = []
    for item in response:
        if not isinstance(item, Mapping):
            raise ValueError(
                f"request {request.request_id!r} returned a non-location result"
            )
        node = dict(item)
        line = node.get("start_line")
        if isinstance(line, int) and not isinstance(line, bool):
            node["start_line"] = from_agent_repr(line)
        nodes.append(node)

    contract = str(request.expected_result["contract"])
    if contract == "ordered_file_start_line":
        return fingerprint_lsp_start_locations(nodes)
    if contract == "unordered_file_start_line_set":
        return fingerprint_lsp_start_location_set(nodes)
    raise ValueError(f"unsupported result contract {contract!r}")


def _validate_expected_result(
    request_id: str,
    operation: str,
    expected_result: Mapping[str, Any],
) -> None:
    contract = str(expected_result.get("contract") or "")
    expected_operation = _LSP_RESULT_CONTRACTS.get(contract)
    if expected_operation != operation:
        raise ValueError(
            f"trace request {request_id!r} has result contract {contract!r} "
            f"incompatible with operation {operation!r}"
        )
    fingerprint = str(expected_result.get("fingerprint") or "")
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise ValueError(
            f"trace request {request_id!r} has invalid expected fingerprint"
        )
    result_count = expected_result.get("result_count")
    if (
        not isinstance(result_count, int)
        or isinstance(result_count, bool)
        or result_count <= 0
    ):
        raise ValueError(
            f"trace request {request_id!r} has invalid expected result count"
        )


def _result_count(response: Any) -> int:
    if isinstance(response, Sequence) and not isinstance(response, (str, bytes)):
        return len(response)
    return 1


def _operation_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_operation: dict[str, list[float]] = {}
    for row in rows:
        by_operation.setdefault(str(row["operation"]), []).append(
            float(row["elapsed_seconds"])
        )
    return [
        {
            "operation": operation,
            "measurements": len(values),
            "mean_seconds": statistics.fmean(values),
            "p50_seconds": statistics.median(values),
            "p95_seconds": _percentile(values, 0.95),
        }
        for operation, values in sorted(by_operation.items())
    ]


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _counts(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts
