<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Language Capabilities

This matrix is generated from `codeminer.languages.LanguageSpec`. It tracks
which surfaces are enabled for each registered language and makes core parity
coverage explicit for languages that are graph-capable, serial-only, or
tree-sitter-only.

Update the registry first, then refresh this table:

```bash
python scripts/language_capability_matrix.py --write docs/language_capabilities.md
python scripts/language_capability_matrix.py --check docs/language_capabilities.md
```

<!-- BEGIN CODEMINER_LANGUAGE_CAPABILITIES -->
| Language | Chunker | GT | Agent | Graph backend | Incremental | LSP command | Core decoder | Core parity |
|----------|---------|----|-------|---------------|-------------|-------------|--------------|-------------|
| Python | yes | yes | yes | scip | lsp | yes | yes | covered |
| Go | yes | yes | yes | scip | lsp | yes | yes | covered |
| Rust | yes | yes | yes | scip | lsp | yes | yes | covered |
| C/C++ | yes | yes | yes | clangd | clangd | yes | no | n/a-no-core-decoder |
| C# | yes | yes | yes | none | none | no | no | n/a-tree-sitter-only |
| Java | yes | yes | yes | lsp | none | yes | no | n/a-no-core-decoder |
| Ruby | yes | yes | yes | none | none | no | no | n/a-tree-sitter-only |
| PHP | yes | yes | yes | none | none | no | no | n/a-tree-sitter-only |
| Kotlin | yes | yes | yes | none | none | no | no | n/a-tree-sitter-only |
| JavaScript | yes | yes | yes | scip | lsp | yes | yes | covered |
| TypeScript | yes | yes | yes | scip | lsp | yes | yes | covered |
<!-- END CODEMINER_LANGUAGE_CAPABILITIES -->

## Parity States

| State | Meaning |
|-------|---------|
| `covered` | The language has an enabled C++ core decoder and belongs in the strict serial/core parity suite. |
| `n/a-no-core-decoder` | The language has a graph backend but no C++ core decoder; parity is not applicable by construction. |
| `n/a-tree-sitter-only` | The language currently supports chunking/retrieval surfaces only; graph/core parity is not applicable yet. |
