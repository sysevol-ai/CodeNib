# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
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
from .lsp_provider import (
    LSPProviderMetadata,
    LSPProviderNodes,
    StaticLSPProvider,
    lsp_result_metadata,
)
from .rerank_agent import RerankAgent, RerankResult, rerank_nodes_with_query
from .route_context import (
    LSPRouteContext,
    build_lsp_route_context,
    canonical_lsp_route_args,
    extract_lsp_symbol_seeds,
    filter_lsp_symbol_seeds,
    fingerprint_lsp_route_nodes,
    is_specific_lsp_symbol_seed,
    normalize_lsp_route_seed_policy,
    render_lsp_route_context,
)
from .runner import (
    AgentRunner,
    CodeNibAgentOptions,
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
    "CodeNibAgentOptions",
    "ContextLedger",
    "ContextLedgerEntry",
    "KeywordExtraction",
    "KeywordExtractor",
    "LSPRouteContext",
    "LSPProviderMetadata",
    "LSPProviderNodes",
    "PlainChatHistory",
    "RerankAgent",
    "RerankResult",
    "TokenBudgetedChatHistory",
    "ToolCallRecord",
    "StaticLSPProvider",
    "agent_working_directory",
    "compile_repo",
    "count_message_tokens",
    "build_lsp_route_context",
    "canonical_lsp_route_args",
    "extract_keywords_from_statement",
    "extract_lsp_symbol_seeds",
    "filter_lsp_symbol_seeds",
    "fingerprint_lsp_route_nodes",
    "has_localization_contract",
    "is_specific_lsp_symbol_seed",
    "lsp_result_metadata",
    "normalize_lsp_route_seed_policy",
    "query",
    "rerank_nodes_with_query",
    "render_lsp_route_context",
    "registry_to_tools",
    "run_agent_in_directory",
    "skill_to_tool_schema",
]
