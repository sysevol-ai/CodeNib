<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Find Related Code (call-graph navigation)

Answer "who calls this?" / "what does this call?" using the symbol graph —
the structural questions grep cannot answer. Complements file_search/file_read:
use search/grep to find a symbol, this to follow its relationships, then
file_read to confirm the bodies that matter.

## When to use

- You found a function/class and want its **callers** (impact / where it's used)
  or **callees** (what it depends on).
- Localizing a bug whose fix sits in a *caller* or *callee* of an obvious symbol.
- Cross-file navigation where grepping a name returns too many or too few hits.

## Inputs

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| symbol    | str  | —       | Function/class name (fuzzy-matched; ambiguity returns candidates). |
| relation  | str  | both    | `callers` / `callees` / `both`. |
| hops      | int  | 1       | 1 or 2 hops. |

## Output

A compact list of related symbols: `name`, `file:line`, `kind`, and the
relation (`caller of X` / `callee of X`). **No source code** — call `file_read`
on the file:line of the ones you want to inspect.
