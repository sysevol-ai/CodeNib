#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Process-isolated promotion gate for the lazy SCIP MCP consumer.

Only the graph-free ``symbol_only`` workload participates in the performance
decision.  Position, route, mixed, and concurrent-first-fallback workloads are
correctness observations.  Every arm sample runs in a fresh process so Python
module state and graph materialization cannot leak between samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CORE_BUILD = _PROJECT_ROOT / "build/core"
_DEFAULT_SUBJECT_MANIFEST = Path(__file__).with_name("scip_mcp_consumer_subjects.json")
for _candidate in (_CORE_BUILD, _PROJECT_ROOT):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

DEFAULT_ITERATIONS = 20
DEFAULT_WARMUPS = 4
DEFAULT_QUERY_WORKLOAD_SIZE = 100
DEFAULT_PROMOTION_THRESHOLD = 0.20
DEFAULT_CONCURRENCY_WORKERS = 16
DEFAULT_WORKER_TIMEOUT = 900.0
PERFORMANCE_WORKLOAD = "symbol_only"
CORRECTNESS_WORKLOADS = ("position_first", "route_first", "mixed")
WORKLOADS = (PERFORMANCE_WORKLOAD, *CORRECTNESS_WORKLOADS)
CONCURRENT_WORKLOAD = "concurrent_first_fallback"
_ARMS = ("legacy", "candidate")
_SYMBOL_QUERY_CATEGORIES = (
    "canonical",
    "display",
    "bare",
    "quoted",
    "ambiguous",
    "missing",
)
_MEMORY_ERROR_EXIT = 70
_SHA256_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _file_receipt(path: Path) -> dict[str, Any]:
    from codenib.compiler.artifact_fingerprints import regular_file_fingerprint

    raw_path = Path(path)
    try:
        if raw_path.is_symlink():
            raise ValueError(f"benchmark artifact is a symlink: {raw_path}")
        fingerprint = regular_file_fingerprint(raw_path)
        resolved = raw_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"benchmark artifact is unavailable: {raw_path}") from exc
    return {
        "path": str(resolved),
        "size_bytes": fingerprint["size"],
        "sha256": fingerprint["sha256"],
    }


