<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeMiner systems paper plan

## Thesis

CodeMiner is a snapshot-indexed semantic service for software-engineering
agents. It compiles an immutable repository snapshot once, exposes the result
through retrieval, graph-navigation, and LSP-shaped interfaces, and reuses the
same artifacts across benchmark instances, queries, models, agent arms, and
repetitions. Context compaction is a consumer-side optimization built on that
service, not the paper's primary contribution.

A defensible claim is:

> Under an agent-visible equivalence guard, a reusable static repository index
> amortizes semantic analysis across agent workloads and serves supported LSP
> requests and context assembly with lower latency than per-session dynamic
> analysis, without degrading a pre-specified task-quality margin.

The system does not claim universal LSP equivalence. Unsaved buffers,
workspace-dependent build state, refactors, and unsupported request classes
must use an explicit live-server fallback.

## System boundary

1. **Snapshot compilation.** `(repository, commit)` identifies immutable source.
   An analysis profile separately identifies languages, schema versions, and
   indexer options. Benchmark instance IDs are aliases, not cache keys.
2. **Shared semantic service.** One profile can contain SCIP/LSP/clangd graph,
   dense and sparse retrieval, and range metadata. Static providers expose
   definition, references, route, retrieval, and graph operations.
3. **Compatibility and freshness.** Requests use stable agent/MCP contracts.
   Static results are promoted only when their agent-visible fingerprints match
   a live reference; unsupported or stale cases fall back explicitly.
4. **Agent runtime.** Multiple queries and trajectories reuse loaded contexts.
   Preload and compact modes control what reaches the model, independently of
   how the semantic evidence was produced.

The source/profile split follows established content-addressed build-cache
principles rather than using an instance ID as artifact identity. Bazel, for
example, separates action metadata from content-addressed outputs. SCIP already
provides a language-neutral static code-intelligence representation. The
CodeMiner contribution is the agent systems layer joining reusable snapshot
artifacts, compatibility-guarded serving, and context lifecycle management.

## Research questions

### RQ1: Reuse and amortization

How much index build time, workspace preparation, and storage does
snapshot/profile reuse remove under real agent workloads?

Report cold build, warm bind, warm load, cache-hit rate, unique snapshots,
artifact bytes, and amortized build seconds per query. Compare:

- legacy per-instance artifact layout;
- snapshot-addressed layout with one profile per exact source snapshot;
- snapshot layout plus incremental update for adjacent commits, as a separate
  optimization when exact final-snapshot parity holds.

### RQ2: Compatible LSP acceleration

For the same file-position request, when can the static provider replace live
JSON-RPC LSP work?

Report equivalence coverage, fallback rate, static and live p50/p95/p99 latency,
language-server startup separately from steady state, throughput, and errors.
Latency comparisons include only equivalent rows; equivalence and fallback are
first-class outcomes, not filtered failures.

### RQ3: End-to-end agent efficiency

With model, task, prompt, tools, and output contract fixed, does the shared
semantic service reduce agent wall time, billed cost, raw tokens, and turns?

Primary comparison: live/dynamic semantic backend versus CodeMiner static
backend. Grep-only, eager preload, and compact context are secondary ablations.
Task quality is a non-inferiority guard, not inferred from a non-significant
difference.

### RQ4: Generality and scaling

How do reuse and serving gains vary with language, repository size, graph size,
query multiplicity, cache warmth, and model size? Use repository-level strata
and report per-language failure causes instead of averaging them away.

## Existing exploratory evidence

The snapshot-reuse profiler produces the following exact counts:

```bash
python scripts/profiling/analyze_snapshot_reuse.py \
  --dataset fishmingyu/codeminer-base-dataset \
  --dataset sysevol-ai/codeminer-synthesis
```

| workload | records | instances | repositories | snapshots | reuse | records/snapshot |
|---|---:|---:|---:|---:|---:|---:|
| CodeMiner base | 100 | 100 | 25 | 100 | 0% | 1.0 |
| synthesis | 500 | 25 | 25 | 25 | 95% | 20.0 |
| combined | 600 | 100 | 25 | 100 | 83.3% | 6.0 |

For the Qwen3-Embedding-0.6B profiles currently under
`/mnt/data/codeminer/profile_log`, the 100 base snapshots required 16,180
observed build seconds (4.49 hours; median 113.9 seconds/snapshot). The 25
snapshots used by synthesis required 4,472 seconds once, or 8.94 amortized
seconds across each of 500 queries. These are measurements of the current
machine and artifacts, not yet a controlled baseline comparison.

Existing agent results are supporting evidence only:

