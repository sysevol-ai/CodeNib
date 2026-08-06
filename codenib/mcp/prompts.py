# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MCP prompt resource - guidance for calling agents."""

CODENIB_GUIDE = """\
# CodeNib Tools Guide

CodeNib serves ranked context and precise navigation over pre-built repository
indexes. Start with `search_context`: it selects a deterministic BM25, dense,
hybrid-RRF, or graph-expanded route from the views that are actually loaded.
Use the lower-level tools when you need to force one operation.

## When to use each tool

### search_context
Best default for repository questions. It returns the selected route, indexed
commit, source fingerprint, and ranked source spans. Use `budget="fast"` for a
smaller retrieval path or `budget="thorough"` when graph expansion is available.

Examples:
- "where is retry backoff implemented"
- "who calls `refresh_credentials` and how is failure handled"

### search_bm25
Best for **keyword / exact-name** lookups.  Use when you know (or can guess)
the function name, class name, error string, or specific identifier you are
looking for.

Examples:
- "find the function `calculate_tax`"
- "where is `DatabaseConnectionError` raised"

### search_regex
Best for **pattern-based** searches across file and symbol nodes in the code
graph. Supports full Python regex syntax plus optional file-glob and node-type
filters.

Examples:
- `def\\s+test_` with `file_glob="**/test_*.py"`: find all test functions
- `TODO|FIXME`: find code annotations
- `class\\s+\\w+Error`: find all custom exception classes

### search_zoekt
Best for **fast raw-text** lookups over the whole repository.  Backed by a
trigram index, so substring and regex queries return in tens of
milliseconds even on large codebases.  Results are *file*-level (with line
ranges and matched snippets), not symbol-level -- use this when you need
hits that span comments, configuration files, vendored data, or any text
that BM25/regex (CodeGraph-only) cannot see.

Default queries are case-sensitive substring matches.  Use Zoekt query
atoms to refine: ``regex:<pattern>`` for regex, ``case:no`` for
case-insensitive, ``lang:python`` to scope by language, ``sym:<name>``
to match symbol definitions.

Examples:
- ``"InvalidTokenError"``: find every textual occurrence of an identifier
- ``"regex:^class\\s+\\w+Repository"`` with ``file_filter="*.py"``: regex
  scoped to Python files
- ``"TODO case:no"``: case-insensitive substring across the repo

### Choosing between them
| Scenario | Tool |
|----------|------|
| General repository question | search_context |
| Know the exact symbol name | search_bm25 |
| Looking for a pattern across CodeGraph file/symbol nodes | search_regex |
| Force vector similarity for a natural-language query | search_semantic |
| Need structural filters (by file glob or node type) | search_regex |
| Need raw-text occurrence anywhere in the repo, fast | search_zoekt |
| Hit may live in comments / docs / configs (off-graph) | search_zoekt |

## Tips
- `search_context` is capability-aware: it does not claim dense or graph
  execution when those views are unavailable. Inspect its `plan` and `source`
  fields before using the returned spans.
- `search_bm25` returns results ranked by BM25 relevance.  Start with
  `top_k=10` and increase if the target is not in the first page.
- `search_regex` returns **all** matches up to `top_k`.  Use `file_glob`
  and `node_type` to narrow down large result sets.
- `search_zoekt` returns file-level matches.  Use ``file_filter`` to
  restrict by path glob/regex.  Zoekt query atoms (``case:yes``,
  ``lang:python``, ``regex:<pattern>``) can be inlined in the ``query``.
- BM25 primarily returns symbols. Regex can return file or symbol nodes
  (for example ``function``, ``class``, or ``method``); use ``node_type`` to
  narrow it. Zoekt results carry ``type="file"``.
"""
