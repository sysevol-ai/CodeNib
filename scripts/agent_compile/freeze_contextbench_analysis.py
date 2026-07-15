#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Freeze ContextBench estimands after the disclosed infrastructure smoke."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    from codeminer.eval.contextbench_study import build_analysis_plan, sha256_file

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--smoke", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    plan = build_analysis_plan(
        protocol,
        protocol_file_sha256=sha256_file(args.protocol),
        infrastructure_smoke_sha256=sha256_file(args.smoke),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"frozen analysis plan; output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
