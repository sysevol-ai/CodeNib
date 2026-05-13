# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MCP prompt resource - guidance for calling agents."""

CODEMINER_GUIDE = """\
# CodeMiner Tools Guide

CodeMiner provides **semantic code search** over pre-built indexes. Results are
structured at the symbol level (functions, classes, methods) with precise file
locations and source content, not raw line-level grep output.

## When to use each tool

### search_bm25
Best for **keyword / exact-name** lookups.  Use when you know (or can guess)
the function name, class name, error string, or specific identifier you are
looking for.

Examples:
- "find the function `calculate_tax`"
- "where is `DatabaseConnectionError` raised"

### search_regex
Best for **pattern-based** searches across the code graph.  Supports full Python
regex syntax plus optional file-glob and node-type filters.

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
| Know the exact symbol name | search_bm25 |
| Looking for a pattern across many files (symbol-level) | search_regex |
| Natural-language description of what the code does | search_bm25 |
| Need structural filters (by file glob or node type) | search_regex |
| Need raw-text occurrence anywhere in the repo, fast | search_zoekt |
| Hit may live in comments / docs / configs (off-graph) | search_zoekt |

## Tips
- `search_bm25` returns results ranked by BM25 relevance.  Start with
  `top_k=10` and increase if the target is not in the first page.
- `search_regex` returns **all** matches up to `top_k`.  Use `file_glob`
  and `node_type` to narrow down large result sets.
- `search_zoekt` returns file-level matches.  Use ``file_filter`` to
  restrict by path glob/regex.  Zoekt query atoms (``case:yes``,
  ``lang:python``, ``r:<regex>``) can be inlined in the ``query``.
- BM25 / regex tools return symbol-level fields (`type` is one of
  ``function``, ``class``, ``method``).  Zoekt results carry ``type="file"``.
"""
