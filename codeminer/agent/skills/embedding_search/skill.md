<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Embedding Search

## When to Use

Embedding search performs semantic retrieval using vector embeddings. It
understands the *meaning* of code, not just the literal tokens. Choose this
skill when:

- The query describes intent or concepts rather than exact identifiers
  (e.g. "function that validates user credentials", "error handling logic").
- You need to find code that is functionally similar to the query even when
  it uses different naming conventions.
- You want to search at different granularity levels -- file-level skeletons
  (`l0`) or individual functions/methods (`l2`).
- Recall on conceptual queries matters more than raw speed.

## When NOT to Use

- **Exact identifier lookups**: If you know the precise name (`parse_config`,
  `UserModel`), use `bm25_search` -- it is faster and more precise for
  literal matches.
- **Structural pattern matching**: Use `regex_search` for file-glob or
  regex-based filtering.
- **Maximum coverage**: When you need both keyword precision and semantic
  recall, prefer `hybrid_search`.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `query` | `str` | *(required)* | Natural-language or code-like search query. |
| `top_k` | `int` | `20` | Maximum number of results to return. |
| `level` | `str` | `l2` | Retrieval granularity: `l0` for file skeletons, `l2` for functions/methods. |
| `return_content` | `bool` | `true` | Include source code content in results. |
| `score_threshold` | `float` | `null` | Minimum similarity score cutoff. |
| `mask_name` | `str` | `null` | Restrict search to a named subset of node IDs. |

## Output

Returns `List[QueriedNode]` -- ranked code nodes with similarity scores and
optional source content.
