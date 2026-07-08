<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# LSP Core Acceleration Gate

## Dynamic Provider Acceleration

The agent-facing LSP contract should stay stable while the backend can switch
from live JSON-RPC to CodeMiner's static graph index when the static index can
serve the same request shape.

Current implementation:

- `codeminer.agent.lsp_provider.StaticLSPProvider` serves
  `textDocument/definition`, `textDocument/references`, and
  `codeminer/lspRoute` over the loaded `symbol_graph`.
- Agent `lsp_definition`, `lsp_references`, and `lsp_route` skills use that
  provider, so dynamic tool calls get the same list-shaped results plus
  trace-only provider metadata.
- MCP `lsp_*` tools use the same provider and keep their serialized output
  format unchanged.
- Runtime traces record `lsp_provider`, `lsp_result_fingerprint`, and a compact
  result preview. Route traces also preserve the existing `route_args` and
  `route_fingerprint` fields.
- `codeminer.eval.agent_runner.LiveLSPReferenceProvider` wraps the existing
  JSON-RPC `LSPClient` as a reference provider for validation. It normalizes LSP
  `Location` / `LocationLink` results into compact `QueriedNode` rows so the
  static and live paths can be compared by fingerprint.

This is the system-level acceleration path: if an agent asks for an LSP-like
operation, CodeMiner can satisfy supported requests from the static index
without starting or round-tripping through a language server. It is separate
from startup preload and compact-context experiments.

The behavior guardrail is **agent-visible equivalence**, not byte-for-byte
native LSP parity. For each supported capability, the static provider must
return the same ordered set of locations that the agent/MCP tool contract will
show to the model or client. Compare the same graph-facing file-position
request against live JSON-RPC LSP, and only treat the static path as a drop-in
acceleration when both providers return the same ordered fingerprint.

Full native LSP behavior is intentionally a larger contract than this gate:
language servers can return token selection ranges instead of symbol scopes,
choose import aliases or re-export sites, account for unsaved buffers, and
depend on workspace/build configuration. CodeMiner should not claim universal
JSON-RPC equivalence from a static snapshot. It should claim fast-path
equivalence only for the request classes whose fingerprints match under the
agent-visible output contract, and fall back explicitly otherwise.

For `definition` and `references`, the first gate uses start-location
fingerprints (`file:start_line`). Live LSP often returns a token selection range,
while the static graph may return an enclosing symbol range and richer symbol
name. Full range/symbol equality is a stricter later gate, not the initial
dynamic-routing requirement.

Validation entry point:

```python
from codeminer.eval.agent_runner import (
    LSPProviderRequest,
    compare_static_to_live_lsp_provider,
)

rows = compare_static_to_live_lsp_provider(
    [
        LSPProviderRequest(
            capability="textDocument/definition",
            arguments={"file_path": "caller.py", "line": 41, "character": 12},
            request_id="demo-definition",
        )
    ],
    graph=symbol_graph,
    project_root="/path/to/repo",
    language="python",
)
```

The request arguments are graph-facing: `line` is 0-based, matching the
executors after the agent boundary conversion. Symbol-only static lookups are
not valid live-LSP comparison requests because JSON-RPC definition/references
operate on file positions.

The live path requires an installed language server or an override such as
`CODEMINER_PYTHON_LSP_CMD`. If no server command is available, use the fake
client unit path only; do not claim live equivalence from it.

Latest local smoke, using a two-file temporary Python repo and
`npx --yes --package pyright pyright-langserver --stdio`:

| request | same start location | static ms | live JSON-RPC ms | saved ms | verdict |
| --- | --- | ---: | ---: | ---: | --- |
| `textDocument/definition` from `caller.py:3` to `callee.py:1` | yes | 0.24 | 124.21 | 123.96 | `equivalent_static_faster` |

Each row reports static/reference provider status, result count, fingerprint,
latency, `latency_saved_ms`, `speedup_ratio`, and one of:

- `equivalent_static_faster`
- `equivalent_static_not_faster`
- `mismatch`
- `fallback_required`
- `static_error`
- `reference_error`

Promotion rule for dynamic LSP acceleration:

- static provider status is `ok`;
- reference provider status is `ok`;
- start-location fingerprints match for the request class being accelerated;
- static latency is lower on the same request shape;
- fallback reason is explicit when the graph or capability is unavailable.

Only after that gate should the harness route that request class to the static
provider by default.

This promotion is independent of whether preloaded route context improves the
final agent trajectory. Preload/compact experiments answer a policy question:
what evidence should be shown before turn 1? This gate answers the provider
question: when the agent or MCP client asks for LSP-shaped evidence, can the
static graph serve the same agent-visible behavior faster than JSON-RPC?

## Cold-start Graph Acceleration

Generic LSP graph support has two separable costs:

- language-server work: `documentSymbol` and optional `references` JSON-RPC calls;
- local decode work: converting the saved LSP payload into `CodeGraph` vertices,
  containment edges, range indexes, and optional reference edges.

Do not add a C++ LSP decoder until local decode is a measured bottleneck. The
current `core/` backend accelerates SCIP text decoding; it does not reduce
language-server latency.

Current C++ acceleration for layered graph work is intentionally narrower:
`codeminer_core.classify_edge_layers(...)` classifies default graph layers for
the Python `MultiGraphIndex` when the pybind extension is present. This is a
query/index helper, not a generic LSP graph decoder. The implementation lives
in `core/graph_layers.{h,cpp}` so it is a real core API rather than binding
glue embedded in `bindings/pybind_module.cpp`.

Profile the shared graph-layer helper separately:

```bash
PYTHONPATH=build/core:$PYTHONPATH \
python scripts/profiling/profile_graph_layers.py --edges 1000000 --reps 3
```

Latest graph-layer sample:

```json
{
  "core_seconds_min": 0.1028488609008491,
  "edges": 1000000,
  "parity": true,
  "python_seconds_min": 0.3491414119489491,
  "speedup_vs_python": 3.3947037321641997
}
```

Use the synthetic decoder profiler to isolate local decode cost:

```bash
python scripts/profiling/profile_lsp_graph_decode.py \
  --files 1000 \
  --methods-per-file 20 \
  --include-references
```

Latest local sample:

```json
{
  "decode_seconds": 1.0546489779371768,
  "edges": 23004,
  "files": 1000,
  "vertices": 22005
}
```

That keeps the next acceleration target on LSP server lifecycle, batching, and
reference-query policy unless real repos show local decode/build time crossing
the promotion threshold below.

Promotion rule for a C++ LSP decoder:

- backend alignment is green for the target language;
- local decode/build time is at least 20% of end-to-end cold-start graph time;
- the C++ path has parity tests against the Python generic decoder for symbols,
  containment edges, reference anchors, and range indexes.

Until those conditions hold, optimize LSP server lifecycle, batching, and
reference-query policy first.
