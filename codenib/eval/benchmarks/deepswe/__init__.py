# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Portable analysis of artifacts produced by the DeepSWE benchmark harness.

This package does not launch DeepSWE or own its task containers.  It reads the
fixed-slot trial artifacts produced by the external harness and provides the
metric, cost, and reporting logic shared by command-line tools and dashboards.
"""

from .metrics import (
    DEFAULT_PRICES_USD_PER_MTOK,
    Pricing,
    add_costs,
    metric_mean,
    metric_sum,
    pricing_for_model,
)
from .reports import (
    export_dashboard_data,
    summary_rows,
    task_rows,
    trial_row,
    write_summary,
)
from .results import COUNTED_JOB_IDS, load_rows, metadata_paths

__all__ = [
    "COUNTED_JOB_IDS",
    "DEFAULT_PRICES_USD_PER_MTOK",
    "Pricing",
    "add_costs",
    "export_dashboard_data",
    "load_rows",
    "metadata_paths",
    "metric_mean",
    "metric_sum",
    "pricing_for_model",
    "summary_rows",
    "task_rows",
    "trial_row",
    "write_summary",
]
