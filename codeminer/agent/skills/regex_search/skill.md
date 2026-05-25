<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Regex Search

> **DEPRECATED.** This skill needs a `RegexNodeIndex` that the current
> pipeline does not build, so it raises at call time. For grep-style regex
> over file contents, use the always-on **`file_search(mode="content")`**
> default tool instead — plain regex over files needs no index.

## When to Use

Regex search performs pattern-based retrieval against the in-memory node index.
It is ideal for structural queries that go beyond simple keyword matching.
Choose this skill when:

- You need to find code matching a specific syntactic pattern
  (e.g. `def test_.*`, `class .*Controller`, `import .*logging`).
- You want to filter results by file path using glob patterns
  (e.g. only `*.py` files, or files under `src/`).
- You need to restrict results to a specific node type such as `function`,
  `class`, or `method`.
- The search target is a structural pattern that BM25 would not rank well
  and embedding search would over-generalise.

## When NOT to Use

- **Natural-language or conceptual queries**: Use `embedding_search` for
  semantic understanding of code intent.
- **Exact keyword lookups**: `bm25_search` is more efficient for simple
  token matching without regex overhead.
- **Broad coverage across retrieval strategies**: Use `hybrid_search` when
  you need both precision and recall.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `pattern` | `str` | *(required)* | Regex pattern or literal string to match against code nodes. |
| `top_k` | `int` | `20` | Maximum number of results to return. |
| `file_glob` | `str` | `null` | Glob to restrict search to matching file paths. |
| `node_type` | `str` | `null` | Filter to a specific node type (`function`, `class`, `method`, etc.). |
| `case_sensitive` | `bool` | `false` | Whether the pattern match is case-sensitive. |
| `use_regex` | `bool` | `true` | Interpret `pattern` as a regular expression. Set to `false` for literal matching. |

## Output

Returns `List[QueriedNode]` -- matched code nodes with optional content.
