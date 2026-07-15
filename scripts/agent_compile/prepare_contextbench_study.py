#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Materialize frozen ContextBench vector artifacts with a durable ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _write_summary(
    path: Path,
    records: Mapping[str, Mapping[str, Any]],
    *,
    planned_instance_ids: set[str],
) -> None:
    ordered = [records[key] for key in sorted(records)]
    recorded_instance_ids = {str(row["instance_id"]) for row in ordered}
    unexpected = recorded_instance_ids - planned_instance_ids
    if unexpected:
        raise ValueError(f"ledger contains unplanned instances: {sorted(unexpected)}")
    payload = {
        "schema_version": 2,
        "planned": len(planned_instance_ids),
        "recorded": len(ordered),
        "pending": len(planned_instance_ids - recorded_instance_ids),
        "ready": sum(row.get("status") == "ready" for row in ordered),
        "failed": sum(row.get("status") == "failed" for row in ordered),
        "records": ordered,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main() -> int:
    from codeminer.eval.contextbench_study import (
        prepare_vector_artifacts,
        protocol_rows,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--instance", action="append", dest="instances")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    all_rows = protocol_rows(protocol)
    instances = list(args.instances or [])
    if not instances and args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be positive")
        instances = [row["instance_id"] for row in all_rows[: args.limit]]
    planned_instance_ids = {
        row["instance_id"]
        for row in all_rows
        if not instances or row["instance_id"] in set(instances)
    }

    ledger: dict[str, Mapping[str, Any]] = {}
    if args.summary.is_file():
        prior = json.loads(args.summary.read_text(encoding="utf-8"))
        ledger.update({row["instance_id"]: row for row in prior.get("records", [])})

    def persist(record: Mapping[str, Any]) -> None:
        ledger[str(record["instance_id"])] = dict(record)
        _write_summary(
            args.summary,
            ledger,
            planned_instance_ids=planned_instance_ids,
        )
        print(
            f"{record['instance_id']}: {record['status']} "
            f"({record['duration_seconds']:.1f}s)",
            flush=True,
        )

    records = prepare_vector_artifacts(
        protocol,
        source_root=args.source_root,
        output_root=args.output_root,
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        batch_size=args.batch_size,
        instance_ids=instances or None,
        force=args.force,
        on_record=persist,
    )
    _write_summary(
        args.summary,
        ledger,
        planned_instance_ids=planned_instance_ids,
    )
    return 1 if any(row["status"] == "failed" for row in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
