<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib systems paper plan

## Thesis

CodeNib is a snapshot-indexed semantic service for software-engineering
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
CodeNib contribution is the agent systems layer joining reusable snapshot
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

Primary comparison: live/dynamic semantic backend versus CodeNib static
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
  --dataset fishmingyu/codenib-base-dataset \
  --dataset sysevol-ai/codeminer-synthesis
```

| workload | records | instances | repositories | snapshots | reuse | records/snapshot |
|---|---:|---:|---:|---:|---:|---:|
| CodeNib base | 100 | 100 | 25 | 100 | 0% | 1.0 |
| synthesis | 500 | 25 | 25 | 25 | 95% | 20.0 |
| combined | 600 | 100 | 25 | 100 | 83.3% | 6.0 |

For the Qwen3-Embedding-0.6B profiles currently under
`${CODENIB_RESULTS_DIR}/profile_log`, the 100 base snapshots required 16,180
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
`${CODENIB_RESULTS_DIR}/lsp_replay_v4`; aggregate them with
`scripts/profiling/aggregate_lsp_replay.py`.

These steady-state request latencies are not cold-index timings. On the same
four already-materialized SCIP payloads, the C++ graph decoder took 0.55-1.56
seconds to materialize graphs, while the earlier full embedding profiles took
minutes per snapshot. The systems result must therefore report the break-even
query count and amortized total cost, not present request latency in isolation.

### Multilanguage replay result

The current controlled replay uses two repositories each for Go, Rust, and
TypeScript/JavaScript: six exact snapshots and 120 independent requests.
Snapshot selection used language and repository size before observing replay
outcomes. The request population is graph-covered and therefore estimates the
indexed fast-path population, not arbitrary LSP traffic. Live providers are
`gopls v0.22.0`, `rust-analyzer
0.3.2777-standalone`, and `typescript-language-server 5.3.0` with TypeScript
6.0.3.

The protocol starts each server without a source-file probe and replays the
fixed request set until live result fingerprints are identical across
consecutive rounds. It also requires at least 10% non-empty live results and at
least two non-empty static/live-equivalent requests before readiness. The latter
condition was added after TypeScript produced a stable but semantically unusable
early state. Each ready server then runs five measured repetitions; failure to
become ready within ten rounds aborts the snapshot.

| language | snapshots | definition equivalent | references equivalent | overall equivalent | equivalent-row speedup p50 |
|---|---:|---:|---:|---:|---:|
| Go | 2 | 20/20 (100%) | 5/20 (25%) | 25/40 (62.5%) | 3.57x |
| Rust | 2 | 19/20 (95%) | 13/20 (65%) | 32/40 (80%) | 3.20x |
| TypeScript/JavaScript | 2 | 16/20 (80%) | 3/20 (15%) | 19/40 (47.5%) | 6.74x |
| overall | 6 | 55/60 (91.7%) | 21/60 (35%) | 76/120 (63.3%) | 3.83x |

Across these snapshots, static request p50 is 0.54 ms and live request p50 is
1.89 ms on equivalent rows. Graph load p50 is 10.81 ms and live process
initialize p50 is 99.05 ms, but live behavioral readiness p50 is 8.34 seconds
(p95 25.75 seconds). Two transient `ContentModified` errors occurred among 380
warmup rows; all 600 measured rows completed without transport errors. The
central systems opportunity is therefore larger than steady-state RPC latency:
compile the snapshot once, load it in milliseconds, and amortize workspace
analysis across all agent queries that share the snapshot.

A one-snapshot-per-language break-even pilot reran the complete SCIP-to-graph
pipeline with warm toolchain/dependency caches. Each rebuilt graph matched its
existing artifact in node count, edge count, and all 20 static request
fingerprints. Using the conservative totals
`static = build + sessions * graph_load` and
`live = sessions * (initialize + behavioral_warmup)`, while excluding static
per-request latency savings, gives:

| language | one-time static build | live setup/session | break-even sessions |
|---|---:|---:|---:|
| Go | 3.39 s | 2.09 s | 2 |
| Rust | 8.17 s | 9.31 s | 1 |
| TypeScript/JavaScript | 27.08 s | 7.51 s | 4 |

These are single-run development measurements, not confidence intervals. The
TypeScript wall time includes failed frozen-lockfile dependency preparation
before successful indexing, while all three runs reuse installed language
toolchains and package caches. Rust SCIP generation uses the repository's
configured nightly rust-analyzer toolchain, independently from the standalone
live LSP binary. The confirmatory build study must separately report clean
dependency cold start, warm toolchain build, and exact snapshot cache hit. The
analysis artifact lives under
`${CODENIB_RESULTS_DIR}/scip_build_pilot`; regenerate it with
`scripts/profiling/analyze_semantic_service_break_even.py`.

Definition is a plausible promoted fast path for Go and Rust under these
profiles. References is not: coverage remains too low, especially for Go and
TypeScript, and must fall back to live LSP. TypeScript also demonstrates that
promotion cannot be global even within a capability; alias resolution and
workspace state require a language/provider/profile-specific gate. Reports and
exact request/graph hashes live under
`${CODENIB_RESULTS_DIR}/lsp_replay_multilang_final`.

### Provider protocol check

A small forced-call crossover check uses Haiku 4.5 on one Go, one Rust, and one
TypeScript snapshot. For two exact-equivalent definition requests per snapshot,
the prompt, tool schema, model, and result DTO are held constant while only the
injected provider changes. Arm order alternates by request and prompt caching is
disabled.

| check | result |
|---|---:|
| paired requests | 6 |
| protocol-valid cells | 12/12 |
| identical tool-result payload pairs | 6/6 |
| identical answer pairs | 6/6 |
| identical turn/token count pairs | 6/6 |
| static tool-duration p50 | 3.55 ms |
| live tool-duration p50 | 4.82 ms |

All cells used two turns and each pair consumed the same tokens. Agent wall time
is intentionally not used as evidence of LSP speedup: remote model calls took
seconds and dominated the millisecond provider difference. This A/B validates
provider wiring and serialization only. It is not an agent ablation because the
prompt supplies the request and the harness forces the tool call. The
five-repetition request replay remains the primary latency result. Reports live
under `${CODENIB_RESULTS_DIR}/lsp_agent_ab_multilang`.

### CodeNib Base agent ablation

The task-level study uses `fishmingyu/codenib-base-dataset` test split at
revision `4eb84e2e8918474969ce68c5b06facf14d6be604` (local dataset fingerprint
`d265af65e9ba4985`). Its sampling frame is every currently supported Go, Rust,
and TypeScript/JavaScript row: 60 tasks, 15 repositories, and 60 exact source
snapshots. It does not admit tasks based on graph coverage, static/live
equivalence, successful LSP adoption, or outcome quality.

The six repositories used for exploratory replay form a repository-disjoint
development partition: Caddy, Gin, Bat, Tokio, Preact, and Vue (25 tasks).
The other nine repositories form the confirmatory partition (35 tasks). All
results may be shown descriptively, but the pre-registered primary comparison
uses only the 35 confirmatory tasks. A final paper should add repositories if
the resulting nine confirmatory clusters give an unacceptably wide interval.

Each task uses `vertex_ai/claude-haiku-4-5` at temperature 0 and runs three
crossover arms for three repetitions, with deterministic within-task arm
randomization and prompt caching disabled:

| arm | filesystem tools | dynamic native LSP tools | provider |
|---|---|---|---|
| `filesystem` | yes | no | none |
| `live_lsp` | yes | definition + references | live JSON-RPC |
| `codenib_lsp` | yes | definition + references | static CodeNib index |

The model chooses whether and when to call LSP. There is no forced tool choice,
request injection, preload, compact mode, graph route tool, or outcome-based
case admission. The two LSP arms have identical system-prompt and tool-schema
hashes. Native definition/reference schemas require `file_path`, `line`, and
`character` in both arms; symbol-name navigation remains a separate CodeNib
extension.

The primary quality endpoint is answer-block recall@5 with a 5-point
non-inferiority margin for `codenib_lsp - live_lsp`. File recall@5, adoption,
turns, tokens, USD, completion, and fallback are secondary. Inference resamples
repositories, then instances, then repetitions. Remote agent wall time and
per-cell LSP duration are descriptive, not the request-latency claim.

Every native LSP call made by the live arm is exported with its resolved
0-based arguments. Those frozen, naturally adopted traces are subsequently
replayed against both providers. Equivalent-request paired latency is the
primary latency endpoint; mismatches, empty results, errors, and fallback remain
in the denominator as compatibility outcomes.

Generate the planning manifest, prepare snapshot-addressed graphs, and then
regenerate the manifest with strict artifact verification:

```bash
HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}" \
codenib-lsp-agent-study-manifest \
  --dataset-revision 4eb84e2e8918474969ce68c5b06facf14d6be604 \
  --output-json ${CODENIB_RESULTS_DIR}/lsp_agent_base_study_manifest_v2.json

