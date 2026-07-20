#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Repeat same-commit graph rebuilds to audit provider stability."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.eval.maintenance_manifest import load_manifest  # noqa: E402
from codeminer.graph.code_graph import CodeGraph  # noqa: E402

try:
    from scripts.profiling.profile_incremental_graph import (
        BASE_BUILD_PROTOCOL_VERSION,
        DEFAULT_BEHAVIOR_REQUESTS_PER_CAPABILITY,
        DEFAULT_LSP_REQUEST_CONCURRENCY,
        PROTOCOL_VERSION,
        compare_graph_behavior,
        compare_graphs,
        run_rebuild_step,
    )
except ModuleNotFoundError:  # Direct execution from scripts/profiling.
    from profile_incremental_graph import (  # type: ignore[no-redef]
        BASE_BUILD_PROTOCOL_VERSION,
        DEFAULT_BEHAVIOR_REQUESTS_PER_CAPABILITY,
        DEFAULT_LSP_REQUEST_CONCURRENCY,
        PROTOCOL_VERSION,
        compare_graph_behavior,
        compare_graphs,
        run_rebuild_step,
    )

STABILITY_PROTOCOL_VERSION = 2


def parse_case(value: str) -> Tuple[str, int]:
    """Parse ``INSTANCE_ID:STEP`` without constraining instance-id syntax."""

    try:
        instance_id, raw_step = value.rsplit(":", 1)
        step = int(raw_step)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(
            f"invalid stability case {value!r}; expected INSTANCE_ID:STEP"
        ) from exc
    if not instance_id or step < 1:
        raise argparse.ArgumentTypeError(
            f"invalid stability case {value!r}; step must be positive"
        )
    return instance_id, step


def exactness(metrics: Dict[str, Dict[str, Any]]) -> Dict[str, bool]:
    """Classify whole-graph and changed-scope multiset equivalence."""

    def exact(names: Iterable[str]) -> bool:
        return all(
            metrics[name]["precision"] == 1.0 and metrics[name]["recall"] == 1.0
            for name in names
        )

    return {
        "whole_exact": exact(("vertices", "edges")),
        "changed_exact": exact(("changed_vertices", "changed_edges")),
    }


def compare_rebuilds(
    candidate: CodeGraph,
    reference: CodeGraph,
    *,
    project_root: Path,
    changed_files: set[str],
    max_behavior_requests: int,
) -> Dict[str, Any]:
    """Compare graph facts and serving behavior for two fresh rebuilds."""

    metrics = compare_graphs(candidate, reference, changed_files)
    behavior = compare_graph_behavior(
        candidate,
        reference,
        project_root=project_root,
        changed_files=changed_files,
        max_per_capability=max_behavior_requests,
    )
    return {
        **exactness(metrics),
        "behavior_exact": behavior.get("exact") is True,
        "behavior": behavior,
        "metrics": metrics,
    }


