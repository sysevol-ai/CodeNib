# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MCP prompt resource - guidance for calling agents."""

CODENIB_FULL_INSTRUCTIONS = (
    "CodeNib provides bounded repository exploration over pre-built indexes. "
    "Start with explore_context to retrieve, navigate, expand dependencies, "
    "and inspect verified source in one call. The full surface also exposes "
    "lower-level search, LSP-shaped navigation, dependency, source, and "
    "manifest tools when one operation must be forced explicitly."
)

CODENIB_EXPLORE_INSTRUCTIONS = (
    "CodeNib provides one bounded repository exploration operation. Use "
    "explore_context with a repository question and optional symbol seeds. "
    "Inspect its plan, source identity, diagnostics, and session usage before "
    "drawing conclusions."
)

CODENIB_EXPLORE_GUIDE = """\
# CodeNib Explore Guide

Use `explore_context` for repository questions. It composes ranked retrieval,
symbol routing, dependency expansion, and bounded source windows in one call.
Provide a precise `query`; add `symbols` when you already know likely entry
points. Start with `budget="balanced"`, use `budget="fast"` for orientation,
or `budget="thorough"` when the first result lacks enough relationships.

Check `plan.route.provider` to see which navigation backend actually ran,
`source.verified` before treating excerpts as live checkout source, and
`diagnostics` for independently unavailable capabilities. Within one stdio
connection, identical verified ranges can become stable session pointers;
the response summary reports retained ranges, deduplication, and evictions.
"""

CODENIB_GUIDE = """\
# CodeNib Tools Guide

CodeNib serves ranked context and precise navigation over pre-built repository
indexes. Start with `explore_context`: it composes ranked retrieval, symbol
routing, dependency expansion, and verified source windows under one bounded
response. Use the lower-level tools when you need to force one operation.
Search responses keep every ranked location but may project large source bodies
under a shared content budget. A truncated hit includes ``content_projection``
metadata; use ``read_source`` with its file and 1-based line range to inspect
content authenticated against the indexed v2 source fingerprint before
finalizing an answer. The displayed commit is not a mutable-HEAD attestation.

## When to use each tool

### explore_context
Best default for repository exploration. It reports the concrete retrieval and
route plan, groups bounded source windows by file, and degrades unavailable
providers through explicit diagnostics. Optional symbol seeds improve route
and dependency precision; repeated verified ranges may use stable per-session
pointers instead of retransmitting identical source.

### search_context
Best lower-level ranked search. It returns the selected route, indexed commit,
source fingerprint, and ranked source spans. Use `budget="fast"` for a smaller
retrieval path or `budget="thorough"` when graph expansion is available.

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

### read_source
Use after search or static navigation identifies a source location. It accepts
only a repository-relative POSIX path and at most 200 lines per call. The
server enables reads only while a retained source authority matches the v2
content fingerprint and per-file records from a manifest or portable-artifact
binding. Each response carries the indexed commit as display provenance and the
content fingerprint used by that authority; it does not claim that mutable Git
HEAD is attested.

### Choosing between them
| Scenario | Tool |
|----------|------|
| General repository question | explore_context |
| Force ranked retrieval without composed source windows | search_context |
| Know the exact symbol name | search_bm25 |
| Looking for a pattern across CodeGraph file/symbol nodes | search_regex |
| Force vector similarity for a natural-language query | search_semantic |
| Need structural filters (by file glob or node type) | search_regex |
| Need raw-text occurrence anywhere in the repo, fast | search_zoekt |
| Hit may live in comments / docs / configs (off-graph) | search_zoekt |
| Inspect an admitted source span | read_source |

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

__all__ = [
    "CODENIB_EXPLORE_GUIDE",
    "CODENIB_EXPLORE_INSTRUCTIONS",
    "CODENIB_FULL_INSTRUCTIONS",
    "CODENIB_GUIDE",
]
