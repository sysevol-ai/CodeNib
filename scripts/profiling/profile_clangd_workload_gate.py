#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Gate native clangd serving across isolated mixed workloads.

Every measured legacy/native arm runs in a fresh Python process. clangd index
generation stays outside query-ready timing; the report alternates arm order,
binds every run to one immutable content receipt, and records exact MCP result
and public-error parity together with wall, CPU, RSS, provider, and stage data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CORE_BUILD = _PROJECT_ROOT / "build/core"
_DEFAULT_SUBJECT_MANIFEST = Path(__file__).with_name("clangd_workload_subjects.json")
for candidate in (_CORE_BUILD, _PROJECT_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts.profiling.profile_fact_batch_buffer import (  # noqa: E402
    DEFAULT_ITERATIONS,
    DEFAULT_WARMUPS,
)
from scripts.profiling.profile_fact_query_index import (  # noqa: E402
    DEFAULT_QUERY_WORKLOAD_SIZE,
    _definition_symbols,
    _even_sample,
)

WORKLOADS = ("symbol_only", "position_first", "route_first", "mixed")
DEFAULT_SYMBOL_THRESHOLD = 0.20
DEFAULT_POSITION_THRESHOLD = 0.20
DEFAULT_ROUTE_THRESHOLD = 0.20
DEFAULT_GRAPH_NON_REGRESSION_BUDGET = 0.20
DEFAULT_PEAK_RSS_RATIO_BUDGET = 1.25
DEFAULT_REPEAT_RSS_SPREAD_BUDGET = 0.10
DEFAULT_MAX_NATIVE_PEAK_RSS_MB = 4096.0
DEFAULT_CONCURRENCY_WORKERS = 8
DEFAULT_WORKER_TIMEOUT = 900.0


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


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _percentile_95(ordered: Sequence[float]) -> float:
    index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) - 1e-12)))
    return ordered[index]


def _summarize(values: Sequence[float | int]) -> dict[str, float | int]:
    if not values:
        raise ValueError("at least one numeric sample is required")
    ordered = sorted(float(value) for value in values)
    return {
        "samples": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "p95": _percentile_95(ordered),
        "max": ordered[-1],
    }


def _peak_rss_bytes() -> int | None:
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        return None
    return peak if sys.platform == "darwin" else peak * 1024


def _current_rss_bytes() -> int | None:
    try:
        fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
        return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    except (IndexError, OSError, ValueError):
        return None


def _canonical_tool_result(result: Any) -> Any:
    if isinstance(result, dict):
        return {str(key): value for key, value in sorted(result.items())}
    normalized = []
    for item in result:
        row = dict(item) if isinstance(item, Mapping) else {"node": str(item)}
        row.pop("lsp_provider", None)
        normalized.append(row)
    return normalized


def _provider_metadata(result: Any) -> list[Mapping[str, Any]]:
    metadata = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, Mapping) and isinstance(
                item.get("lsp_provider"), Mapping
            ):
                metadata.append(item["lsp_provider"])
    return metadata


