---
title: Benchmarks
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Benchmarks

<div class="codenib-section-lede" markdown>

CodeNib serves repository context to coding agents, so it is evaluated on three
questions of increasing scope: does a method return the right code, does an
agent read the right code while it works, and does the task then get solved?
This section separates those layers, states how far each method and benchmark
has been checked, and links every published number to its page.

</div>

## Three layers

A layer is a property of the measurement, not of the method. An agent can be
scored at L1 on its final answer and at L2 on its whole trajectory. Each layer
is a separate claim: a strong localization score does not show that an agent
read efficiently, and neither shows that the task was solved.

| Layer | What is scored | Claim it supports | Benchmarks |
| --- | --- | --- | --- |
| **L1 Localization** | The method's final ranked files, symbols, code blocks, or line regions, against locations derived from the golden patch or released by the benchmark | The method finds the code the task depends on | CodeNib Base, CodeNib Synthesis, SWE-bench Lite, Loc-Bench v1, SWE-Explore, Agent Retrieval Bench |
| **L2 Agent context** | Every source span the agent viewed during its trajectory, and the context it finally kept, against annotated gold context | The agent reads the right code, and how much else it reads | ContextBench |
| **L3 Resolution** | Whether the patch produced for the task passes the task's tests | The task was solved | ContextBench Pass@1, DeepSWE; see [L3 Resolution](#l3-resolution) |

Every method receives the task text and the task's source snapshot: a clean
checkout at the base commit, or, for Agent Retrieval Bench, the released
corpus chunks. Ground truth reaches the scorer only after predictions are
written, and every published result states how failed cases are counted.
[Harnesses and metrics](harnesses.md) defines every metric and lists the
command that produces it.

## Evidence levels

Each method and benchmark is labelled with the highest level it has reached.
Every level is a separate claim. A pinned contract, for example, says nothing
about quality.

| Level | Means | Does not mean |
| --- | --- | --- |
| **Tested** | CodeNib's test suite covers the method or loader | That a benchmark run exists |
| **Runnable** | A harness in this repository runs the method on the benchmark and scores it | That a result is published |
| **Pinned** | Checked against a fixed upstream revision: vendored prompts, tool signatures, or exact agreement with the official scorer | Task quality, or compatibility with another upstream revision |
| **Measured** | A complete result on a fixed, published case set, presented as a comparison of methods, with failures counted | That the result holds for other repositories, models, or languages, or at another layer |

