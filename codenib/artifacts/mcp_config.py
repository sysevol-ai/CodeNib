# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Generate reviewable MCP client configuration for a verified artifact."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .runtime import bind_context_artifact

MCP_CONFIG_HOSTS = ("claude", "codex", "json")
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def render_artifact_mcp_config(
    artifact_path: str | Path,
    repo_path: str | Path,
    *,
    host: str,
    server_name: str = "codenib",
    command: str = "codenib",
    expected_repository: str | None = None,
    claude_scope: str = "project",
) -> str:
    """Verify a binding and render a host command or generic JSON config."""

    if host not in MCP_CONFIG_HOSTS:
        raise ValueError(f"unsupported MCP config host: {host}")
    if not _SERVER_NAME_RE.fullmatch(server_name):
        raise ValueError(
            "MCP server name must use letters, digits, dot, dash, or underscore"
        )
    if not command or "\x00" in command or "\n" in command or "\r" in command:
        raise ValueError("MCP command must be a non-empty single-line value")
    if claude_scope not in {"local", "project", "user"}:
        raise ValueError("Claude MCP scope must be local, project, or user")

    with bind_context_artifact(
        artifact_path,
        repo_path,
        expected_repository=expected_repository,
    ) as binding:
        args = [
            "mcp",
            "--artifact",
            str(binding.artifact.root),
            "--repo",
            str(binding.repo_path),
            "--repository",
            binding.artifact.repository,
        ]
    server = {
        "type": "stdio",
        "command": command,
        "args": args,
    }
    if host == "json":
        return (
            json.dumps(
                {"mcpServers": {server_name: server}},
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    if host == "claude":
        payload = json.dumps(server, ensure_ascii=True, separators=(",", ":"))
        return (
            shlex.join(
                [
                    "claude",
                    "mcp",
                    "add-json",
                    "--scope",
                    claude_scope,
                    server_name,
                    payload,
                ]
            )
            + "\n"
        )
    return shlex.join(["codex", "mcp", "add", server_name, "--", command, *args]) + "\n"


__all__ = ["MCP_CONFIG_HOSTS", "render_artifact_mcp_config"]
