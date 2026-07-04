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
from .history import PlainChatHistory, TokenBudgetedChatHistory, count_message_tokens
from .rerank_agent import RerankAgent, RerankResult, rerank_nodes_with_query
from .runner import AgentRunner, CodeMinerAgentOptions, compile_repo, query
from .runtime import AGENT_TRACE_SCHEMA_VERSION, AgentRunTrace, AgentTraceEvent
from .tool_schema import registry_to_tools, skill_to_tool_schema

__all__ = [
    "AgentResult",
    "AgentRunTrace",
    "AgentTraceEvent",
    "AgentRunner",
    "AGENT_TRACE_SCHEMA_VERSION",
    "CodeMinerAgentOptions",
    "KeywordExtraction",
    "KeywordExtractor",
    "PlainChatHistory",
    "RerankAgent",
    "RerankResult",
    "TokenBudgetedChatHistory",
    "ToolCallRecord",
    "compile_repo",
    "count_message_tokens",
    "extract_keywords_from_statement",
    "query",
    "rerank_nodes_with_query",
    "registry_to_tools",
    "skill_to_tool_schema",
]
