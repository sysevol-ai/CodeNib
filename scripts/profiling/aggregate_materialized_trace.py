#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Aggregate a complete mixed-operation batch at repository-snapshot level."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.eval.reports.materialized_trace import (  # noqa: E402
    aggregate_materialized_trace,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--reports-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260713)
    args = parser.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    reports = {}
    for path in sorted(args.reports_root.glob("*/report.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("rows") and payload.get("materialization"):
            reports[path.parent.name] = payload
    result = aggregate_materialized_trace(
        plan,
        reports,
        bootstrap_samples=args.bootstrap_samples,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(_headline(result), indent=2))
    return 0


def _headline(result: dict) -> dict:
    overall = result["overall"]
    return {
        "snapshot_count": result["protocol"]["snapshot_count"],
        "request_count": result["protocol"]["request_count"],
        "materialization_seconds": overall["materialization_seconds"],
        "load_seconds": overall["load_seconds"],
        "operation_p50_seconds": overall["operation_p50_seconds"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