- synthesis establishes compact-versus-grep non-inferiority at a 5-point
  margin under a repository-clustered bootstrap while reducing Haiku USD and
  wall time;
- the 100-instance base set does not have enough repository-level precision to
  establish the same non-inferiority margin;
- all current base and synthesis results use the same 25 repositories and one
  stochastic repetition, so they are development evidence rather than an
  independent confirmatory test;
- current latency runs used fixed arm order, and the synthesis compact arm ran
  separately from its baselines. They cannot be the final controlled latency
  result.

The corrected real-repository LSP replay pilot covers four Python snapshots
(Astropy, Xarray, Matplotlib, and scikit-learn), 80 independent requests, two
warmups, and three measured repetitions. Requests are sampled deterministically
from graph-covered reference anchors, so this is a compatibility and latency
pilot for the indexed population, not an estimate of arbitrary LSP traffic.
The live reference is pinned to `basedpyright@1.39.9`; every report records the
repository commit, graph shape, graph capabilities, and server command.

| capability | equivalent requests | static p50 | live p50 | speedup p50 |
|---|---:|---:|---:|---:|
| definition | 30/40 (75%) | 0.71 ms | 1.26 ms | 1.69x |
| references | 18/40 (45%) | 0.88 ms | 17.67 ms | 17.88x |
| overall | 48/80 (60%) | 0.77 ms | 1.42 ms | 2.46x |

Graph load p50 is 59.8 ms, static provider initialization p50 is 0.01 ms, and
live-server startup p50 is 1,056.8 ms. Two transport timeouts occurred among
160 warmup rows and are recorded separately; all 240 measured rows completed
without transport errors. Persisting the exact SCIP declaration
occurrence separately from its scope improved definition equivalence from 70%
to 75%; it did not change reference equivalence. This is the intended guardrail
behavior: a schema fix receives credit only for the capability it repairs.

The low overall equivalence coverage remains the most important pilot result: a
global static replacement is invalid. The system must promote only
provider/profile/capability slices that meet a predeclared equivalence threshold
and send all other requests to the live fallback. Transport failures are now
reported as errors rather than being collapsed into valid empty LSP responses.
The corrected reports live under
`/mnt/data/codeminer/results/lsp_replay_v4`; aggregate them with
`scripts/profiling/aggregate_lsp_replay.py`.

These steady-state request latencies are not cold-index timings. On the same
four already-materialized SCIP payloads, the C++ graph decoder took 0.55-1.56
seconds to materialize graphs, while the earlier full embedding profiles took
minutes per snapshot. The systems result must therefore report the break-even
query count and amortized total cost, not present request latency in isolation.

### Multilanguage replay pilot

A second development pilot adds two repositories each for Go, Rust, and
TypeScript/JavaScript (six exact snapshots, 120 independent requests). Snapshot
selection used language and repository size before observing replay outcomes.
The request population is still graph-covered and therefore not a traffic-wide
coverage estimate. Live providers are `gopls v0.22.0`, `rust-analyzer
0.3.2777-standalone`, and `typescript-language-server 5.3.0` with TypeScript
6.0.3.

This pilot uses behavioral readiness instead of an arbitrary source-file probe:
start the server without probing, replay the fixed request set until live result
fingerprints are identical for two consecutive rounds, require at least 10% of
live results to be non-empty, then begin three measured repetitions. The run
fails rather than emitting latency if behavior does not stabilize within ten
warmup rounds. This protocol corrected false 0% results observed while
rust-analyzer and tsserver were still loading their workspaces.

| language | snapshots | definition equivalent | references equivalent | overall equivalent | equivalent-row speedup p50 |
|---|---:|---:|---:|---:|---:|
| Go | 2 | 20/20 (100%) | 4/20 (20%) | 24/40 (60%) | 3.71x |
| Rust | 2 | 19/20 (95%) | 13/20 (65%) | 32/40 (80%) | 3.74x |
| TypeScript/JavaScript | 2 | 14/20 (70%) | 2/20 (10%) | 16/40 (40%) | 9.62x |
| overall | 6 | 53/60 (88.3%) | 19/60 (31.7%) | 72/120 (60%) | 4.10x |

Across these snapshots, graph load p50 is 14.6 ms and live process initialize
p50 is 88.4 ms, but live behavioral warmup p50 is 7.58 seconds (p95 26.38
seconds). Two transient `ContentModified` errors occurred among 380 warmup rows;
all 360 measured rows completed without transport errors. The central systems
opportunity is therefore larger than steady-state RPC latency: compile the
snapshot once, load the static service in milliseconds, and amortize dynamic
workspace analysis across all agent queries that share the snapshot.

