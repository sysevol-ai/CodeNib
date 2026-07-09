<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# lsp_definition

LSP-compatible "go to definition" using the runtime's selected semantic
provider. Use it when you have a file+line from `read`/`grep` and want the
definition target without fanning out through raw grep. A symbol-only lookup is
available when the selected provider supports it.

Inputs are agent-facing: `line` is 1-based like `read` output. Results are
compact node locations only; runtime traces identify the serving provider. Read
the returned file/range before using it in a final answer.
