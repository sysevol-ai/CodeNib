<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Synthesis Traversal Pipeline

CodeNib's synthesis pipeline generates a code-search retrieval benchmark of
natural-language queries paired with verified ground-truth symbols and files.
Each query is grounded on real source code (not LLM-invented targets), graded by
an LLM judge, and projected into a HuggingFace dataset organized by language
config. This guide covers the dataset wrapper, the three query families, and the
scripts that produce, rebuild, push, and report on the data.

The published dataset is [`sysevol-ai/codeminer-synthesis`](https://huggingface.co/datasets/sysevol-ai/codeminer-synthesis).

## Overview

The pipeline emits one JSON record per query. Records carry the query text, the
source repository and `base_commit`, a `category` label, and ground-truth
`gt_files` / `gt_symbols` / `gt_symbol_nodes` (with code content and line spans).
Queries fall into three families, each produced by its own multiplier script:

| Family | Script | Categories produced |
|--------|--------|---------------------|
| Behavioral | `multiply_behavior_queries.py` | `behavioral` |
| Generic multiply | `multiply_queries.py` | `module_hint`, `file_hint`, `symbol_hint`, `reasoning` (and optionally `behavioral`) |
| Traversal | `multiply_traversal_queries.py` | `traversal` |

The standard per-config mix is the **base 50-row mix** plus an optional 10
traversal rows, as encoded in `codenib/dataset/codenib_synthesis.py`:

```python
EXPECTED_BASE_CATEGORY_COUNTS = {
    "behavioral": 20,
    "module_hint": 10,
    "file_hint": 10,
    "symbol_hint": 5,
    "reasoning": 5,
}
EXPECTED_TRAVERSAL_COUNT = 10
EXPECTED_CATEGORY_COUNTS_WITH_TRAVERSAL = {**EXPECTED_BASE_CATEGORY_COUNTS, "traversal": 10}
```

### What makes traversal queries special

Behavioral and generic-multiply queries are anchored on a single code block.
**Traversal queries** instead walk a 2–3 symbol chain in the code graph and
deliberately contain *no direct lexical hook* to the chain symbols — so a
keyword/grep agent has nothing to land on, while a graph agent can follow edges
from one anchor to the next. The full chain is recorded as ground truth.

Three **stances** govern the walk direction and which chain members may be
described (anchors) vs. hidden (the answer to discover):

| Stance | Direction | Chain | Anchors described |
|--------|-----------|-------|-------------------|
| `trace_back` | predecessor (upstream callers) | symptom → caller(s), 1 hop | the leaf symptom (index 0) |
| `trace_down` | successor (downstream callees) | entry → callee(s), 1 hop | the entry/top (index 0) |
| `bridge` | successor | start → middle → end, 2 hops | both ends (first + last) |

`trace_back` / `trace_down` build 2-symbol chains (1 hop); `bridge` builds
3-symbol chains (2 hops). The generator enforces an **anti-grep** check: a
post-generation pass scans the candidate query for any forbidden lexical token
(symbol basenames, file names/stems, name parts ≥ 4 chars that are not common
words like `data`/`name`/`path`) and retries with a stronger nudge if any leak.
A separate LLM judge grades each query on topic correctness, graph-only
solvability, and stance compliance, returning one of `valid` / `fix` /
`regenerate`.

!!! note "Line-number origin"
    Ground-truth code blocks follow the same conventions as the rest of the
    dataset code — see [Ground-Truth Locator](gt_locator.md) and
    [`codenib/dataset/CLAUDE.md`](https://github.com/sysevol-ai/CodeMiner/blob/main/codenib/dataset/CLAUDE.md) for the
    0-based (`CodeChunk`) vs. 1-based (`CodeLocation`) boundary.

## Dataset wrapper

`CodeNibSynthesisDataset` (in `codenib/dataset/codenib_synthesis.py`) loads
the published HuggingFace dataset, projects each row into CodeNib's common
dataset shape, and can clone + check out the row's repository at its
`base_commit`.

```python
from codenib.dataset.codenib_synthesis import CodeNibSynthesisDataset

ds = CodeNibSynthesisDataset(configs="Python,Go")  # subset of configs
data = ds.load()
print(ds.get_summary())  # by_language / by_category / by_source_config counts
```

Defaults: `dataset="sysevol-ai/codeminer-synthesis"` (`DEFAULT_DATASET`),
`split="test"` (`DEFAULT_SPLIT`). The constructor also accepts `filter_instance`
(a regex applied to `query_id` or `instance_id`), `root`, `repo_root`, `log`, and
`force_reload`. `load()` optionally takes `idx_list` or `idx_range` (mutually
exclusive). `process_instance()` clones the repo and resets it to the row's
`base_commit`; `load_eval_metadata()` projects the GT columns into the eval
metadata shape when no metadata file is provided.

### Configs

The dataset is organized by language config. `ALL_CONFIGS` is the canonical tuple:

```python
ALL_CONFIGS = ("C++_C", "Go", "Python", "Rust", "TypeScript_JavaScript")
```

Each config maps to a `language_group` label (`C++_C` → `C++/C`,
`TypeScript_JavaScript` → `TypeScript/JavaScript`, etc.).
`parse_config_list(configs)` normalizes a selector — `None`, `"all"`, an empty
list, a comma-separated string, or a sequence — into concrete config names, and
raises on any name not in `ALL_CONFIGS`.

### Schema

Rows are materialized with the `SYNTHESIS_FEATURES` HuggingFace `Features` schema.
Top-level fields:

| Field | Type | Notes |
|-------|------|-------|
| `repo` | string | `owner/name` |
| `instance_id` | string | SWE-bench-style instance id |
| `base_commit` | string | commit the GT was extracted at |
| `query` | string | the natural-language search query |
| `problem_statement` | string | falls back to `query` when absent |
| `category` | string | `behavioral` / `module_hint` / `file_hint` / `symbol_hint` / `reasoning` / `traversal` |
| `gt_symbols` | list[string] | ground-truth symbol names |
| `gt_symbol_nodes` | list[struct] | per-node `content`, `file`, `node_name`, `type`, `start_line`, `end_line` |
| `gt_files` | list[string] | ground-truth files |
| `query_id` | string | unique per query |
| `length_variant` | string | `simple` / `detailed` |
| `judge_verdict` | string | `valid` / `fix` / `regenerate` / `unknown` |
| `judge_reason` | string | one-sentence judge rationale |
| `chain_metadata` | struct | traversal-only chain provenance (see below) |
| `language_group` | string | e.g. `Python`, `C++/C` |
| `source_config` | string | originating HF config |
| `gt_code_blocks_count` | int64 | count of `gt_symbol_nodes` |

`chain_metadata` (populated for traversal rows) carries `anchor_indices`,
`anchor_symbols`, `anti_grep_leaks_at_last_gen`, `chain_length`, `direction`,
`hidden_indices`, `hidden_symbols`, `hop_count`, `spans_files`, `spans_n_files`,
and `stance`.

### Normalization and validation

- `normalize_synthesis_record(row, config_name)` projects a raw HF row into the
  schema above. It tolerates legacy column names (`question` → `query`,
  `target_symbols` → `gt_symbols`, `target_files` → `gt_files`,
  `target_symbol_nodes` → `gt_symbol_nodes`, `query_type`/`difficulty` →
  `category`), fills `language_group` from the config when missing, stamps
  `source_config`, and computes `gt_code_blocks_count`.
- `validate_synthesis_records(rows, *, expected_category_counts=None, compare_category_distribution=True)`
  returns a structured report (`row_count`, `by_config`, `by_category`,
  `by_config_category`, `issues`, `error_count`, `warning_count`). It flags
  missing/duplicate `query_id`, missing required fields, empty ground truth,
  non-`valid` judge verdicts (warning), GT files whose extension does not match
  the row's `language_group`, and divergence from `expected_category_counts` or
  from a reference config's category distribution.

## Scripts reference

The generation scripts use the Claude Agent SDK. Install and authenticate it
before running the multipliers:

```bash
pip install claude-agent-sdk
# then choose one auth path:
claude login
# or:
export ANTHROPIC_API_KEY="sk-ant-..."
```

All multiplier scripts read a **seed file** — a JSON whose first row provides
`repo`, `instance_id`, and `base_commit` (and optionally `language_group`) — and
write `synthesized_queries_<instance_id>.json` into `--output-dir`. They share a
generator/judge/post-fix loop and write incrementally.

### `multiply_behavior_queries.py` — behavioral queries

Synthesizes N behavioral queries for one instance, cycling `query_index` so each
query gets a distinct anchor block, and mixing `simple` (5–30 word) and
`detailed` (80–150 word) length variants.

```bash
python scripts/multiply_behavior_queries.py \
    --seed-file synthesis_output/seed_<instance>.json \
    --output-dir synthesis_output_behavior_x20/ \
    --n-queries 20 --simple-ratio 0.5 \
    --model opus --judge-model opus \
    --behavioral-consensus-runs 3 \
    --cache-dir ~/.codenib --repo-cache-dir ~/.codenib
```

Flags: `--seed-file` (required), `--output-dir` (default
`synthesis_output_behavior_x20`), `--n-queries` (default 20), `--simple-ratio`
(default 0.5, must be in [0,1]), `--model` (default `opus`), `--judge-model`
(default `opus`), `--max-turns` (default 10), `--sampling-seed` (default 42),
`--assignment-seed` (default 0), `--cache-dir`, `--repo-cache-dir`,
`--behavioral-consensus-runs` (default 3), `--post-fix-max-retries` (default 3,
set 0 to skip).

!!! note "Consensus voting"
    `--behavioral-consensus-runs N` runs N LLM passes per query; blocks selected
    by majority (≥ `ceil(N/2)`) become the ground truth. `N=3` yields organic
    multi-hop GT; `N=1` is faster but suppresses multi-hop.

### `multiply_queries.py` — generic query multiplication

Synthesizes a mix of `module_hint` / `file_hint` / `symbol_hint` / `reasoning`
(and optionally `behavioral`) queries. Non-behavioral rows are grounded on
anchors pulled from a populated behavioral JSON via `--anchor-file`, so their GT
is real rather than hallucinated.

```bash
python scripts/multiply_queries.py \
    --seed-file synthesis_output/seed_<instance>.json \
    --anchor-file synthesis_output_behavior_x20/synthesized_queries_<instance>.json \
    --output-dir synthesis_output_mixed_x30/ \
    --type-counts "module_hint=10,file_hint=10,symbol_hint=5,reasoning=5" \
    --model opus --judge-model opus
```

Flags: `--seed-file` (required), `--anchor-file` (default = `--seed-file`),
`--output-dir` (default `synthesis_output_mixed_x30`), `--type-counts` (required;
comma-separated `<type>=<count>`; valid types `behavioral`, `module_hint`,
`file_hint`, `symbol_hint`, `reasoning`), `--simple-ratio` (default 0.5),
`--model` (default `opus`), `--judge-model` (default `opus`), `--max-turns`
(default 10), `--sampling-seed` (default 42), `--assignment-seed` (default 0),
`--cache-dir`, `--repo-cache-dir`, `--behavioral-consensus-runs` (default 3),
`--post-fix-max-retries` (default 3).

### `multiply_traversal_queries.py` — traversal queries

Synthesizes traversal queries whose answer requires walking a 2–3 symbol chain
(1-hop `trace_back` / `trace_down`, 2-hop `bridge`) and whose text has no direct
lexical hook to the chain symbols. GT carries the full chain plus
`chain_metadata`.

```bash
python scripts/multiply_traversal_queries.py \
    --seed-file synthesis_output_behavior/synthesized_queries_<instance>.json \
    --output-dir synthesis_output_traversal_x10/ \
    --stance-counts "trace_back=10,trace_down=10,bridge=10" \
    --model opus --judge-model opus \
    --cache-dir ~/.codenib --repo-cache-dir ~/.codenib
```

Flags: `--seed-file` (required), `--output-dir` (default
`synthesis_output_traversal_v2_x30`), `--stance-counts` (default
`trace_back=10,trace_down=10,bridge=10`; valid stances `trace_back`,
`trace_down`, `bridge`), `--n-queries` (**deprecated**; use `--stance-counts`),
`--model` (default `opus`), `--judge-model` (default `opus`), `--sampling-seed`
(default 42), `--anti-grep-retries` (default 2; extra generator attempts when the
first leaks chain symbol names), `--judge-retries` (default 2; regenerate after
judge rejection), `--max-turns` (default 10), `--post-fix-max-retries` (default
3), `--keep-invalid` (keep non-`valid` rows for diagnostics — leave off for
release), `--allow-partial` (exit 0 even if fewer than requested rows are
generated), `--exclude-chain-file` (repeatable; an existing traversal JSON file
or directory whose `gt_symbols` chains should be skipped), `--cache-dir`,
`--repo-cache-dir`.

!!! warning
    `--keep-invalid` retains rows the judge flagged non-`valid`. Use it only for
    diagnostics; release generation should leave it off so only `valid` rows are
    written.

### `generate_codenib_synthesis_config.py` — one full config

Drives all three multipliers end-to-end for a single instance and config: it
writes a seed file from `codenib-base`, runs behavioral → mixed → traversal,
normalizes and combines the rows, and runs the quality gate.

```bash
python scripts/generate_codenib_synthesis_config.py \
    --config Python --instance-id <instance_id> \
    --include-traversal \
    --output-dir synthesis_output/Python/ \
    --cache-dir ~/.codenib --repo-cache-dir ~/.codenib
```

Flags: `--config` (required; one of `ALL_CONFIGS`), `--instance-id` (required),
`--split` (default `test`), `--source-dataset` (default
`fishmingyu/codeminer-base-dataset`), `--counts` (comma-separated `category=N`;
defaults to the base 50-row mix; include `traversal=10` to add traversal rows),
`--include-traversal` (shortcut for base mix + `traversal=10`),
`--traversal-stance-counts` (default = even split, so `traversal=10` becomes
`trace_back=4,trace_down=3,bridge=3`), `--traversal-judge-retries` (default 2),
`--output-dir` (required), `--combined-file` (override combined output path),
`--model-name` (default `opus`), `--judge-model` (default `opus`), `--max-turns`
(default 10), `--behavioral-consensus-runs` (default 3), `--post-fix-max-retries`
(default 3), `--sampling-seed` (default 42), `--assignment-seed` (default 0),
`--simple-ratio` (default 0.5), `--cache-dir`, `--repo-cache-dir`,
`--skip-existing` (reuse existing per-category JSON), `--allow-quality-errors`
(write combined output even if validation finds errors).

The script writes `<config>_combined.json` plus a `<config>_combined.quality.json`
report and returns nonzero on validation errors unless `--allow-quality-errors`
is set.

### `rebuild_codenib_synthesis_dataset.py` — assemble the HF dataset

Assembles one parquet file per language config, writes dataset-card metadata with
the benchmark split set to `test`, runs the quality gate, and optionally uploads.

```bash
python scripts/rebuild_codenib_synthesis_dataset.py \
    --replacement Python=synthesis_output/Python/Python_combined.json \
    --include-traversal --keep-extra-categories \
    --output-dir build/codenib-synthesis/ \
    --push-to-hub sysevol-ai/codeminer-synthesis
```

Flags: `--dataset` (default `DEFAULT_DATASET`), `--source-split` (default
`test`), `--output-split` (default `test`), `--configs` (default `all`),
`--replacement` (repeatable; `CONFIG=/path/to/file-or-dir` — replace a config
with local rows), `--append` (repeatable; `CONFIG=/path/to/file-or-dir` — append
local rows to a config), `--keep-extra-categories` (keep categories outside the
base mix, such as `traversal`), `--include-traversal` (expect base 50-row mix +
10 traversal rows per config), `--no-enforce-base-counts` (do not warn on
divergence from the 50-row base mix), `--allow-category-drift` (do not warn when
configs have different category distributions), `--allow-quality-errors` (write
output even when the quality gate finds errors), `--output-dir` (required),
`--push-to-hub` (dataset repo id to update after the gate passes), `--private`,
`--no-reuse-card-body` (do not copy the source dataset's README body).

The build writes per-config parquet, a `README.md` card, `.gitattributes`, and a
`quality_report.json`; `main()` returns nonzero on quality errors unless
`--allow-quality-errors`.

## Push to HF + reporting

### `push_synthesis_to_hf.py` — push a directory of JSONs as one config

Flattens all `*.json` files in `--input-dir` into rows and pushes them as one
config of the multi-config dataset. The first push creates the repo; later pushes
with a different `--config-name` add configurations under the same dataset.
Authenticate first with `hf auth login` (or `huggingface-cli login`).

```bash
python scripts/push_synthesis_to_hf.py \
    --input-dir synthesis_output_behavior_x20/ \
    --repo-id sysevol-ai/codeminer-synthesis \
    --config-name behavioral_x20
```

Flags: `--input-dir` (required), `--repo-id` (default
`sysevol-ai/codeminer-synthesis`), `--config-name` (required), `--split` (default
`test`), `--private` (default public), `--license` (default `apache-2.0`),
`--commit-message` (override the auto-generated message), `--dry-run` (build the
`Dataset` locally but do not push).

See [Uploading Datasets to HuggingFace](upload_dataset_to_huggingface.md) for
general upload/auth guidance.

### `report_codenib_synthesis.py` — report and gate quality

Loads the published dataset, validates it, and prints a text report
(split table, per-config category counts, language counts, and the first issues).
Useful as a CI quality gate.

```bash
python scripts/report_codenib_synthesis.py \
    --dataset sysevol-ai/codeminer-synthesis \
    --json-output reports/synthesis_quality.json
```

Flags: `--dataset` (default `DEFAULT_DATASET`), `--split` (default `test`),
`--configs` (default `all`), `--inspect-fallback-split` (if `--split` is absent,
inspect the first available split while still reporting the missing expected
split as an error), `--expected-category-counts` (comma-separated `category=N`;
defaults to the base 50-row mix), `--allow-category-drift` (do not warn when
configs have different category distributions), `--json-output` (write the full
report JSON), `--max-issues` (default 40), `--no-fail` (always exit zero),
`--fail-on-warnings` (exit nonzero on warnings as well as errors).

By default `main()` exits nonzero when `error_count > 0`; `--fail-on-warnings`
also fails on warnings, and `--no-fail` forces a zero exit.

## Related guides

- [Ground-Truth Locator](gt_locator.md) — how GT code blocks are extracted from patches.
- [Collecting SWE-bench Instances](collect_swebench.md) — sourcing the base instances.
- [Uploading Datasets to HuggingFace](upload_dataset_to_huggingface.md) — upload/auth mechanics.
- [Diagnosing Query Leaks](diagnose_query_leak.md) — analyzing lexical leakage in queries.
