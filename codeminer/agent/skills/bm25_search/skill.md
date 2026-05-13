<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# BM25 Search

## When to Use

BM25 search is a fast, keyword-based retrieval method built on term-frequency
scoring (TF-IDF family). Choose this skill when:

- You need to find exact identifier names, function signatures, or class names.
- The query contains specific keywords or tokens that appear verbatim in the
  source code (e.g. `parse_config`, `HTTPClient`, `TODO`).
- Speed matters more than semantic understanding -- BM25 is the fastest
  retrieval option available.
- You want to locate simple text patterns without the overhead of embedding
  computation.

## When NOT to Use

- **Semantic or conceptual queries**: If the query describes *intent* rather
  than exact tokens (e.g. "function that handles authentication"), use
  `embedding_search` instead.
- **Pattern matching with wildcards or regex**: Use `regex_search` for
  structural pattern matching.
- **High-recall scenarios requiring both keyword and semantic coverage**: Use
  `hybrid_search` to combine BM25 with embedding retrieval.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `query` | `str` | *(required)* | The search query -- literal keywords work best. |
| `top_k` | `int` | `20` | Maximum number of results to return. |
| `filter_test` | `bool` | `false` | When true, exclude test files from results. |
| `return_content` | `bool` | `true` | Include source code content in results. |
| `wrap_with_line_numbers` | `bool` | `true` | Prefix each line with its line number. |

## Output

Returns `List[QueriedNode]` -- ranked code nodes with scores and optional
source content.
