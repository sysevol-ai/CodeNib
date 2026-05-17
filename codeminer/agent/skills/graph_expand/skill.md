<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# graph_expand

LSP-aligned 1-hop graph query. Given one or more **source ranges** (file +
line span) or **symbol names**, returns the symbols related to each seed
tagged by role:

- `defined`  — symbols whose definition spans the seed range
- `callees`  — symbols the seed range calls/references
- `callers`  — symbols that call into the seed range/symbol

Each result carries the call-site `anchor_line` (where the relationship was
observed), so you can reason about graph structure without reading any
code. To inspect a candidate's body, follow up with `file_read` (planned
default tool, tracked in #145; until that lands, agents can fall back to
reading lines via shell / other available file-reading primitive).

## When to use

- Just got a BM25 / embedding hit — expand it to see what it calls, what
  calls it, and what other symbols share its range
- Following a call chain across turns: range → callees → pick one → its
  callers → ...
- Resolving an ambiguous symbol name to all definition sites that match it

## When NOT to use

- As a first retrieval step (use `bm25_search` / `embedding_search` /
  `regex_search` to get seed locations first)
- For semantic similarity (use `embedding_search`)
- For reading code bodies — use `file_read` once you've picked candidates
  (see note on `file_read` availability above)

## Upstream seeding contract

When `graph_expand` is reached from another tool, the **caller must pass
`ranges`, not `symbols`** — because:

- BM25 / embedding hits naturally carry `file + start_line + end_line` on
  every result; passing those directly avoids a string-based symbol name
  round-trip that can fail on non-Python languages (rust / ts / go raw
  SCIP names, clangd USRs).
- LSP-aligned: ranges are the universal addressing primitive across
  languages. Symbol resolution via `unified_name` is best-effort and
  language-specific.
- See #119 Point 2 — "no span-to-symbol resolution on input side."

`symbols` is intended for LLM-driven follow-up calls only: the agent saw
a callee name in a previous `graph_expand` result and wants to drill into
it without re-querying its range. In that case `symbols=[...]` is the
shortest path.

## Input shapes

Provide at least one of `ranges` or `symbols` (or both).

| Parameter      | Type           | Default | Description                                                                                                  |
|----------------|----------------|---------|--------------------------------------------------------------------------------------------------------------|
| `ranges`       | `List[Dict]`   | none    | `[{"file": str, "start_line": int, "end_line": int}, ...]`. 0-based inclusive. Inverted ranges auto-swap.    |
| `symbols`      | `List[str]`    | none    | Identity names (`"foo.py:Cls.method"`) or display unified_names. Unknown symbols silently dropped.           |
| `mode`         | `str`          | `all`   | `"defined"` / `"callees"` / `"callers"` / `"all"`                                                            |
| `top_k`        | `int`          | `50`    | Cap on result count. When truncated, a sentinel record is appended.                                          |
| `filter_tests` | `bool`         | `true`  | Exclude test files from results.                                                                             |
| `hops`         | `int`          | `1`     | Multi-hop reserved for future. v1 silently clamps to 1 — iterate via repeated calls for deeper traversal.    |

## Output

`List[QueriedNode]`. Each result carries:

- `node_name`, `type` (function / class / method / ...), `file`, `start_line`, `end_line`
- `role` — `"defined"` / `"callees"` / `"callers"`
- `edge_kind` — graph edge type for callees/callers; `None` for defined
- `anchor_file`, `anchor_line` — call-site location (for callees: in source range; for callers: in the calling symbol)

**No code body is returned.** Use `file_read(file, start_line, end_line)` to
fetch any body you decide is worth inspecting (see availability note above).

**Dedup behaviour.** When the same target/caller vertex is reached from
multiple seeds (or appears as several overloads with the same display name
in one file), graph_expand returns it **once** — keeping the first anchor.
If you need every call site, use the lower-level `query_range` API
directly (not exposed as a skill).

## Why iterate instead of multi-hop

Each call is cheap. Asking for `hops=2` upfront forces the tool to make
relevance decisions that the LLM is better at: "is callee X interesting
enough to walk into" depends on the current task, which only the LLM knows.
A repeated `hops=1` chain is both more controllable and almost always
shorter than the equivalent batch expansion.