def _finite_nonnegative(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return number


def _nearest_rank_p95(ordered: Sequence[float]) -> float:
    if not ordered:
        raise ValueError("at least one sample is required")
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _summarize(values: Sequence[float | int]) -> dict[str, float | int]:
    if not values:
        raise ValueError("at least one numeric sample is required")
    ordered = sorted(
        _finite_nonnegative(value, label=f"sample[{index}]")
        for index, value in enumerate(values)
    )
    return {
        "samples": len(ordered),
        "min": ordered[0],
        "p50": statistics.median(ordered),
        "p95": _nearest_rank_p95(ordered),
        "max": ordered[-1],
    }


def _abba_schedule(samples_per_arm: int) -> list[dict[str, Any]]:
    if isinstance(samples_per_arm, bool) or samples_per_arm < 0:
        raise ValueError("samples_per_arm must be a non-negative even integer")
    if samples_per_arm % 2:
        raise ValueError("samples_per_arm must be even for balanced ABBA blocks")
    schedule: list[dict[str, Any]] = []
    for block in range(samples_per_arm // 2):
        arms = (
            ("legacy", "candidate", "candidate", "legacy")
            if block % 2 == 0
            else ("candidate", "legacy", "legacy", "candidate")
        )
        for slot, arm in enumerate(arms):
            schedule.append({"block": block, "slot": slot, "arm": arm})
    return schedule


def _promotion_decision(
    legacy: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    threshold: float,
    parity: bool,
    graph_free: bool,
) -> dict[str, Any]:
    if not 0 <= threshold < 1:
        raise ValueError("promotion threshold must be in [0, 1)")
    ratio = 1.0 - threshold
    legacy_p50 = _finite_nonnegative(legacy.get("p50"), label="legacy p50")
    candidate_p50 = _finite_nonnegative(candidate.get("p50"), label="candidate p50")
    legacy_p95 = _finite_nonnegative(legacy.get("p95"), label="legacy p95")
    candidate_p95 = _finite_nonnegative(candidate.get("p95"), label="candidate p95")
    if legacy_p50 <= 0 or legacy_p95 <= 0:
        raise ValueError("legacy p50 and p95 must be positive")
    p50_passed = candidate_p50 <= legacy_p50 * ratio
    p95_passed = candidate_p95 <= legacy_p95 * ratio
    return {
        "threshold": threshold,
        "required_candidate_ratio": ratio,
        "p50": {
            "legacy_seconds": legacy_p50,
            "candidate_seconds": candidate_p50,
            "improvement_fraction": (legacy_p50 - candidate_p50) / legacy_p50,
            "passed": p50_passed,
        },
        "p95": {
            "legacy_seconds": legacy_p95,
            "candidate_seconds": candidate_p95,
            "improvement_fraction": (legacy_p95 - candidate_p95) / legacy_p95,
            "passed": p95_passed,
        },
        "parity": bool(parity),
        "graph_free": bool(graph_free),
        "passed": bool(parity and graph_free and p50_passed and p95_passed),
    }


def _current_rss_bytes() -> int | None:
    try:
        fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
        return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    except (IndexError, OSError, ValueError):
        return None


def _peak_rss_bytes() -> int | None:
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        return None
    return peak if sys.platform == "darwin" else peak * 1024


def _provider_metadata(result: object) -> list[dict[str, Any]]:
    if not isinstance(result, list):
        return []
    return [
        dict(row["lsp_provider"])
        for row in result
        if isinstance(row, Mapping) and isinstance(row.get("lsp_provider"), Mapping)
    ]


def _run_mcp_workload(
    ctx: Any,
    *,
    workload: str,
    symbol_queries: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    route_symbols: Sequence[str],
    route_queries: Sequence[str],
    capture_payload: bool,
) -> dict[str, Any]:
    """Run actual MCP implementations and hash their complete public JSON."""

    from codenib.mcp.tools.lsp import (
        lsp_definition_impl,
        lsp_references_impl,
        lsp_route_impl,
    )

    if workload not in WORKLOADS:
        raise ValueError(f"unknown MCP consumer workload: {workload}")
    payload: list[dict[str, Any]] = []
    calls = 0
    results = 0
    public_errors = 0
    serialized_bytes = 0
    observed_metadata: set[str] = set()

    def record(
        operation: str,
        arguments: Mapping[str, Any],
        result: Any,
        *,
        query_category: str | None = None,
    ) -> None:
        nonlocal calls, public_errors, results, serialized_bytes
        row = {
            "operation": operation,
            "arguments": dict(arguments),
            "result": result,
        }
        if query_category is not None:
            row["query_category"] = query_category
        encoded = json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        serialized_bytes += len(encoded)
        calls += 1
        if isinstance(result, Mapping) and "error" in result:
            public_errors += 1
        elif isinstance(result, list):
            results += len(result)
        for metadata in _provider_metadata(result):
            observed_metadata.add(
                json.dumps(metadata, sort_keys=True, separators=(",", ":"))
            )
        payload.append(row)

    def symbol_calls(selected: Sequence[Mapping[str, Any]]) -> None:
        for query in selected:
            symbol = str(query["symbol"])
            category = str(query["category"])
            definition_args = {"symbol": symbol, "top_k": 100}
            record(
                "definition_by_symbol",
                definition_args,
                lsp_definition_impl(ctx, **definition_args),
                query_category=category,
            )
            for include_declaration in (False, True):
                references_args = {
                    "symbol": symbol,
                    "include_declaration": include_declaration,
                    "top_k": 100,
                }
                record(
                    "references_by_symbol",
                    references_args,
                    lsp_references_impl(ctx, **references_args),
                    query_category=category,
                )

    def position_calls() -> None:
        for raw in positions:
            arguments = {
                "file_path": str(raw["file_path"]),
                "line": int(raw["line"]),
                "top_k": 100,
            }
            if raw.get("character") is not None:
                arguments["character"] = int(raw["character"])
            record(
                "definition_by_position",
                arguments,
                lsp_definition_impl(ctx, **arguments),
            )
            reference_arguments = {**arguments, "include_declaration": True}
            record(
                "references_by_position",
                reference_arguments,
                lsp_references_impl(ctx, **reference_arguments),
            )

    def route_calls() -> None:
        symbol_arguments = {
            "symbols": list(route_symbols),
            "query": "",
            "top_k": 100,
            "include_neighbors": True,
        }
        record(
            "route_by_symbol",
            symbol_arguments,
            lsp_route_impl(ctx, **symbol_arguments),
        )
        for query in route_queries:
            query_arguments = {
                "symbols": [],
                "query": query,
                "top_k": 100,
                "include_neighbors": True,
            }
            record(
                "route_by_query",
                query_arguments,
                lsp_route_impl(ctx, **query_arguments),
            )

    if workload == PERFORMANCE_WORKLOAD:
        symbol_calls(symbol_queries)
    elif workload == "position_first":
        position_calls()
        symbol_calls(symbol_queries)
    elif workload == "route_first":
        route_calls()
        symbol_calls(symbol_queries)
    else:
        midpoint = max(1, len(symbol_queries) // 2)
        symbol_calls(symbol_queries[:midpoint])
        position_calls()
        symbol_calls(symbol_queries[midpoint:])
        route_calls()

    outcome: dict[str, Any] = {
        "public_payload_sha256": _json_digest(payload),
        "calls": calls,
        "results": results,
        "public_errors": public_errors,
        "serialized_bytes": serialized_bytes,
        "observed_public_metadata": [
            json.loads(value) for value in sorted(observed_metadata)
        ],
        "symbol_query_category_counts": {
            category: sum(query.get("category") == category for query in symbol_queries)
            for category in _SYMBOL_QUERY_CATEGORIES
        },
    }
    if capture_payload:
        outcome["public_payload"] = payload
    return outcome


def _run_concurrent_first_fallback(
    ctx: Any,
    *,
    route_symbols: Sequence[str],
    workers: int,
) -> dict[str, Any]:
    import threading

    from codenib.mcp.tools.lsp import lsp_route_impl

    if workers < 2:
        raise ValueError("concurrency_workers must be at least 2")
    barrier = threading.Barrier(workers)

    def call(_index: int) -> Any:
        barrier.wait()
        return lsp_route_impl(
            ctx,
            symbols=list(route_symbols),
            query="",
            top_k=100,
            include_neighbors=True,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(call, range(workers)))
    digests = [_json_digest(result) for result in results]
    return {
        "public_payload_sha256": _json_digest(results),
        "calls": workers,
        "results": sum(len(result) for result in results if isinstance(result, list)),
        "public_errors": sum(
            int(isinstance(result, Mapping) and "error" in result) for result in results
        ),
        "serialized_bytes": len(
            json.dumps(results, ensure_ascii=False, allow_nan=False).encode("utf-8")
        ),
        "thread_payload_sha256": sorted(set(digests)),
        "thread_results_identical": len(set(digests)) == 1,
    }


def _worker(config: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one arm in the current worker process."""

    from codenib.mcp.context import ServerContext
    from codenib.scip_interface.scip_query import observe_fact_query_snapshot

    arm = config.get("arm")
    workload = config.get("workload")
    if arm not in _ARMS:
        raise ValueError(f"unknown benchmark arm: {arm!r}")
    if workload not in (*WORKLOADS, CONCURRENT_WORKLOAD):
        raise ValueError(f"unknown benchmark workload: {workload!r}")
    if workload == CONCURRENT_WORKLOAD and arm != "candidate":
        raise ValueError("concurrent first fallback is candidate-only")

    manifest_path = Path(str(config["manifest_path"])).resolve(strict=True)
    project_root = Path(str(config["project_root"])).resolve(strict=True)
    expected_receipt = config.get("candidate_receipt")
    if not isinstance(expected_receipt, Mapping):
        raise ValueError("worker candidate_receipt must be a mapping")
    receipt_before = dict(observe_fact_query_snapshot(expected_receipt))
    rss_start = _current_rss_bytes()
    peak_start = _peak_rss_bytes()
    cpu_started = time.process_time()
    wall_started = time.perf_counter()
    ctx: Any = None
    provider: Any = None
    try:
        views = {"symbol_graph"} if arm == "legacy" else ()
        ctx = ServerContext.load(manifest_path, views=views)
        public_fallback_reason = ctx.lsp_provider_selection.get("fallback_reason")
        expected_fallback_reason = config.get("public_fallback_reason")
        if public_fallback_reason != expected_fallback_reason:
            raise RuntimeError("public LSP fallback reason changed before a worker")
        expected_selection = config.get("public_provider_selection")
        if not isinstance(expected_selection, Mapping):
            raise ValueError("worker public_provider_selection must be a mapping")

        if arm == "legacy":
            if ctx.symbol_graph is None or ctx.lsp_provider is None:
                raise RuntimeError("legacy worker could not load the symbol graph")
            if ctx.lsp_provider_selection != dict(expected_selection):
                raise RuntimeError("legacy public provider selection changed")
            provider = ctx.lsp_provider
        else:
            from codenib.scip_interface.scip_query_provider import (
                load_scip_query_provider,
            )

            provider = load_scip_query_provider(
                manifest_path,
                language=str(config["language"]),
                project_root=project_root,
                public_fallback_reason=public_fallback_reason,
            )
            if provider.candidate_receipt != dict(expected_receipt):
                raise RuntimeError(
                    "candidate receipt changed before provider injection"
                )
            if provider.snapshot_id != expected_selection.get("index_snapshot"):
                raise RuntimeError("candidate public snapshot differs from legacy")
            ctx.lsp_provider = provider

        startup_wall_seconds = time.perf_counter() - wall_started
        workload_started = time.perf_counter()
        if workload == CONCURRENT_WORKLOAD:
            outcome = _run_concurrent_first_fallback(
                ctx,
                route_symbols=config["route_symbols"],
                workers=int(config["concurrency_workers"]),
            )
        else:
            outcome = _run_mcp_workload(
                ctx,
                workload=str(workload),
                symbol_queries=config["symbol_queries"],
                positions=config["positions"],
                route_symbols=config["route_symbols"],
                route_queries=config["route_queries"],
                capture_payload=bool(config.get("capture_payload", False)),
            )
        workload_wall_seconds = time.perf_counter() - workload_started
        wall_seconds = time.perf_counter() - wall_started
        cpu_seconds = time.process_time() - cpu_started
        diagnostics = provider.diagnostics() if arm == "candidate" else None
        selection = dict(expected_selection)
    finally:
        if ctx is not None:
            ctx.close()

    receipt_after = dict(observe_fact_query_snapshot(expected_receipt))
    peak_rss = _peak_rss_bytes()
    return {
        "arm": arm,
        "workload": workload,
        "process_id": os.getpid(),
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "startup_wall_seconds": startup_wall_seconds,
        "workload_wall_seconds": workload_wall_seconds,
        "rss_start_bytes": rss_start,
        "peak_rss_start_bytes": peak_start,
        "peak_rss_bytes": peak_rss,
        "peak_rss_growth_bytes": (
            max(0, peak_rss - peak_start)
            if peak_rss is not None and peak_start is not None
            else None
        ),
        "provider_selection": selection,
        "diagnostics": diagnostics,
        "receipt_before": receipt_before,
        "receipt_after": receipt_after,
        "igraph_imported": "igraph" in sys.modules,
        "code_graph_imported": "codenib.graph.code_graph" in sys.modules,
        **outcome,
    }


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    paths = [str(_CORE_BUILD), str(_PROJECT_ROOT)]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment.pop("CODENIB_CORE_PROFILE", None)
    environment.pop("CODENIB_SCIP_FACT_QUERY_PROVIDER", None)
    return environment


def _run_isolated_worker(
    config: Mapping[str, Any], *, timeout_seconds: float
) -> dict[str, Any]:
    process_started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        cwd=str(_PROJECT_ROOT),
        env=_worker_environment(),
        input=json.dumps(config, sort_keys=True, allow_nan=False),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    process_wall_seconds = time.perf_counter() - process_started
    if completed.returncode == _MEMORY_ERROR_EXIT:
        try:
            failure = json.loads(completed.stderr)
        except json.JSONDecodeError:
            failure = {}
        raise MemoryError(
            str(failure.get("message") or "isolated SCIP MCP worker exhausted memory")
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"isolated SCIP MCP worker failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "isolated SCIP MCP worker returned invalid JSON: "
            f"{completed.stdout[-1000:]!r}; stderr={completed.stderr[-1000:]!r}"
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError("isolated SCIP MCP worker returned a non-object")
    result["process_wall_seconds"] = process_wall_seconds
    return result


def _git_command(project_root: Path, *arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(project_root), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout.strip()
    return output if completed.returncode == 0 else None


def _git_identity(project_root: Path) -> dict[str, Any]:
    commit = _git_command(project_root, "rev-parse", "HEAD")
    status = _git_command(
        project_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    return {
        "commit": commit if commit and _COMMIT_RE.fullmatch(commit) else None,
        "dirty": bool(status) if status is not None else None,
        "remote": _git_command(project_root, "remote", "get-url", "origin"),
    }


def _canonical_repository(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    repository = value.strip()
    match = re.fullmatch(r"git@github\.com:(.+)", repository)
    if match:
        repository = f"https://github.com/{match.group(1)}"
    if repository.endswith("/"):
        repository = repository[:-1]
    if not repository.endswith(".git"):
        repository += ".git"
    return repository


def _load_subject_manifest(
    path: Path, *, subject_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("SCIP MCP subject manifest must use schema_version 1")
    if set(payload) != {"schema_version", "subjects"}:
        raise ValueError("SCIP MCP subject manifest has unexpected fields")
    subjects = payload.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        raise ValueError("SCIP MCP subject manifest requires subjects")
    ids: list[str] = []
    for subject in subjects:
        if not isinstance(subject, Mapping) or set(subject) != {
            "id",
            "language",
            "repository",
            "revision",
        }:
            raise ValueError("SCIP MCP subject entry has malformed fields")
        identifier = subject.get("id")
        language = subject.get("language")
        repository = subject.get("repository")
        revision = subject.get("revision")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("SCIP MCP subject id is malformed")
        if language not in {"python", "rust"}:
            raise ValueError(f"subject {identifier!r} language is unsupported")
        if not isinstance(repository, str) or not repository.startswith("https://"):
            raise ValueError(f"subject {identifier!r} repository is malformed")
        if not isinstance(revision, str) or not _COMMIT_RE.fullmatch(revision):
            raise ValueError(f"subject {identifier!r} revision is not a commit SHA")
        ids.append(identifier)
    if len(ids) != len(set(ids)):
        raise ValueError("SCIP MCP subject ids must be unique")
    matches = [subject for subject in subjects if subject["id"] == subject_id]
    if len(matches) != 1:
        raise ValueError(f"subject id is absent or duplicated: {subject_id!r}")
    return dict(payload), dict(matches[0])


def _symbol_query_variants(
    graph: Any,
    *,
    definitions: Sequence[str],
    target_count: int,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    from codenib.agent.lsp_graph import _bare, resolve_symbol_candidates

    pools: dict[str, list[str]] = {
        category: [] for category in _SYMBOL_QUERY_CATEGORIES
    }
    ambiguous_seeds: list[str] = []

    def add(category: str, seed: object) -> None:
        if not isinstance(seed, str) or not seed or seed in pools[category]:
            return
        pools[category].append(seed)

    for name in definitions:
        info = graph.get_node_info_by_name(name) or {}
        display = info.get("unified_name")
        add("canonical", name)
        if isinstance(display, str) and display:
            candidates = resolve_symbol_candidates(graph, display)
            if candidates == [name]:
                add("display", display)
            elif len(candidates) > 1:
                ambiguous_seeds.append(display)
        bare = _bare(str(display or name))
        if bare:
            candidates = resolve_symbol_candidates(graph, bare)
            if candidates == [name]:
                add("bare", bare)
            elif len(candidates) > 1:
                ambiguous_seeds.append(bare)
        quoted = f"`{name}`"
        if resolve_symbol_candidates(graph, quoted) == [name]:
            add("quoted", quoted)

    for seed in ambiguous_seeds:
        if len(resolve_symbol_candidates(graph, seed)) > 1:
            add("ambiguous", seed)

    missing_index = 0
    while len(pools["missing"]) < target_count:
        seed = f"__codenib_missing_scip_consumer_symbol_{missing_index}__"
        missing_index += 1
        if not resolve_symbol_candidates(graph, seed):
            add("missing", seed)
        if missing_index > target_count * 100:
            raise ValueError("fixed subject cannot prove a missing symbol seed")

    absent = [category for category, seeds in pools.items() if not seeds]
    if absent:
        raise ValueError(
            "fixed subject cannot produce required symbol query variants: "
            + ", ".join(absent)
        )

    queries: list[dict[str, str]] = []
    used: set[str] = set()
    offsets = {category: 0 for category in _SYMBOL_QUERY_CATEGORIES}

    def select(category: str) -> bool:
        seeds = pools[category]
        while offsets[category] < len(seeds):
            seed = seeds[offsets[category]]
            offsets[category] += 1
            if seed in used:
                continue
            used.add(seed)
            queries.append({"category": category, "symbol": seed})
            return True
        return False

    for category in _SYMBOL_QUERY_CATEGORIES:
        if not select(category):
            raise ValueError(
                f"fixed subject has no distinct {category} symbol query seed"
            )
    while len(queries) < target_count:
        progressed = False
        for category in _SYMBOL_QUERY_CATEGORIES:
            if len(queries) >= target_count:
                break
            progressed = select(category) or progressed
        if not progressed:
            raise ValueError(
                f"fixed subject provides fewer than {target_count} distinct "
                "symbol query seeds"
            )

    counts = {
        category: sum(query["category"] == category for query in queries)
        for category in _SYMBOL_QUERY_CATEGORIES
    }
    if sum(counts.values()) != target_count or not all(counts.values()):
        raise RuntimeError("symbol query variant partition is incomplete")
    return queries, counts


def _workload_inputs(graph: Any, *, query_workload_size: int) -> dict[str, Any]:
    from codenib.agent.lsp_graph import _terms
    from codenib.types import node_has_definition

    names = list(getattr(graph, "name_to_vertex", {}) or {})
    definitions = [
        str(name)
        for name in names
        if node_has_definition(graph.get_node_info_by_name(name) or {})
    ]
    if not definitions:
        raise ValueError("subject graph has no definition symbols")
    symbol_queries, symbol_query_category_counts = _symbol_query_variants(
        graph,
        definitions=definitions,
        target_count=query_workload_size,
    )

    positions: list[dict[str, Any]] = []
    seen_positions: set[tuple[str, int]] = set()
    position_limit = min(16, query_workload_size)
    for name in definitions:
        info = graph.get_node_info_by_name(name) or {}
        file_path = info.get("file")
        start_line = info.get("selection_line")
        if start_line is None:
            start_line = info.get("start_line")
        if (
            not isinstance(file_path, str)
            or not file_path
            or isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or start_line < 0
        ):
            continue
        key = (file_path, start_line)
        if key in seen_positions:
            continue
        seen_positions.add(key)
        positions.append({"file_path": file_path, "line": start_line + 1})
        if len(positions) >= position_limit:
            break
    if not positions:
        raise ValueError("subject graph has no position-query sample")

    route_symbols = definitions[: min(4, len(definitions))]
    if not route_symbols:
        raise ValueError("subject graph has no route seed")
    route_queries: list[str] = []
    for name in definitions:
        info = graph.get_node_info_by_name(name) or {}
        display = str(info.get("unified_name") or name).rsplit(":", 1)[-1]
        terms = sorted(_terms(display))
        if len(terms) >= 2:
            route_queries.append(" ".join(terms[:4]))
            break
    if not route_queries:
        raise ValueError("fixed subject has no query-only route seed")
    return {
        "symbol_queries": symbol_queries,
        "symbol_query_category_counts": symbol_query_category_counts,
        "positions": positions,
        "route_symbols": route_symbols,
        "route_queries": route_queries,
        "definition_symbol_count": len(definitions),
        "graph_vertex_count": int(graph.graph.vcount()),
        "graph_edge_count": int(graph.graph.ecount()),
    }


def _artifact_receipts(candidate_receipt: Mapping[str, Any]) -> dict[str, Any]:
    manifest = candidate_receipt.get("manifest")
    index = candidate_receipt.get("index")
    graph = candidate_receipt.get("graph")
    if not all(isinstance(value, Mapping) for value in (manifest, index, graph)):
        raise ValueError("candidate receipt omits artifact bindings")
    entry_path = Path(str(manifest["entry_path"]))
    return {
        "manifest": _file_receipt(Path(str(manifest["path"]))),
        "index": _file_receipt(entry_path / str(index["relative_path"])),
        "graph": _file_receipt(entry_path / str(graph["relative_path"])),
    }


def _observe_receipt(candidate_receipt: Mapping[str, Any]) -> dict[str, Any]:
    from codenib.scip_interface.scip_query import observe_fact_query_snapshot

    return dict(observe_fact_query_snapshot(candidate_receipt))


def _prepare_subject(
    *,
    manifest_path: Path,
    project_root: Path,
    subject_manifest: Path,
    subject_id: str,
    query_workload_size: int,
) -> dict[str, Any]:
    from codenib.mcp.context import ServerContext
    from codenib.scip_interface.scip_query_provider import load_scip_query_provider

    if manifest_path.is_symlink():
        raise ValueError("RepoManifest path must not be a symlink")
    if subject_manifest.is_symlink():
        raise ValueError("subject manifest path must not be a symlink")
    manifest_path = manifest_path.resolve(strict=True)
    project_root = project_root.resolve(strict=True)
    subject_manifest = subject_manifest.resolve(strict=True)
    subject_manifest_receipt = _file_receipt(subject_manifest)
    manifest_payload, subject = _load_subject_manifest(
        subject_manifest, subject_id=subject_id
    )
    if _file_receipt(subject_manifest) != subject_manifest_receipt:
        raise RuntimeError("SCIP MCP subject manifest changed while loading")
    git_before = _git_identity(project_root)
    benchmark_git_before = _git_identity(_PROJECT_ROOT)
    benchmark_sources_before = {
        "harness": _file_receipt(Path(__file__)),
        "provider": _file_receipt(
            _PROJECT_ROOT / "codenib/scip_interface/scip_query_provider.py"
        ),
        "snapshot_observer": _file_receipt(
            _PROJECT_ROOT / "codenib/scip_interface/scip_query.py"
        ),
    }
    if git_before["commit"] != subject["revision"]:
        raise ValueError(
            f"subject {subject_id!r} requires commit {subject['revision']}; "
            f"observed {git_before['commit']}"
        )
    if git_before["dirty"] is not False:
        raise ValueError(f"subject {subject_id!r} checkout must be clean")
    if _canonical_repository(git_before["remote"]) != _canonical_repository(
        subject["repository"]
    ):
        raise ValueError(f"subject {subject_id!r} repository remote does not match")

    candidate = load_scip_query_provider(
        manifest_path,
        language=subject["language"],
        project_root=project_root,
    )
    candidate_receipt = candidate.candidate_receipt
    if candidate_receipt.get("language") != subject["language"]:
        raise RuntimeError("candidate language does not match the fixed subject")
    initial_snapshot = _observe_receipt(candidate_receipt)
    artifact_before = _artifact_receipts(candidate_receipt)

    legacy_context = ServerContext.load(manifest_path, views={"symbol_graph"})
    try:
        if legacy_context.symbol_graph is None or legacy_context.lsp_provider is None:
            raise RuntimeError("fixed subject has no usable legacy symbol graph")
        public_fallback_reason = legacy_context.lsp_provider_selection.get(
            "fallback_reason"
        )
        public_provider_selection = dict(legacy_context.lsp_provider_selection)
        inputs = _workload_inputs(
            legacy_context.symbol_graph,
            query_workload_size=query_workload_size,
        )
    finally:
        legacy_context.close()

    return {
        "manifest_path": str(manifest_path),
        "project_root": str(project_root),
        "subject_manifest": {
            "path": str(subject_manifest),
            "sha256": subject_manifest_receipt["sha256"],
            "payload": manifest_payload,
            "entry": subject,
        },
        "language": subject["language"],
        "candidate_receipt": candidate_receipt,
        "initial_snapshot": initial_snapshot,
        "artifact_before": artifact_before,
        "git_before": git_before,
        "benchmark_git_before": benchmark_git_before,
        "benchmark_sources_before": benchmark_sources_before,
        "public_fallback_reason": public_fallback_reason,
        "public_provider_selection": public_provider_selection,
        "inputs": inputs,
    }


def _validate_worker_run(
    run: Mapping[str, Any],
    *,
    arm: str,
    workload: str,
    expected_snapshot: Mapping[str, Any],
) -> None:
    if run.get("arm") != arm or run.get("workload") != workload:
        raise RuntimeError("isolated worker returned the wrong arm or workload")
    process_id = run.get("process_id")
    if (
        isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        raise RuntimeError("isolated worker returned an invalid process id")
    for metric in (
        "wall_seconds",
        "process_wall_seconds",
        "cpu_seconds",
        "startup_wall_seconds",
        "workload_wall_seconds",
    ):
        try:
            _finite_nonnegative(run.get(metric), label=metric)
        except ValueError as exc:
            raise RuntimeError(f"isolated worker returned malformed {metric}") from exc
    for metric in (
        "rss_start_bytes",
        "peak_rss_start_bytes",
        "peak_rss_bytes",
        "peak_rss_growth_bytes",
    ):
        value = run.get(metric)
        if value is not None:
            try:
                _finite_nonnegative(value, label=metric)
            except ValueError as exc:
                raise RuntimeError(
                    f"isolated worker returned malformed {metric}"
                ) from exc
    if run.get("receipt_before") != dict(expected_snapshot):
        raise RuntimeError("candidate snapshot changed before an isolated worker")
    if run.get("receipt_after") != dict(expected_snapshot):
        raise RuntimeError("candidate snapshot changed during an isolated worker")
    digest = run.get("public_payload_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise RuntimeError("isolated worker returned a malformed public payload digest")
    if arm == "candidate" and not isinstance(run.get("diagnostics"), Mapping):
        raise RuntimeError("candidate worker omitted provider diagnostics")


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
        "calls",
        "results",
        "public_errors",
        "serialized_bytes",
    )
    summary: dict[str, Any] = {}
    for metric in metrics:
        values = [run[metric] for run in runs if run.get(metric) is not None]
        if values:
            summary[metric] = _summarize(values)
    summary["public_payload_sha256"] = sorted(
        {str(run["public_payload_sha256"]) for run in runs}
    )
    summary["process_ids"] = sorted({int(run["process_id"]) for run in runs})
    return summary


def _parity(
    legacy_runs: Sequence[Mapping[str, Any]],
    candidate_runs: Sequence[Mapping[str, Any]],
) -> bool:
    if not legacy_runs or not candidate_runs:
        return False
    fields = (
        "public_payload_sha256",
        "calls",
        "results",
        "public_errors",
        "serialized_bytes",
    )
    return all(
        len({run.get(field) for run in legacy_runs}) == 1
        and {run.get(field) for run in legacy_runs}
        == {run.get(field) for run in candidate_runs}
        for field in fields
    )


def _symbol_candidate_graph_free(
    run: Mapping[str, Any], *, expected_symbol_count: int
) -> bool:
    diagnostics = run.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return False
    return (
        run.get("igraph_imported") is False
        and run.get("code_graph_imported") is False
        and diagnostics.get("state") == "NATIVE_READY"
        and diagnostics.get("logical_backend") == "persisted-symbol-graph-v1"
        and diagnostics.get("native_physical_backend") == "native-scip-fact-query-v1"
        and diagnostics.get("native_definition_count") == expected_symbol_count
        and diagnostics.get("native_references_count") == expected_symbol_count * 2
        and diagnostics.get("native_symbol_call_count") == expected_symbol_count * 3
        and diagnostics.get("native_symbol_call_count") == run.get("calls")
        and diagnostics.get("receipt_observation_count") == 0
        and diagnostics.get("graph_load_attempt_count") == 0
        and diagnostics.get("graph_load_count") == 0
        and diagnostics.get("graph_publication_count") == 0
        and diagnostics.get("fallback_call_count") == 0
        and diagnostics.get("graph_fallback_count") == 0
        and diagnostics.get("graph_call_count") == 0
        and diagnostics.get("graph_materialization_seconds") is None
        and diagnostics.get("terminal_failure") is None
    )


def _expected_candidate_counts(
    workload: str, inputs: Mapping[str, Any]
) -> dict[str, int]:
    symbol_count = len(inputs["symbol_queries"])
    route_calls = 1 + len(inputs["route_queries"])
    position_calls = 2 * len(inputs["positions"])
    if workload == "position_first":
        fallback_calls = position_calls
    elif workload == "route_first":
        fallback_calls = route_calls
    elif workload == "mixed":
        fallback_calls = position_calls + route_calls
    else:
        raise ValueError(f"unknown correctness workload: {workload}")
    return {
        "native_definition_count": symbol_count,
        "native_references_count": symbol_count * 2,
        "native_symbol_call_count": symbol_count * 3,
        "fallback_call_count": fallback_calls,
        # The first fallback observes before and after publication, then after
        # the graph call. Every later fallback brackets its graph call with a
        # fresh pre/post observation.
        "receipt_observation_count": 2 * fallback_calls + 1,
    }


def _correctness_candidate_ready(
    run: Mapping[str, Any], *, expected: Mapping[str, int]
) -> bool:
    diagnostics = run.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return False
    materialization = diagnostics.get("graph_materialization_seconds")
    try:
        materialization_seconds = _finite_nonnegative(
            materialization, label="graph_materialization_seconds"
        )
    except ValueError:
        return False
    fallback_calls = expected.get(
        "fallback_call_count", diagnostics.get("fallback_call_count")
    )
    return bool(
        materialization_seconds >= 0
        and diagnostics.get("state") == "GRAPH_READY"
        and diagnostics.get("logical_backend") == "persisted-symbol-graph-v1"
        and diagnostics.get("fallback_physical_backend")
        == "lazy-persisted-symbol-graph-v1"
        and diagnostics.get("graph_load_attempt_count") == 1
        and diagnostics.get("graph_load_count") == 1
        and diagnostics.get("graph_publication_count") == 1
        and isinstance(fallback_calls, int)
        and not isinstance(fallback_calls, bool)
        and fallback_calls > 0
        and diagnostics.get("native_definition_count")
        == expected.get(
            "native_definition_count", diagnostics.get("native_definition_count")
        )
        and diagnostics.get("native_references_count")
        == expected.get(
            "native_references_count", diagnostics.get("native_references_count")
        )
        and diagnostics.get("native_symbol_call_count")
        == expected.get(
            "native_symbol_call_count", diagnostics.get("native_symbol_call_count")
        )
        and diagnostics.get("fallback_call_count") == fallback_calls
        and diagnostics.get("graph_fallback_count") == fallback_calls
        and diagnostics.get("graph_call_count") == fallback_calls
        and diagnostics.get("receipt_observation_count")
        == expected.get(
            "receipt_observation_count",
            diagnostics.get("receipt_observation_count"),
        )
        and diagnostics.get("terminal_failure") is None
    )


def _concurrency_passed(run: Mapping[str, Any], *, workers: int) -> bool:
    diagnostics = run.get("diagnostics")
    return bool(
        isinstance(diagnostics, Mapping)
        and run.get("calls") == workers
        and run.get("public_errors") == 0
        and run.get("thread_results_identical") is True
        and len(run.get("thread_payload_sha256") or ()) == 1
        and diagnostics.get("state") == "GRAPH_READY"
        and diagnostics.get("logical_backend") == "persisted-symbol-graph-v1"
        and diagnostics.get("fallback_physical_backend")
        == "lazy-persisted-symbol-graph-v1"
        and diagnostics.get("graph_load_attempt_count") == 1
        and diagnostics.get("graph_load_count") == 1
        and diagnostics.get("graph_publication_count") == 1
        and diagnostics.get("fallback_call_count") == workers
        and diagnostics.get("graph_fallback_count") == workers
        and diagnostics.get("graph_call_count") == workers
        and diagnostics.get("receipt_observation_count", 0) >= 2
        and diagnostics.get("terminal_failure") is None
    )


def _worker_config(
    prepared: Mapping[str, Any],
    *,
    arm: str,
    workload: str,
    concurrency_workers: int,
    capture_payload: bool,
) -> dict[str, Any]:
    inputs = prepared["inputs"]
    return {
        "arm": arm,
        "workload": workload,
        "manifest_path": prepared["manifest_path"],
        "project_root": prepared["project_root"],
        "language": prepared["language"],
        "candidate_receipt": prepared["candidate_receipt"],
        "public_fallback_reason": prepared["public_fallback_reason"],
        "public_provider_selection": prepared["public_provider_selection"],
        "symbol_queries": inputs["symbol_queries"],
        "positions": inputs["positions"],
        "route_symbols": inputs["route_symbols"],
        "route_queries": inputs["route_queries"],
        "concurrency_workers": concurrency_workers,
        "capture_payload": capture_payload,
    }


def _run_checked(
    prepared: Mapping[str, Any],
    *,
    arm: str,
    workload: str,
    concurrency_workers: int,
    capture_payload: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    run = _run_isolated_worker(
        _worker_config(
            prepared,
            arm=arm,
            workload=workload,
            concurrency_workers=concurrency_workers,
            capture_payload=capture_payload,
        ),
        timeout_seconds=timeout_seconds,
    )
    _validate_worker_run(
        run,
        arm=arm,
        workload=workload,
        expected_snapshot=prepared["initial_snapshot"],
    )
    if (
        workload != CONCURRENT_WORKLOAD
        and run.get("symbol_query_category_counts")
        != prepared["inputs"]["symbol_query_category_counts"]
    ):
        raise RuntimeError("isolated worker changed the symbol query partition")
    return run


def _require_balanced_samples(
    runs: Mapping[str, Sequence[Mapping[str, Any]]], *, expected: int
) -> None:
    if set(runs) != set(_ARMS):
        raise RuntimeError("symbol samples omit a benchmark arm")
    counts = {arm: len(runs[arm]) for arm in _ARMS}
    if counts != {"legacy": expected, "candidate": expected}:
        raise RuntimeError(
            f"symbol sample imbalance: expected {expected} per arm, got {counts}"
        )


def _canonical_protocol(
    *,
    iterations: int,
    warmups: int,
    requested_query_workload_size: int,
    observed_query_workload_size: int,
    promotion_threshold: float,
    concurrency_workers: int,
) -> dict[str, Any]:
    expected = {
        "iterations_per_arm": DEFAULT_ITERATIONS,
        "warmups_per_arm": DEFAULT_WARMUPS,
        "requested_query_workload_size": DEFAULT_QUERY_WORKLOAD_SIZE,
        "observed_query_workload_size": DEFAULT_QUERY_WORKLOAD_SIZE,
        "promotion_threshold": DEFAULT_PROMOTION_THRESHOLD,
        "concurrency_workers": DEFAULT_CONCURRENCY_WORKERS,
    }
    observed = {
        "iterations_per_arm": iterations,
        "warmups_per_arm": warmups,
        "requested_query_workload_size": requested_query_workload_size,
        "observed_query_workload_size": observed_query_workload_size,
        "promotion_threshold": promotion_threshold,
        "concurrency_workers": concurrency_workers,
    }
    return {"expected": expected, "observed": observed, "passed": observed == expected}


def profile_scip_mcp_consumer(
    *,
    manifest_path: Path,
    project_root: Path,
    subject_id: str,
    subject_manifest: Path = _DEFAULT_SUBJECT_MANIFEST,
    iterations: int = DEFAULT_ITERATIONS,
    warmups: int = DEFAULT_WARMUPS,
    query_workload_size: int = DEFAULT_QUERY_WORKLOAD_SIZE,
    promotion_threshold: float = DEFAULT_PROMOTION_THRESHOLD,
    concurrency_workers: int = DEFAULT_CONCURRENCY_WORKERS,
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT,
) -> dict[str, Any]:
    """Measure one fixed Python or Rust subject and return its independent gate."""

    if isinstance(iterations, bool) or iterations <= 0 or iterations % 2:
        raise ValueError("iterations must be a positive even integer")
    if isinstance(warmups, bool) or warmups < 0 or warmups % 2:
        raise ValueError("warmups must be a non-negative even integer")
    if isinstance(query_workload_size, bool) or query_workload_size < len(
        _SYMBOL_QUERY_CATEGORIES
    ):
        raise ValueError(
            "query_workload_size must cover all six symbol query categories"
        )
    if not 0 <= promotion_threshold < 1:
        raise ValueError("promotion_threshold must be in [0, 1)")
    if isinstance(concurrency_workers, bool) or concurrency_workers < 2:
        raise ValueError("concurrency_workers must be at least 2")
    if worker_timeout_seconds <= 0:
        raise ValueError("worker_timeout_seconds must be positive")

    prepared = _prepare_subject(
        manifest_path=manifest_path,
        project_root=project_root,
        subject_manifest=subject_manifest,
        subject_id=subject_id,
        query_workload_size=query_workload_size,
    )
    canonical_protocol = _canonical_protocol(
        iterations=iterations,
        warmups=warmups,
        requested_query_workload_size=query_workload_size,
        observed_query_workload_size=len(prepared["inputs"]["symbol_queries"]),
        promotion_threshold=promotion_threshold,
        concurrency_workers=concurrency_workers,
    )
    warmup_schedule = _abba_schedule(warmups)
    measured_schedule = _abba_schedule(iterations)

    warmup_runs: list[dict[str, Any]] = []
    for entry in warmup_schedule:
        run = _run_checked(
            prepared,
            arm=entry["arm"],
            workload=PERFORMANCE_WORKLOAD,
            concurrency_workers=concurrency_workers,
            capture_payload=False,
            timeout_seconds=worker_timeout_seconds,
        )
        run["schedule"] = dict(entry)
        warmup_runs.append(run)

    symbol_runs: dict[str, list[dict[str, Any]]] = {
        "legacy": [],
        "candidate": [],
    }
    measured_runs: list[dict[str, Any]] = []
    for entry in measured_schedule:
        arm = str(entry["arm"])
        run = _run_checked(
            prepared,
            arm=arm,
            workload=PERFORMANCE_WORKLOAD,
            concurrency_workers=concurrency_workers,
            capture_payload=False,
            timeout_seconds=worker_timeout_seconds,
        )
        run["schedule"] = dict(entry)
        symbol_runs[arm].append(run)
        measured_runs.append(run)
    _require_balanced_samples(symbol_runs, expected=iterations)

    correctness_orders = {
        "position_first": ("legacy", "candidate"),
        "route_first": ("candidate", "legacy"),
        "mixed": ("legacy", "candidate"),
    }
    correctness_runs: dict[str, dict[str, dict[str, Any]]] = {}
    for workload, order in correctness_orders.items():
        correctness_runs[workload] = {}
        for arm in order:
            correctness_runs[workload][arm] = _run_checked(
                prepared,
                arm=arm,
                workload=workload,
                concurrency_workers=concurrency_workers,
                capture_payload=False,
                timeout_seconds=worker_timeout_seconds,
            )

    concurrency_run = _run_checked(
        prepared,
        arm="candidate",
        workload=CONCURRENT_WORKLOAD,
        concurrency_workers=concurrency_workers,
        capture_payload=False,
        timeout_seconds=worker_timeout_seconds,
    )

    final_snapshot = _observe_receipt(prepared["candidate_receipt"])
    artifact_after = _artifact_receipts(prepared["candidate_receipt"])
    git_after = _git_identity(Path(prepared["project_root"]))
    benchmark_git_after = _git_identity(_PROJECT_ROOT)
    benchmark_sources_after = {
        "harness": _file_receipt(Path(__file__)),
        "provider": _file_receipt(
            _PROJECT_ROOT / "codenib/scip_interface/scip_query_provider.py"
        ),
        "snapshot_observer": _file_receipt(
            _PROJECT_ROOT / "codenib/scip_interface/scip_query.py"
        ),
    }
    immutable_benchmark = (
        benchmark_git_after == prepared["benchmark_git_before"]
        and benchmark_git_after.get("dirty") is False
        and benchmark_sources_after == prepared["benchmark_sources_before"]
    )
    immutable_inputs = (
        final_snapshot == prepared["initial_snapshot"]
        and artifact_after == prepared["artifact_before"]
        and git_after == prepared["git_before"]
        and git_after.get("dirty") is False
    )

    all_runs = [
        *warmup_runs,
        *measured_runs,
        *(
            run
            for workload_runs in correctness_runs.values()
            for run in workload_runs.values()
        ),
        concurrency_run,
    ]
    process_ids = [int(run["process_id"]) for run in all_runs]
    process_isolation = len(process_ids) == len(set(process_ids))

    legacy_summary = _summarize_runs(symbol_runs["legacy"])
    candidate_summary = _summarize_runs(symbol_runs["candidate"])
    symbol_parity = _parity(symbol_runs["legacy"], symbol_runs["candidate"])
    graph_free = all(
        _symbol_candidate_graph_free(
            run,
            expected_symbol_count=len(prepared["inputs"]["symbol_queries"]),
        )
        for run in symbol_runs["candidate"]
    )
    promotion = _promotion_decision(
        legacy_summary["wall_seconds"],
        candidate_summary["wall_seconds"],
        threshold=promotion_threshold,
        parity=symbol_parity,
        graph_free=graph_free,
    )

    correctness: dict[str, Any] = {}
    correctness_passed = True
    for workload in CORRECTNESS_WORKLOADS:
        legacy = correctness_runs[workload]["legacy"]
        candidate = correctness_runs[workload]["candidate"]
        parity = _parity([legacy], [candidate])
        expected_counts = _expected_candidate_counts(workload, prepared["inputs"])
        lazy_graph_ready = _correctness_candidate_ready(
            candidate, expected=expected_counts
        )
        passed = parity and lazy_graph_ready
        correctness_passed = correctness_passed and passed
        correctness[workload] = {
            "performance_gated": False,
            "parity": parity,
            "lazy_graph_ready": lazy_graph_ready,
            "expected_candidate_counts": expected_counts,
            "passed": passed,
            "arm_order": list(correctness_orders[workload]),
            "legacy": _summarize_runs([legacy]),
            "candidate": _summarize_runs([candidate]),
            "raw": {"legacy": legacy, "candidate": candidate},
        }

    concurrent_passed = _concurrency_passed(
        concurrency_run, workers=concurrency_workers
    )
    overall_passed = bool(
        canonical_protocol["passed"]
        and promotion["passed"]
        and correctness_passed
        and concurrent_passed
        and immutable_inputs
        and immutable_benchmark
        and process_isolation
    )
    return {
        "schema_version": 1,
        "benchmark": "scip_mcp_consumer_gate_v1",
        "language": prepared["language"],
        "passed": overall_passed,
        "subject": {
            "id": subject_id,
            "entry": prepared["subject_manifest"]["entry"],
            "project_root": prepared["project_root"],
            "manifest_path": prepared["manifest_path"],
            "git_before": prepared["git_before"],
            "git_after": git_after,
            "immutable_checkout": immutable_inputs,
            "definition_symbol_count": prepared["inputs"]["definition_symbol_count"],
            "graph_vertex_count": prepared["inputs"]["graph_vertex_count"],
            "graph_edge_count": prepared["inputs"]["graph_edge_count"],
        },
        "benchmark_checkout": {
            "git_before": prepared["benchmark_git_before"],
            "git_after": benchmark_git_after,
            "sources_before": prepared["benchmark_sources_before"],
            "sources_after": benchmark_sources_after,
            "unchanged_and_clean": immutable_benchmark,
        },
        "subject_manifest": {
            "path": prepared["subject_manifest"]["path"],
            "sha256": prepared["subject_manifest"]["sha256"],
            "schema_version": prepared["subject_manifest"]["payload"]["schema_version"],
        },
        "configuration": {
            "performance_workload": PERFORMANCE_WORKLOAD,
            "iterations_per_arm": iterations,
            "warmups_per_arm": warmups,
            "query_workload_size": len(prepared["inputs"]["symbol_queries"]),
            "symbol_query_category_counts": prepared["inputs"][
                "symbol_query_category_counts"
            ],
            "symbol_calls_per_seed": {
                "definition": 1,
                "references_include_declaration_false": 1,
                "references_include_declaration_true": 1,
            },
            "promotion_threshold": promotion_threshold,
            "percentile_method": "nearest-rank",
            "warmup_arm_order": warmup_schedule,
            "measured_arm_order": measured_schedule,
            "correctness_arm_order": correctness_orders,
            "concurrency_workers": concurrency_workers,
            "worker_timeout_seconds": worker_timeout_seconds,
            "fresh_process_per_sample": True,
            "process_isolation_observed": process_isolation,
            "cold_start_scope": "fresh process; filesystem page cache uncontrolled",
            "promotion_metric": "wall_seconds from provider startup through workload",
            "canonical_protocol": canonical_protocol,
        },
        "receipts": {
            "candidate": prepared["candidate_receipt"],
            "snapshot_before": prepared["initial_snapshot"],
            "snapshot_after": final_snapshot,
            "artifacts_before": prepared["artifact_before"],
            "artifacts_after": artifact_after,
            "unchanged": immutable_inputs,
        },
        "symbol_only": {
            "performance_gated": True,
            "parity": symbol_parity,
            "graph_free": graph_free,
            "legacy": legacy_summary,
            "candidate": candidate_summary,
            "promotion": promotion,
        },
        "correctness": correctness,
        "concurrent_first_fallback": {
            "performance_gated": False,
            "workers": concurrency_workers,
            "passed": concurrent_passed,
            "observations": _summarize_runs([concurrency_run]),
            "raw": concurrency_run,
        },
        "raw": {
            "warmups": warmup_runs,
            "symbol_measured": measured_runs,
        },
        "gates": {
            "canonical_protocol": canonical_protocol["passed"],
            "symbol_p50": promotion["p50"]["passed"],
            "symbol_p95": promotion["p95"]["passed"],
            "symbol_parity": symbol_parity,
            "symbol_graph_free": graph_free,
            "correctness_parity": correctness_passed,
            "concurrent_single_publication": concurrent_passed,
            "immutable_inputs": immutable_inputs,
            "immutable_benchmark": immutable_benchmark,
            "process_isolation": process_isolation,
            "passed": overall_passed,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--subject-id")
    parser.add_argument(
        "--subject-manifest",
        type=Path,
        default=_DEFAULT_SUBJECT_MANIFEST,
    )
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument(
        "--query-workload-size",
        type=int,
        default=DEFAULT_QUERY_WORKLOAD_SIZE,
    )
    parser.add_argument(
        "--promotion-threshold",
        type=float,
        default=DEFAULT_PROMOTION_THRESHOLD,
    )
    parser.add_argument(
        "--concurrency-workers",
        type=int,
        default=DEFAULT_CONCURRENCY_WORKERS,
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=DEFAULT_WORKER_TIMEOUT,
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        try:
            config = json.load(sys.stdin)
            if not isinstance(config, Mapping):
                raise ValueError("worker input must be a JSON object")
            result = _worker(config)
        except MemoryError as exc:
            print(
                json.dumps(
                    {"error_type": "MemoryError", "message": str(exc)},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return _MEMORY_ERROR_EXIT
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0

    if args.manifest is None or args.project_root is None or not args.subject_id:
        _parser().error("--manifest, --project-root, and --subject-id are required")
    report = profile_scip_mcp_consumer(
        manifest_path=args.manifest,
        project_root=args.project_root,
        subject_id=args.subject_id,
        subject_manifest=args.subject_manifest,
        iterations=args.iterations,
        warmups=args.warmups,
        query_workload_size=args.query_workload_size,
        promotion_threshold=args.promotion_threshold,
        concurrency_workers=args.concurrency_workers,
        worker_timeout_seconds=args.worker_timeout_seconds,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
