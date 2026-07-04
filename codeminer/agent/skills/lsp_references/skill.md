<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# lsp_references

Static-index "find references" backed by CodeMiner's symbol graph, not a live
LSP server. Use it when you need call/use sites for a symbol or for the symbol
at a file+line from `read`/`grep`.

Inputs are agent-facing: `line` is 1-based like `read` output. Results are
compact definition/reference locations only. Read the returned file/range
before using it in a final answer.
