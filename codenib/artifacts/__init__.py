# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Portable repository-context artifacts."""

from .archive import extract_context_artifact_archive
from .context import (
    CONTEXT_ARTIFACT_MANIFEST,
    CONTEXT_ARTIFACT_SCHEMA,
    PORTABLE_CONTEXT_VIEWS,
    ContextArtifactResult,
    stage_context_artifact,
)
from .github import (
    GitHubArtifactFetchResult,
    GitHubArtifactRecord,
    fetch_github_context_artifact,
    resolve_github_context_artifact,
)
from .mcp_config import MCP_CONFIG_HOSTS, render_artifact_mcp_config
from .portable_views import (
    SourceTrust,
    normalize_owned_query_view,
    validate_portable_query_view,
)
from .runtime import (
    ContextArtifactBinding,
    VerifiedContextArtifact,
    bind_context_artifact,
    query_context_artifact,
    verify_context_artifact,
)

__all__ = [
    "CONTEXT_ARTIFACT_MANIFEST",
    "CONTEXT_ARTIFACT_SCHEMA",
    "PORTABLE_CONTEXT_VIEWS",
    "ContextArtifactResult",
    "ContextArtifactBinding",
    "GitHubArtifactFetchResult",
    "GitHubArtifactRecord",
    "MCP_CONFIG_HOSTS",
    "SourceTrust",
    "VerifiedContextArtifact",
    "bind_context_artifact",
    "extract_context_artifact_archive",
    "fetch_github_context_artifact",
    "normalize_owned_query_view",
    "query_context_artifact",
    "validate_portable_query_view",
    "resolve_github_context_artifact",
    "render_artifact_mcp_config",
    "stage_context_artifact",
    "verify_context_artifact",
]
