# Agent Retrieval Bench

CodeNib supports the four positive static retrieval tasks in
[Agent Retrieval Bench](https://github.com/eyuansu62/agent-retrieval-bench)
through `codenib-arb`. This labs runner consumes released corpus chunks, runs
CodeNib BM25, and calls the pinned upstream scoring functions. It also scores
external file rankings without invoking a retriever or model.

| Release | Task | Samples |
| --- | --- | ---: |
| `v2_code2test` | Find tests for a code change | 106 |
| `v2_comment2context` | Find context for a review comment | 80 |
| `v2_trace2code` | Find code implicated by a failure trace | 101 |
| `v2_edit2ripple` | Find files affected by an anchor edit | 58 |
| **Total** | | **345** |

Abstention/selective evaluation, line/block scores, interactive agent
trajectories, and automatic leaderboard submission are outside this first
adapter. The static BM25 route makes zero model API calls.


<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:arb -->
| Benchmark card | |
| --- | --- |
| Layer | L1 Localization: the method's final ranked files, symbols, code blocks, or line regions |
| Origin | External |
| Task | Four static tasks; code→test, comment→context, trace→code, edit→ripple |
| Ground truth | Released gold files |
| Granularity | File |
| Languages | Mixed; multi-language repositories |
| Size | 345 positive samples |
| Pinned revisions | Evaluator `07014c98`, dataset `5901e1ee` |
| Scorer | Official functions called directly (Recall@k, MRR, BCY) |
| Setup | ARB checkout and verified release download |
| CodeNib entry point | `codenib-arb` |
| Evidence | [Pinned](#validation); official scorer; 8-case smoke only |
| Upstream | <https://github.com/eyuansu62/agent-retrieval-bench> |
<!-- END CODENIB_SUPPORT_MATRIX:card:arb -->

## Pins and preparation

| Input | Pinned revision |
| --- | --- |
| Upstream evaluator | `07014c986f3deadb1548c62b32c0ffbe6a81465d` |
| [Released dataset](https://huggingface.co/datasets/eyuansu71/agent_retrieval_bench) | `5901e1ee3aff048290db72edf9c63bc498b79ea3` |

Install CodeNib and use a separate checkout for ARB. The upstream download
command requires the Hugging Face `hf` CLI, `zstd`, and `tar`; it verifies each
release archive's published checksum before extraction.

```bash
python -m pip install -e '.[datasets]'
git clone https://github.com/eyuansu62/agent-retrieval-bench /tmp/arb
git -C /tmp/arb checkout 07014c986f3deadb1548c62b32c0ffbe6a81465d
python -m venv /tmp/arb-env
/tmp/arb-env/bin/pip install /tmp/arb huggingface_hub

for release in v2_code2test v2_comment2context v2_trace2code v2_edit2ripple; do
  /tmp/arb-env/bin/arb download-benchmark \
    --version "$release" \
    --revision 5901e1ee3aff048290db72edf9c63bc498b79ea3 \
    --local-dir /tmp/arb-data \
    --hf-bin /tmp/arb-env/bin/hf
done

codenib-arb prepare \
  --data-root /tmp/arb-data \
  --output /tmp/arb-prepared.json
```

Preparation loads all four releases by default. Repeat `--release` or
`--sample-id` to select a subset; `--limit-per-task 2` selects the first two
sample IDs in each task for a deterministic smoke test. Selection happens
before retrieval. The output freezes sample records, selection, source hashes,
corpus hashes, and snapshot identities. Manifest paths are anchored at
`--data-root`, regardless of the current working directory.

Local fingerprints detect subsequent changes; they do not authenticate a
modified local copy as the published release. Use the verified download above
and retain its receipts. Keep the prepared file alongside the data root it
references. Invalid sample schemas stop preparation; corpus preparation errors
remain attached to their selected samples and produce a nonzero exit status.

## Retrieve and rescore

```bash
codenib-arb run \
  --prepared /tmp/arb-prepared.json \
  --upstream /tmp/arb \
  --output /tmp/arb-bm25

codenib-arb evaluate \
  --prepared /tmp/arb-prepared.json \
  --pred /tmp/arb-bm25/predictions.json \
  --upstream /tmp/arb \
  --output /tmp/arb-rescored
```

Each output directory contains `config.json`, `predictions.json`, and
`results.json`. Native retrieval also writes `checkpoint.json` after every
case, atomically binding its predictions to input and integrity hashes. Use the
same command with `--resume` after interruption; the input and implementation receipts must
match. Resume verifies the native checkpoint and rebuilds the public prediction
export from it; edited exports cannot replace native results. Completed successes
and failures are retained. Use a new output directory to retry failures or change inputs. A directory lock excludes
concurrent writers; an interrupted rescore removes the previous report before
starting so an old success cannot be mistaken for a new result.

Preparation checks each selected release against its complete published sample
count before applying sample IDs or limits, so truncated downloads cannot reduce
the denominator silently.

The evaluator runs in an isolated subprocess against the clean, pinned ARB
checkout, rejecting hidden Git index flags as well as ordinary local changes.
It needs no installation in CodeNib's environment. Optionally set
`--evaluator-python /tmp/arb-env/bin/python`. Offline rescoring never reruns
retrieval and does not infer the cost of externally produced predictions.

External systems can supply a JSON array or `.jsonl` records:

```json
{"sample_id":"<released id>","repo":"owner/repo","base_commit":"<40-character SHA>","status":"ok","ranked_files":["src/first.py","tests/test_first.py"]}
```

Use `status: "error"` with an `error` field for a retrieval failure. An explicit
empty `ranked_files` list is a valid zero-score result; an absent prediction is
a failure. Duplicate file paths collapse at their first occurrence. Unknown or
duplicate sample IDs reject the input. Snapshot mismatches and unknown file
paths fail the corresponding case.

## Metric contract

CodeNib calls upstream `target_gold_files`, `query_has_leakage`, `recall_at`,
`reciprocal_rank`, and `bcy_curve.evaluate_sample` rather than maintaining a
second metric implementation.

| Metric | Ranking and content |
| --- | --- |
| File Recall@5/10/20 | First 5, 10, or 20 distinct files |
| MRR | Full supplied file ranking; each row records `ranking_depth` |
| BCY@4000/8000/16000/32000 | First 20 distinct files, packed with the official regex code tokenizer and canonical renderer |

BCY reads the released `kind=file` text, which can be truncated by the corpus
builder. It counts a gold file only when content tokens fit; header tokens do
not establish coverage. The boundary file contributes a token prefix. The
report retains upstream packing details and coverage-threshold sensitivities.
The old upstream `gold_coverage@8k` character-packing score is not reported as
canonical BCY.

The native baseline indexes every released file and symbol chunk, including
tests, documentation, configuration, and extensionless files. It uses CodeNib's
existing tokenizer and BM25 implementation, not upstream's BM25 ranker. The
indexed identity is `path:symbol:ordinal`; all chunk hits are ranked before
first-hit file deduplication. No graph or embedding index is constructed.
Queries contain only the released `query` object serialized with sorted JSON
keys. Gold, fix commits, and hard-negative annotations never enter retrieval.

Primary aggregates are per-sample means over **all selected cases**, with
failures contributing zero. The report also includes valid-only means,
per-task summaries, failure IDs, and per-case errors. Commands exit nonzero on
failure. Always report coverage and the selected case set with a score;
truncated external rankings can lower MRR relative to a complete ranking.

## Validation

All 345 positive samples and their 294 release/snapshot pairs passed corpus
preparation. The deterministic smoke selection uses `--limit-per-task 2`,
giving eight samples across the four tasks. Every case completed retrieval and
official scoring; offline rescoring matched all 64 metric cells and all BCY
packing details. See the [validation receipt](../assets/arb_smoke.json) for
selected IDs, input hashes, per-case metrics, and implementation fingerprints.

| Smoke-only metric | Mean over 8 selected cases |
| --- | ---: |
| Recall@5 | 0.2292 |
| Recall@10 | 0.4375 |
| Recall@20 | 0.6250 |
| MRR | 0.1848 |
| BCY@4000 | 0.1250 |
| BCY@8000 | 0.2292 |
| BCY@16000 | 0.5000 |
| BCY@32000 | 0.6250 |

These are integration smoke results, not a full benchmark result or a
leaderboard comparison. No model API calls were made. A full retrieval run on
all 345 samples remains a separate measurement.

## Extending retrieval experiments

The imported-ranking boundary can score additional methods on the same cases.
These routes are candidates for later adapters, not measured ARB support in
this release:

| Existing CodeNib capability | Proposed comparison |
| --- | --- |
| Zoekt trigram and regex search | Multi-round exact symbol, path, and trace search |
| FAISS vector retrieval | Semantic recall control on the same released text |
| CodeGraph relationships | Search seeds plus definition/reference/call expansion |
| Hybrid retrieval and reranking | Candidate fusion and ordering at fixed budgets |

Before comparing agent strategies, fix the model, tool permissions, query
budget, and final context budget. Report index build time and size separately
from query latency and model cost.

Code-graph indexes usually need the full repository, which shows more than the
released corpus because the corpus builder truncates long files. ARB treats
such runs as a separate condition
([issue #2](https://github.com/eyuansu62/agent-retrieval-bench/issues/2)).
The native baseline reads only the released corpus. A full-source run may index
a clean working tree at each sample's `base_commit`, without later history. It
must be labeled as full source, keep gold labels and resolving changes out of
retrieval inputs, score only files in the released candidate set, and compute
canonical BCY on the released text.

An indexed-interactive comparison, where an agent searches iteratively with
and without graph expansion under a fixed model and budget, is a follow-up
track. It is separate from ARB's closed-tool diagnostic and from the static
scores on this page.
