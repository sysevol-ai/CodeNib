<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# BM25 Search

## When to Use

BM25 search is a fast, keyword-based retrieval method built on term-frequency
scoring (TF-IDF family) over pre-chunked symbol nodes. Choose this skill when:

- You need to find exact identifier names, function signatures, or class names.
- The query contains specific keywords or tokens that appear verbatim in the
  source code (e.g. `parse_config`, `HTTPClient`, `TODO`).
- Speed matters more than semantic understanding -- BM25 is the fastest
  retrieval option available, and it ranks symbol chunks by relevance (which
  raw `grep` cannot).

Set `names_only=true` for the compact LocAgent-style entry point: it returns
symbol NAME tags (name + file:line + kind, no code bodies), which you then
navigate with `find_callers` / `find_callees` / `trace` and read on demand.

## When NOT to Use

- **Semantic or conceptual queries**: If the query describes *intent* rather
  than exact tokens (e.g. "function that handles authentication"), use
  `embedding_search` instead.
- **Raw text / regex over file contents**: Use the always-on `grep` tool.
- **High-recall scenarios requiring both keyword and semantic coverage**: Use
  `hybrid_search` to combine BM25 with embedding retrieval.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `query` | `str` | *(required)* | The search query -- literal keywords work best. |
| `top_k` | `int` | `20` | Maximum number of results to return. |
| `names_only` | `bool` | `false` | Return NAME tags only (no code bodies). |
| `filter_test` | `bool` | `false` | When true, exclude test files from results. |

## Output

Returns `List[QueriedNode]` -- ranked code nodes with scores. Names are
relabeled to their readable `unified_name` when a symbol graph is available;
code bodies are omitted when `names_only=true`.
