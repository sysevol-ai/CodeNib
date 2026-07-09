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

## Confirmatory protocol

Freeze the artifact profile, compact policy, metrics, and 5-point
non-inferiority margin before running held-out repositories.

1. Select new repositories from SWE-bench Multilingual without inspecting
   agent outcomes. Keep inclusion rules based on indexability and valid ground
   truth.
2. Use one exact snapshot artifact for every query, arm, model, and repetition.
   Record snapshot/profile IDs and cache-hit state in every result.
3. Interleave or deterministically randomize arm order. Run microbenchmarks at
   least five times after declared warmups; run stochastic agent cells with at
   least three repetitions on the confirmatory subset.
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
