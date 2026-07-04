# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compatibility re-export for prebuilt-index staging helpers."""

from codeminer.eval.agent_runner.prebuilt import (
    has_full_indexes,
    instance_dir,
    load_prebuilt_code_graph,
    model_suffix,
    repo_path_for,
    stage_prebuilt_indexes,
)

__all__ = [
    "has_full_indexes",
    "instance_dir",
    "load_prebuilt_code_graph",
    "model_suffix",
    "repo_path_for",
    "stage_prebuilt_indexes",
]
