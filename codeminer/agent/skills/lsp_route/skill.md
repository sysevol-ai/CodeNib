<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# lsp_route

Static-index semantic route map backed by CodeNib's symbol graph, not a live
LSP server. Use it early when a localization request names a symbol, error code,
config field, API, or cross-file behavior and you need a compact map of likely
endpoint, bridge/factory, provider/value, and type anchors.

Call with the most specific symbol-like names you know:

- `symbols=["NewResolver", "DefaultConfig"]`
- `symbols=["F523"]`

If no reliable symbol is known yet, call with `symbols=[]` plus the original
request as `query`; the static graph will try query-seeded route anchors.

Results are compact node locations with role and `via` markers only. Read the
returned files/ranges before using them in a final answer.
