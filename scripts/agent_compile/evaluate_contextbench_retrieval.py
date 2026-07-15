#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Evaluate frozen ContextBench preload retrieval against human contexts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    from codeminer.eval.contextbench_study import evaluate_preload_retrieval

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--prebuilt-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--instance", action="append", dest="instances")
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    records = evaluate_preload_retrieval(
        protocol,
        prebuilt_root=args.prebuilt_root,
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        instance_ids=args.instances,
    )
    payload = {
        "schema_version": 1,
        "planned_rows": len(records),
        "ready_rows": sum(row["status"] == "ready" for row in records),
        "missing_index_rows": sum(row["status"] == "missing_index" for row in records),
        "failed_rows": sum(row["status"] == "failed" for row in records),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"ready={payload['ready_rows']} missing={payload['missing_index_rows']} "
        f"failed={payload['failed_rows']}; output={args.output}"
    )
    return 1 if payload["failed_rows"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
