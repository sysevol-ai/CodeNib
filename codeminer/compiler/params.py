# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Layered parameter resolution for skill execution.

Three effective layers (merged from the user's 4-layer design):

  config.yaml defaults  →  session adjustments  →  query-time overrides
      (Layer 1+2)              (Layer 3)              (Layer 4)

The ``resolve_params`` function merges these layers with later layers
taking precedence, and tracks provenance for debuggability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass(slots=True)
class SessionContext:
    """Layer 3: Runtime session/repo context injected once per session."""

    repo_path: Optional[str] = None
    repo_size: Optional[int] = None  # file count
    primary_language: Optional[str] = None
    index_freshness: Dict[str, float] = field(default_factory=dict)
    user_expertise: str = "intermediate"
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "repo_size": self.repo_size,
            "primary_language": self.primary_language,
            "index_freshness": dict(self.index_freshness),
            "user_expertise": self.user_expertise,
            "extras": dict(self.extras),
        }


@dataclass(slots=True)
class ResolvedParams:
    """Final resolved parameter set after merging all layers."""

    params: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "params": dict(self.params),
            "provenance": dict(self.provenance),
        }


@runtime_checkable
class SessionParamRules(Protocol):
    """Protocol for session-aware parameter adjustment rules."""

    def adjust(
        self, base_params: Dict[str, Any], session: SessionContext
    ) -> Dict[str, Any]: ...


class DefaultSessionRules:
    """Built-in session-aware adjustments."""

    def adjust(
        self, base_params: Dict[str, Any], session: SessionContext
    ) -> Dict[str, Any]:
        adjusted = dict(base_params)

        # Scale top_k for large repositories.
        if session.repo_size and session.repo_size > 10_000:
            if "top_k" in adjusted and isinstance(adjusted["top_k"], (int, float)):
                adjusted["top_k"] = int(adjusted["top_k"] * 2)

        return adjusted


def resolve_params(
    defaults: Dict[str, Any],
    session_ctx: Optional[SessionContext] = None,
    query_params: Optional[Dict[str, Any]] = None,
    *,
    session_rules: Optional[SessionParamRules] = None,
) -> ResolvedParams:
    """
    Merge parameter layers with later layers taking precedence.

    Args:
        defaults: Layer 1+2 defaults from config.yaml.
        session_ctx: Layer 3 runtime session context.
        query_params: Layer 4 query-time overrides (invocation inputs).
        session_rules: Optional rules for session-based adjustment.

    Returns:
        ``ResolvedParams`` with merged params and provenance tracking.
    """
    result: Dict[str, Any] = {}
    provenance: Dict[str, str] = {}

    # Layer 1+2: config defaults
    for k, v in defaults.items():
        result[k] = v
        provenance[k] = "defaults"

    # Layer 3: session context adjustments
    if session_ctx is not None:
        rules = session_rules or DefaultSessionRules()
        adjusted = rules.adjust(result, session_ctx)
        for k, v in adjusted.items():
            if k not in result or result[k] != v:
                result[k] = v
                provenance[k] = "session"

    # Layer 4: query-time overrides (highest priority)
    if query_params:
        for k, v in query_params.items():
            result[k] = v
            provenance[k] = "query"

    return ResolvedParams(params=result, provenance=provenance)
