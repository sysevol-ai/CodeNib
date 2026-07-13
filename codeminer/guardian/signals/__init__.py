# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Signals sub-package for Repository Guardian.

Re-exports everything that was previously exported from the flat
``codeminer.guardian.signals`` module and ``codeminer.guardian.graph_diff``
module, so existing imports continue to work unchanged.
"""

from .churn import Hotspot, churn_hotspots
from .tests import TestFailure, TestResult, run_test_suite
from .graph_diff import (
    DriftSignal,
    EdgeChange,
    compute_drift_signals,
    diff_graphs,
    drift_findings,
    latest_snapshot_commit,
    load_snapshot,
    save_snapshot,
)

__all__ = [
    # churn
    "Hotspot",
    "churn_hotspots",
    # tests
    "TestFailure",
    "TestResult",
    "run_test_suite",
    # graph_diff
    "DriftSignal",
    "EdgeChange",
    "compute_drift_signals",
    "diff_graphs",
    "drift_findings",
    "latest_snapshot_commit",
    "load_snapshot",
    "save_snapshot",
]