def _run_mcp_workload(
    provider: Any,
    *,
    workload: str,
    symbols: Sequence[str],
    positions: Sequence[Mapping[str, Any]],
    route_symbols: Sequence[str],
    route_queries: Sequence[str],
) -> dict[str, Any]:
    from codenib.mcp.tools.lsp import (
        lsp_definition_impl,
        lsp_references_impl,
        lsp_route_impl,
    )

    if workload not in WORKLOADS:
        raise ValueError(f"unknown workload: {workload}")
    context = SimpleNamespace(lsp_provider=provider, symbol_graph=None)
    digest = hashlib.sha256()
    calls = 0
    results = 0
    errors = 0
    serialized_bytes = 0
    observed_backends: set[str] = set()
    observed_fallbacks: set[str] = set()

    def record(operation: str, result: Any) -> None:
        nonlocal calls, results, errors, serialized_bytes
        for metadata in _provider_metadata(result):
            if metadata.get("backend"):
                observed_backends.add(str(metadata["backend"]))
            if metadata.get("fallback_reason"):
                observed_fallbacks.add(str(metadata["fallback_reason"]))
        canonical = {"operation": operation, "result": _canonical_tool_result(result)}
        encoded = json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)
        serialized_bytes += len(encoded)
        calls += 1
        if isinstance(result, dict) and "error" in result:
            errors += 1
        elif isinstance(result, list):
            results += len(result)

    def symbol_calls(selected: Sequence[str]) -> None:
        for symbol in selected:
            record(
                "definition_by_symbol",
                lsp_definition_impl(context, symbol=symbol, top_k=100),
            )
            record(
                "references_by_symbol",
                lsp_references_impl(context, symbol=symbol, top_k=100),
            )

    def position_calls() -> None:
        for position in positions:
            arguments = {
                "file_path": str(position["file_path"]),
                "line": int(position["line"]),
                "top_k": 100,
            }
            if position.get("character") is not None:
                arguments["character"] = int(position["character"])
            record(
                "definition_by_position",
                lsp_definition_impl(context, **arguments),
            )
            record(
                "references_by_position",
                lsp_references_impl(context, **arguments),
            )

    def route_call() -> None:
        record(
            "route_by_symbol",
            lsp_route_impl(
                context,
                symbols=list(route_symbols),
                query="clangd mixed workload gate",
                top_k=100,
                include_neighbors=True,
            ),
        )
        for route_query in route_queries:
            record(
                "route_by_query",
                lsp_route_impl(
                    context,
                    symbols=[],
                    query=route_query,
                    top_k=100,
                    include_neighbors=True,
                ),
            )

    if workload == "symbol_only":
        symbol_calls(symbols)
    elif workload == "position_first":
        position_calls()
        symbol_calls(symbols)
    elif workload == "route_first":
        route_call()
        symbol_calls(symbols)
    else:
        midpoint = max(1, len(symbols) // 2)
        symbol_calls(symbols[:midpoint])
        position_calls()
        symbol_calls(symbols[midpoint:])
        route_call()

    return {
        "result_digest": f"sha256:{digest.hexdigest()}",
        "calls": calls,
        "results": results,
        "public_errors": errors,
        "serialized_bytes": serialized_bytes,
        "observed_backends": sorted(observed_backends),
        "observed_fallbacks": sorted(observed_fallbacks),
    }


def _run_concurrent_route_workload(
    provider: Any,
    *,
    route_symbols: Sequence[str],
    workers: int,
) -> dict[str, Any]:
    import threading

    from codenib.mcp.tools.lsp import lsp_route_impl

    if workers < 2:
        raise ValueError("concurrency_workers must be at least 2")
    start = threading.Barrier(workers)

    def route_once(_index: int) -> tuple[str, int, bool, list[str]]:
        context = SimpleNamespace(lsp_provider=provider, symbol_graph=None)
        start.wait()
        result = lsp_route_impl(
            context,
            symbols=list(route_symbols),
            query="concurrent first route",
            top_k=100,
            include_neighbors=True,
        )
        metadata = _provider_metadata(result)
        return (
            _json_digest(_canonical_tool_result(result)),
            len(result) if isinstance(result, list) else 0,
            isinstance(result, dict) and "error" in result,
            sorted({str(item["backend"]) for item in metadata if item.get("backend")}),
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        outcomes = list(executor.map(route_once, range(workers)))
    digests = [digest for digest, _count, _error, _backends in outcomes]
    return {
        "result_digest": _json_digest(outcomes),
        "calls": workers,
        "results": sum(count for _digest, count, _error, _backends in outcomes),
        "public_errors": sum(
            int(error) for _digest, _count, error, _backends in outcomes
        ),
        "serialized_bytes": 0,
        "observed_backends": sorted(
            {
                backend
                for _digest, _count, _error, backends in outcomes
                for backend in backends
            }
        ),
        "observed_fallbacks": [],
        "thread_result_digests": sorted(set(digests)),
        "thread_results_identical": len(set(digests)) == 1,
    }


def _capability_decisions(provider: Any) -> dict[str, Any]:
    decisions = {}
    for capability in ("definition", "references", "route"):
        decision = provider.can_serve(capability)
        decisions[capability] = (
            decision.to_dict() if hasattr(decision, "to_dict") else str(decision)
        )
    return decisions


def _worker(config: Mapping[str, Any]) -> dict[str, Any]:
    import codenib_core

    from codenib.agent.lsp_provider import StaticLSPProvider
    from codenib.ls_index.clangd_decode import ClangdGraphDecoder

    arm = str(config["arm"])
    workload = str(config["workload"])
    idx_directory = Path(str(config["idx_directory"])).resolve()
    project_root = Path(str(config["project_root"])).resolve()
    position_encoding = str(config["position_encoding"])
    receipt_before = dict(
        codenib_core.clangd_fact_query_snapshot(
            idx_directory=str(idx_directory),
            project_root=str(project_root),
            position_encoding=position_encoding,
        )
    )
    rss_start = _current_rss_bytes()
    peak_start = _peak_rss_bytes()
    cpu_started = time.process_time()
    wall_started = time.perf_counter()
    decoder = ClangdGraphDecoder(
        str(idx_directory),
        str(project_root),
        position_encoding=position_encoding,
    )
    stages: dict[str, float] = {}

    startup_started = time.perf_counter()
    if arm == "legacy":
        graph_started = time.perf_counter()
        graph = decoder.materialize_code_graph()
        stages["legacy_materialize_graph"] = time.perf_counter() - graph_started
        provider = StaticLSPProvider(
            graph,
            snapshot_id=receipt_before["snapshot_id"],
            backend="persisted-symbol-graph-v1",
        )
        backend = "persisted-symbol-graph-v1"
    elif arm == "native":
        provider = decoder.decode_native_query_provider()
        backend = provider.provider_backend
    else:
        raise ValueError(f"unknown benchmark arm: {arm}")
    startup_wall = time.perf_counter() - startup_started
    selection = {
        "provider": provider.provider,
        "backend": backend,
        "status": "ok",
        "index_snapshot": provider.snapshot_id,
        "fallback_reason": None,
        "capabilities": _capability_decisions(provider),
    }

    workload_started = time.perf_counter()
    if workload == "concurrent_route_first":
        outcome = _run_concurrent_route_workload(
            provider,
            route_symbols=config["route_symbols"],
            workers=int(config["concurrency_workers"]),
        )
    else:
        outcome = _run_mcp_workload(
            provider,
            workload=workload,
            symbols=config["symbols"],
            positions=config["positions"],
            route_symbols=config["route_symbols"],
            route_queries=config["route_queries"],
        )
    workload_wall = time.perf_counter() - workload_started
    wall_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started

    if arm == "native":
        count_fields = {
            "record_count",
            "definition_count",
            "reference_count",
            "occurrence_count",
            "snapshot_file_count",
            "snapshot_index_bytes",
            "native_file_count",
            "native_raw_symbol_count",
            "native_raw_reference_count",
            "native_definition_count",
            "native_reference_count",
            "native_occurrence_count",
            "native_decoded_record_count",
            "native_index_bytes",
            "native_decompressed_string_bytes",
            "native_materialized_string_bytes",
            "native_string_entry_count",
            "fact_query_native",
            "position_fact_query_native",
        }
        index_counts = {
            name: int(decoder.query_profile[name])
            for name in sorted(count_fields)
            if isinstance(decoder.query_profile.get(name), (int, float))
            and not isinstance(decoder.query_profile.get(name), bool)
        }
        for name, value in decoder.query_profile.items():
            if name in count_fields or name in {"snapshot_id"}:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                stage_name = name if name.startswith("native_") else f"native_{name}"
                stages[stage_name] = float(value)
        if getattr(provider, "graph_materialization_seconds", 0.0):
            stages["lazy_materialize_graph"] = float(
                provider.graph_materialization_seconds
            )
        if getattr(provider, "position_initialization_seconds", 0.0):
            stages["lazy_native_position_index"] = float(
                provider.position_initialization_seconds
            )
    else:
        index_counts = {}

    receipt_after = dict(
        codenib_core.clangd_fact_query_snapshot(
            idx_directory=str(idx_directory),
            project_root=str(project_root),
            position_encoding=position_encoding,
        )
    )
    peak_rss = _peak_rss_bytes()
    graph_materializations = int(getattr(provider, "graph_materialization_count", 0))
    return {
        "arm": arm,
        "workload": workload,
        "process_id": os.getpid(),
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "startup_wall_seconds": startup_wall,
        "workload_wall_seconds": workload_wall,
        "rss_start_bytes": rss_start,
        "peak_rss_start_bytes": peak_start,
        "peak_rss_bytes": peak_rss,
        "peak_rss_growth_bytes": (
            max(0, peak_rss - peak_start)
            if peak_rss is not None and peak_start is not None
            else None
        ),
        "provider_selection": selection,
        "fallback_reason": getattr(provider, "last_fallback_reason", None),
        "graph_materialization_count": graph_materializations,
        "graph_materialized": bool(decoder._graph_materialized),
        "range_indexes_built": bool(decoder._range_indexes_built),
        "stages_seconds": stages,
        "index_counts": index_counts,
        "receipt_before": receipt_before,
        "receipt_after": receipt_after,
        **outcome,
    }


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    paths = [str(_CORE_BUILD), str(_PROJECT_ROOT)]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment["CODENIB_NATIVE_CLANGD_FACT_QUERY_INDEX"] = "required"
    environment.pop("CODENIB_CORE_PROFILE", None)
    return environment


def _run_isolated_worker(
    config: Mapping[str, Any], *, timeout_seconds: float
) -> dict[str, Any]:
    process_started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        cwd=str(_PROJECT_ROOT),
        env=_worker_environment(),
        input=json.dumps(config, sort_keys=True),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    process_wall_seconds = time.perf_counter() - process_started
    if completed.returncode != 0:
        raise RuntimeError(
            f"isolated clangd worker failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "isolated clangd worker returned invalid JSON: "
            f"{completed.stdout[-1000:]!r}; stderr={completed.stderr[-1000:]!r}"
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError("isolated clangd worker returned a non-object result")
    result["process_wall_seconds"] = process_wall_seconds
    return result


def _riff_versions(idx_directory: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(idx_directory.glob("*.idx"), key=lambda item: item.name):
        data = path.read_bytes()
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"CdIx":
            raise ValueError(f"invalid clangd RIFF header: {path}")
        declared = struct.unpack_from("<I", data, 4)[0]
        if declared + 8 != len(data):
            raise ValueError(f"invalid clangd RIFF length: {path}")
        position = 12
        version = None
        while position < len(data):
            if position + 8 > len(data):
                raise ValueError(f"truncated clangd RIFF chunk: {path}")
            chunk_id = data[position : position + 4]
            size = struct.unpack_from("<I", data, position + 4)[0]
            start = position + 8
            end = start + size
            if end > len(data):
                raise ValueError(f"truncated clangd RIFF payload: {path}")
            if chunk_id == b"meta":
                if size != 4 or version is not None:
                    raise ValueError(f"invalid clangd RIFF meta chunk: {path}")
                version = struct.unpack_from("<I", data, start)[0]
            position = end + (size & 1)
        if version is None:
            raise ValueError(f"missing clangd RIFF meta chunk: {path}")
        key = str(version)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: int(item[0])))


def _git_revision(project_root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and len(revision) == 40 else None


def _git_dirty(project_root: Path) -> bool | None:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return bool(completed.stdout.strip()) if completed.returncode == 0 else None


def _git_remote(project_root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(project_root), "remote", "get-url", "origin"],
        text=True,
        capture_output=True,
        check=False,
    )
    remote = completed.stdout.strip()
    return remote if completed.returncode == 0 and remote else None


def _load_subject_manifest(
    path: Path, *, subject_id: str | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("clangd workload subject manifest must use schema_version 1")
    subjects = payload.get("subjects")
    if not isinstance(subjects, list) or len(subjects) < 3:
        raise ValueError(
            "clangd workload subject manifest requires at least 3 subjects"
        )
    ids = []
    observed_categories: set[str] = set()
    for subject in subjects:
        if not isinstance(subject, Mapping):
            raise ValueError("clangd workload subject entries must be objects")
        for field in ("id", "repository", "tag", "revision", "categories"):
            if field not in subject:
                raise ValueError(f"clangd workload subject is missing {field!r}")
        identifier = str(subject["id"])
        revision = str(subject["revision"])
        categories = subject["categories"]
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError(f"subject {identifier!r} revision is not a commit SHA")
        if not isinstance(categories, list) or not categories:
            raise ValueError(f"subject {identifier!r} categories must be non-empty")
        ids.append(identifier)
        observed_categories.update(str(category) for category in categories)
    if len(ids) != len(set(ids)):
        raise ValueError("clangd workload subject ids must be unique")
    required_categories = {
        "template-heavy",
        "macro-heavy",
        "header-heavy",
        "multi-target",
    }
    missing = sorted(required_categories - observed_categories)
    if missing:
        raise ValueError(
            "clangd workload subject manifest misses categories: " + ", ".join(missing)
        )
    selected = None
    if subject_id is not None:
        matches = [subject for subject in subjects if subject["id"] == subject_id]
        if len(matches) != 1:
            raise ValueError(f"subject id is absent or duplicated: {subject_id!r}")
        selected = dict(matches[0])
    return payload, selected


def _workload_inputs(
    idx_directory: Path,
    project_root: Path,
    *,
    query_workload_size: int,
) -> dict[str, Any]:
    from codenib.agent.lsp_graph import _terms
    from codenib.agent.lsp_provider import StaticLSPProvider
    from codenib.ls_index.clangd_decode import ClangdGraphDecoder
    from codenib.mcp.tools.lsp import lsp_definition_impl, lsp_references_impl

    graph = ClangdGraphDecoder(
        str(idx_directory), str(project_root)
    ).materialize_code_graph()
    all_symbols = _definition_symbols(graph)
    if not all_symbols:
        raise ValueError("benchmark index has no definition symbols")
    symbols = _even_sample(all_symbols, query_workload_size)
    legacy_context = SimpleNamespace(
        lsp_provider=StaticLSPProvider(graph), symbol_graph=graph
    )
    native_decoder = ClangdGraphDecoder(str(idx_directory), str(project_root))
    native_provider = native_decoder.decode_native_query_provider()
    native_context = SimpleNamespace(lsp_provider=native_provider, symbol_graph=None)
    candidate_positions = native_provider.position_samples(
        max(100, query_workload_size * 20)
    )
    positions: list[dict[str, Any]] = []
    seen_positions: set[tuple[str, int, int]] = set()
    for sample in candidate_positions:
        file_path = sample.get("file_path")
        start_line = sample.get("start_line")
        character = sample.get("start_character")
        if (
            not file_path
            or not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(character, int)
            or isinstance(character, bool)
        ):
            continue
        key = (str(file_path), start_line, character)
        if key in seen_positions:
            continue
        # MCP input lines are one-based; clangd occurrence rows are zero-based.
        position = {
            "file_path": key[0],
            "line": start_line + 1,
            "character": character,
        }
        legacy_results = [
            implementation(legacy_context, **position, top_k=100)
            for implementation in (lsp_definition_impl, lsp_references_impl)
        ]
        native_results = [
            implementation(native_context, **position, top_k=100)
            for implementation in (lsp_definition_impl, lsp_references_impl)
        ]
        if any(not isinstance(result, list) for result in legacy_results):
            continue
        if any(not isinstance(result, list) for result in native_results):
            continue
        if [_canonical_tool_result(result) for result in native_results] != [
            _canonical_tool_result(result) for result in legacy_results
        ]:
            continue
        native_backends = {
            str(metadata.get("backend"))
            for result in native_results
            for metadata in _provider_metadata(result)
            if metadata.get("backend")
        }
        if native_backends != {"native-clangd-fact-query-v1"}:
            continue
        seen_positions.add(key)
        positions.append(position)
        if len(positions) >= query_workload_size:
            break
    if not positions:
        raise ValueError("benchmark index has no graph-free native position sample")
    route_symbols = symbols[: min(4, len(symbols))]
    if not route_symbols:
        raise ValueError("benchmark index has no route seed")
    route_queries: list[str] = []
    for symbol in route_symbols:
        info = graph.get_node_info_by_name(symbol) or {}
        display = str(info.get("unified_name") or symbol)
        semantic_name = display.rsplit(":", 1)[-1]
        terms = sorted(_terms(semantic_name))
        if len(terms) >= 2:
            route_queries.append(" ".join(terms[:4]))
            break
    if not route_queries:
        raise ValueError("benchmark index has no query-only route seed")
    return {
        "symbols": symbols,
        "positions": positions,
        "position_candidate_count": len(candidate_positions),
        "position_encoding": native_provider.position_encoding,
        "route_symbols": route_symbols,
        "route_queries": route_queries,
        "definition_symbol_count": len(all_symbols),
        "graph_vertex_count": graph.graph.vcount(),
        "graph_edge_count": graph.graph.ecount(),
    }


def _validate_run_receipt(run: Mapping[str, Any], expected_snapshot: str) -> None:
    before = run.get("receipt_before")
    after = run.get("receipt_after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise RuntimeError("isolated worker omitted content receipts")
    if before.get("snapshot_id") != expected_snapshot:
        raise RuntimeError("clangd index changed before an isolated worker")
    if after.get("snapshot_id") != expected_snapshot:
        raise RuntimeError("clangd index changed during an isolated worker")


def _summarize_runs(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not runs:
        raise ValueError("at least one isolated run is required")
    metrics = (
        "wall_seconds",
        "process_wall_seconds",
        "cpu_seconds",
        "startup_wall_seconds",
        "workload_wall_seconds",
        "rss_start_bytes",
        "peak_rss_start_bytes",
        "peak_rss_bytes",
        "peak_rss_growth_bytes",
        "graph_materialization_count",
        "calls",
        "results",
        "public_errors",
        "serialized_bytes",
    )
    summary: dict[str, Any] = {}
    for metric in metrics:
        values = [
            run[metric]
            for run in runs
            if isinstance(run.get(metric), (int, float))
            and not isinstance(run.get(metric), bool)
        ]
        if values:
            summary[metric] = _summarize(values)
    stage_names = sorted(
        {str(name) for run in runs for name in (run.get("stages_seconds") or {})}
    )
    summary["stages_seconds"] = {
        name: _summarize(
            [
                run["stages_seconds"][name]
                for run in runs
                if name in (run.get("stages_seconds") or {})
            ]
        )
        for name in stage_names
    }
    summary["result_digests"] = sorted({str(run.get("result_digest")) for run in runs})
    summary["provider_selections"] = [
        json.loads(value)
        for value in sorted(
            {json.dumps(run.get("provider_selection"), sort_keys=True) for run in runs}
        )
    ]
    summary["observed_backends"] = sorted(
        {str(value) for run in runs for value in run.get("observed_backends", [])}
    )
    summary["observed_fallbacks"] = sorted(
        {str(value) for run in runs for value in run.get("observed_fallbacks", [])}
    )
    summary["fallback_reasons"] = sorted(
        {str(run["fallback_reason"]) for run in runs if run.get("fallback_reason")}
    )
    summary["process_ids"] = sorted({int(run["process_id"]) for run in runs})
    summary["index_counts"] = [
        json.loads(value)
        for value in sorted(
            {json.dumps(run.get("index_counts") or {}, sort_keys=True) for run in runs}
        )
    ]
    return summary


def _improvement(legacy: float, native: float) -> float:
    if legacy <= 0:
        raise ValueError("legacy timing must be positive")
    return (legacy - native) / legacy


def _query_ready_gate(
    *,
    legacy: float,
    native: float,
    threshold: float,
    parity: bool,
    graph_free: bool | None = None,
) -> dict[str, Any]:
    """Apply a query-ready gate without a floating-point boundary miss."""

    improvement = _improvement(legacy, native)
    passed = parity and native <= legacy * (1.0 - threshold)
    gate: dict[str, Any] = {
        "threshold": threshold,
        "improvement_fraction": improvement,
    }
    if graph_free is not None:
        gate["graph_free"] = graph_free
        passed = passed and graph_free
    gate["passed"] = passed
    return gate


def _regression(legacy: float, native: float) -> float:
    return -_improvement(legacy, native)


def _repeat_spread(values: Sequence[float | int]) -> float:
    if len(values) < 2:
        return 0.0
    median = statistics.median(float(value) for value in values)
    if median <= 0:
        return 0.0
    return (max(values) - min(values)) / median


def profile_clangd_workload_gate(
    *,
    idx_directory: Path,
    project_root: Path,
    iterations: int = DEFAULT_ITERATIONS,
    warmups: int = DEFAULT_WARMUPS,
    query_workload_size: int = DEFAULT_QUERY_WORKLOAD_SIZE,
    symbol_threshold: float = DEFAULT_SYMBOL_THRESHOLD,
    position_threshold: float = DEFAULT_POSITION_THRESHOLD,
    route_threshold: float = DEFAULT_ROUTE_THRESHOLD,
    graph_non_regression_budget: float = DEFAULT_GRAPH_NON_REGRESSION_BUDGET,
    peak_rss_ratio_budget: float = DEFAULT_PEAK_RSS_RATIO_BUDGET,
    repeat_rss_spread_budget: float = DEFAULT_REPEAT_RSS_SPREAD_BUDGET,
    max_native_peak_rss_mb: float = DEFAULT_MAX_NATIVE_PEAK_RSS_MB,
    concurrency_workers: int = DEFAULT_CONCURRENCY_WORKERS,
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT,
    clangd_generation_seconds: float | None = None,
    subject_manifest: Path = _DEFAULT_SUBJECT_MANIFEST,
    subject_id: str | None = None,
) -> dict[str, Any]:
    """Run the complete process-isolated clangd workload promotion gate."""

    if iterations < 1 or warmups < 0:
        raise ValueError("iterations must be positive and warmups non-negative")
    if query_workload_size < 1:
        raise ValueError("query_workload_size must be positive")
    if not 0 <= symbol_threshold < 1:
        raise ValueError("symbol_threshold must be between 0 (inclusive) and 1")
    if not 0 <= position_threshold < 1:
        raise ValueError("position_threshold must be between 0 (inclusive) and 1")
    if not 0 <= route_threshold < 1:
        raise ValueError("route_threshold must be between 0 (inclusive) and 1")
    if graph_non_regression_budget < 0:
        raise ValueError("graph_non_regression_budget must be non-negative")
    if peak_rss_ratio_budget < 1:
        raise ValueError("peak_rss_ratio_budget must be at least 1")
    if repeat_rss_spread_budget < 0:
        raise ValueError("repeat_rss_spread_budget must be non-negative")
    if max_native_peak_rss_mb <= 0:
        raise ValueError("max_native_peak_rss_mb must be positive")
    if concurrency_workers < 2:
        raise ValueError("concurrency_workers must be at least 2")
    if worker_timeout_seconds <= 0:
        raise ValueError("worker_timeout_seconds must be positive")
    if clangd_generation_seconds is not None and clangd_generation_seconds < 0:
        raise ValueError("clangd_generation_seconds must be non-negative")

    idx_directory = idx_directory.resolve()
    project_root = project_root.resolve()
    subject_manifest = subject_manifest.resolve()
    if not idx_directory.is_dir() or not any(idx_directory.glob("*.idx")):
        raise ValueError(f"no .idx files in {idx_directory}")
    if not project_root.is_dir():
        raise FileNotFoundError(project_root)
    if not subject_manifest.is_file():
        raise FileNotFoundError(subject_manifest)

    import codenib_core

    manifest, selected_subject = _load_subject_manifest(
        subject_manifest, subject_id=subject_id
    )
    manifest_digest = _file_digest(subject_manifest)
    git_revision = _git_revision(project_root)
    git_dirty = _git_dirty(project_root)
    git_remote = _git_remote(project_root)
    revision_match = (
        git_revision == selected_subject.get("revision")
        if selected_subject is not None
        else None
    )
    inputs = _workload_inputs(
        idx_directory,
        project_root,
        query_workload_size=query_workload_size,
    )
    initial_idx_digest = _idx_digest(idx_directory)
    idx_sha256, idx_bytes, idx_files = initial_idx_digest
    receipt = dict(
        codenib_core.clangd_fact_query_snapshot(
            idx_directory=str(idx_directory),
            project_root=str(project_root),
            position_encoding=inputs["position_encoding"],
        )
    )
    snapshot_id = str(receipt["snapshot_id"])
    contract = dict(codenib_core.clangd_fact_query_contract())
    version_counts = _riff_versions(idx_directory)
    supported_versions = [int(value) for value in contract["supported_versions"]]
    observed_versions = [int(value) for value in version_counts]
    version_gate = all(version in supported_versions for version in observed_versions)

    base_config = {
        "idx_directory": str(idx_directory),
        "project_root": str(project_root),
        "symbols": inputs["symbols"],
        "positions": inputs["positions"],
        "route_symbols": inputs["route_symbols"],
        "route_queries": inputs["route_queries"],
        "position_encoding": inputs["position_encoding"],
        "concurrency_workers": concurrency_workers,
    }
    workload_runs: dict[str, dict[str, list[dict[str, Any]]]] = {
        workload: {"legacy": [], "native": []} for workload in WORKLOADS
    }
    warmup_arm_order = []
    for warmup in range(warmups):
        for workload_index, workload in enumerate(WORKLOADS):
            order = (
                ("legacy", "native")
                if (warmup + workload_index) % 2 == 0
                else ("native", "legacy")
            )
            warmup_arm_order.append(
                {"warmup": warmup, "workload": workload, "order": list(order)}
            )
            for arm in order:
                run = _run_isolated_worker(
                    {**base_config, "arm": arm, "workload": workload},
                    timeout_seconds=worker_timeout_seconds,
                )
                _validate_run_receipt(run, snapshot_id)

    measured_arm_order = []
    for iteration in range(iterations):
        for workload_index, workload in enumerate(WORKLOADS):
            order = (
                ("legacy", "native")
                if (iteration + workload_index) % 2 == 0
                else ("native", "legacy")
            )
            measured_arm_order.append(
                {"iteration": iteration, "workload": workload, "order": list(order)}
            )
            for arm in order:
                run = _run_isolated_worker(
                    {**base_config, "arm": arm, "workload": workload},
                    timeout_seconds=worker_timeout_seconds,
                )
                _validate_run_receipt(run, snapshot_id)
                workload_runs[workload][arm].append(run)

    concurrency_runs = []
    for _iteration in range(iterations):
        run = _run_isolated_worker(
            {
                **base_config,
                "arm": "native",
                "workload": "concurrent_route_first",
            },
            timeout_seconds=worker_timeout_seconds,
        )
        _validate_run_receipt(run, snapshot_id)
        concurrency_runs.append(run)

    final_receipt = dict(
        codenib_core.clangd_fact_query_snapshot(
            idx_directory=str(idx_directory),
            project_root=str(project_root),
            position_encoding=inputs["position_encoding"],
        )
    )
    final_idx_digest = _idx_digest(idx_directory)
    if final_receipt != receipt or final_idx_digest != initial_idx_digest:
        raise RuntimeError("clangd index changed during the workload gate")
    final_git_revision = _git_revision(project_root)
    final_git_dirty = _git_dirty(project_root)
    source_checkout_unchanged = (
        final_git_revision == git_revision and final_git_dirty == git_dirty
    )

    workloads: dict[str, Any] = {}
    parity_passed = True
    graph_gates: dict[str, Any] = {}
    materialization_gates: dict[str, Any] = {}
    rss_gates: dict[str, Any] = {}
    symbol_gate: dict[str, Any] | None = None
    position_gate: dict[str, Any] | None = None
    route_gate: dict[str, Any] | None = None
    max_native_peak_rss_bytes = int(max_native_peak_rss_mb * 1024 * 1024)

    for workload in WORKLOADS:
        legacy_runs = workload_runs[workload]["legacy"]
        native_runs = workload_runs[workload]["native"]
        legacy_summary = _summarize_runs(legacy_runs)
        native_summary = _summarize_runs(native_runs)
        legacy_digests = {str(run["result_digest"]) for run in legacy_runs}
        native_digests = {str(run["result_digest"]) for run in native_runs}
        legacy_error_counts = {int(run["public_errors"]) for run in legacy_runs}
        native_error_counts = {int(run["public_errors"]) for run in native_runs}
        legacy_call_counts = {int(run["calls"]) for run in legacy_runs}
        native_call_counts = {int(run["calls"]) for run in native_runs}
        parity = (
            len(legacy_digests) == 1
            and legacy_digests == native_digests
            and len(legacy_error_counts) == 1
            and legacy_error_counts == native_error_counts
            and legacy_call_counts == native_call_counts
        )
        parity_passed = parity_passed and parity

        legacy_wall = float(legacy_summary["wall_seconds"]["median"])
        native_wall = float(native_summary["wall_seconds"]["median"])
        if workload == "symbol_only":
            symbol_gate = _query_ready_gate(
                legacy=legacy_wall,
                native=native_wall,
                threshold=symbol_threshold,
                parity=parity,
            )
        elif workload == "position_first":
            graph_free = all(
                run.get("graph_materialization_count") == 0
                and run.get("graph_materialized") is False
                and run.get("observed_backends") == ["native-clangd-fact-query-v1"]
                for run in native_runs
            )
            position_gate = _query_ready_gate(
                legacy=legacy_wall,
                native=native_wall,
                threshold=position_threshold,
                parity=parity,
                graph_free=graph_free,
            )
        elif workload == "route_first":
            graph_free = all(
                run.get("graph_materialization_count") == 0
                and run.get("graph_materialized") is False
                and set(run.get("observed_backends") or ())
                == {
                    "native-clangd-fact-query-v1",
                    "native-clangd-route-adjacency-v1",
                }
                and not run.get("observed_fallbacks")
                for run in native_runs
            )
            route_gate = _query_ready_gate(
                legacy=legacy_wall,
                native=native_wall,
                threshold=route_threshold,
                parity=parity,
                graph_free=graph_free,
            )
        else:
            regression = _regression(legacy_wall, native_wall)
            graph_gates[workload] = {
                "budget": graph_non_regression_budget,
                "regression_fraction": regression,
                "passed": parity and regression <= graph_non_regression_budget,
            }

        expected_materializations = 0
        materialization_passed = all(
            run.get("graph_materialization_count") == expected_materializations
            and run.get("graph_materialized") is False
            and run.get("range_indexes_built") is False
            for run in native_runs
        )
        materialization_gates[workload] = {
            "expected_native_count": expected_materializations,
            "observed_native_counts": sorted(
                {int(run["graph_materialization_count"]) for run in native_runs}
            ),
            "passed": materialization_passed,
        }

        legacy_peaks = [
            float(run["peak_rss_bytes"])
            for run in legacy_runs
            if isinstance(run.get("peak_rss_bytes"), (int, float))
        ]
        native_peaks = [
            float(run["peak_rss_bytes"])
            for run in native_runs
            if isinstance(run.get("peak_rss_bytes"), (int, float))
        ]
        rss_available = len(legacy_peaks) == len(legacy_runs) and len(
            native_peaks
        ) == len(native_runs)
        legacy_peak = statistics.median(legacy_peaks) if legacy_peaks else 0.0
        native_peak = statistics.median(native_peaks) if native_peaks else 0.0
        ratio = native_peak / legacy_peak if legacy_peak else float("inf")
        repeat_spread = _repeat_spread(native_peaks)
        rss_gates[workload] = {
            "measurement_available": rss_available,
            "peak_ratio_budget": peak_rss_ratio_budget,
            "native_to_legacy_peak_ratio": ratio,
            "absolute_native_peak_budget_bytes": max_native_peak_rss_bytes,
            "repeat_spread_budget": repeat_rss_spread_budget,
            "repeat_spread_fraction": repeat_spread,
            "passed": (
                rss_available
                and ratio <= peak_rss_ratio_budget
                and max(native_peaks, default=float("inf")) <= max_native_peak_rss_bytes
                and repeat_spread <= repeat_rss_spread_budget
            ),
        }
        workloads[workload] = {
            "parity": {
                "passed": parity,
                "legacy_result_digests": sorted(legacy_digests),
                "native_result_digests": sorted(native_digests),
                "legacy_public_error_counts": sorted(legacy_error_counts),
                "native_public_error_counts": sorted(native_error_counts),
            },
            "legacy": legacy_summary,
            "native": native_summary,
        }

    if symbol_gate is None:
        raise RuntimeError("symbol-only gate was not evaluated")
    if position_gate is None:
        raise RuntimeError("position-first gate was not evaluated")
    if route_gate is None:
        raise RuntimeError("route-first gate was not evaluated")
    concurrency_thread_digests = {
        str(digest)
        for run in concurrency_runs
        for digest in run.get("thread_result_digests", [])
    }
    concurrency_passed = len(concurrency_thread_digests) == 1 and all(
        run.get("graph_materialization_count") == 0
        and run.get("graph_materialized") is False
        and run.get("range_indexes_built") is False
        and run.get("thread_results_identical") is True
        and run.get("public_errors") == 0
        and run.get("observed_backends") == ["native-clangd-route-adjacency-v1"]
        for run in concurrency_runs
    )
    concurrency = {
        "workers": concurrency_workers,
        "runs": len(concurrency_runs),
        "passed": concurrency_passed,
        "graph_materialization_counts": [
            run["graph_materialization_count"] for run in concurrency_runs
        ],
        "thread_result_digests": sorted(concurrency_thread_digests),
        "native": _summarize_runs(concurrency_runs),
    }
    graph_passed = all(gate["passed"] for gate in graph_gates.values())
    materialization_passed = all(
        gate["passed"] for gate in materialization_gates.values()
    )
    rss_passed = all(gate["passed"] for gate in rss_gates.values())
    subject_passed = selected_subject is None or (
        revision_match is True
        and git_dirty is False
        and final_git_dirty is False
        and source_checkout_unchanged
    )
    overall_passed = (
        parity_passed
        and symbol_gate["passed"]
        and position_gate["passed"]
        and route_gate["passed"]
        and graph_passed
        and materialization_passed
        and rss_passed
        and concurrency_passed
        and version_gate
        and subject_passed
    )

    return {
        "schema_version": 1,
        "benchmark": "clangd_mixed_workload_gate_v3",
        "timer": {
            "wall": "time.perf_counter",
            "cpu": "time.process_time; scheduler-sensitive",
        },
        "subject": {
            "project_root": str(project_root),
            "git_revision": git_revision,
            "git_dirty": git_dirty,
            "final_git_revision": final_git_revision,
            "final_git_dirty": final_git_dirty,
            "source_checkout_unchanged": source_checkout_unchanged,
            "git_remote": git_remote,
            "idx_directory": str(idx_directory),
            "idx_files": idx_files,
            "idx_bytes": idx_bytes,
            "idx_sha256": idx_sha256,
            "native_snapshot_id": snapshot_id,
            "definition_symbols": inputs["definition_symbol_count"],
            "graph_vertices": inputs["graph_vertex_count"],
            "graph_edges": inputs["graph_edge_count"],
            "subject_id": subject_id,
            "manifest_entry": selected_subject,
            "manifest_revision_match": revision_match,
        },
        "subject_manifest": {
            "path": str(subject_manifest),
            "sha256": manifest_digest,
            "schema_version": manifest["schema_version"],
            "subject_count": len(manifest["subjects"]),
        },
        "configuration": {
            "iterations": iterations,
            "warmups": warmups,
            "query_workload_size": len(inputs["symbols"]),
            "position_query_workload_size": len(inputs["positions"]),
            "position_candidate_count": inputs["position_candidate_count"],
            "query_only_route_count": len(inputs["route_queries"]),
            "alternating_arm_order": True,
            "warmup_arm_order": warmup_arm_order,
            "measured_arm_order": measured_arm_order,
            "process_isolated_arms": True,
            "cold_start_scope": "fresh process; filesystem page cache uncontrolled",
            "query_ready_wall_metric": "wall_seconds",
            "process_launch_wall_metric": "process_wall_seconds",
            "query_ready_excludes_process_launch_and_outer_receipt_checks": True,
            "worker_timeout_seconds": worker_timeout_seconds,
            "symbol_threshold": symbol_threshold,
            "position_threshold": position_threshold,
            "route_threshold": route_threshold,
            "position_encoding": inputs["position_encoding"],
            "graph_non_regression_budget": graph_non_regression_budget,
            "peak_rss_ratio_budget": peak_rss_ratio_budget,
            "repeat_rss_spread_budget": repeat_rss_spread_budget,
            "max_native_peak_rss_bytes": max_native_peak_rss_bytes,
        },
        "repository_preparation": {
            "clangd_generation_seconds": clangd_generation_seconds,
            "included_in_query_ready_gate": False,
        },
        "content_receipt": {"before": receipt, "after": final_receipt},
        "version_matrix": {
            "supported_versions": supported_versions,
            "observed_file_versions": version_counts,
            "generated_fixture_versions": [18, 19, 20],
            "generated_fixture_test": (
                "test_symbol_results_errors_counts_and_relations_match_graph"
            ),
            "passed": version_gate,
        },
        "workloads": workloads,
        "raw_runs": workload_runs,
        "concurrency": concurrency,
        "raw_concurrency_runs": concurrency_runs,
        "gates": {
            "parity": {"passed": parity_passed},
            "symbol_only": symbol_gate,
            "position_first": position_gate,
            "route_first": route_gate,
            "graph_non_regression": {
                "passed": graph_passed,
                "workloads": graph_gates,
            },
            "lazy_graph_materialization": {
                "passed": materialization_passed,
                "workloads": materialization_gates,
            },
            "peak_rss": {"passed": rss_passed, "workloads": rss_gates},
            "concurrent_graph_free_route": {
                "expected_count": 0,
                "passed": concurrency_passed,
            },
            "version_matrix": {"passed": version_gate},
            "subject_revision": {
                "passed": subject_passed,
                "required": subject_id is not None,
                "revision_match": revision_match,
                "clean_checkout_before": git_dirty is False,
                "clean_checkout_after": final_git_dirty is False,
                "checkout_unchanged": source_checkout_unchanged,
            },
            "passed": overall_passed,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--idx-directory", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument(
        "--query-workload-size", type=int, default=DEFAULT_QUERY_WORKLOAD_SIZE
    )
    parser.add_argument(
        "--symbol-threshold", type=float, default=DEFAULT_SYMBOL_THRESHOLD
    )
    parser.add_argument(
        "--position-threshold", type=float, default=DEFAULT_POSITION_THRESHOLD
    )
    parser.add_argument(
        "--route-threshold", type=float, default=DEFAULT_ROUTE_THRESHOLD
    )
    parser.add_argument(
        "--graph-non-regression-budget",
        type=float,
        default=DEFAULT_GRAPH_NON_REGRESSION_BUDGET,
    )
    parser.add_argument(
        "--peak-rss-ratio-budget",
        type=float,
        default=DEFAULT_PEAK_RSS_RATIO_BUDGET,
    )
    parser.add_argument(
        "--repeat-rss-spread-budget",
        type=float,
        default=DEFAULT_REPEAT_RSS_SPREAD_BUDGET,
    )
    parser.add_argument(
        "--max-native-peak-rss-mb",
        type=float,
        default=DEFAULT_MAX_NATIVE_PEAK_RSS_MB,
    )
    parser.add_argument(
        "--concurrency-workers", type=int, default=DEFAULT_CONCURRENCY_WORKERS
    )
    parser.add_argument(
        "--worker-timeout-seconds", type=float, default=DEFAULT_WORKER_TIMEOUT
    )
    parser.add_argument("--clangd-generation-seconds", type=float)
    parser.add_argument(
        "--subject-manifest", type=Path, default=_DEFAULT_SUBJECT_MANIFEST
    )
    parser.add_argument("--subject-id")
    parser.add_argument("--output-json", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        config = json.load(sys.stdin)
        result = _worker(config)
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    if args.idx_directory is None or args.project_root is None:
        raise SystemExit("--idx-directory and --project-root are required")
    report = profile_clangd_workload_gate(
        idx_directory=args.idx_directory,
        project_root=args.project_root,
        iterations=args.iterations,
        warmups=args.warmups,
        query_workload_size=args.query_workload_size,
        symbol_threshold=args.symbol_threshold,
        position_threshold=args.position_threshold,
        route_threshold=args.route_threshold,
        graph_non_regression_budget=args.graph_non_regression_budget,
        peak_rss_ratio_budget=args.peak_rss_ratio_budget,
        repeat_rss_spread_budget=args.repeat_rss_spread_budget,
        max_native_peak_rss_mb=args.max_native_peak_rss_mb,
        concurrency_workers=args.concurrency_workers,
        worker_timeout_seconds=args.worker_timeout_seconds,
        clangd_generation_seconds=args.clangd_generation_seconds,
        subject_manifest=args.subject_manifest,
        subject_id=args.subject_id,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report["gates"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
