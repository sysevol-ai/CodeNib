# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent package consolidating agent utilities and implementations."""

from .agent_types import AgentResult, ToolCallRecord
from .extract_agent import (
    KeywordExtraction,
    KeywordExtractor,
    extract_keywords_from_statement,
)
from .harness import (
    AgentHarnessSpec,
    AgentRunAccumulator,
    agent_working_directory,
    run_agent_in_directory,
)
from .history import PlainChatHistory, TokenBudgetedChatHistory, count_message_tokens
from .rerank_agent import RerankAgent, RerankResult, rerank_nodes_with_query
from .runner import (
    AgentRunner,
    CodeMinerAgentOptions,
    compile_repo,
    has_localization_contract,
    query,
)
from .runtime import (
    AGENT_TRACE_SCHEMA_VERSION,
    AgentRunTrace,
    AgentTraceEvent,
    ContextLedger,
    ContextLedgerEntry,
)
from .tool_schema import registry_to_tools, skill_to_tool_schema

__all__ = [
    "AgentResult",
    "AgentRunTrace",
    "AgentRunAccumulator",
    "AgentTraceEvent",
    "AgentRunner",
    "AGENT_TRACE_SCHEMA_VERSION",
    "AgentHarnessSpec",
    "CodeMinerAgentOptions",
    "ContextLedger",
    "ContextLedgerEntry",
    "KeywordExtraction",
    "KeywordExtractor",
    "PlainChatHistory",
    "RerankAgent",
    "RerankResult",
    "TokenBudgetedChatHistory",
    "ToolCallRecord",
    "agent_working_directory",
    "compile_repo",
    "count_message_tokens",
    "extract_keywords_from_statement",
    "has_localization_contract",
    "query",
    "rerank_nodes_with_query",
    "registry_to_tools",
    "run_agent_in_directory",
    "skill_to_tool_schema",
]
