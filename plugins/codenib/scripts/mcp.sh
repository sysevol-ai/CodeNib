#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Start CodeNib's MCP server for the project Claude Code opened.
#
# The search index is built or refreshed first. CodeNib keeps it under
# ~/.codenib, never in the checkout. The code graph is prepared on first use.
# Only the MCP protocol may reach stdout, so everything else goes to stderr.
set -euo pipefail

project="${1:-${CLAUDE_PROJECT_DIR:-$PWD}}"

if command -v codenib >/dev/null 2>&1; then
  codenib=(codenib)
elif command -v uvx >/dev/null 2>&1; then
  codenib=(uvx --quiet --from "codenib[graph,mcp]" codenib)
else
  echo "CodeNib is not installed. Run /codenib:setup, or install it with" \
    "python -m pip install 'codenib[graph,mcp]', then reconnect in /mcp." >&2
  exit 1
fi

if ! root="$(git -C "$project" rev-parse --show-toplevel 2>/dev/null)"; then
  echo "CodeNib serves Git checkouts; $project is not inside one." >&2
  exit 1
fi

"${codenib[@]}" index "$root" --preset fast >&2
exec "${codenib[@]}" mcp "$root" --defer-graph --tool-surface explore