export CODENIB_HOME="${CODENIB_HOME:-$HOME/.codenib}"
export CODENIB_TEMP_DIR="${CODENIB_TEMP_DIR:-${TMPDIR:-/tmp}/codenib}"
export CODENIB_SCIP_TOOLS_DIR="${CODENIB_SCIP_TOOLS_DIR:-${CODENIB_TEMP_DIR}/scip-tools}"
export PATH="${CODENIB_SCIP_TOOLS_DIR}/go-tools/bin:\
${CODENIB_SCIP_TOOLS_DIR}/go/bin:\
${CODENIB_SCIP_TOOLS_DIR}/node-tools/node_modules/.bin:\
${CODENIB_SCIP_TOOLS_DIR}:${PATH}"
codenib-lsp-agent-study-artifacts \
  --manifest-json ${CODENIB_RESULTS_DIR}/lsp_agent_base_study_manifest_v2.json \
  --source-root ${CODENIB_PREBUILT_DIR} \
  --output-root ${CODENIB_RESULTS_DIR}/lsp_agent_base_artifacts_v4 \
  --reuse-root ${CODENIB_RESULTS_DIR}/lsp_agent_base_artifacts_v3 \
  --workers 12

HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}" \
codenib-lsp-agent-study-manifest \
  --dataset-revision 4eb84e2e8918474969ce68c5b06facf14d6be604 \
  --prebuilt-root ${CODENIB_RESULTS_DIR}/lsp_agent_base_artifacts_v4 \
  --output-json ${CODENIB_RESULTS_DIR}/lsp_agent_base_study_manifest_v2.json
