#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Profile Python vs C++ graph-layer edge classification.

This isolates the shared ``MultiGraphIndex`` classifier from graph decoding and
language-server time. The C++ path is optional; when ``codeminer_core`` is not
available the report records ``core_available=false``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_CORE = _PROJECT_ROOT / "build" / "core"
if _CORE.exists() and str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from codeminer.graph.layers import classify_edge_layers
from codeminer.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_IMPORT,
    EDGE_TYPE_REFERENCE,
    EDGE_TYPE_TYPE_USE,
)

EDGE_TYPE_PATTERN = (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    EDGE_TYPE_IMPORT,
    EDGE_TYPE_TYPE_USE,
    "custom",
)


def build_edge_types(edge_count: int) -> list[str]:
    """Return a deterministic mixed edge-type workload."""

    if edge_count < 0:
        raise ValueError("edge_count must be non-negative")
    pattern = EDGE_TYPE_PATTERN
    return [pattern[i % len(pattern)] for i in range(edge_count)]


def profile_layers(edge_count: int, reps: int) -> dict:
    """Profile Python and optional C++ layer classification."""

    if reps <= 0:
        raise ValueError("reps must be positive")

    edge_types = build_edge_types(edge_count)
    python_times, python_result = _run_reps(edge_types, reps, use_core=False)

    core_available = importlib.util.find_spec("codeminer_core") is not None
    core_times = []
    core_result = None
    if core_available:
        core_times, core_result = _run_reps(edge_types, reps, use_core=True)

    parity = core_result is None or core_result == python_result
    python_min = min(python_times)
    core_min = min(core_times) if core_times else None
    return {
        "edges": edge_count,
        "reps": reps,
        "core_available": core_available,
        "parity": parity,
        "python_seconds_min": python_min,
        "core_seconds_min": core_min,
        "speedup_vs_python": (
            python_min / core_min if core_min and core_min > 0 else None
        ),
        "layers": {name: len(ids) for name, ids in python_result.items()},
    }


def _run_reps(
    edge_types: list[str], reps: int, *, use_core: bool
) -> tuple[list[float], dict]:
    times = []
    result = {}
    for _ in range(reps):
        t0 = time.perf_counter()
        result = classify_edge_layers(edge_types, use_core=use_core)
        times.append(time.perf_counter() - t0)
    return times, result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", type=int, default=1_000_000)
    parser.add_argument("--reps", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    print(json.dumps(profile_layers(args.edges, args.reps), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
