<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# LSP-route adoption experiment

Status: historical feedback loop, not the current provider-acceleration gate.

This document records the old agent-policy question: when `lsp_route` is exposed
as a dynamic tool, does the model discover and use it, and what happens when the
same route evidence is preloaded before turn 1?

It is **not** the current proof target for LSP acceleration. The active target is
provider-level: when an agent or MCP client asks for an LSP-shaped operation,
CodeNib should serve the supported request from the static graph index faster
than live JSON-RPC LSP while preserving the agent-visible output contract. That
gate lives in `docs/experiments/lsp_core_acceleration.md`.

Preload is already covered by the compact-context line of work. It is a policy
choice about what context to show before turn 1, not a prerequisite for proving
that dynamic LSP requests can be accelerated by the static provider.

This is not a claim that CodeNib's agent is smarter than Claude Code, Codex,
or opencode.

## Internal Arms

Run the small feedback slice first:

```bash
python scripts/agent_compile/run_sweep.py \
    --config scripts/agent_compile/configs/lsp_route_adoption.yaml \
    --output-dir results/agent_compile/lsp_route_adoption_dynamic_hint_sanity \
    --instances astral-sh__ruff-15309 caddyserver__caddy-5761 \
    --no-resume

python scripts/agent_compile/aggregate.py \
    --cells-dir results/agent_compile/lsp_route_adoption_dynamic_hint_sanity/cells \
    --output-dir results/agent_compile/lsp_route_adoption_dynamic_hint_sanity \
    --baseline grep_only

python scripts/agent_compile/lsp_latency_replay.py \
    --cells-dir results/agent_compile/lsp_route_adoption_dynamic_hint_sanity/cells \
    --output-dir results/agent_compile/lsp_route_adoption_dynamic_hint_sanity \
    --prebuilt-dir /mnt/data/codeminer
```

The three arms are:

| arm | meaning |
| --- | --- |
| `grep_only` | read/grep/glob/bash only. |
| `lsp_route_skill` | expose the same `lsp_route` backend as a dynamic tool. |
| `lsp_route_preload` | run the same `lsp_route` backend before turn 1 and inject route hints. |

## Current Evidence

Before the dynamic tool guidance, Haiku ignored the dynamic `lsp_route` skill on
the five-case slice: tool invocation was 0%. Query fallback and seed hygiene
made preload coverage reliable, but did not prove a broad cost win.

After adding dynamic `lsp_route` guidance, the two-case sanity result was:

| arm | lsp tool invoke | startup context | turns | tokens | files@5 |
| --- | --- | --- | --- | --- | --- |
| `grep_only` | 0% | 0% | 11.0 | 76,486 | 1.000 |
| `lsp_route_skill` | 100% | 0% | 11.0 | 95,436 | 1.000 |
| `lsp_route_preload` | 0% | 100% | 12.5 | 105,208 | 1.000 |

Per-instance quality/cost signal is mixed:

| instance | dynamic delta tokens | preload delta tokens | note |
| --- | ---: | ---: | --- |
| `astral-sh__ruff-15309` | +44,168 | +22,523 | route hints did not reduce exploration. |
| `caddyserver__caddy-5761` | -6,268 | +34,923 | dynamic route helped, preload over-steered. |

Those quality/cost numbers are useful guardrails for route-context policy, but
they are not the provider acceleration proof. The old same-backend latency
replay compared the same `lsp_route` backend through two exposure paths:

1. `dynamic`: the model spends a turn deciding to call `lsp_route`, waits for
   the tool result, then spends a later turn using it.
2. `preload`: CodeNib builds the same route context before turn 1, so the
   first model response already sees it.

The backend `duration_ms` for both paths is recorded. The remaining latency
advantage comes from avoiding an extra model-tool-model round trip.

The strict same-backend latency proof uses paired replay: for every dynamic
`lsp_route` tool call, replay the exact canonical `route_args` directly against
the same prebuilt graph backend and compare the replay backend duration with the
dynamic path's `model_can_use_ms`. The observed preload arm remains useful as a
policy guardrail, but it may choose different seeds/query text than the dynamic
model-generated call.

### Trace-schema v4 replay check