A Measured cell is always read with its benchmark's layer: Measured at L1 or L2
is not a resolution result. The same levels appear in the
[context methods matrix](../concepts/methods.md#support-matrix).

## Benchmarks

Each benchmark name links to its card: origin, ground truth, granularity,
size, pinned revisions, scorer, and setup.
[Datasets](datasets.md) also covers SWE-bench Verified, SWE-bench
Multilingual, SWE-bench Pro, and local JSON inputs, which are used for
collection, joins, or custom runs rather than as scored benchmarks.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:benchmarks -->
| Benchmark | Layer | Task | Languages | Entry point | Evidence |
| --- | --- | --- | --- | --- | --- |
| [CodeNib Base](datasets.md#codenib-base) | L1 Localization | Real issues; find the code the golden patch changed | C/C++, Go, Python, Rust, TypeScript/JavaScript | `CodeNibBaseDataset` | [Measured](grep_jev.md) |
| [CodeNib Synthesis](datasets.md#codenib-synthesis) | L1 Localization | Synthetic behavioral, hint, reasoning, and traversal queries | C/C++, Go, Python, Rust, TypeScript/JavaScript | `CodeNibSynthesisDataset` | Runnable |
| [SWE-bench Lite](datasets.md#swe-bench-lite) | L1 Localization | Real issues; find the code the golden patch changed | Python | `SwebenchDataset` | [Runnable](../agent_integrations.md#fixed-swe-bench-lite-coverage); dataset revision and a 50-case selection are pinned for paired policy runs |
| [Loc-Bench v1](datasets.md#loc-bench-v1) | L1 Localization | Real issues, including features, performance, and security; find the code the golden patch changed | Python | `LocbenchDataset` | Runnable |
| [SWE-Explore](swe_explore.md) | L1 Localization | Rank the source regions an issue needs | Seven languages in the fixed 20-case set | `codenib-swe-explore-benchmark` | [Pinned](swe_explore.md); scorer agrees with the official evaluator |
| [Agent Retrieval Bench](agent_retrieval_bench.md) | L1 Localization | Four static tasks; code→test, comment→context, trace→code, edit→ripple | Mixed; multi-language repositories | `codenib-arb` | [Measured](agent_retrieval_bench.md#results); six methods on all 345 samples |
| [ContextBench](contextbench.md) | L2 Agent context | Real issues; an agent explores, and everything it reads is scored | C, C++, Go, Java, JavaScript, Python, Rust, TypeScript | `codenib-contextbench` | [Measured](contextbench-results.md); 500 canonical tasks with a research route; leaderboard review pending |
| [ContextBench Pass@1](#l3-resolution) | L3 Resolution | The same tasks; a solver writes a patch, and native tests judge it | C, C++, Go, Java, JavaScript, Python, Rust, TypeScript | `scripts/benchmark_context_solver.py`, `scripts/contextbench_verified.py` | [Measured](contextbench-results.md); 388/500 resolved with a research route; leaderboard review pending |
| [DeepSWE](#l3-resolution) | L3 Resolution | Solve the task in a container; an external harness runs the solver | Set by the external harness | `codenib.eval.benchmarks.deepswe`, `python -m scripts.guardian.deepswe.results` | Tested; artifact analysis only; no published result |
<!-- END CODENIB_SUPPORT_MATRIX:benchmarks -->

## Method × benchmark matrix

Rows follow the three paradigms in
[context methods](../concepts/methods.md): one-shot retrieval (A), fixed LLM
workflows (B), and agentic tool loops (C). A cell shows the highest level
reached for that pair; "—" means no harness in this repository accepts that
pair today. Research scripts count as harnesses, and the cell notes name them.
Numbered notes follow each table.

### Localization (L1)

#### CodeNib methods

<!-- BEGIN CODENIB_SUPPORT_MATRIX:grid:L1:CodeNib -->
| Method | Base | Synthesis | SWE-bench Lite | Loc-Bench | SWE-Explore | ARB |
| --- | --- | --- | --- | --- | --- | --- |
| A · BM25 | [Runnable](grep_jev.md#what-was-measured)<sup>1</sup> | Runnable<sup>2</sup> | Runnable | Runnable | [Runnable](swe_explore.md#localization-summary)<sup>3</sup> | [Measured](agent_retrieval_bench.md#results)<sup>4</sup> |
| A · Dense | [Runnable](grep_jev.md#what-was-measured)<sup>5</sup> | Runnable | Runnable | Runnable | Runnable | [Measured](agent_retrieval_bench.md#results)<sup>6</sup> |
| A · Hybrid (RRF) | [Runnable](grep_jev.md#what-was-measured)<sup>7</sup> | — | Runnable | Runnable | Runnable | [Measured](agent_retrieval_bench.md#results)<sup>8</sup> |
| A · Graph expansion | Runnable | — | Runnable | Runnable | Runnable | [Measured](agent_retrieval_bench.md#results)<sup>9</sup> |
| A · Retrieve → rerank | [Measured](grep_jev.md#what-was-measured)<sup>10</sup> | — | Runnable | Runnable | Runnable | [Runnable](agent_retrieval_bench.md#retrieval-methods)<sup>11</sup> |
| A · Planner `auto` | Runnable | — | Runnable | Runnable | Runnable | — |
| B · Model-planned grep → Jev | [Measured](grep_jev.md) | — | — | — | — | — |
| C · CodeNib agent | Runnable | Runnable | Runnable | Runnable | — | — |

1. **BM25 × Base.** Scored only as the first stage of BM25 → Jev.
2. **BM25 × Synthesis.** BM25 and Dense run through `examples/eval_synthesized_queries.py` on rows exported to JSON, scored on file and symbol names only.
3. **BM25 × SWE-Explore.** Control scores on the 20 validation cases; not a quality claim.
4. **BM25 × ARB.** All 345 samples; trails ARB's own BM25 because CodeNib's tokenizer does not split camelCase identifiers.
5. **Dense × Base.** Scored only as the first stage of dense → Qwen3-Reranker-4B.
6. **Dense × ARB.** All 345 samples; Qwen3-Embedding-0.6B with no instruction.
7. **Hybrid (RRF) × Base.** Scored only as the first stage of hybrid → Jev and hybrid → Qwen3-Reranker-4B.
8. **Hybrid (RRF) × ARB.** All 345 samples; file-level RRF of the BM25 and dense rankings.
9. **Graph expansion × ARB.** All 345 samples, full-source condition; a graph for 190 of 218 snapshots; lower Recall@20 than BM25 on the samples with a graph.
10. **Retrieve → rerank × Base.** Measured through the frozen-candidate research scripts `benchmark_jev_base.py` and `benchmark_jev_dense.py`, not through `RetrieveRerankPipeline`.
11. **Retrieve → rerank × ARB.** `--method rerank` over a completed released-corpus run.
<!-- END CODENIB_SUPPORT_MATRIX:grid:L1:CodeNib -->

The embedding-model and reranker sweeps on CodeNib Base have complete
retained artifacts. They are listed as evidence for the
[validated models](../rag_ops.md#validated-models), but their scores are not
published on this site, so those cells stay at Runnable.

#### Reproduced agents and controls

These rows run published localization agents, and agents with no CodeNib tool
at all, through the same harness, labels, and scorer as CodeNib's methods.
[Reproduced agents](../agent_integrations.md) explains what each agent
reproduces, how closely it follows upstream, and whether its own backend can be
swapped for CodeNib's.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:grid:L1:Reproduced+Control -->
| Method | Base | SWE-bench Lite | Loc-Bench |
| --- | --- | --- | --- |
| B · Agentless v1.5.0 | Runnable<sup>1</sup> | Runnable | Runnable |
| C · LocAgent | Runnable | [Pinned](../agent_integrations.md#locagent-on-swe-bench-lite)<sup>2</sup> | Runnable |
| C · CoSIL | Runnable | Runnable | Runnable |
| C · OrcaLoca SearchAgent | Runnable | [Pinned](../agent_integrations.md#paired-provider-compatibility) | Runnable |
| C · Claude / Codex without CodeNib | Runnable | Runnable | Runnable |

1. **Agentless v1.5.0 × Base.** Upstream's Python-only file filter limits it to the Python issues.
2. **LocAgent × SWE-bench Lite.** One complete run on a fixed 50-case selection, with the CodeNib backend only and no native comparison arm.
<!-- END CODENIB_SUPPORT_MATRIX:grid:L1:Reproduced+Control -->

Runnable means the harness accepts the pair, not that the agent was designed
for it. The reproduced agents follow upstream policies built for Python
repositories.

### Agent context and resolution (L2, L3)

Only methods with at least one L2 or L3 harness appear here.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:grid:L2+L3 -->
| Method | ContextBench (L2) | ContextBench Pass@1 (L3) |
| --- | --- | --- |
| A · BM25 | Runnable<sup>1</sup> | — |
| A · Dense | Runnable | — |
| A · Graph expansion | Runnable | — |
| C · CodeNib agent | [Measured](contextbench-results.md#historical-post-run-24-task-results)<sup>2</sup> | — |
| C · Claude Agent SDK with CodeNib source tools | [Measured](contextbench-results.md)<sup>3</sup> | [Measured](contextbench-results.md)<sup>4</sup> |

1. **BM25 × ContextBench.** Through the research harness `scripts/benchmark_graph_decisions.py`, which also runs dense and graph-expansion arms. Its published 100-task arms rerank with a different decision model, not Jev.
2. **CodeNib agent × ContextBench.** Historical 24-task development subset, with source tools only (`read`, `grep`, `glob`) and no index view. `codenib-contextbench` runs the same `AgentRunner` with its own prompt, budgets, and model, plus the `bm25_search` skill, and does not reproduce these numbers.
3. **Claude Agent SDK with CodeNib source tools × ContextBench.** All 500 canonical tasks, for a native Opus control and for the complementary Haiku search-pair research arm. The population was already exposed during development.
4. **Claude Agent SDK with CodeNib source tools × ContextBench Pass@1.** 388/500 resolved when the search-pair arm's context feeds an Opus mini SWE-agent repair loop. A leaderboard submission awaits maintainer review.
<!-- END CODENIB_SUPPORT_MATRIX:grid:L2+L3 -->

### Research arms

Experiments often try configurations that are not methods anyone should
choose: a different search budget, a post-processing stage, a router between
two workers. Those **research arms** are listed on their benchmark's results
page, each with its components and outcome, and do not appear in this matrix.
An arm moves into the matrix only when it becomes a supported CodeNib surface.
On [ContextBench](contextbench-results.md), for example, the complementary
Haiku search pair, the worker router, evidence review, and function completion
are all arms; the matrix credits their results to the method they run on and
says so in the cell note.

## L3 Resolution

Resolution is measured by running a solver on the task and grading its patch
with the task's own tests. Neither path below is part of the installed package.

### ContextBench Pass@1

ContextBench's leaderboard requires Pass@1 next to its retrieval metrics. The
`codenib-contextbench` runner measures context retrieval only, so its results
stay at L2. Resolution runs use research scripts: `benchmark_context_solver.py`
runs a pinned mini SWE-agent on the retrieved context, and a separate grader
per source (`contextbench_verified.py`, `contextbench_pro.py`,
`contextbench_polybench.py`, `contextbench_multiswe.py`) tests the patch in the
task's native environment without any model call. Reference patches and tests
enter only the grader.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:contextbench_pass1 -->
| Benchmark card | |
| --- | --- |
| Layer | L3 Resolution: whether the patch produced for the task passes the task's tests |
| Origin | External |
| Task | The same tasks; a solver writes a patch, and native tests judge it |
| Ground truth | Each task's native tests |
| Granularity | Resolved or unresolved per task |
| Languages | C, C++, Go, Java, JavaScript, Python, Rust, TypeScript |
| Size | 500 tasks in the verified configuration |
| Pinned revisions | Same dataset; pinned per-source images, parsers, and grading scripts |
| Scorer | Native graders for the Verified, Pro, Poly, and Multi sources |
| Setup | Pinned task images, a mini SWE-agent checkout, and Docker |
| CodeNib entry point | `scripts/benchmark_context_solver.py`, `scripts/contextbench_verified.py` |
| Evidence | [Measured](contextbench-results.md); 388/500 resolved with a research route; leaderboard review pending |
<!-- END CODENIB_SUPPORT_MATRIX:card:contextbench_pass1 -->

### DeepSWE

An external harness runs the solver in task containers. CodeNib packages only
the analysis of its trial artifacts, and keeps the harness adapter, launchers,
and dashboard under `scripts/guardian/deepswe/`. No DeepSWE result is
published on this site.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:deepswe -->
| Benchmark card | |
| --- | --- |
| Layer | L3 Resolution: whether the patch produced for the task passes the task's tests |
| Origin | External |
| Task | Solve the task in a container; an external harness runs the solver |
| Ground truth | The task's tests |
| Granularity | Pass or fail per trial |
| Languages | Set by the external harness |
| Size | Set by the external harness |
| Pinned revisions | Set by the external harness |
| Scorer | External harness; CodeNib only aggregates its trial artifacts |
| Setup | The external DeepSWE harness and task containers |
| CodeNib entry point | `codenib.eval.benchmarks.deepswe`, `python -m scripts.guardian.deepswe.results` |
| Evidence | Tested; artifact analysis only; no published result |
<!-- END CODENIB_SUPPORT_MATRIX:card:deepswe -->

## Published results

| Result | Layer | Benchmark | Headline | Scope |
| --- | --- | --- | --- | --- |
| [Model-planned grep → Jev](grep_jev.md) | L1 | CodeNib Base, revision `4eb84e2e` | **71.40%** code-block Recall@5, against 58.62% for the same grep candidates in their original order | Research run with frozen search plans; paired 95% interval +6.44 to +19.67 points |
| [Same split, other routes](grep_jev.md#what-was-measured) | L1 | CodeNib Base | BM25 → Jev 59.35%; dense → Qwen3-Reranker-4B 63.40%; hybrid → Qwen3-Reranker-4B 65.03%; hybrid → Jev 67.92% | Code-block Recall@5 for each route |
| [Fresh product run](grep_jev.md#does-the-product-preview-reproduce-these-candidates) | L1 | CodeNib Base | 48.37% → **65.57%** after Jev (+17.20 points) | All 100 cases attempted; one failed and scored zero |
| [SWE-Explore validation](swe_explore.md) | L1 | SWE-Explore, 20 fixed cases | **1,020/1,020** real-output metric values equal to the official evaluator's | Scorer agreement; the BM25 control is not a quality claim |
| [ContextBench search](contextbench-results.md) | L2 | ContextBench verified, 500 canonical tasks | Complementary Haiku search pair: **77.07%** line Recall and 28.16% F1, against 72.83% and 33.95% for a native Opus control; Recall +4.24 points, paired 95% interval +1.59 to +6.41 | Research route on an already-exposed population; different models and billing on each side |
| [ContextBench resolution](contextbench-results.md) | L3 | ContextBench verified, 500 canonical tasks | **388/500 resolved (77.60%)** with the search pair's context and an Opus mini SWE-agent repair loop, at $338.88 recorded search-plus-repair model usage | First attempts only, failures retained; the leaderboard submission awaits maintainer review |

The multi-view system design is described in the
[CodeNib paper](https://arxiv.org/abs/2607.25431).

## Prepare inputs

<div class="codenib-section-grid" markdown>

-   <span class="codenib-card-eyebrow">Collect</span>

    **SWE-bench instances**

    Sample representative tasks across repositories and languages without
    losing their source metadata.

    [Collect benchmark inputs →](../collect_swebench.md)

-   <span class="codenib-card-eyebrow">Synthesize</span>

    **Query datasets**

    Generate traversal, behavioral, and multi-step queries with explicit
    curation and verification stages.

    [Run the synthesis pipeline →](../synthesis_pipeline.md)

-   <span class="codenib-card-eyebrow">Prepare</span>

    **Ground-truth locations**

    Extract expected files, symbols, and one-based source ranges from benchmark
    patches.

    [Use the GT locator →](../gt_locator.md)

-   <span class="codenib-card-eyebrow">Package</span>

    **Artifact bundles**

    Preserve manifests, metrics, diagnostics, and provenance as one verifiable
    evaluation output.

    [Build an artifact bundle →](../evaluation_artifacts.md)

</div>
