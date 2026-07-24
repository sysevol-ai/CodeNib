<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# lsp_definition

LSP-compatible "go to definition" using the runtime's selected semantic
provider. Use it when you have an exact source position from repository
evidence and want the definition target without fanning out through raw grep.
It deliberately uses the native LSP common denominator: a repository-relative
file, 1-based line, and 0-based character.
Use `lsp_route` for CodeNib-specific symbol-name navigation.

Inputs are agent-facing: `line` is 1-based like `read` output. Results are
compact node locations only; runtime traces identify the serving provider. Read
the returned file/range before using it in a final answer.
