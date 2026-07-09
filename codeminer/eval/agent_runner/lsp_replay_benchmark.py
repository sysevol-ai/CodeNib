# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-level LSP request replay benchmark.

This benchmark keeps the latency question below the agent loop: generate or
load deterministic LSP-shaped requests, replay the same requests against
CodeMiner's static graph provider and a live JSON-RPC language server, then
aggregate latency only for rows whose agent-visible fingerprints match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from codeminer.agent.lsp_provider import (
    CAPABILITY_DEFINITION,
    CAPABILITY_REFERENCES,
    JSON_RPC_LSP_PROVIDER,
    StaticLSPProvider,
    normalize_lsp_capability,
)
from codeminer.compiler.snapshot_store import SourceSnapshot
from codeminer.ls_index.index_quality import (
    IndexQualityPolicy,
    assess_index_quality,
    discover_compilation_database,
)
from codeminer.types import EDGE_TYPE_REFERENCE

from .live_lsp_provider import LiveLSPReferenceProvider
from .lsp_provider_cli import load_lsp_provider_requests
from .lsp_provider_validation import (
    FingerprintSelector,
    LSPProviderRequest,
    compare_static_lsp_provider,
    default_lsp_provider_fingerprint,
)
from .lsp_readiness import wait_for_lsp_provider_readiness

Clock = Callable[[], float]
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NATIVE_CAPABILITIES = frozenset({CAPABILITY_DEFINITION, CAPABILITY_REFERENCES})


def generate_lsp_replay_requests(
    graph: Any,
    *,
    project_root: str | Path | None = None,
    capabilities: Sequence[str] = (CAPABILITY_DEFINITION, CAPABILITY_REFERENCES),
    max_per_capability: int = 50,
    definition_top_k: int = 8,
    references_top_k: int = 40,
    include_declaration: bool = True,
) -> list[LSPProviderRequest]:
    """Generate deterministic file-position LSP replay requests from graph refs.

    Reference edges carry real source anchors and target symbols, which lets the
    benchmark place the cursor on a symbol token instead of hand-picking lines.
    Requests that cannot be tied back to a source token are skipped.
    """

    if max_per_capability < 1:
        raise ValueError("max_per_capability must be positive")
    requested_capabilities = [
        normalize_lsp_capability(capability) for capability in capabilities
    ]
    unsupported = [
        capability
        for capability in requested_capabilities
        if capability not in _NATIVE_CAPABILITIES
    ]
    if unsupported:
        raise ValueError(
            "replay request generation supports only native capabilities "
            f"({', '.join(sorted(_NATIVE_CAPABILITIES))}); got {unsupported}"
        )

    counts = Counter()
    seen: set[tuple[str, str, int, int]] = set()
    requests: list[LSPProviderRequest] = []
    candidates = _reference_position_candidates(
        graph, project_root=_project_root(graph, project_root)
    )
    for candidate in _evenly_spaced(candidates, max_per_capability):
        for capability in requested_capabilities:
            if counts[capability] >= max_per_capability:
                continue
            key = (
                capability,
                candidate["file_path"],
                candidate["line"],
                candidate["character"],
            )
            if key in seen:
                continue
            seen.add(key)
            args: dict[str, Any] = {
                "file_path": candidate["file_path"],
                "line": candidate["line"],
                "character": candidate["character"],
            }
            if capability == CAPABILITY_DEFINITION:
                args["top_k"] = definition_top_k
            else:
                args["include_declaration"] = include_declaration
                args["top_k"] = references_top_k
            requests.append(
                LSPProviderRequest(
                    capability=f"textDocument/{capability}",
                    arguments=args,
                    request_id=_request_id(capability, candidate),
                )
            )
            counts[capability] += 1
        if all(
            counts[capability] >= max_per_capability
            for capability in requested_capabilities
        ):
            break
    return requests


