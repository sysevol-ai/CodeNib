# graph/ — rules

`CodeGraph` (`code_graph.py`) is an [igraph](https://igraph.org)-backed semantic
graph: directories/files/symbols as vertices, containment + reference as edges.
`roi_subgraph.py` extracts a region-of-interest subgraph; `traverse_graph.py`
and `dependency.py` walk it; `graph/incremental/` patches the graph in place
from LSP edits.

## Conventions

- **Node / edge types are centralized in [`codeminer/types.py`](../types.py).**
  Vertices: `directory`, `file`, `symbol`, `class`, `function`, `method`,
  `field`. Edges: `contain`, `reference`. Use the `NODE_TYPE_*` / `EDGE_TYPE_*`
  constants and `is_symbol_node()` — never hard-code the string literals.
- **Persisted-graph schema is versioned.** `_SCHEMA_VERSION` in `code_graph.py`
  guards the pickle: `load_graph()` raises if an on-disk `schema_version`
  mismatches, so stale caches fail loudly instead of drifting. **Bump
  `_SCHEMA_VERSION` whenever you change vertex/edge attributes or the top-level
  pickle keys**, and expect cached `graph.pkl` files to be regenerated.
- **C++ decoder parity.** `core/` is a C++ backend (libigraph) mirroring
  `CodeGraph` / `SCIPGraphDecoder`. If you change graph construction or the
  serialized layout, keep the C++ decoder in sync — divergence is a silent
  correctness bug, not a build error.

## Incremental patching (`graph/incremental/`)

- One patcher per language (`patcher_python.py`, `patcher_go.py`,
  `patcher_cpp.py`, `patcher_rust.py`, `patcher_ts.py`), all on
  `patcher_base.py`, driven by `lsp_client.py` + `change_mgr.py`.
- A new language needs a `patcher_<lang>.py` that subclasses the base; don't
  fork the dispatch logic in `graph_patcher.py`.
