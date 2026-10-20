# ContextBench

`codenib-contextbench` prepares tasks, runs CodeNib's native localization agent
with OpenRouter, exports the official unified trajectory format, and invokes
the pinned [ContextBench evaluator](https://github.com/EuniAI/ContextBench).
It supports the full dataset and verified configuration, all eight dataset
languages, and local JSON, JSONL, or Parquet inputs.

See the [experiment results](contextbench-results.md) for the measured search
and resolution runs, including the 500-task search pair and its Pass@1. Those
runs use separate research scripts (Claude Agent SDK search workers, a mini
SWE-agent solver, and native graders), not this CLI's OpenRouter runner; the
[harness list](harnesses.md#agent-context) names each one.

The agent searches and reads source code. This workflow measures context
retrieval; it does not generate patches or execute issue-resolution tests.
External unified trajectories can include a `model_patch` for official EditLoc
scoring. Reference patches and gold annotations are never supplied to the model.


<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:contextbench -->
| Benchmark card | |
| --- | --- |
| Layer | L2 Agent context: every source span an agent viewed during its trajectory, and its final context |
| Origin | External |
| Task | Real issues; an agent explores, and everything it reads is scored |
| Ground truth | Annotated gold context |
| Granularity | File, symbol, byte span, line, trajectory |
| Languages | C, C++, Go, Java, JavaScript, Python, Rust, TypeScript |
| Size | 1,136 tasks; 500 in the verified configuration |
| Pinned revisions | Evaluator `1436c28a`, dataset `c2855792` |
| Scorer | Official evaluator in an isolated subprocess |
| Setup | Evaluator checkout and a separate Python 3.10 or 3.11 environment |
| CodeNib entry point | `codenib-contextbench` |
| Evidence | [Measured](contextbench-results.md); 500 canonical tasks with a research route; leaderboard review pending |
| Upstream | <https://github.com/EuniAI/ContextBench> |
<!-- END CODENIB_SUPPORT_MATRIX:card:contextbench -->

## Preparing a leaderboard submission

The [official submission guide](https://github.com/ContextBench/contextbench.github.io/blob/main/submissions/README.md)
accepts pull requests to **ContextBench/contextbench.github.io**, the leaderboard
repository. The evaluator repository is separate. Maintainers review the evidence
and publish accepted results; passing the format check alone does not publish a
score. These requirements were checked against leaderboard commit
`a40dfa6b16f14cafd2b1cc46faf0969b38ef03b9` on 2026-10-11.

An agent using CodeNib retrieval belongs to `system_type: agent`. The `backbone`
category requires the benchmark's adapted mini SWE-agent harness. The current
[schema validator](https://github.com/ContextBench/contextbench.github.io/blob/a40dfa6b16f14cafd2b1cc46faf0969b38ef03b9/scripts/submissions.mjs)
requires file, block and line recall/precision/F1 **and Pass@1**. A retrieval-only
run from this package does not establish Pass@1: producing a diagnosis or a patch
is insufficient without issue-resolution outcomes. Do not use zero as a placeholder
for an unmeasured score. A separate retrieval-only category would require agreement
from the benchmark maintainers.

The public
[JiuwenSwarm submission](https://github.com/ContextBench/contextbench.github.io/tree/main/submissions/2026-09-12_openjiuwen-jiuwenswarm)
uses the 500-task `contextbench_verified` population. Its README distinguishes
500 resolution outcomes from 499 valid retrieval evaluations. The website's
1,136-task total describes the dataset, not every leaderboard run. Use the exact
task IDs and revision of the comparison; a development subset does not qualify
automatically for the main rankings.

Once a complete agent evaluation is available, copy the official template into
one `submissions/YYYY-MM-DD_system-name/` directory per system/run:

| Artifact | Evidence to retain |
| --- | --- |
| `metadata.json` | Model/agent identity, submitter, dataset configuration and revision, evaluator commit, selected instance count, reproduction command |
| `results.json` | Aggregated measured metrics; optional cost is USD per instance |
| `README.md` | Method, environment, selection and tuning disclosures, aggregation, failures/retries, artifact index |
| `logs/` | Original per-case retrieval scores, resolution outcomes, execution logs, all attempts |
| `trajectories/` | Original model/tool interactions with instance and attempt identities |

Keep each committed file at most 50 MiB; gzip or split larger artifacts and
provide checksums and extraction instructions. Retain failed attempts. Remove
credentials and document redactions. External links and Git LFS pointers alone
do not replace the actual artifacts.

From the leaderboard checkout, using Node.js 20 or newer:

```bash
npm run submissions:validate -- submissions/YYYY-MM-DD_system-name
```

Then open a PR from the submitter's fork to `main` using its leaderboard
submission template. Do not edit existing scores or run the maintainer-only
publication step as part of preparing evidence.

For aggregation, `block` corresponds to upstream `final.symbol`. The published
submission averages per-instance metrics; its F1 is the mean of individual F1s,
not a recomputation from mean precision and recall. The pinned evaluator emits
coverage and precision without F1; derive each instance's F1 from those values
before averaging. This package's `final_*`
summary metrics are micro averages, so exporting those directly as leaderboard
macro scores would change the scoring population/weighting. Keep the per-case
outputs and an explicit aggregation script.

Serving API cost and complete agent API cost also have different boundaries.
Record planner, reranker, solver/tool-model calls and all attempts separately,
then report the complete agent total if comparing against a full-agent entry.
Report graph construction, cache refresh, CPU/GPU time and disk separately from
provider API charges. Repeated serving calls within one issue-resolution attempt
must all contribute to that attempt's total.

## Installation

From a CodeNib checkout:

```bash
python -m pip install -e '.[agent,datasets]'
git clone https://github.com/EuniAI/ContextBench.git /tmp/ContextBench
git -C /tmp/ContextBench checkout 1436c28a8eb95496da4ea69ad458b9f8a8eb7d61
```

Use an isolated evaluator environment with upstream's pinned parser dependencies:

```bash
python3.11 -m venv /tmp/contextbench-evaluator
/tmp/contextbench-evaluator/bin/pip install -r /tmp/ContextBench/requirements.txt
```

Pass `--evaluator-python /tmp/contextbench-evaluator/bin/python` to run or score.
Do not install upstream's legacy `tree-sitter==0.20.4` requirements into the
CodeNib environment. Some `tree-sitter-language-pack` versions can also work,
but version 1.8.1 expects string input while the pinned upstream extractor sends
bytes and silently returns empty symbol sets. The command checks extraction of
known functions in all eight dataset languages before model calls, so
incompatible parsers cannot produce
misleading perfect symbol scores. Python 3.10/3.11 supports the pinned
`tree-sitter-languages==1.10.2` wheel.

The module entry point works without reinstalling console scripts:

```bash
python -m codenib.eval.benchmarks.contextbench_runner --help
```

## Small OpenRouter run

Set `OPENROUTER_API_KEY` in the environment, then run one explicit task:

```bash
codenib-contextbench run \
  --instance-id psf__requests-1142 \
  --model openrouter/openai/gpt-4.1-mini \
  --max-turns 4 --max-tokens 512 --top-k 3 \
  --contextbench-root /tmp/ContextBench \
  --evaluator-python /tmp/contextbench-evaluator/bin/python \
  --output results/contextbench-smoke \
  --live
```

Model IDs follow LiteLLM's `openrouter/<provider>/<model>` convention. The
default is `openrouter/openai/gpt-4.1-mini`. `--live` enables billed requests;
unit tests do not call an API. `--max-turns` bounds exploration calls, with at
most one additional final-answer call. Transient requests can retry once;
`--request-timeout` bounds each request. These are call/token limits rather
than a dollar spending cap.

The default retrieval skill is `bm25_search`, accompanied by `read`, `grep`,
and `glob`. `--skill repository_search` selects CodeNib's query-facing sparse
retrieval skill. Both use manifest-bound BM25 artifacts and require no GPU or
embedding service. The task checkout is pinned to `base_commit`; indexes and
annotations live outside it. The agent has no shell or write tool.

## Selection and complete batches

The pinned Hugging Face release exposes one `train` split for each config:

| Configuration | Task count | Selection |
| --- | ---: | --- |
| `default` | 1,136 | Full release |
| `contextbench_verified` | 500 | Curated subset; not the same as `--source Verified` |

Repeat `--instance-id`, `--language`, or `--source` to select a union within a
filter. Different filters intersect. Both curated `instance_id` and
`original_inst_id` are accepted. Sources are `Verified`, `Pro`, `Poly`, and
`Multi`; languages are Python, JavaScript, TypeScript, Go, C, Rust, C++, and
Java (CLI values are lowercase, with `cpp` for C++). `--limit N` selects the
first N matching rows in release order. Omitting filters and `--limit` runs
the entire selected configuration.

```bash
codenib-contextbench run \
  --config contextbench_verified --language python --limit 3 \
  --contextbench-root /tmp/ContextBench \
  --evaluator-python /tmp/contextbench-evaluator/bin/python \
  --model openrouter/openai/gpt-4.1-mini \
  --output results/contextbench-three --live
```

`--data /path/to/full.parquet` uses a local release instead of downloading
Hugging Face data. JSON/JSONL rows use the same fields, and `gold_context` may
be a JSON string or a list. Gold paths retain their original spelling so the
official scorer applies its normalization exactly once, including its existing
behavior for dotfiles. The release includes a task with no problem
statement; a model run records that as a failure, preserving its denominator.
Three rows have empty gold-context lists. They remain in the selection and
use the official evaluator's empty-gold metric behavior.

`prepare` validates the selection and checks out repositories without making
model calls. It writes a separate preparation run:

```bash
codenib-contextbench prepare --limit 1 --output results/contextbench-preparation
```

Pass `--repos-root results/contextbench-preparation/repos` to a subsequent
`run` or `evaluate` command with the same selection to reuse those clean
checkouts. Each experiment still owns its own output directory and indexes.

## Resume and artifacts

Repeat the same run command with `--resume` to reuse completed agent cases.
The case digest, model, budgets, skills, and CodeNib implementation digest must
match. Failed agent cases are retried; the official metrics are recomputed.
Changing the experiment requires a new output directory. Existing checkouts
are audited, never reset or cleaned. A run lock prevents two processes from
issuing duplicate billed calls into one output directory.

| Artifact | Meaning |
| --- | --- |
| `run.json` | Input identity, revisions, implementation digest, and model settings |
| `cases.jsonl`, `gold.jsonl` | Selected tasks and scoring-only annotations |
| `cases/<instance_id>.json` | Actual messages, answer, native trace, token/cost records, snapshot audit, or failure |
| `predictions.jsonl` | Official `traj_data` with steps and cumulative viewed context |
| `metrics.json`, `evaluation.log` | Official per-task metrics and evaluator diagnostics |
| `report.json` | Coverage, failures, official aggregate metrics, and run provenance |

Each delivered tool response is a trajectory step, including empty results
and failed searches. Only numbered source lines actually delivered count as
viewed spans. Glob listings and name-only search hits do not count as reads.
Repeated reads remain in steps for redundancy; final context merges their
union. File, symbol, byte-span, and line coverage/precision and trajectory
AUC/redundancy come directly from the official evaluator. Scores aggregate
over successfully scored cases; every requested case and failure is reported
separately, and any failed case makes the command exit nonzero.

For truncated JSON responses, complete returned nodes and complete numbered
lines in a partial trailing node are retained. An incomplete trailing source
line is excluded. Source ranges are 1-based and inclusive; discontinuous
excerpts retain their gaps.

## Evaluate existing trajectories

```bash
codenib-contextbench evaluate \
  --data results/contextbench-smoke/cases.jsonl \
  --pred results/contextbench-smoke/predictions.jsonl \
  --repos-root results/contextbench-smoke/repos \
  --contextbench-root /tmp/ContextBench \
  --evaluator-python /tmp/contextbench-evaluator/bin/python \
  --output results/contextbench-rescore
```

Inputs use ContextBench's unified JSON/JSONL format, not raw external-agent
logs. Use upstream `contextbench.process_trajectories` to convert those logs
first. Missing predictions become explicit failures; duplicate identities or
predictions outside the selected case set are rejected. The bridge disables
upstream's reference-patch fallback, so EditLoc is absent unless an actual
`model_patch` is supplied.

## Reproducibility and verification

| Input | Pinned revision |
| --- | --- |
| Official evaluator | `1436c28a8eb95496da4ea69ad458b9f8a8eb7d61` |
| `Contextbench/ContextBench` dataset | `c2855792b006af41c67202d33883fb9d46362853` |

The evaluator runs in a subprocess, reads the same audited source snapshot as
the agent, and keeps its checkout operation local. The runner rejects a changed
upstream revision or modified evaluator source. Reference patches are not
applied, and issue-resolution success is not inferred from retrieval scores.

The integration has been checked against every row of both pinned dataset
configurations, including agreement with upstream's gold-path parsing. Local
tests exercise native retrieval in all eight languages and official scoring
on a temporary repository.

A live OpenRouter smoke run on `psf__requests-1142`, at source commit
`22623bd8c265b78b161542663ee980738441c307`, completed with
`openrouter/openai/gpt-4.1-mini`, four exploration turns, a 512-token response
limit, and `top_k=3`. It made five model calls including the final answer and
reported 15,605 prompt tokens and 297 completion tokens. The audited checkout
remained clean, and the official evaluator scored the case successfully:

| Final granularity | Coverage | Precision |
| --- | ---: | ---: |
| File | 1.0000 | 1.0000 |
| Symbol | 1.0000 | 1.0000 |
| Byte span | 1.0000 | 0.5579 |
| Line | 1.0000 | 0.5583 |

This verifies one live execution path; it is not a full-dataset quality result.
The runtime cost estimate was unavailable (`cost_usd: null`); raw token and
cache counts are retained, and a missing cost must not be interpreted as zero.

```bash
pytest test/dataset/test_contextbench_dataset.py \
  test/eval/test_contextbench.py test/eval/test_contextbench_runner.py \
  -m 'not integration' -q
CONTEXTBENCH_CHECKOUT=/tmp/ContextBench \
  pytest test/eval/test_contextbench_runner.py -m integration -q
```

The second command checks real official scoring against a temporary local
repository, including all four granularities and repeated-read redundancy.
It requires the pinned evaluator checkout but no model credentials.
