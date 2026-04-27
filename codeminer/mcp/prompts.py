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

### Choosing between them
| Scenario | Tool |
|----------|------|
| Know the exact symbol name | search_bm25 |
| Looking for a pattern across many files | search_regex |
| Natural-language description of what the code does | search_bm25 |
| Need structural filters (by file glob or node type) | search_regex |

## Tips
- `search_bm25` returns results ranked by BM25 relevance.  Start with
  `top_k=10` and increase if the target is not in the first page.
- `search_regex` returns **all** matches up to `top_k`.  Use `file_glob`
  and `node_type` to narrow down large result sets.
- Both tools return symbol-level results: `node_name`, `type`, `file`,
  `start_line`, `end_line`, and `content`.
"""