def run_lsp_replay_benchmark(
    requests: Iterable[LSPProviderRequest | Mapping[str, Any]],
    *,
    graph: Any,
    project_root: str | Path,
    language: str,
    source_repo: Optional[str] = None,
    source_commit: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
    warmup_reps: int = 1,
    warmup_until_stable: bool = False,
    max_warmup_reps: int = 10,
    warmup_poll_seconds: float = 1.0,
    min_live_nonempty_fraction: float = 0.1,
    minimum_equivalent_count: int = 0,
    measured_reps: int = 5,
    skip_probe: bool = False,
    compdb_path: str | Path | None = None,
    baseline_graph: Any = None,
    quality_policy: IndexQualityPolicy | None = None,
    allow_low_quality_artifact: bool = False,
    wait_until_idle: bool = False,
    idle_timeout_s: float = 60.0,
    fingerprint_selector: FingerprintSelector = default_lsp_provider_fingerprint,
    clock: Clock = time.monotonic,
    live_provider_factory: Callable[..., LiveLSPReferenceProvider] = (
        LiveLSPReferenceProvider
    ),
) -> dict[str, Any]:
    """Run a warm provider-level replay benchmark and return JSON-ready data."""

    if warmup_reps < 0:
        raise ValueError("warmup_reps must be non-negative")
    if max_warmup_reps < max(1, warmup_reps):
        raise ValueError("max_warmup_reps must be at least warmup_reps")
    if not 0 <= min_live_nonempty_fraction <= 1:
        raise ValueError("min_live_nonempty_fraction must be between 0 and 1")
    if measured_reps < 1:
        raise ValueError("measured_reps must be positive")
    if bool(source_repo) != bool(source_commit):
        raise ValueError("source_repo and source_commit must be provided together")

    request_list = [_coerce_request(request) for request in requests]
    _validate_native_requests(request_list)
    resolved_compdb = compdb_path or discover_compilation_database(project_root)
    artifact_quality = assess_index_quality(
        graph,
        project_root=project_root,
        language=language,
        compdb_path=resolved_compdb,
        baseline_graph=baseline_graph,
        policy=quality_policy,
    )
    if not artifact_quality["passed"] and not allow_low_quality_artifact:
        failures = ", ".join(artifact_quality["failure_names"])
        raise ValueError(f"graph artifact quality guardrail failed: {failures}")

    static_init_start = clock()
    static_provider = StaticLSPProvider(graph)
    static_provider_init_ms = (clock() - static_init_start) * 1000

    live_provider = live_provider_factory(
        project_root=project_root,
        language=language,
        command=command,
        skip_probe=skip_probe,
    )
    idle_wait_ms: Optional[float] = None
    warmup_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    live_start = clock()
    live_provider.start()
    live_start_ms = (clock() - live_start) * 1000
    with live_provider:
        if wait_until_idle:
            idle_wait_start = clock()
            wait = getattr(live_provider, "wait_until_idle", None)
            if not callable(wait):
                raise RuntimeError(
                    "live reference provider does not support idle waiting"
                )
            if not wait(max_wait_s=idle_timeout_s):
                raise RuntimeError(
                    "live reference provider did not become idle within "
                    f"{idle_timeout_s}s"
                )
            idle_wait_ms = (clock() - idle_wait_start) * 1000

        readiness = wait_for_lsp_provider_readiness(
            request_list,
            graph=graph,
            static_provider=static_provider,
            live_provider=live_provider,
            minimum_reps=warmup_reps,
            until_stable=warmup_until_stable,
            max_reps=max_warmup_reps,
            poll_seconds=warmup_poll_seconds,
            min_live_nonempty_fraction=min_live_nonempty_fraction,
            minimum_equivalent_count=minimum_equivalent_count,
            fingerprint_selector=fingerprint_selector,
            clock=clock,
        )
        warmup_rows.extend(readiness.rows)

        for rep in range(1, measured_reps + 1):
            comparisons = compare_static_lsp_provider(
                request_list,
                graph=graph,
                static_provider=static_provider,
                reference_provider=live_provider,
                reference_provider_name=JSON_RPC_LSP_PROVIDER,
                fingerprint_selector=fingerprint_selector,
                clock=clock,
            )
            for comparison in comparisons:
                row = comparison.to_dict()
                row["rep"] = rep
                comparison_rows.append(row)

    effective_command = getattr(live_provider, "effective_command", None)
    if effective_command is None:
        effective_command = getattr(live_provider, "command", None) or command
    payload = {
        "schema_version": 2,
        "benchmark": "lsp_request_replay",
        "subject": _benchmark_subject(
            graph,
            project_root=project_root,
            language=language,
            source_repo=source_repo,
            source_commit=source_commit,
            live_command=effective_command,
        ),
        "request_source": {"kind": "provided"},
        "request_count": len(request_list),
        "warmup_reps": readiness.completed_reps,
        "warmup_protocol": {
            "minimum_reps": warmup_reps,
            "until_stable": warmup_until_stable,
            "max_reps": max_warmup_reps,
            "poll_seconds": warmup_poll_seconds,
            "min_live_nonempty_fraction": min_live_nonempty_fraction,
            "minimum_equivalent_count": minimum_equivalent_count,
            "stable": readiness.stable,
            "live_nonempty_count": readiness.live_nonempty_count,
            "equivalent_count": readiness.equivalent_count,
        },
        "measured_reps": measured_reps,
        "warmup_summary": _summarize_rows(warmup_rows),
        "setup": {
            "static_provider_init_ms": static_provider_init_ms,
            "live_start_ms": live_start_ms,
            "idle_wait_ms": idle_wait_ms,
            "warmup_wall_ms": readiness.wall_ms,
        },
        "artifact_quality": artifact_quality,
        "requests": [request.to_dict() for request in request_list],
        "comparisons": comparison_rows,
    }
    payload["summary"] = summarize_lsp_replay_benchmark(payload)
    return payload


