# CORE-Bench

Evaluate CodeNib retrieval on CORE-Bench's released code chunks. Choose BM25,
dense retrieval, or their reciprocal-rank fusion, then score the rankings with
the official evaluator.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:core_bench -->
| Benchmark card | |
| --- | --- |
| Layer | L1 Localization: the method's final ranked files, symbols, code blocks, or line regions |
| Origin | External |
| Task | Rank released code chunks within each query's eligible snapshot |
| Ground truth | Released positive query-document relevance labels |
| Granularity | Chunk |
| Languages | Mixed; released Level-2 and Level-3 corpora |
| Size | Selected repository corpora and queries |
| Pinned revisions | Evaluator `5d00cdca`, dataset `23da5d97` |
| Scorer | Official collector's binary relevance metrics |
| Setup | Local release files and pinned evaluator checkout |
| CodeNib entry point | `python -m codenib.eval.benchmarks.core_bench` |
| Evidence | Runnable; CodeNib retrieval with official metrics |
| Upstream | <https://github.com/zhangfw123/CORE-Bench-Eval> |
<!-- END CODENIB_SUPPORT_MATRIX:card:core_bench -->

## Prepare the data

Install the `graph` and `datasets` extras and pandas for the evaluator. Dense
retrieval also uses the `semantic` extra.

```bash
uv pip install -e ".[graph,datasets]" pandas
# Dense/hybrid: uv pip install -e ".[graph,datasets,semantic]" pandas

git clone https://github.com/zhangfw123/CORE-Bench-Eval.git /tmp/CORE-Bench-Eval
git -C /tmp/CORE-Bench-Eval checkout 5d00cdcaade77796fa6e5ac4852404002029c00a
hf download zhangfw123/CORE-Bench --repo-type dataset \
  --revision 23da5d97a8a4495d4bd6d5616aa86d5a12a60699 --local-dir /tmp/core-data
```

## Run retrieval

Pass a repository directory containing `corpus.jsonl`, `queries.jsonl` and
`qrels.jsonl`, its task-group parent, or a complete `LEVEL-2` or `LEVEL-3`
directory. Keep the released level/task/repository layout.

```bash
python -m codenib.eval.benchmarks.core_bench run \
  --data /tmp/core-data/data/LEVEL-2/SWE-bench_Verified/psf__requests \
  --upstream /tmp/CORE-Bench-Eval --method bm25 --limit 3 \
  --output results/core-smoke
```

`--limit` selects queries in release order within each repository.
`--query-id` selects explicit IDs in a single repository. Dense indexing encodes
the selected queries' eligible candidates; BM25 uses the full corpus for its
document-frequency statistics.

Use `--method dense --device cuda` for local CodeRankEmbed or `--method hybrid`
for BM25/dense fusion. To use Jina's hosted embeddings instead, set
`JINA_API_KEY` and select its endpoint:

```bash
python -m codenib.eval.benchmarks.core_bench run \
  --data /tmp/core-data/data/LEVEL-2/SWE-bench_Verified/psf__requests \
  --upstream /tmp/CORE-Bench-Eval --method dense --limit 3 \
  --embedding-provider openai --model jina-code-embeddings-0.5b \
  --embedding-base-url https://api.jina.ai/v1 \
  --embedding-api-key-env JINA_API_KEY --embedding-dimension 896 \
  --embedding-batch-size 8 --output results/core-api-smoke
```

`openai` selects the API protocol; the supplied endpoint selects the service.
Set the API batch size and request timing to fit your provider's quota.

## Read and collect results

`scores.json` contains official binary-relevance metrics and the query selection.
Group means average all selected query rows. Prediction files retain the
released query/document IDs.

The `official/` directory contains `<task>_test_<repository>_predictions.json`
files for the upstream collector's `--model_dirs` option. Complete a task group
before collecting it against the full release; the collector expects a file
for every repository in that group.

Score saved predictions with the same selection:

```bash
python -m codenib.eval.benchmarks.core_bench evaluate \
  --data /tmp/core-data/data/LEVEL-2/SWE-bench_Verified/psf__requests \
  --upstream /tmp/CORE-Bench-Eval --limit 3 \
  --predictions results/core-smoke/official --output results/core-rescore
```

Pass the saved `official/` directory to `--predictions`. Add `--resume` with the
same output, data selection and model settings after an interruption. Completed
queries and encoding batches are reused; each output has one writer. Keep the
output directory with its adjacent `.run.json` identity receipt. API batch size
can be adjusted when resuming.

??? note "Evaluator and development reference"

    The default local encoder is `nomic-ai/CodeRankEmbed` at revision
    `3c4b60807d71f79b43f3c4363786d9493691f8b1`. Reports record the embedding model,
    provider, dimensions and API endpoint. Keep these settings with comparisons.

    Missing predictions remain in the selected-query denominator. The official
    collector uses binary relevance metrics; graded MTEB NDCG has a different
    scoring contract. A limited selection measures that selection.

    ```bash
    python -m pytest test/dataset/test_core_bench_dataset.py test/eval/test_core_bench.py -q
    CORE_BENCH_CHECKOUT=/tmp/CORE-Bench-Eval \
      python -m pytest test/eval/test_core_bench.py -m integration -q
    ```
