<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# lsp_definition

Static-index "go to definition" backed by CodeMiner's symbol graph, not a live
LSP server. Use it when you have a file+line from `read`/`grep` or a symbol
name and want the definition target without fanning out through raw grep.

Inputs are agent-facing: `line` is 1-based like `read` output. Results are
compact node locations only. Read the returned file/range before using it in a
final answer.
