# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Repository Guardian — persistent, proactive, non-modifying codebase monitor.

Phase 1 (this package) is the single-cycle skeleton: ``sync → index → observe →
investigate → report``. No repository memory, graph-diff, or candidate patches
yet (RFC Phases 2-5). See ``Repository_Guardian/repository_guardian_issue.md``.
"""

from .cycle import GuardianConfig, Reporter, run_cycle
from .investigate import Evidence, investigate_hotspot
from .llm_investigator import investigate_with_llm
from .report import Finding, GuardianReport, render_markdown
from .signals import Hotspot, TestResult, churn_hotspots, run_test_suite

__all__ = [
    "GuardianConfig",
    "Reporter",
    "run_cycle",
    "Evidence",
    "investigate_hotspot",
    "investigate_with_llm",
    "Finding",
    "GuardianReport",
    "render_markdown",
    "Hotspot",
    "TestResult",
    "churn_hotspots",
    "run_test_suite",
]
