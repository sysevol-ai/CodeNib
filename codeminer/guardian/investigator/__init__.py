# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Investigator sub-package for Repository Guardian.

Re-exports everything that was previously exported from the flat
``codeminer.guardian.investigator`` and ``codeminer.guardian.llm_investigator``
modules so existing imports continue to work.

The generic narrative investigator remains in ``runner.py``. The Repository
Guardian investigation agent is implemented by ``agent.py``.
"""

from .agent import TOOLS, run_investigation_agent, run_investigator
from .probes import hotspot_query, investigate_hotspot, read_file
from .runner import (
    LLMUsage,
    _parse_verdict,
    _run_search,
    build_test_failure_context,
    investigate_signal,
)

# sandbox.py — no guardian-level imports
from .sandbox import (
    CurrentSnapshotSandbox,
    PriorSnapshotSandbox,
    ReadOnlySourceHandle,
    SandboxHandle,
    WorktreeSandbox,
)
from .types import (
    BudgetLedger,
    CommandResult,
    InvestigationRunResult,
    InvestigationTask,
    InvestigatorResult,
    ProbeRunResult,
    ProcessStatus,
    SourceExcerpt,
    TestStatus,
    ToolResult,
)

__all__ = [
    # From runner.py
    "LLMUsage",
    "InvestigatorResult",
    "InvestigationRunResult",
    "InvestigationTask",
    "BudgetLedger",
    "CommandResult",
    "ProcessStatus",
    "ProbeRunResult",
    "TestStatus",
    "ToolResult",
    "SourceExcerpt",
    "_parse_verdict",
    "_run_search",
    "build_test_failure_context",
    "investigate_signal",
    "run_investigator",
    "run_investigation_agent",
    "TOOLS",
    # From sandbox.py
    "SandboxHandle",
    "ReadOnlySourceHandle",
    "CurrentSnapshotSandbox",
    "PriorSnapshotSandbox",
    "WorktreeSandbox",
    # From probes.py
    "hotspot_query",
    "investigate_hotspot",
    "read_file",
]
