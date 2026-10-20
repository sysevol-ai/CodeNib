<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Datasets

Loading a dataset, scoring a benchmark, and running an agent policy are
separate steps. A loader means CodeNib can read the tasks and prepare
checkouts and ground truth. It does not mean any particular method has been
run on that dataset; the [method × benchmark matrix](index.md#method-benchmark-matrix)
records that.

Every benchmark has a card with the same fields: layer, origin, task, ground
truth, granularity, languages, size, pins, scorer, setup, entry point, and
evidence. The cards are generated from `scripts/support_matrix.yaml`. Benchmarks
with their own guide (SWE-Explore, Agent Retrieval Bench, and ContextBench)
carry the card at the top of that guide; the rest are below.

Patch-derived code blocks (`gt_code_blocks`) use **1-based, inclusive** lines,
matching the [GT locator](../gt_locator.md) output. CodeNib's chunkers and
graph use 0-based lines internally. CodeNib Synthesis keeps the graph's
0-based lines in `gt_symbol_nodes`, and the scorer shifts them by one before
comparing spans.

## Localization benchmarks

### CodeNib Base

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:codenib_base -->
| Benchmark card | |
| --- | --- |
| Layer | L1 Localization: the method's final ranked files, symbols, code blocks, or line regions |
| Origin | CodeNib |
| Task | Real issues; find the code the golden patch changed |
| Ground truth | Derived from golden patches by the GT locator |
| Granularity | File, symbol, code block |
| Languages | C/C++, Go, Python, Rust, TypeScript/JavaScript |
| Size | 100 issues from 25 repositories; 151 code blocks |
| Pinned revisions | Dataset revision `4eb84e2e` |
| Scorer | CodeNib shared retrieval metrics |
| Setup | Hugging Face download and repository clones |
| CodeNib entry point | `CodeNibBaseDataset` |
| Evidence | [Measured](grep_jev.md) |
| Upstream | <https://huggingface.co/datasets/fishmingyu/codenib-base-dataset> |
<!-- END CODENIB_SUPPORT_MATRIX:card:codenib_base -->

The ground-truth columns are `gt_target_files`, the modified and deleted
symbols, and `gt_code_blocks` with file, symbol, 1-based lines, and change type.
The loader takes no revision argument and reuses its first cached download
until `force_reload`; published results load revision
`4eb84e2e8918474969ce68c5b06facf14d6be604` explicitly.

### CodeNib Synthesis

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:codenib_synthesis -->
| Benchmark card | |
| --- | --- |
| Layer | L1 Localization: the method's final ranked files, symbols, code blocks, or line regions |
| Origin | CodeNib |
| Task | Synthetic behavioral, hint, reasoning, and traversal queries |
| Ground truth | Generated with the query, then judged |
| Granularity | File, symbol span |
| Languages | C/C++, Go, Python, Rust, TypeScript/JavaScript |
| Size | 500 queries, 100 per language group |
| Pinned revisions | None in the loader; five configurations, one per language group |
| Scorer | CodeNib shared retrieval metrics |
| Setup | Hugging Face download and repository clones |
| CodeNib entry point | `CodeNibSynthesisDataset` |
| Evidence | Runnable |
| Upstream | <https://huggingface.co/datasets/sysevol-ai/codeminer-synthesis> |
<!-- END CODENIB_SUPPORT_MATRIX:card:codenib_synthesis -->

The ground-truth columns are `gt_files`, `gt_symbols`, and `gt_symbol_nodes`
with line spans. The five configurations are `C++_C`, `Go`, `Python`, `Rust`,
and `TypeScript_JavaScript`. Synthetic queries complement the issue-derived
CodeNib Base labels; they do not replace them. The
[synthesis pipeline](../synthesis_pipeline.md) describes how the queries and
their labels were generated and checked.

### SWE-bench Lite

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:swebench_lite -->
| Benchmark card | |
| --- | --- |
| Layer | L1 Localization: the method's final ranked files, symbols, code blocks, or line regions |
| Origin | External |
| Task | Real issues; find the code the golden patch changed |
| Ground truth | Derived from golden patches by the GT locator |
| Granularity | File, function, code block |
| Languages | Python |
| Size | Full split, or a pinned 50-case policy selection |
| Pinned revisions | Dataset revision and seeded case selection for paired policy runs |
| Scorer | CodeNib shared retrieval metrics |
| Setup | Repository clones; annotation file built on first use |
| CodeNib entry point | `SwebenchDataset` |
| Evidence | [Runnable](../agent_integrations.md#fixed-swe-bench-lite-coverage); dataset revision and a 50-case selection are pinned for paired policy runs |
| Upstream | <https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite> |
<!-- END CODENIB_SUPPORT_MATRIX:card:swebench_lite -->

`SwebenchDataset` loads Lite by default, and runner CLIs select it as
`swebench_lite`. Ground truth is files, functions, and code blocks derived from
the golden patch. `load_eval_metadata(path)` generates a missing annotation
file at `path` with `build_gt_metadata`, which clones each repository. The BM25,
graph, and retrieve → rerank baselines pass such a default path; the agent
runners and the embedding baseline need `--eval-instances`.

The [paired policy cases](../agent_integrations.md#fixed-swe-bench-lite-coverage)
pin the dataset revision and a seeded, repository-balanced 50-case selection.

### Loc-Bench v1

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:locbench_v1 -->
| Benchmark card | |
| --- | --- |
| Layer | L1 Localization: the method's final ranked files, symbols, code blocks, or line regions |
| Origin | External |
| Task | Real issues with edited and added functions |
| Ground truth | An annotation file supplied with `--eval-instances` |
| Granularity | File, symbol |
| Languages | Python |
| Size | Full release |
| Pinned revisions | None in the loader |
| Scorer | CodeNib shared retrieval metrics |
| Setup | Repository clones and an annotation file |
| CodeNib entry point | `LocbenchDataset` |
| Evidence | Runnable |
| Upstream | <https://huggingface.co/datasets/czlll/Loc-Bench_V1> |
<!-- END CODENIB_SUPPORT_MATRIX:card:locbench_v1 -->

The dataset's `edit_functions` and `added_functions` columns are loaded but not
scored. No shipped command builds the annotation file for Loc-Bench (the GT
locator CLI covers SWE-bench Verified and Lite). No aggregate result is
published.

### SWE-Explore and Agent Retrieval Bench

Both have their own guides, with their cards at the top:

- [SWE-Explore](swe_explore.md) scores ranked line regions with the
  benchmark's official 17 metrics. You supply the released JSONL; issue text
  and base commits come from pinned SWE-bench Verified, Multilingual, and Pro
  joins (below). The adapter is `codenib.eval.benchmarks.swe_explore`.
- [Agent Retrieval Bench](agent_retrieval_bench.md) scores file rankings for
  four static tasks over the released corpus chunks. `codenib.dataset.agent_retrieval_bench`
  prepares the cases; `codenib-arb` retrieves and scores them.

## Agent context benchmarks

[ContextBench](contextbench.md) scores what an agent read, not only what it
answered. `codenib.dataset.contextbench` loads the pinned release
(`load_contextbench_cases`), keeps each task's `problem_statement` separate from
its `gold_context`, and selects tasks by ID, language, or source (`Verified`,
`Pro`, `Poly`, or `Multi`). Use the curated `instance_id` or the
`original_inst_id` that the release records for each task; a source name alone
does not reproduce the 500-task `contextbench_verified` configuration.

## Sources and joins

These datasets feed collection, joins, or custom runs. No harness scores them
as a benchmark on its own.

| Dataset | Loader | Used for |
| --- | --- | --- |
| [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified) | `SwebenchDataset(dataset="princeton-nlp/SWE-bench_Verified")`, the [GT locator](../gt_locator.md) CLI | Python rows for [SWE-bench collection](../collect_swebench.md); issue text for the SWE-Explore join |
| [SWE-bench Multilingual](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual) | `SwebenchMultilingualDataset` | Non-Python rows for collection and CodeNib Base; all 182 rows referenced by the pinned SWE-Explore release resolve uniquely. It needs an external annotation file. |
| [SWE-bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) | Pinned join inside the SWE-Explore adapter | Supplies issue text and commits that the released SWE-Explore rows omit. All 215 referenced rows resolve uniquely. This is not a general SWE-bench Pro task runner. |
| Local JSON | `LocalJsonDataset` | Your own repository, query, and expected `gt_files` / `gt_symbols`, from the Python API. No runner CLI selects it, and it has no line-level ground truth. |

## Choosing a dataset

- Use **CodeNib Base** to compare retrieval routes across languages on real
  issues. It has the most published method comparisons on this site.
- Use **CodeNib Synthesis** for query types that issues rarely contain, such as
  multi-hop traversal questions.
- Use **SWE-bench Lite** to compare with published Python localization agents.
- Use **SWE-Explore** when you need the benchmark's official region metrics,
  including line-budgeted recall and nDCG.
- Use **Agent Retrieval Bench** for tasks other than issue localization:
  finding tests, review context, failure causes, or ripple effects.
- Use **ContextBench** to measure what an agent reads while it explores, in
  eight languages, against annotated gold context.