After adding route args, route fingerprints, and relative event timestamps, the
two-case run under `results/agent_compile/lsp_route_latency_trace_probe` shows
the strict same-backend path claim is measurable:

| instance | same route replay | dynamic backend ms | replay backend ms | dynamic visible ms | saved ms | extra trips |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `astral-sh__ruff-15309` | yes | 2477.5 | 1779.7 | 5476.0 | 3696.3 | 1 |
| `caddyserver__caddy-5761` | yes | 107.2 | 54.7 | 2567.0 | 2512.3 | 1 |

This supports only a narrow exposure-path observation: when the exact dynamic
`route_args` are replayed directly against the same prebuilt graph backend, the
route evidence is available without waiting for the model-tool-model round
trip.

It does **not** prove that the current observed preload policy chooses the same
route evidence. In this run, observed preload used different query text/seeds
than the model-generated dynamic calls, so preload-vs-dynamic quality/cost is a
separate policy problem.

### Synthesized benchmark smoke

The current agent-level feedback path should run on
`sysevol-ai/codeminer-synthesis`, because it gives multiple queries per repo and
category labels that separate symbol-heavy tasks from traversal/behavioral
tasks. Use `lsp_route_synthesis_smoke.yaml` for a small Haiku feedback slice:

```bash
PYTHONPATH=$PWD python scripts/agent_compile/run_synthesis_sweep.py \
  --config scripts/agent_compile/configs/lsp_route_synthesis_smoke.yaml \
  --output-dir results/agent_compile/lsp_route_synthesis_smoke \
  --synthesis-configs Go --categories traversal \
  --max-instances 1 --max-queries 1 --reps 1

python scripts/agent_compile/aggregate_synthesis.py \
  --cells-dir results/agent_compile/lsp_route_synthesis_smoke/cells \
  --output-dir results/agent_compile/lsp_route_synthesis_smoke
```

The first smoke on `caddyserver__caddy-6115` is intentionally tiny
(`n=1/query/category`), so it is a route-policy diagnostic, not an accuracy
claim:

| category | arm | answer_rec@5 | turns | cost$ | dynamic call | startup context | note |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| traversal | `grep_only` | 0.667 | 13 | 0.1813 | 0% | 0% | baseline explored with bash/read. |
| traversal | `lsp_route_skill` | 0.667 | 12 | 0.1684 | 0% | 0% | dynamic tool was offered but not adopted. |
| traversal | `lsp_route_preload` | 0.667 | 15 | 0.2609 | 0% | 100% | query fallback route was available, but added cost. |
| symbol_hint | `grep_only` | 0.000 | 3 | 0.0216 | 0% | 0% | grep found the helper, not the GT bridge method. |
| symbol_hint | `lsp_route_skill` | 0.000 | 8 | 0.0714 | 100% | 0% | dynamic route was adopted; visible latency included one extra model round trip. |
| symbol_hint | `lsp_route_preload` | 0.000 | 4 | 0.0309 | 0% | 100% | seeded route was available before turn 1, but did not change final target choice. |

Mechanism read:

- Dynamic adoption is category-sensitive, not globally zero. The same model
  ignored `lsp_route` on the traversal query and called it on the symbol-hint
  query.
- The static backend itself is fast in both exposure modes: preload route
  construction was 47-184 ms, and the dynamic symbol-hint backend call was 21
  ms. The dynamic route evidence became model-visible after about 2.3 s because
  it paid a model-tool-model round trip.
- Route evidence is not yet task-shaped enough. On the symbol-hint query,
  `lsp_route` returned both `indexFold` and `MatchFile.selectFile()`, but the
  agent committed to the helper while the synthesis GT targeted the bridge
  method, so answer recall stayed zero.

The aggregator now includes an `LSP route exposure` section with
`dynamic_call`, `startup_context`, `backend_ms`, `visible_ms`, and
`extra_trips`. Those columns are the minimum diagnostics for scaling this to
larger synthesized slices.

## Historical Same-backend Latency Protocol

Do not use this protocol to decide whether static LSP provider acceleration is
ready. Use `docs/experiments/lsp_core_acceleration.md` for the active
static-vs-live JSON-RPC provider gate.