```

The legacy prebuilt-tree audit found 22/60 instance worktrees at the declared
base commit and 38/60 at a different commit, with no verifiable profile binding.
All 60 source commits were available in the local Git object stores, so the
artifact preparer created detached snapshot worktrees and rebuilt every graph
without re-cloning repositories. A subsequent compatibility audit found that
the symbol graph had discarded exact SCIP character ranges and local symbols,
so natural receiver/local-variable LSP requests failed on the static arm. The
v4 profile reuses the exact v3 graph and SCIP outputs and adds a separately
versioned `lsp_index.pkl`; no language indexer is rerun. The strict schema-v2
manifest reports 60/60 `ready` subjects with manifest SHA
`434fd80a0f688812d29e45fb7ae749e9392deaf4af5d035ce6da94d1f69fdde6`.
Independent loading found 567,234 nodes and 3,215,634 edges across the 60 graphs;
the occurrence artifacts contain 10,287,017 exact positions. Every graph
project root and Git HEAD matches the declared snapshot and no worktree has
tracked changes. Artifact preparation satisfies the launch gate but does not
count as any of the 540 planned model cells.

Run the zero-cost execution preflight before any model cells:

```bash
HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}" \
codenib-lsp-agent-study-run \
  --manifest-json ${CODENIB_RESULTS_DIR}/lsp_agent_base_study_manifest_v2.json \
  --artifact-root ${CODENIB_RESULTS_DIR}/lsp_agent_base_artifacts_v4 \
  --preflight