def _load_graph_rows(path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        instance_id = row.get("instance_id")
        step = row.get("step")
        if instance_id and isinstance(step, int) and step > 0:
            rows[(instance_id, step)] = row
    return rows


def _load_completed(path: Path) -> set[Tuple[str, int, int]]:
    if not path.is_file():
        return set()
    completed = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if row.get("status") == "ok":
            completed.add((row["instance_id"], row["step"], row["repeat"]))
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transition-manifest", required=True, type=Path)
    parser.add_argument("--graph-results", required=True, type=Path)
    parser.add_argument("--baseline-work-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", action="append", type=parse_case, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--cache-root", type=Path, default=Path("/mnt/data/codeminer"))
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--lsp-request-timeout-s", type=float, default=120.0)
    parser.add_argument("--lsp-reference-retries", type=int, default=1)
    parser.add_argument(
        "--lsp-request-concurrency",
        type=int,
        default=DEFAULT_LSP_REQUEST_CONCURRENCY,
    )
    parser.add_argument(
        "--behavior-requests-per-capability",
        type=int,
        default=DEFAULT_BEHAVIOR_REQUESTS_PER_CAPABILITY,
    )
    parser.add_argument("--strategy-attempts", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.lsp_request_concurrency < 1:
        parser.error("--lsp-request-concurrency must be positive")
    if args.behavior_requests_per_capability < 1:
        parser.error("--behavior-requests-per-capability must be positive")

    _, chains, manifest_id = load_manifest(args.transition_manifest)
    graph_rows = _load_graph_rows(args.graph_results)
    completed = _load_completed(args.output) if args.resume else set()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)

    with args.output.open("a", encoding="utf-8") as output:
        for instance_id, step in args.case:
            try:
                target = next(row for row in chains[instance_id] if row["step"] == step)
                original_row = graph_rows[(instance_id, step)]
            except (KeyError, StopIteration):
                parser.error(f"case {instance_id}:{step} is absent from inputs")

            changed_files = set(
                original_row.get("symbol-level-patch", {}).get("changed_files", [])
            )
            baseline_path = (
                args.baseline_work_root / instance_id / f"step{step}" / "rebuild.pkl"
            )
            if not baseline_path.is_file():
                parser.error(f"missing original rebuild graph: {baseline_path}")

            references = [("original", baseline_path)]
            for repeat in range(1, args.repeats + 1):
                repeat_dir = (
                    args.output_root / instance_id / f"step{step}" / f"repeat{repeat}"
                )
                repeat_path = repeat_dir / "rebuild.pkl"
                key = (instance_id, step, repeat)
                if key not in completed:
                    info = run_rebuild_step(
                        target,
                        args.cache_root,
                        repeat_dir,
                        None,
                        args.timeout_s,
                        print,
                        graph_route="lsp",
                        reference_timeout_s=args.lsp_request_timeout_s,
                        reference_retries=args.lsp_reference_retries,
                        lsp_request_concurrency=args.lsp_request_concurrency,
                        strategy_attempts=args.strategy_attempts,
                    )
                    if info.get("status") != "ok" or not repeat_path.is_file():
                        row = {
                            "stability_protocol_version": STABILITY_PROTOCOL_VERSION,
                            "graph_protocol_version": PROTOCOL_VERSION,
                            "base_build_protocol_version": BASE_BUILD_PROTOCOL_VERSION,
                            "lsp_request_concurrency": args.lsp_request_concurrency,
                            "transition_manifest_id": manifest_id,
                            "instance_id": instance_id,
                            "language": target["language"],
                            "step": step,
                            "commit": target["to_commit"],
                            "repeat": repeat,
                            "status": info.get("status", "error"),
                            "build": info,
                        }
                        output.write(json.dumps(row) + "\n")
                        output.flush()
                        continue

                    candidate = CodeGraph.load_graph(str(repeat_path))
                    comparisons = []
                    for reference_name, reference_path in references:
                        reference = CodeGraph.load_graph(str(reference_path))
                        comparison = compare_rebuilds(
                            candidate,
                            reference,
                            project_root=args.cache_root / instance_id / "repo",
                            changed_files=changed_files,
                            max_behavior_requests=(
                                args.behavior_requests_per_capability
                            ),
                        )
                        comparisons.append(
                            {
                                "reference": reference_name,
                                **comparison,
                            }
                        )
                    row = {
                        "stability_protocol_version": STABILITY_PROTOCOL_VERSION,
                        "graph_protocol_version": PROTOCOL_VERSION,
                        "base_build_protocol_version": BASE_BUILD_PROTOCOL_VERSION,
                        "lsp_request_concurrency": args.lsp_request_concurrency,
                        "transition_manifest_id": manifest_id,
                        "instance_id": instance_id,
                        "language": target["language"],
                        "step": step,
                        "commit": target["to_commit"],
                        "repeat": repeat,
                        "status": "ok",
                        "changed_files": sorted(changed_files),
                        "vertices": candidate.graph.vcount(),
                        "edges": candidate.graph.ecount(),
                        "build": info,
                        "comparisons": comparisons,
                    }
                    output.write(json.dumps(row) + "\n")
                    output.flush()
                if repeat_path.is_file():
                    references.append((f"repeat{repeat}", repeat_path))


if __name__ == "__main__":
    main()
