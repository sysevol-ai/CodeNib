#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compare two CodeGraph pickles on the backend-alignment surface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from codenib.graph.backend_alignment import (
    BackendAlignmentTolerances,
    compare_backend_graphs,
)
from codenib.graph.code_graph import CodeGraph


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--max-missing-symbols", type=int, default=0)
    parser.add_argument("--max-extra-symbols", type=int, default=0)
    parser.add_argument("--max-missing-containment", type=int, default=0)
    parser.add_argument("--max-extra-containment", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    tolerances = BackendAlignmentTolerances(
        max_missing_symbols=args.max_missing_symbols,
        max_extra_symbols=args.max_extra_symbols,
        max_missing_containment=args.max_missing_containment,
        max_extra_containment=args.max_extra_containment,
    )
    report = compare_backend_graphs(
        CodeGraph.load_graph(str(args.reference)),
        CodeGraph.load_graph(str(args.candidate)),
        tolerances=tolerances,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
