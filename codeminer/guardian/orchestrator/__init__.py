# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Orchestrator sub-package for Repository Guardian.

Re-exports everything that was previously exported from the flat
``codeminer.guardian.hypothesize`` module so existing imports continue to work.
"""

from .runner import Hypothesis, heuristic_hypotheses, hypothesize

__all__ = [
    "Hypothesis",
    "heuristic_hypotheses",
    "hypothesize",
]
