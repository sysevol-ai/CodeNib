# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The source-only grep/Jev vertical shared by the CLI and MCP tool."""

from __future__ import annotations

import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from ..agent.runtime.grep_jev import GrepJevConfig, GrepJevError
from ..compiler.manifest import RepoManifest
from ..compiler.manifest_source import resolve_compiler_source_selection
from ..paths import repo_index_dir
from ..source_fingerprint import capture_repository_source, lexical_repository_path
from .context import ServerContext
from .tools._validation import MAX_SEARCH_QUERY_CHARS, bounded_integer, required_text
from .tools.explore import explore_context_impl


def explore_repository(
    repository: str | Path,
    config: GrepJevConfig,
    query: str,
    *,
    top_k: int = 5,
    budget: str = "balanced",
    filter_test: bool = False,
    check_cancelled: Callable[[], None] = lambda: None,
) -> dict:
    """Refresh source each call, without building or loading any index.

    The existing source binding owns filesystem authority for the whole call.
    The temporary context borrows that authority; no session retains it. The
    final whole-tree validation is the delivery boundary. A changed source or
    cancellation cannot publish a response from a partially validated query.
    No commit claim is made for a mutable working tree.
    """
    query = required_text(query, name="query", maximum=MAX_SEARCH_QUERY_CHARS)
    top_k = bounded_integer(top_k, name="top_k", maximum=20)
    if budget not in {"fast", "balanced", "thorough"}:
        raise ValueError("budget must be 'fast', 'balanced', or 'thorough'.")
    config.credential()  # Missing credentials must fail before scanning source.
    if shutil.which("rg") is None:
        raise GrepJevError("Install ripgrep (rg) to use grep → Jev")
    deadline = time.monotonic() + config.timeout

    def check() -> None:
        check_cancelled()
        if time.monotonic() >= deadline:
            raise GrepJevError("grep → Jev request timed out")

    root = lexical_repository_path(repository)
    selection = resolve_compiler_source_selection(repo_index_dir(root))
    with capture_repository_source(
        root, selection=selection, check_cancelled=check
    ) as source:
        identity = source.authenticated_identity_snapshot(check_cancelled=check)
        manifest = RepoManifest(
            repo_path=str(root),
            source_fingerprint=identity.fingerprint,
            source_selection=identity.source_selection,
            file_count=identity.file_count,
        )
        # Direct injection uses the same verified reader and evidence formatter
        # as indexed MCP. The lexical source binding remains owned by this call.
        remaining_config = replace(
            config, timeout=max(0.001, deadline - time.monotonic())
        )
        context = ServerContext(
            manifest=manifest, grep_jev=remaining_config, source_error=None
        )
        context._install_repository_source(source)
        response = explore_context_impl(
            context,
            query,
            top_k=top_k,
            budget=budget,
            include_dependencies=False,
            filter_test=filter_test,
            check_cancelled=check,
        )
        source.authenticated_identity_snapshot(check_cancelled=check)
        response["source"]["verification_scope"] = "content-bytes"
        response["source"]["commit_verified"] = False
        return response