Compare access paths, not agent brands.

### Arms

| arm | LSP backend | route exposure |
| --- | --- | --- |
| `codeminer_dynamic_lsp` | CodeNib `lsp_route` over the prebuilt `symbol_graph` | tool call inside the agent loop |
| `codeminer_preload_lsp` | same CodeNib `lsp_route` over the same prebuilt `symbol_graph` | startup context before turn 1 |
| `external_dynamic_same_lsp` | same CodeNib `lsp_route` exposed through a thin external-agent tool wrapper | tool call inside Claude Code/Codex/opencode loop |

If an external agent has native LSP, run it separately as
`external_native_lsp`. Do not mix native LSP with the same-backend comparison.

### Task Prompt

Every dynamic arm receives the same user task from the dataset and this output
contract. The preload arm receives the same task plus the rendered CodeNib
route context before turn 1.

```text
Files: path/one.ext, path/two.ext
Symbols: path/one.ext:symbol_name, ...
Locations: path/one.ext:START-END, path/two.ext:START-END
```

No prompt may include ground-truth files, symbols, patches, or dataset-specific
scorer hints.

### Latency Capture

Use the same feedback slice before any larger run:

```text
astral-sh__ruff-15309
caddyserver__caddy-5761
```

Then expand to the five-case `lsp_route_adoption.yaml` slice only if the
two-case run is legible.

Record, per cell:

| field | requirement |
| --- | --- |
| `lsp_backend_duration_ms` | measured inside the same `lsp_route` implementation. Dynamic path uses `trace_summary.lsp_route_tool_calls[*].duration_ms`; preload path uses `trace_summary.lsp_route_context.duration_ms`. |
| `route_args` | canonical `symbols/query/top_k/include_neighbors` used for the backend call. Dynamic path uses resolved execution args, not only the model's raw JSON. |
| `route_fingerprint` | stable hash of the ordered compact route nodes. Dynamic and preload rows are comparable only when the hash and args match or the difference is explicitly explained. |
| `lsp_visible_turn` | dynamic path: `trace_summary.lsp_route_tool_calls[*].model_can_use_turn`; preload path: `trace_summary.lsp_route_context.visible_turn = 0`. |
| `route_visible_ms` | dynamic path: `trace_summary.lsp_route_tool_calls[*].model_can_use_ms`; preload path: `trace_summary.lsp_route_context.route_visible_ms`. |
| `extra_model_round_trips` | dynamic path: `trace_summary.lsp_route_tool_calls[*].extra_model_round_trips`; preload path: 0. |
| `route_result_count` | number of route nodes returned. |
| `wall_time_ms` | full cell runtime, secondary because model variance dominates. |
| `files@5` | guardrail only; latency wins do not matter if localization collapses. |

### Latency Formula

The decision metric is route evidence latency, not final answer quality:

```text
dynamic_route_visible_ms =
    time_to_first_model_tool_request
  + lsp_backend_duration_ms
  + time_to_next_model_turn_that_can_use_the_result

preload_route_visible_ms =
    preload_lsp_backend_duration_ms
```

For internal CodeNib runs, trace schema v4 records relative event timestamps.
`trace_summary` therefore reports dynamic `model_can_use_turn` /
`model_can_use_ms` by finding the next `llm_call` after the completed
`lsp_route` tool result. The preload path reports visible turn 0 and uses the
startup route context backend duration as the route-visible time.

### Historical Policy Gate

Treat this adoption/preload policy claim as useful on a slice only if all hold:

1. Dynamic and preload arms use the same `lsp_route` implementation and the
   same prebuilt `symbol_graph`.
2. Route result count and route args are comparable per instance.
3. Preload has `lsp_visible_turn = 0`; dynamic has `lsp_visible_turn >= 1`.
4. Preload backend duration is not materially slower than dynamic backend
   duration for the same route args.
5. `files@5` does not regress enough to invalidate the latency comparison.

Even if this claim holds, it does not promote static LSP provider acceleration.
It only says that a chosen piece of route context can be made visible earlier.
Provider acceleration is promoted separately by agent-visible fingerprint
equivalence and static-vs-live latency.
