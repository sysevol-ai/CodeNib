<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Graph Range Query

`CodeGraph` supports LSP-aligned queries that resolve a **source-file line range** (or a
symbol) into the symbols defined there and the reference edges crossing that range.
These return typed records (`NodeRef`/`EdgeRef`/`RangeQueryResult`) so consumers never
poke `graph.vs[...]` directly. The API lives in `codenib/graph/code_graph.py`.

## Building the indexes

Range queries are served from per-file indexes that must be built once after the graph
is fully constructed (and again after any incremental patcher batch):

```python
graph.build_range_indexes()   # O(V + E); pickled with the graph by save_graph()
```

Because the indexes are saved with the graph, a subsequent `load_graph()` does not need
to rebuild them.

## Querying

```python
result = graph.query_range(
    file="src/auth.py",
    start_line=10,      # inclusive, 0-based
    end_line=42,        # inclusive, 0-based
    kinds=None,         # defaults to {EDGE_TYPE_REFERENCE}
    depth=1,            # only depth=1 is supported in v1
)

result.defined    # List[NodeRef]  — symbols defined within the range
result.outgoing   # List[EdgeRef]  — references anchored inside the range
result.incoming   # List[EdgeRef]  — references targeting symbols defined in the range
```

`query_range_by_symbol(name, kinds=None, depth=1)` resolves a symbol by its identity
`name` (the globally-unique, semi-raw SCIP symbol — not the display `unified_name`) to
its file/line span and forwards to `query_range`. It returns an empty result if the
symbol is unknown or has no associated range.

By default only `EDGE_TYPE_REFERENCE` edges are returned; `CONTAIN` edges have no
meaningful anchor and are excluded. Pass an explicit `kinds` set to opt in.

Named graph layers can be used instead of raw edge-type sets:

```python
result = graph.query_range("src/auth.py", 10, 42, layer="dependency")
containment = graph.layer("containment")
```

Today `dependency` includes `reference` edges; the same API is ready for import
and type-use edges once decoders emit them separately.

## Typed results

`NodeRef`: `vid`, `name` (identity), `unified_name` (display, may collide), `file`,
`start_line`, `end_line`, `kind`.

`EdgeRef`: `eid`, `source_vid`, `target_vid`, `edge_kind`, and `anchor_file` /
`anchor_line` (the call/reference site).

## Anchor invariants & the multi-edge schema

Reference edges carry an **anchor** — the location of the call/reference site:

- `anchor_file` equals the *source* vertex's file (the anchor lives at the call site,
  never the target).
- `anchor_file`/`anchor_line` are populated only for `REFERENCE` edges; `CONTAIN` edges
  leave both `None`.
- `outgoing` and `incoming` are disjoint: self-edges (recursion) are emitted once, in
  `outgoing`.

Edges are deduplicated on the 5-tuple
`(source_id, target_id, type, anchor_file, anchor_line)`, so the same symbol pair can be
connected by multiple anchored reference edges — one per occurrence.

!!! note "Line-number convention"
    `CodeGraph` line ranges are **0-based** (matching tree-sitter / the graph's internal
    representation). This differs from the **1-based** `CodeLocation` lines used in
    output and HuggingFace datasets.
