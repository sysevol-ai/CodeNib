#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
#
# Start CodeNib's MCP server for the project the agent opened. Claude Code
# passes the project directory; a host that does not set it starts the server
# in the project, so the working directory stands in.
#
# The search index is built or refreshed first. CodeNib keeps it under
# ~/.codenib, never in the checkout. The code graph is prepared on first use.
# Only the MCP protocol may reach stdout, so everything else goes to stderr.
set -euo pipefail

project="${1:-${CLAUDE_PROJECT_DIR:-$PWD}}"
if [[ ! -d "$project" ]]; then
  project="$PWD"
fi

# CodeNib before 0.2.5 does not know the options passed below. An older one on
# the PATH is passed over for uvx, or named so the user can upgrade it.
serves_this_plugin() {
  "$@" mcp --help 2>/dev/null | grep -q -- '--defer-graph'
}

if command -v codenib >/dev/null 2>&1 && serves_this_plugin codenib; then
  codenib=(codenib)
elif command -v uvx >/dev/null 2>&1; then
  codenib=(uvx --quiet --from "codenib[graph,mcp]>=0.2.5" codenib)
elif command -v codenib >/dev/null 2>&1; then
  echo "The installed CodeNib is older than 0.2.5, which this plugin needs." \
    "Upgrade it with python -m pip install -U 'codenib[graph,mcp]', then" \
    "restart the MCP server." >&2
  exit 1
else
  echo "CodeNib is not installed. Run /codenib:setup, or install it with" \
    "python -m pip install 'codenib[graph,mcp]', then restart the MCP server." >&2
  exit 1
fi

if ! root="$(git -C "$project" rev-parse --show-toplevel 2>/dev/null)"; then
  echo "CodeNib serves Git checkouts; $project is not inside one." >&2
  exit 1
fi

"${codenib[@]}" index "$root" --preset fast >&2
exec "${codenib[@]}" mcp "$root" --defer-graph --tool-surface explore