A one-snapshot-per-language break-even pilot reran the complete SCIP-to-graph
pipeline with warm toolchain/dependency caches. Each rebuilt graph matched its
existing artifact in node count, edge count, and all 20 static request
fingerprints. Using the conservative totals
`static = build + sessions * graph_load` and
`live = sessions * (initialize + behavioral_warmup)`, while excluding static
per-request latency savings, gives:

| language | one-time static build | live setup/session | break-even sessions |
|---|---:|---:|---:|
| Go | 3.39 s | 2.07 s | 2 |
| Rust | 8.17 s | 7.70 s | 2 |
| TypeScript/JavaScript | 27.08 s | 7.60 s | 4 |

These are single-run development measurements, not confidence intervals. The
TypeScript wall time includes failed frozen-lockfile dependency preparation
before successful indexing, while all three runs reuse installed language
toolchains and package caches. Rust SCIP generation uses the repository's
configured nightly rust-analyzer toolchain, independently from the standalone
live LSP binary. The confirmatory build study must separately report clean
dependency cold start, warm toolchain build, and exact snapshot cache hit. The
analysis artifact lives under
`/mnt/data/codeminer/results/scip_build_pilot`; regenerate it with
`scripts/profiling/analyze_semantic_service_break_even.py`.

Definition is a plausible promoted fast path for Go and Rust under these
profiles. References is not: coverage remains too low, especially for Go and
TypeScript, and must fall back to live LSP. TypeScript also demonstrates that
promotion cannot be global even within a capability; alias resolution and
workspace state require a language/provider/profile-specific gate. Reports and
exact request/graph hashes live under
`/mnt/data/codeminer/results/lsp_replay_multilang_pilot`.

## Confirmatory protocol

Freeze the artifact profile, compact policy, metrics, and 5-point
non-inferiority margin before running held-out repositories.

1. Select new repositories from SWE-bench Multilingual without inspecting
   agent outcomes. Keep inclusion rules based on indexability and valid ground
   truth.
2. Use one exact snapshot artifact for every query, arm, model, and repetition.
   Record snapshot/profile IDs and cache-hit state in every result.
3. Interleave or deterministically randomize arm order. Establish live-server
   readiness by stable observable fingerprints, not sleep time or a random-file
   probe. Run microbenchmarks at least five times after declared warmups; run
   stochastic agent cells with at least three repetitions on the confirmatory
   subset.
4. Cluster task statistics by repository. For generated queries, resample
   repositories first and queries within repository second.
5. Report completion and fallback rates. Do not silently remove malformed
   model responses, missing indexes, LSP errors, or unsupported capabilities.
6. Separate raw token volume, cache-read tokens, billed USD, wall time, server
   startup, index build, and index load. None is a substitute for the others.

## Required experiment matrix

| experiment | workload | primary metric | guardrail |
|---|---|---|---|
| snapshot build/reuse | 20-40 held-out multilingual snapshots, repeated consumers | total and amortized build time; bytes | exact snapshot/profile identity |
| LSP replay | real definition/reference traces from those snapshots | equivalent-row p50/p95 latency | fingerprint equivalence and fallback rate |
| agent backend A/B | same model and agent, live versus static semantic provider | wall time and USD | files@5/block@5 non-inferiority |
| context ablation | grep, eager, compact over the static provider | USD, tokens, turns | same non-inferiority margin |
| scale analysis | language, graph size, repository size, queries/snapshot | slope and break-even query count | completion rate |

## Related systems and differentiation

- [SWE-agent](https://arxiv.org/abs/2405.15793) establishes that the
  agent-computer interface materially affects software-agent behavior.
- [OpenHands](https://arxiv.org/abs/2407.16741) provides a general agent
  platform and benchmark integration.
- [Agentless](https://arxiv.org/abs/2407.01489) and
  [AutoCodeRover](https://arxiv.org/abs/2404.05427) emphasize structured
  localization and cost.
- [RepoGraph](https://arxiv.org/abs/2410.14684) and
  [LocAgent](https://aclanthology.org/2025.acl-long.426/) inject repository
  graphs into localization workflows.
- [SCIP](https://github.com/sourcegraph/scip) is the static code-intelligence
  substrate for definition/reference-style navigation.
- [Bazel remote caching](https://bazel.build/remote/caching) is prior art for
  reusable content-addressed build outputs.

CodeMiner should therefore be evaluated as an amortized semantic service for
agents, not as a claim that static indexing, code graphs, or content-addressed
caching are individually new.
