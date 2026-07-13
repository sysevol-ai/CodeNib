# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Investigator sub-package for Repository Guardian.

Re-exports everything that was previously exported from the flat
``codeminer.guardian.investigator`` and ``codeminer.guardian.llm_investigator``
modules so existing imports continue to work.

Import order matters here to avoid circular imports:
1. runner.py is imported first (defines LLMUsage and other primitives)
2. probes.py is imported after (imports from runner.py and from ..report)
"""

# runner.py first — it defines LLMUsage which report.py needs
from .runner import (
    LLMUsage,
    InvestigatorResult,
    ProbeRecord,
    _parse_verdict,
    _run_search,
    build_test_failure_context,
    investigate_signal,
    run_investigator,
)

# sandbox.py — no guardian-level imports
from .sandbox import SandboxHandle, WorktreeSandbox

# probes.py last — imports from ..report (which imports from this package)
from .probes import (
    dispatch_advanced_probe,
    hotspot_query,
    investigate_hotspot,
    read_file,
)

__all__ = [
    # From runner.py
    "LLMUsage",
    "InvestigatorResult",
    "ProbeRecord",
    "_parse_verdict",
    "_run_search",
    "build_test_failure_context",
    "investigate_signal",
    "run_investigator",
    # From sandbox.py
    "SandboxHandle",
    "WorktreeSandbox",
    # From probes.py
    "dispatch_advanced_probe",
    "hotspot_query",
    "investigate_hotspot",
    "read_file",
]