def summarize_lsp_replay_benchmark(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate replay rows into equivalence guardrails and latency metrics."""

    rows = [
        dict(row)
        for row in payload.get("comparisons") or []
        if isinstance(row, Mapping)
    ]
    by_capability: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        request = row.get("request") or {}
        capability = str(request.get("capability") or row.get("capability") or "")
        by_capability.setdefault(normalize_lsp_capability(capability), []).append(row)

    summary_by_capability = {
        capability: _summarize_rows(capability_rows)
        for capability, capability_rows in sorted(by_capability.items())
    }
    return {
        "overall": _summarize_rows(rows),
        "by_capability": summary_by_capability,
    }


def render_lsp_replay_benchmark_markdown(payload: Mapping[str, Any]) -> str:
    """Render replay benchmark results as a compact markdown report."""

    summary = payload.get("summary") or summarize_lsp_replay_benchmark(payload)
    setup = payload.get("setup") or {}
    lines = ["# LSP request replay benchmark", ""]
    lines.append(
        "Latency distributions include only rows whose static and live "
        "agent-visible fingerprints are equivalent. Mismatches and errors are "
        "reported as guardrail failures, not folded into speedup metrics."
    )
    lines.append("")
    lines.append(
        f"Requests: {payload.get('request_count', 'n/a')}; "
        f"warmup reps: {payload.get('warmup_reps', 'n/a')}; "
        f"measured reps: {payload.get('measured_reps', 'n/a')}"
    )
    setup_parts = []
    if "graph_load_ms" in setup:
        setup_parts.append(f"graph load {_fmt(setup.get('graph_load_ms'), 2)} ms")
    setup_parts.extend(
        [
            f"static provider init {_fmt(setup.get('static_provider_init_ms'), 2)} ms",
            f"live LSP start {_fmt(setup.get('live_start_ms'), 2)} ms",
        ]
    )
    lines.append("Setup: " + "; ".join(setup_parts))
    quality = payload.get("artifact_quality") or {}
    if quality:
        failures = ", ".join(quality.get("failure_names") or []) or "none"
        compdb = quality.get("compile_db") or {}
        lines.append(
            "Artifact quality: "
            f"{_yes_no(quality.get('passed'))}; "
            f"compile DB entries {compdb.get('entry_count', 'n/a')}; "
            f"source coverage {_fmt_percent(compdb.get('source_coverage'))}; "
            "graph/compile DB coverage "
            f"{_fmt_percent(compdb.get('graph_compdb_coverage'))}; "
            f"failures {failures}"
        )
    lines.append("")
    head = [
        "scope",
        "equiv/rows",
        "mismatch",
        "errors",
        "static p50/p95/p99 ms",
        "live p50/p95/p99 ms",
        "speedup p50",
        "saved p50 ms",
    ]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("| " + " | ".join("---" for _ in head) + " |")
    rows = [("overall", summary.get("overall") or {})]
    rows.extend(
        (capability, stats)
        for capability, stats in (summary.get("by_capability") or {}).items()
    )
    for label, stats in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(label),
                    f"{stats.get('equivalent_row_count', 0)}/{stats.get('row_count', 0)}",
                    str(stats.get("mismatch_count", 0)),
                    str(stats.get("error_count", 0)),
                    _latency_triplet(stats.get("static_ms") or {}),
                    _latency_triplet(stats.get("live_ms") or {}),
                    _fmt((stats.get("speedup_ratio") or {}).get("p50"), 2),
                    _fmt((stats.get("latency_saved_ms") or {}).get("p50"), 2),
                ]
            )
            + " |"
        )
    lines.append("")
    guardrail_pass = (summary.get("overall") or {}).get("guardrail_pass")
    lines.append(f"Equivalence guardrail: {_yes_no(guardrail_pass)}")
    lines.append("")
    return "\n".join(lines)


def exit_code_for_lsp_replay_benchmark(
    payload: Mapping[str, Any],
    *,
    require_all_equivalent: bool = False,
    allow_low_quality_artifact: bool = False,
) -> int:
    """Return a process exit code for replay benchmark gates."""

    quality = payload.get("artifact_quality") or {}
    if quality.get("passed") is False and not allow_low_quality_artifact:
        return 4
    overall = (payload.get("summary") or {}).get("overall") or {}
    if int(overall.get("row_count") or 0) == 0:
        return 2
    if int(overall.get("equivalent_row_count") or 0) == 0:
        return 2
    if int(overall.get("error_count") or 0) > 0:
        return 2
    if require_all_equivalent and int(overall.get("equivalent_row_count") or 0) != int(
        overall.get("row_count") or 0
    ):
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--graph", help="Path to a CodeGraph graph.pkl")
    source.add_argument(
        "--prebuilt-root", help="Prebuilt root containing instance dirs"
    )
    parser.add_argument("--instance-id", help="Required with --prebuilt-root")
    parser.add_argument(
        "--project-root",
        help="Repository root for live LSP. Defaults to prebuilt instance repo.",
    )
    parser.add_argument("--language", required=True, help="Graph/LSP language key")
    parser.add_argument(
        "--compile-db",
        help=(
            "Compilation database used to audit C/C++ source coverage. "
            "Defaults to project-root/compile_commands.json or build/."
        ),
    )
    parser.add_argument(
        "--baseline-graph",
        help="Optional previous graph.pkl used for vertex/edge shrink guardrails.",
    )
    parser.add_argument("--min-compile-db-entries", type=int, default=1)
    parser.add_argument("--min-source-coverage", type=float, default=0.01)
    parser.add_argument("--min-graph-compdb-coverage", type=float, default=0.5)
    parser.add_argument("--min-baseline-graph-ratio", type=float, default=0.5)
    parser.add_argument(
        "--allow-low-quality-artifact",
        action="store_true",
        help="Run and report replay even when the artifact quality gate fails.",
    )
    parser.add_argument(
        "--source-repo",
        help="Canonical repository name used for snapshot identity",
    )
    parser.add_argument(
        "--source-commit",
        help="Exact source commit used for snapshot identity",
    )
    parser.add_argument(
        "--requests",
        help=(
            "Optional JSON/JSONL request file. If omitted, requests are "
            "generated deterministically from graph reference edges."
        ),
    )
    parser.add_argument(
        "--capabilities",
        default="definition,references",
        help="Comma-separated capabilities to generate when --requests is omitted.",
    )
    parser.add_argument(
        "--max-per-capability",
        type=int,
        default=50,
        help="Generated request cap per capability.",
    )
    parser.add_argument("--warmup-reps", type=int, default=1)
    parser.add_argument("--warmup-until-stable", action="store_true")
    parser.add_argument("--max-warmup-reps", type=int, default=10)
    parser.add_argument("--warmup-poll-seconds", type=float, default=1.0)
    parser.add_argument("--min-live-nonempty-fraction", type=float, default=0.1)
    parser.add_argument(
        "--minimum-equivalent-count",
        type=int,
        default=0,
        help=(
            "Require this many equivalent requests before live behavior can "
            "be considered ready."
        ),
    )
    parser.add_argument("--measured-reps", type=int, default=5)
    parser.add_argument(
        "--command",
        help="Optional live LSP command string, for example 'pyright-langserver --stdio'",
    )
    parser.add_argument("--skip-probe", action="store_true")
    parser.add_argument(
        "--wait-until-idle",
        action="store_true",
        help="Wait for live-server background analysis before warmups",
    )
    parser.add_argument("--idle-timeout", type=float, default=60.0)
    parser.add_argument("--output-json", help="Write machine-readable benchmark report")
    parser.add_argument("--output-markdown", help="Write markdown benchmark report")
    parser.add_argument(
        "--require-all-equivalent",
        action="store_true",
        help="Exit non-zero if any measured row has a mismatch.",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        project_root = _project_root_from_args(args)
        graph, graph_load_ms = _load_graph_from_args(args, project_root=project_root)
        if args.requests:
            requests = load_lsp_provider_requests(args.requests)
        else:
            requests = generate_lsp_replay_requests(
                graph,
                project_root=project_root,
                capabilities=_split_csv(args.capabilities),
                max_per_capability=args.max_per_capability,
            )
        if not requests:
            raise ValueError("no LSP replay requests available")
        baseline_graph = None
        if args.baseline_graph:
            from .prebuilt import load_code_graph_artifact

            baseline_graph = load_code_graph_artifact(
                args.baseline_graph, project_root=project_root
            )
        quality_policy = IndexQualityPolicy(
            min_compile_db_entries=args.min_compile_db_entries,
            min_source_coverage=args.min_source_coverage,
            min_graph_compdb_coverage=args.min_graph_compdb_coverage,
            min_baseline_vertex_ratio=args.min_baseline_graph_ratio,
            min_baseline_edge_ratio=args.min_baseline_graph_ratio,
        )
        payload = run_lsp_replay_benchmark(
            requests,
            graph=graph,
            project_root=project_root,
            language=args.language,
            source_repo=args.source_repo,
            source_commit=args.source_commit,
            command=_command_from_arg(args.command),
            warmup_reps=args.warmup_reps,
            warmup_until_stable=args.warmup_until_stable,
            max_warmup_reps=args.max_warmup_reps,
            warmup_poll_seconds=args.warmup_poll_seconds,
            min_live_nonempty_fraction=args.min_live_nonempty_fraction,
            minimum_equivalent_count=args.minimum_equivalent_count,
            measured_reps=args.measured_reps,
            skip_probe=args.skip_probe,
            compdb_path=args.compile_db,
            baseline_graph=baseline_graph,
            quality_policy=quality_policy,
            allow_low_quality_artifact=args.allow_low_quality_artifact,
            wait_until_idle=args.wait_until_idle,
            idle_timeout_s=args.idle_timeout,
        )
        if args.requests:
            request_path = Path(args.requests).resolve()
            payload["request_source"] = {
                "kind": "file",
                "path": str(request_path),
                "sha256": _file_sha256(request_path),
            }
        else:
            payload["request_source"] = {
                "kind": "graph_reference_edges",
                "capabilities": _split_csv(args.capabilities),
                "max_per_capability": args.max_per_capability,
            }
        graph_source = _graph_source_from_args(args)
        payload["subject"]["graph"].update(
            {
                "artifact_path": str(graph_source),
                "artifact_sha256": _file_sha256(graph_source),
            }
        )
        payload["setup"]["graph_load_ms"] = graph_load_ms
        _write_reports(
            payload,
            output_json=args.output_json,
            output_markdown=args.output_markdown,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should fail cleanly.
        parser.exit(1, f"codeminer-lsp-replay-benchmark: {exc}\n")

    if not args.quiet and not args.output_markdown:
        sys.stdout.write(render_lsp_replay_benchmark_markdown(payload))
    return exit_code_for_lsp_replay_benchmark(
        payload,
        require_all_equivalent=args.require_all_equivalent,
        allow_low_quality_artifact=args.allow_low_quality_artifact,
    )


def _reference_position_candidates(
    graph: Any, *, project_root: Optional[Path]
) -> list[dict[str, Any]]:
    if not hasattr(graph, "graph"):
        return []
    candidates: list[dict[str, Any]] = []
    for edge in graph.graph.es:
        attrs = edge.attributes()
        if attrs.get("type") != EDGE_TYPE_REFERENCE:
            continue
        file_path = attrs.get("anchor_file")
        line = _coerce_int(attrs.get("anchor_line"))
        if not file_path or line is None:
            continue
        target_name = _node_name_for_vid(graph, edge.target)
        if not target_name:
            continue
        character = _source_character_for_target(
            graph,
            target_name=target_name,
            file_path=str(file_path),
            line=line,
            project_root=project_root,
        )
        if character is None:
            continue
        candidates.append(
            {
                "file_path": str(file_path),
                "line": line,
                "character": character,
                "target_name": target_name,
            }
        )
    candidates.sort(
        key=lambda item: (
            item["file_path"],
            item["line"],
            item["character"],
            item["target_name"],
        )
    )
    return candidates


def _evenly_spaced(
    candidates: Sequence[Mapping[str, Any]], limit: int
) -> list[Mapping[str, Any]]:
    if limit >= len(candidates):
        return list(candidates)
    if limit == 1:
        return [candidates[len(candidates) // 2]]
    return [
        candidates[round(index * (len(candidates) - 1) / (limit - 1))]
        for index in range(limit)
    ]


def _source_character_for_target(
    graph: Any,
    *,
    target_name: str,
    file_path: str,
    line: int,
    project_root: Optional[Path],
) -> Optional[int]:
    line_text = _source_line(file_path, line, project_root=project_root)
    if line_text is None:
        return None
    for token in _target_tokens(graph, target_name):
        match = re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
            line_text,
        )
        if match:
            return int(match.start())
    return None


def _target_tokens(graph: Any, target_name: str) -> list[str]:
    info = graph.get_node_info_by_name(target_name) or {}
    seeds = [
        target_name,
        str(info.get("name") or ""),
        str(info.get("unified_name") or ""),
    ]
    tokens: set[str] = set()
    for seed in seeds:
        token = _bare_symbol(seed)
        if token and _IDENT_RE.fullmatch(token):
            tokens.add(token)
    return sorted(tokens, key=lambda value: (-len(value), value))


def _source_line(
    file_path: str,
    line: int,
    *,
    project_root: Optional[Path],
) -> Optional[str]:
    path = Path(file_path)
    if not path.is_absolute():
        if project_root is None:
            return None
        path = project_root / path
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[line]
    except (OSError, IndexError):
        return None


def _project_root(graph: Any, project_root: str | Path | None) -> Optional[Path]:
    root = (
        project_root
        if project_root is not None
        else getattr(graph, "project_root", None)
    )
    return Path(root).resolve() if root else None


def _benchmark_subject(
    graph: Any,
    *,
    project_root: str | Path,
    language: str,
    source_repo: Optional[str],
    source_commit: Optional[str],
    live_command: Optional[Sequence[str]],
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    snapshot = (
        SourceSnapshot(source_repo, source_commit)
        if source_repo is not None and source_commit is not None
        else None
    )
    graph_data = getattr(graph, "graph", None)
    vertex_attributes = (
        set(graph_data.vs.attribute_names()) if graph_data is not None else set()
    )
    return {
        "project_root": str(root),
        "repository": snapshot.repo if snapshot is not None else None,
        "git_commit": snapshot.commit if snapshot is not None else _git_commit(root),
        "snapshot_id": snapshot.snapshot_id if snapshot is not None else None,
        "language": language,
        "graph": {
            "node_count": graph_data.vcount() if graph_data is not None else None,
            "edge_count": graph_data.ecount() if graph_data is not None else None,
            "has_selection_line": "selection_line" in vertex_attributes,
        },
        "reference_provider": {
            "provider": JSON_RPC_LSP_PROVIDER,
            "command": list(live_command) if live_command is not None else None,
        },
    }


def _git_commit(project_root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(str(row.get("verdict") or "unknown") for row in rows)
    equivalent = [row for row in rows if row.get("same_result") is True]
    errors = sum(
        1
        for row in rows
        if str(row.get("verdict") or "")
        in {"static_error", "reference_error", "unknown"}
    )
    static_ms = [_duration(row, "static_call") for row in equivalent]
    live_ms = [_duration(row, "reference_call") for row in equivalent]
    speedups = [
        float(row["speedup_ratio"])
        for row in equivalent
        if isinstance(row.get("speedup_ratio"), (int, float))
    ]
    saved = [
        float(row["latency_saved_ms"])
        for row in equivalent
        if isinstance(row.get("latency_saved_ms"), (int, float))
    ]
    return {
        "row_count": len(rows),
        "equivalent_row_count": len(equivalent),
        "mismatch_count": verdicts.get("mismatch", 0),
        "error_count": errors,
        "verdict_counts": dict(sorted(verdicts.items())),
        "guardrail_pass": bool(rows) and len(equivalent) == len(rows),
        "static_ms": _latency_stats(static_ms),
        "live_ms": _latency_stats(live_ms),
        "speedup_ratio": _latency_stats(speedups),
        "latency_saved_ms": _latency_stats(saved),
    }


def _duration(row: Mapping[str, Any], key: str) -> Optional[float]:
    call = row.get(key) or {}
    if not isinstance(call, Mapping):
        return None
    value = call.get("duration_ms")
    return float(value) if isinstance(value, (int, float)) else None


def _latency_stats(values: Sequence[Optional[float]]) -> dict[str, Optional[float]]:
    data = sorted(float(value) for value in values if isinstance(value, (int, float)))
    if not data:
        return {
            "count": 0,
            "p50": None,
            "p95": None,
            "p99": None,
            "min": None,
            "max": None,
        }
    return {
        "count": len(data),
        "p50": _percentile(data, 50),
        "p95": _percentile(data, 95),
        "p99": _percentile(data, 99),
        "min": data[0],
        "max": data[-1],
    }


def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _latency_triplet(stats: Mapping[str, Any]) -> str:
    return "/".join(
        [
            _fmt(stats.get("p50"), 2),
            _fmt(stats.get("p95"), 2),
            _fmt(stats.get("p99"), 2),
        ]
    )


def _coerce_request(raw: LSPProviderRequest | Mapping[str, Any]) -> LSPProviderRequest:
    if isinstance(raw, LSPProviderRequest):
        return raw
    capability = str(raw.get("capability") or raw.get("lsp_method") or "")
    arguments = raw.get("arguments") or raw.get("resolved_arguments") or {}
    return LSPProviderRequest(
        capability=capability,
        arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
        request_id=raw.get("request_id"),
    )


def _validate_native_requests(requests: Sequence[LSPProviderRequest]) -> None:
    unsupported = [
        f"{request.request_id or index}:{request.normalized_capability or '<missing>'}"
        for index, request in enumerate(requests, 1)
        if request.normalized_capability not in _NATIVE_CAPABILITIES
    ]
    if unsupported:
        allowed = ", ".join(sorted(_NATIVE_CAPABILITIES))
        raise ValueError(
            "LSP replay benchmark supports only native capabilities "
            f"({allowed}); unsupported requests: {', '.join(unsupported)}"
        )


def _node_name_for_vid(graph: Any, vid: int) -> Optional[str]:
    info = graph.get_node_info_by_id(vid) or {}
    value = info.get("name")
    return str(value) if value else None


def _bare_symbol(symbol: str) -> str:
    return symbol.split(":")[-1].split(".")[-1].split("#")[-1].rstrip("()").strip()


def _request_id(capability: str, candidate: Mapping[str, Any]) -> str:
    raw = (
        f"{capability}:{candidate['file_path']}:{candidate['line']}:"
        f"{candidate['character']}:{_bare_symbol(str(candidate['target_name']))}"
    )
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw)


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_graph_from_args(
    args: argparse.Namespace, *, project_root: str
) -> tuple[Any, float]:
    from .prebuilt import load_code_graph_artifact, load_prebuilt_code_graph

    start = time.monotonic()
    if args.graph:
        graph = load_code_graph_artifact(str(args.graph), project_root=project_root)
    else:
        if not args.instance_id:
            raise ValueError("--instance-id is required with --prebuilt-root")
        graph = load_prebuilt_code_graph(str(args.prebuilt_root), str(args.instance_id))
        graph.project_root = str(Path(project_root).resolve())
    return graph, (time.monotonic() - start) * 1000


def _graph_source_from_args(args: argparse.Namespace) -> Path:
    if args.graph:
        return Path(args.graph).resolve()
    if not args.instance_id:
        raise ValueError("--instance-id is required with --prebuilt-root")
    return (Path(args.prebuilt_root) / str(args.instance_id) / "graph.pkl").resolve()


def _project_root_from_args(args: argparse.Namespace) -> str:
    from .prebuilt import repo_path_for

    if args.project_root:
        return str(Path(args.project_root).resolve())
    if args.prebuilt_root and args.instance_id:
        return repo_path_for(str(args.prebuilt_root), str(args.instance_id))
    raise ValueError("--project-root is required when using --graph")


def _command_from_arg(command: str | None) -> list[str] | None:
    if not command:
        return None
    parsed = shlex.split(command)
    if not parsed:
        raise ValueError("--command cannot be empty")
    return parsed


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_reports(
    payload: Mapping[str, Any],
    *,
    output_json: str | Path | None = None,
    output_markdown: str | Path | None = None,
) -> None:
    if output_json:
        path = Path(output_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if output_markdown:
        path = Path(output_markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_lsp_replay_benchmark_markdown(payload), encoding="utf-8")


def _fmt(value: Any, digits: int) -> str:
    return f"{float(value):.{digits}f}" if isinstance(value, (int, float)) else "n/a"


def _fmt_percent(value: Any) -> str:
    return f"{float(value) * 100:.2f}%" if isinstance(value, (int, float)) else "n/a"


def _yes_no(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "n/a"


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "exit_code_for_lsp_replay_benchmark",
    "generate_lsp_replay_requests",
    "render_lsp_replay_benchmark_markdown",
    "run_lsp_replay_benchmark",
    "summarize_lsp_replay_benchmark",
]
