# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""MCP prompt resource - guidance for calling agents."""

CODENIB_FULL_INSTRUCTIONS = (
    "CodeNib finds repository implementations and source evidence. "
    "Use explore_context with a question or symbol name in query; include any "
    "known file hints there. It returns bounded source windows with file and "
    "line evidence. The full surface also exposes "
    "lower-level search, LSP-shaped navigation, dependency, source, and "
    "manifest tools when one operation must be forced explicitly. Graph "
    "dependencies are indexed static references, including fields, type uses "
    "and function values. They do not prove invocation or complete runtime "
    "coverage; inspect source call sites before describing calls. In on-demand "
    "graph mode, a cold query without symbols starts with source; symbols or "
    "include_dependencies=true request provider and graph preparation. A complete "
    "warm graph is reused; include_dependencies=false omits expansion. Check "
    "source verification and coverage before relying on a result."
)

CODENIB_EXPLORE_INSTRUCTIONS = (
    "CodeNib finds implementations, source evidence, and indexed relationships "
    "for repository questions. Start with explore_context using query alone: "
    "a question, symbol name, error text, and any file hints belong in query. "
    "Read the returned source windows and file/line evidence; check verification "
    "and coverage diagnostics. In on-demand graph mode, a cold query without "
    "symbols starts with source. For indexed relationships, provide symbols or "
    "include_dependencies=true; preparation can install providers and build a "
    "graph. A complete warm graph is reused; include_dependencies=false omits "
    "expansion. Indexed references include fields, types, and function values. "
    "Inspect source before claiming invocation or a complete runtime path; "
    "external and dynamic calls may be absent."
)

CODENIB_GREP_INSTRUCTIONS = (
    "CodeNib searches current repository source with model-planned grep and Jev. "
    "Use explore_context with a precise repository question. Each call uses the "
    "user's OpenRouter account: planning receives the query and directory overview, "
    "and scoring receives candidate source snippets. Inspect plan.retrieval for "
    "actual models, reported cost, and failures; source.verified for source identity. "
    "This mode does not load a graph: symbols, direction, and include_dependencies "
    "do not add graph navigation. budget bounds returned context, not API spend. "
    "Do not automatically repeat a failed or empty query."
)

CODENIB_EXPLORE_GUIDE = """\
# CodeNib Explore Guide

Find the code behind a repository question with `explore_context`. Start with
`query` alone: use a question, symbol name, or error text, and include any known
file hints in that query. The result provides bounded source windows with file
paths and line ranges to inspect.

Use `budget="balanced"` initially, `budget="fast"` for orientation, or
`budget="thorough"` for more returned context. The budget controls returned
context and does not request graph preparation.

In on-demand graph mode, a cold query without symbols returns ranked source
by default. When you need indexed relationships, provide `symbols` or
`include_dependencies=true` to prepare and use the graph. That request can
install providers and build the graph. Once a complete graph is ready,
ordinary queries reuse it; set
`include_dependencies=false` to omit dependency expansion explicitly.

Check `plan.route.provider` to see which navigation backend actually ran,
`source.verified` before treating excerpts as live checkout source, and
`diagnostics` for independently unavailable capabilities. Within one stdio
connection, identical verified ranges can become stable session pointers;
the response summary reports retained ranges, deduplication, and evictions.
"""

CODENIB_GUIDE = """\
# CodeNib Tools Guide

CodeNib finds repository implementations and source evidence. Start with an
`explore_context` query containing your question, known symbol names, or error
text and any file hints. It returns bounded source windows with file paths and
line ranges. Use the lower-level tools when you need to force one operation.
Search responses keep every ranked location but may project large source bodies
under a shared content budget. A truncated hit includes ``content_projection``
metadata; use ``read_source`` with its file and 1-based line range to inspect
content authenticated against the indexed v2 source fingerprint before
finalizing an answer. The displayed commit is not a mutable-HEAD attestation.

## When to use each tool

### explore_context
Find the code behind a repository question with `query` alone. It reports the
retrieval and route plan, groups bounded source windows by file, and describes
unavailable providers in diagnostics. Supply `symbols` when requesting indexed
locations and relationships for known entry points; repeated verified ranges
may use stable per-session
pointers instead of retransmitting identical source. Dependency expansion
follows indexed static references, including field, type and function-value
uses. Inspect source call sites to establish invocation; external and dynamic
calls may be absent.

In on-demand graph mode, a cold query without symbols starts with ranked source.
For indexed relationships, provide symbols or `include_dependencies=true`;
preparation can install providers and build the graph.
Once a complete graph is ready, ordinary queries reuse it. The context budget
controls returned context, not whether graph preparation is requested.

### search_context
Best lower-level ranked search. It returns the selected route, indexed commit,
source fingerprint, and ranked source spans. Use `budget="fast"` for a smaller
retrieval path or `budget="thorough"` when graph expansion is available.

Examples:
- "where is retry backoff implemented"
- "who calls `refresh_credentials` and how is failure handled"

### dependency_subgraph
Follow incoming (`impact`) or outgoing (`dependencies`) indexed static
references, or use `both` for a neighborhood bounded by `depth`. Results retain
legacy `callers`/`callees` labels; `semantics.reference_direction` states the
reference direction. Edges may include calls, fields, type uses and function
values. Inspect source call sites to identify actual invocations. External and
dynamic calls may be absent; an empty result does not prove there are no callers.

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
| Follow indexed static references between symbols | dependency_subgraph |
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