```

The current preflight passes all 60 subjects across 15 repositories and creates
240 independent live-readiness probes for 540 planned cells while loading all
10,287,017 occurrences. A corrected one-task, one-repetition Haiku development
smoke completed all three arms without errors, with matching live/static
prompt, tool-schema, and model hashes and the same occurrence-index hash in
every cell. All three cells reached answer-block recall@5 of 1; neither LSP arm
adopted its optional native LSP tools. This is a pilot observation, not a
result; adoption remains a pre-registered secondary endpoint and zero-call
cells stay in the denominator.

The first graph-only development attempt was stopped after 81/225 cells. Its
six naturally adopted live requests replayed as static errors because they
targeted local variables or receivers. Those cells are retained as diagnostic
evidence and are not resumed or pooled with schema-v2 outcomes. Replaying the
same frozen requests after adding exact occurrences produced 30/30 equivalent
measured rows: Caddy's paired median speedup was 3.79x and Gin's was 9.73x.
Generated cross-language smoke requests were 70% equivalent for Rust and 50%
for TypeScript; compatibility mismatches remain in the denominator, while only
equivalent rows enter latency estimates.

The schema-v2 Haiku campaign is now complete:

| partition | repositories | planned cells | successful | errors | static/live metric pairs | static/live adoption cells | cost |
|---|---:|---:|---:|---:|---:|---:|---:|
| development | 6 | 225 | 225 | 0 | 75/75 | 1/1 | $63.9891 |
| confirmatory | 9 | 315 | 312 | 3 | 102/105 | 3/2 | $97.8545 |

All 75 development pairs have matching prompt, tool-schema, model, and
occurrence-index identities. Their exploratory static-minus-live answer-block
recall@5 delta is -0.0178 with a repository/instance/repetition bootstrap 95%
interval of [-0.0864, 0.0354], which does not establish 5-point
non-inferiority.

The three confirmatory errors are the three `live_lsp` repetitions of
`facebook__docusaurus-8927`. The TypeScript server produced no stable non-empty
reference result after eight readiness rounds, before any model call. They are
retained in the 315-cell denominator and were not selectively retried. The
pre-registered primary analysis is therefore not analysis-ready. Across the
102 complete metric pairs, the descriptive static-minus-live delta is +0.00294
with a 95% interval of [-0.0914, 0.0859], also failing the non-inferiority gate.
Under failure-inclusive recall bounds, assigning every missing live score its
best or worst value gives a point identified set of [-0.0257, 0.00286] and a
bootstrap interval envelope of [-0.1510, 0.0828]. This is a sensitivity bound,
not an imputed confirmatory estimate.

Dynamic adoption was rare: the 540 cells produced seven adoption cells and
eight LSP calls, including four live-arm calls. All three arms had a median of
20 turns in both partitions, so this campaign does not support an end-to-end
agent latency claim. It instead supplies naturally chosen requests for the
provider-level latency endpoint and identifies tool-selection policy as the
next agent-harness bottleneck.

All four live-arm requests were exported without outcome filtering and replayed
for five measured repetitions against the exact static and live providers.
They cover three snapshots, two languages, two definitions, and two references.
All 4/4 requests and 20/20 measured rows were behaviorally equivalent, with no
provider errors or fallback. Static p50/p95 latency was 0.19/0.58 ms versus
2.22/12.65 ms live; paired speedup p50 was 11.43x. This is direct mechanism
evidence for serving an agent's native LSP call from the snapshot index, but
four adopted requests are too few for a general compatibility claim. The
failure-inclusive analysis and replay artifacts are
`${CODENIB_RESULTS_DIR}/lsp_agent_base_haiku_confirmatory_v2_analysis.json`
and `${CODENIB_RESULTS_DIR}/lsp_agent_base_haiku_v2_replay/aggregate.json`.

Reproduce the failure-aware summary and frozen trace export with
`scripts/analysis/analyze_lsp_agent_study.py`; aggregate the per-snapshot replay
reports with `scripts/profiling/aggregate_lsp_replay.py`.

Run the repository-disjoint development gate before spending confirmatory
budget:

```bash
codenib-lsp-agent-study-run \
  --manifest-json ${CODENIB_RESULTS_DIR}/lsp_agent_base_study_manifest_v2.json \
  --artifact-root ${CODENIB_RESULTS_DIR}/lsp_agent_base_artifacts_v4 \
  --output-root ${CODENIB_RESULTS_DIR}/lsp_agent_base_haiku_development_v2 \
  --role development \
  --vertex-project "${VERTEX_PROJECT}" \
  --vertex-location us-east5
```

The development gate is operational, not outcome-selective. Proceed to the
held-out block only after all 225 cells are recorded, every cell is either
successful or has an explained harness failure, all 75 live/static pairs match
their prompt, tool-schema, and model hashes, live readiness is stable, and the
primary metric is scoreable. LSP adoption rate and the direction of the
development quality delta are reported outcomes, not go/no-go criteria; the
protocol must not be changed after inspecting them.

The runner writes one atomic JSON file per cell, records live readiness
separately, keeps errors in the planned denominator, and resumes without
re-running completed cells. Repository shards are supported, but each concurrent
shard must use its own output root. Alternate models require a separate
`--secondary-model --model ...` run so they cannot change the pinned Haiku
primary block. The local open-model comparison uses `openai/qwen3.5-27b` with
`--disable-thinking`; Qwen results are secondary and never pooled with Haiku.
Its one-task, one-repetition 65,536-token pilot completed all three arms without
errors or LSP adoption; both LSP arms had matching prompt, schema, and model
hashes, and all three arms reached answer-block recall@5 of 1. A 32,768-token
server context was insufficient for the final structured-answer request, so the
secondary protocol pins 65,536 tokens without changing the Haiku block.

## Confirmatory protocol

Freeze the artifact profile, provider policy, metrics, and 5-point
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
| agent backend A/B | CodeNib Base tasks with dynamic LSP adoption | block recall@5 | repository-clustered 5-point non-inferiority |
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

CodeNib should therefore be evaluated as an amortized semantic service for
agents, not as a claim that static indexing, code graphs, or content-addressed
caching are individually new.
