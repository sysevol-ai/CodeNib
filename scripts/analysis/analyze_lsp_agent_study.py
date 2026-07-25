#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Finalize LSP agent-study cells and export naturally adopted live traces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.eval.agent_runner import (  # noqa: E402
    CODENIB_LSP_ARM,
    LIVE_LSP_ARM,
    analyze_lsp_agent_noninferiority,
    collect_live_lsp_replay_bundles,
    summarize_lsp_agent_study,
)
from codenib.eval.agent_runner.metrics import load_cell_jsons  # noqa: E402


def _load_cells(roots: Sequence[Path]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for root in roots:
        cells_dir = root / "cells" if (root / "cells").is_dir() else root
        for cell in load_cell_jsons(cells_dir):
            key = (
                str(cell.get("instance_id") or ""),
                int(cell.get("rep") or 0),
                str(cell.get("arm") or ""),
            )
            if key in seen:
                raise ValueError(f"duplicate study cell across input roots: {key!r}")
            seen.add(key)
            cells.append(cell)
    return cells


def _write_live_requests(
    bundles: Sequence[dict[str, Any]], output_dir: Path
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for bundle in bundles:
        path = output_dir / f"{bundle['instance_id']}.requests.json"
        path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        index.append(
            {
                "instance_id": bundle["instance_id"],
                "language": bundle.get("language"),
                "request_count": len(bundle["requests"]),
                "path": str(path),
            }
        )
    (output_dir / "index.json").write_text(
        json.dumps({"bundles": index}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return index


def _complete_metric_cells(cells: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], set[str]] = {}
    for cell in cells:
        arm = str(cell.get("arm") or "")
        metric = (
            ((cell.get("metrics") or {}).get("answer_blocks") or {}).get(5)
            or ((cell.get("metrics") or {}).get("answer_blocks") or {}).get("5")
            or {}
        ).get("recall")
        if arm in {CODENIB_LSP_ARM, LIVE_LSP_ARM} and isinstance(metric, (int, float)):
            key = (str(cell.get("instance_id") or ""), int(cell.get("rep") or 0))
            grouped.setdefault(key, set()).add(arm)
    complete = {
        key for key, arms in grouped.items() if arms == {CODENIB_LSP_ARM, LIVE_LSP_ARM}
    }
    return [
        cell
        for cell in cells
        if (str(cell.get("instance_id") or ""), int(cell.get("rep") or 0)) in complete
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cell_roots", nargs="+", type=Path)
    parser.add_argument("--role", default="confirmatory")
    parser.add_argument("--expected-pair-count", required=True, type=int)
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--export-live-requests", type=Path)
    args = parser.parse_args(argv)

    cells = _load_cells(args.cell_roots)
    role_cells = [cell for cell in cells if cell.get("role") == args.role]
    primary = analyze_lsp_agent_noninferiority(
        role_cells,
        role=args.role,
        expected_pair_count=args.expected_pair_count,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
    )
    complete_case_count = int(primary["complete_metric_pair_count"])
    complete_metric_cells = _complete_metric_cells(role_cells)
    complete_case = analyze_lsp_agent_noninferiority(
        complete_metric_cells,
        role=args.role,
        expected_pair_count=complete_case_count,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
    )
    bundles = collect_live_lsp_replay_bundles(cells)
    exported = (
        _write_live_requests(bundles, args.export_live_requests)
        if args.export_live_requests
        else []
    )
    payload = {
        "schema_version": 1,
        "role": args.role,
        "inputs": [str(path) for path in args.cell_roots],
        "study": summarize_lsp_agent_study(role_cells),
        "status": {
            "planned_cell_count": args.expected_pair_count * 3,
            "observed_cell_count": len(role_cells),
            "successful_cell_count": sum(
                cell.get("status") == "ok" for cell in role_cells
            ),
            "error_cell_count": sum(
                cell.get("status") == "error" for cell in role_cells
            ),
            "errors": [
                {
                    key: cell.get(key)
                    for key in (
                        "instance_id",
                        "repo",
                        "language",
                        "rep",
                        "arm",
                        "error_stage",
                        "error_type",
                        "error_message",
                    )
                }
                for cell in role_cells
                if cell.get("status") == "error"
            ],
        },
        "primary_analysis": primary,
        "complete_case_descriptive": {
            "interpretation": ("descriptive only; excludes pairs without both metrics"),
            **complete_case,
        },
        "live_trace_export": {
            "bundle_count": len(bundles),
            "request_count": sum(len(bundle["requests"]) for bundle in bundles),
            "bundles": exported,
        },
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
