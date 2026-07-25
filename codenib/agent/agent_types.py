# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Data types for the agent runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..llm.usage import TokenUsage, UsageRecord
from .runtime.trace import AgentRunTrace


@dataclass
class ToolCallRecord:
    """Record of a single tool invocation during an agent run."""

    tool_call_id: str
    skill_id: str
    arguments: Dict[str, Any]
    resolved_arguments: Optional[Dict[str, Any]] = None
    result: Any = None
    duration_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class AgentResult:
    """Outcome of ``AgentRunner.run()``."""

    answer: str
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    total_turns: int = 0
    total_duration_ms: float = 0.0
    usage: Optional[TokenUsage] = None
    usage_records: List[UsageRecord] = field(default_factory=list)
    trace: Optional[AgentRunTrace] = None
