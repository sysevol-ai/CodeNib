<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Hybrid Search

## When to Use

Hybrid search combines results from multiple retrieval strategies (typically
BM25 keyword search and embedding semantic search) using weighted score
fusion. Choose this skill when:

- You need both precision (exact keyword hits) and recall (semantic
  understanding) in a single result set.
- The query mixes specific identifiers with conceptual descriptions
  (e.g. "the parse_config function that handles YAML validation").
- You want the most comprehensive retrieval coverage and are willing to
  accept higher latency.
- Previous single-strategy searches returned incomplete results.

## When NOT to Use

- **Simple keyword lookups**: Use `bm25_search` alone -- it is faster and
  sufficient for exact token matching.
- **Pure semantic queries**: Use `embedding_search` alone when the query is
  entirely conceptual with no specific identifiers.
- **Pattern/structural queries**: Use the `grep` default tool for file-glob or
  regex-based filtering.

## How It Works

The executor accepts pre-computed candidate lists from upstream retrievers.
It normalises scores across branches, applies per-retriever weights, and
merges results by code location. When the same code node appears in multiple
branches its weighted scores are summed, boosting high-confidence matches.

If weights are not provided or their length does not match the number of
candidate lists, uniform weights (1.0 each) are used.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `candidates` | `List[List[QueriedNode]]` | *(required)* | Result lists from upstream retrievers. |
| `top_k` | `int` | `20` | Maximum number of fused results to return. |
| `weights` | `List[float]` | `[]` | Fusion weight for each candidate list. Uniform if omitted. |

## Output

Returns `List[QueriedNode]` -- merged and re-ranked results sorted by
fused score (descending).
