# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent package consolidating agent utilities and implementations."""

from __future__ import annotations

from typing import Any

from .._lazy import exported_dir, load_export

_EXPORTS = {
    "AgentResult": ("codenib.agent.agent_types", "AgentResult"),
    "ToolCallRecord": ("codenib.agent.agent_types", "ToolCallRecord"),
    "KeywordExtraction": ("codenib.agent.extract_agent", "KeywordExtraction"),
    "KeywordExtractor": ("codenib.agent.extract_agent", "KeywordExtractor"),
    "extract_keywords_from_statement": (
        "codenib.agent.extract_agent",
        "extract_keywords_from_statement",
    ),
    "AgentHarnessSpec": ("codenib.agent.harness", "AgentHarnessSpec"),
    "AgentRunAccumulator": ("codenib.agent.harness", "AgentRunAccumulator"),
    "agent_working_directory": (
        "codenib.agent.harness",
        "agent_working_directory",
    ),
    "run_agent_in_directory": (
        "codenib.agent.harness",
        "run_agent_in_directory",
    ),
    "PlainChatHistory": ("codenib.agent.history", "PlainChatHistory"),
    "TokenBudgetedChatHistory": (
        "codenib.agent.history",
        "TokenBudgetedChatHistory",
    ),
    "count_message_tokens": ("codenib.agent.history", "count_message_tokens"),
    "LSPProviderMetadata": (
        "codenib.agent.lsp_provider",
        "LSPProviderMetadata",
    ),
    "LSPProviderNodes": ("codenib.agent.lsp_provider", "LSPProviderNodes"),
    "StaticLSPProvider": ("codenib.agent.lsp_provider", "StaticLSPProvider"),
    "lsp_result_metadata": (
        "codenib.agent.lsp_provider",
        "lsp_result_metadata",
    ),
    "RerankAgent": ("codenib.agent.rerank_agent", "RerankAgent"),
    "RerankResult": ("codenib.agent.rerank_agent", "RerankResult"),
    "rerank_nodes_with_query": (
        "codenib.agent.rerank_agent",
        "rerank_nodes_with_query",
    ),
    "LSPRouteContext": ("codenib.agent.route_context", "LSPRouteContext"),
    "build_lsp_route_context": (
        "codenib.agent.route_context",
        "build_lsp_route_context",
    ),
    "canonical_lsp_route_args": (
        "codenib.agent.route_context",
        "canonical_lsp_route_args",
    ),
    "extract_lsp_symbol_seeds": (
        "codenib.agent.route_context",
        "extract_lsp_symbol_seeds",
    ),
    "filter_lsp_symbol_seeds": (
        "codenib.agent.route_context",
        "filter_lsp_symbol_seeds",
    ),
    "fingerprint_lsp_route_nodes": (
        "codenib.agent.route_context",
        "fingerprint_lsp_route_nodes",
    ),
    "is_specific_lsp_symbol_seed": (
        "codenib.agent.route_context",
        "is_specific_lsp_symbol_seed",
    ),
    "normalize_lsp_route_seed_policy": (
        "codenib.agent.route_context",
        "normalize_lsp_route_seed_policy",
    ),
    "render_lsp_route_context": (
        "codenib.agent.route_context",
        "render_lsp_route_context",
    ),
    "AgentRunner": ("codenib.agent.runner", "AgentRunner"),
    "CodeNibAgentOptions": ("codenib.agent.runner", "CodeNibAgentOptions"),
    "compile_repo": ("codenib.agent.runner", "compile_repo"),
    "has_localization_contract": (
        "codenib.agent.runner",
        "has_localization_contract",
    ),
    "query": ("codenib.agent.runner", "query"),
    "AGENT_TRACE_SCHEMA_VERSION": (
        "codenib.agent.runtime",
        "AGENT_TRACE_SCHEMA_VERSION",
    ),
    "AgentRunTrace": ("codenib.agent.runtime", "AgentRunTrace"),
    "AgentTraceEvent": ("codenib.agent.runtime", "AgentTraceEvent"),
    "ContextLedger": ("codenib.agent.runtime", "ContextLedger"),
    "ContextLedgerEntry": ("codenib.agent.runtime", "ContextLedgerEntry"),
    "registry_to_tools": ("codenib.agent.tool_schema", "registry_to_tools"),
    "skill_to_tool_schema": (
        "codenib.agent.tool_schema",
        "skill_to_tool_schema",
    ),
}

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


def __getattr__(name: str) -> Any:
    return load_export(globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return exported_dir(globals(), _EXPORTS)
