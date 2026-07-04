<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# lsp_route

Static-index semantic route map backed by CodeMiner's symbol graph, not a live
LSP server. Use it when several symbols or a read/search result look related
and you need a compact map of likely endpoint, bridge/factory, provider/value,
and type anchors.

Results are compact node locations with role and `via` markers only. Read the
returned files/ranges before using them in a final answer.
