# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Repository Guardian — persistent, proactive, non-modifying codebase monitor.

The package has two peer tool agents over one shared runtime:

* :mod:`.loop.agent` is the cycle agent. It owns hypotheses, evidence grades,
  memory decisions, and report submission.
* :mod:`.investigator.agent` is the investigation agent. It investigates one
  delegated hypothesis and returns a typed evidence result.
* :mod:`.agent.runtime` owns their common model/tool turn mechanics.
* :mod:`.llm.session` and :mod:`.llm.context` own the provider session and the
  single shared transcript context manager.
* :mod:`.cycle` composes repository signals, snapshots, memory, and both agents
  for one commit.
"""

from .cycle import GuardianConfig, run_cycle
from .investigator import LLMUsage, read_file
from .report import Evidence, Finding, GuardianReport, render_markdown
from .signals import Hotspot, TestFailure, TestResult, churn_hotspots, run_test_suite

__all__ = [
    "GuardianConfig",
    "run_cycle",
    "Evidence",
    "LLMUsage",
    "read_file",
    "Finding",
    "GuardianReport",
    "render_markdown",
    "Hotspot",
    "TestFailure",
    "TestResult",
    "churn_hotspots",
    "run_test_suite",
]
