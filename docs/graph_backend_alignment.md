<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Graph Backend Alignment

CodeMiner can build `CodeGraph` from different backends: SCIP, clangd, future
generic LSP drivers, or language-specific resolvers. Every backend must produce
the same graph schema even when it legitimately discovers a different number of
references.

`codeminer.graph.signature` defines the comparison contract used by serial/core
parity tests and future cross-backend alignment harnesses:

| Surface | Compared Data |
|---------|---------------|
| Vertex names | Full identity `name` set |
| Vertex attrs | `type`, `file`, `start_line`, `end_line`, `unified_name` |
| Edges | Multiset of `(src, tgt, type, anchor_file, anchor_line)` |

The edge comparison is a multiset so multiple references between the same
symbols remain visible. `anchor_file` and `anchor_line` are part of the edge
identity because range queries and incremental patching depend on call-site
locations.

## Strict Parity

Serial/core decoder parity uses:

```python
from codeminer.graph.signature import assert_graph_signatures_equal

assert_graph_signatures_equal(serial_graph, core_graph, tag="python")
```

This is bit-for-bit at the graph-contract level. Any vertex, edge, multiplicity,
or compared attribute difference is a regression.

## Backend Alignment

LSP-vs-SCIP or resolver-vs-SCIP comparisons may allow explicit reference-count
differences while keeping schema drift visible:

```python
from codeminer.graph.signature import (
    GraphAlignmentTolerance,
    assert_graph_alignment_within_tolerance,
)

assert_graph_alignment_within_tolerance(
    scip_graph,
    lsp_graph,
    GraphAlignmentTolerance(max_extra_edges=25, max_edge_count_deltas=25),
    tag="java-lsp-vs-scip",
)
```

Do not use broad tolerances to hide schema problems. Name-set and vertex-attr
tolerances should remain zero unless the harness documents why that backend is
allowed to differ.
