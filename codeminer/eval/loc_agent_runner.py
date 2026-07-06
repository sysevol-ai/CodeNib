# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Deprecated compatibility wrapper for external LOC baseline helpers."""

from __future__ import annotations

import warnings

from codeminer.eval.agent_runner.loc_baseline import (
    BUILTIN_DATASET_CHOICES,
    DEFAULT_METRICS_K,
    add_common_args,
    already_done_ids,
    build_dataset,
    build_result_entry,
    run_agent_baseline,
)

_DEPRECATION_MESSAGE = (
    "codeminer.eval.loc_agent_runner is deprecated; import external "
    "localization baseline helpers from "
    "codeminer.eval.agent_runner.loc_baseline instead."
)

warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=2)

__all__ = [
    "BUILTIN_DATASET_CHOICES",
    "DEFAULT_METRICS_K",
    "add_common_args",
    "already_done_ids",
    "build_dataset",
    "build_result_entry",
    "run_agent_baseline",
]
