<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Language Capabilities

This matrix is generated from `codenib.languages.LanguageSpec`. It tracks
which surfaces are enabled for each registered language and makes core parity
coverage explicit for languages that are graph-capable, serial-only, or
tree-sitter-only.

`yes` and `active` describe registered CodeNib capabilities, not the tools
installed on the current machine. Run
`codenib toolchain status /path/to/repository --scope all` to inspect the exact
SCIP/LSP providers, and run
`codenib doctor /path/to/repository --require graph` before building a graph
view. See [SCIP And Graph Indexing](scip_index.md#language-provider-map) for the
package map and installation boundary.

Update the registry first, then refresh this table:

```bash
python scripts/language_capability_matrix.py --write docs/language_capabilities.md
python scripts/language_capability_matrix.py --check docs/language_capabilities.md
```

<!-- BEGIN CODENIB_LANGUAGE_CAPABILITIES -->
| Language | Chunker | GT | Agent | Graph backend | SCIP cold-start | Incremental | LSP command | Core decoder | Core parity |
|----------|---------|----|-------|---------------|-----------------|-------------|-------------|--------------|-------------|
| Python | yes | yes | yes | scip | active | lsp | yes | yes | covered |
| Go | yes | yes | yes | scip | active | lsp | yes | yes | covered |
| Rust | yes | yes | yes | scip | active | lsp | yes | yes | covered |
| C/C++ | yes | yes | yes | clangd | none | clangd | yes | no | n/a-no-core-decoder |
| C# | yes | yes | yes | scip | active | none | yes | no | n/a-no-core-decoder |
| Java | yes | yes | yes | scip | active | none | yes | no | n/a-no-core-decoder |
| Ruby | yes | yes | yes | scip | active | none | yes | yes | covered |
| PHP | yes | yes | yes | scip | active | none | yes | no | n/a-no-core-decoder |
| Kotlin | yes | yes | yes | scip | active | none | yes | no | n/a-no-core-decoder |
| Swift | yes | yes | yes | none | none | none | no | no | n/a-tree-sitter-only |
| Scala | yes | yes | yes | scip | active | none | no | no | n/a-no-core-decoder |
| Lua | yes | yes | yes | none | none | none | no | no | n/a-tree-sitter-only |
| JavaScript | yes | yes | yes | scip | active | lsp | yes | yes | covered |
| TypeScript | yes | yes | yes | scip | active | lsp | yes | yes | covered |
<!-- END CODENIB_LANGUAGE_CAPABILITIES -->

## SCIP Cold-Start States

| State | Meaning |
|-------|---------|
| `active` | The active cold-start graph backend is SCIP or a SCIP-first hybrid route. |
| `candidate` | A known SCIP indexer should be evaluated before treating the current LSP or tree-sitter-only path as final. It is not routed until smoke, backend alignment, decoder support, and parity gates pass. |
| `none` | No SCIP cold-start option is currently tracked for the language. |

## Parity States

| State | Meaning |
|-------|---------|
| `covered` | The language has an enabled C++ core decoder and belongs in the strict serial/core parity suite. |
| `n/a-no-core-decoder` | The language has a graph backend but no C++ core decoder; parity is not applicable by construction. |
| `n/a-tree-sitter-only` | The language currently supports chunking/retrieval surfaces only; graph/core parity is not applicable yet. |
