<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# lsp_references

LSP-compatible "find references" using the runtime's selected semantic
provider. Use it when you need call/use sites for the symbol at an exact source
position obtained from repository evidence. It deliberately uses the native
LSP common denominator: a repository-relative file, 1-based line, and 0-based
character. Use `lsp_route` for CodeNib-specific symbol-name navigation.

Inputs are agent-facing: `line` is 1-based like `read` output. Results are
compact definition/reference locations only; runtime traces identify the
serving provider. Read the returned file/range before using it in a final answer.
