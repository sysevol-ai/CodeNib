# Agent Retrieval Bench

[Agent Retrieval Bench](https://github.com/eyuansu62/agent-retrieval-bench)
(ARB) asks a retriever to rank the files a coding agent needs next for four
workflow signals. `codenib-arb` runs six CodeNib retrieval methods on all four
positive tasks and scores their rankings with ARB's own evaluator. It can also
score file rankings produced by any other tool.

| Release | Task | Samples |
| --- | --- | ---: |
| `v2_code2test` | Find tests for a code change | 106 |
| `v2_comment2context` | Find context for a review comment | 80 |
| `v2_trace2code` | Find code implicated by a failure trace | 101 |
| `v2_edit2ripple` | Find files affected by an anchor edit | 58 |
| **Total** | | **345** |

Selective (abstention) evaluation, line and block scores, and interactive
agent trajectories are not covered. No method calls a model API.


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
| Evidence | [Measured](#results); six methods on all 345 samples |
| Upstream | <https://github.com/eyuansu62/agent-retrieval-bench> |
<!-- END CODENIB_SUPPORT_MATRIX:card:arb -->

## Results

On all 345 positive samples, a dense ranking reranked by a cross-encoder puts
gold files earliest on every metric. `rerank` rescores the top 50 files of a
dense run with Qwen3-Reranker-4B; over the Qwen3-Embedding-4B ranking it
reaches Recall@20 0.80 and BCY@8k 0.56. Without reranking, the 4B embedding
model leads. CodeNib's BM25 now matches ARB's own BM25 baseline.

| Method | Source | Recall@5 | Recall@20 | MRR | BCY@8k |
| --- | --- | ---: | ---: | ---: | ---: |
| `rerank` over `dense` 4B | Released corpus | 0.5149 | 0.8006 | 0.3293 | 0.5590 |
| `rerank` over `dense` 0.6B | Released corpus | 0.4983 | 0.7836 | 0.3190 | 0.5443 |
| `dense` 4B | Released corpus | 0.3984 | 0.7248 | 0.2827 | 0.4356 |
| `hybrid` (0.6B) | Released corpus | 0.3324 | 0.6925 | 0.2447 | 0.3476 |
| `dense` 0.6B | Released corpus | 0.3884 | 0.6841 | 0.2774 | 0.4111 |
| `bm25` | Released corpus | 0.1923 | 0.4490 | 0.1553 | 0.2191 |
| `zoekt` | Released corpus | 0.2498 | 0.4400 | 0.1605 | 0.2569 |
| `graph_expansion` | Full source | 0.1923 | 0.4890 | 0.1617 | 0.2227 |
| ARB BM25 (control) | Released corpus | 0.1899 | 0.4452 | 0.1520 | 0.2051 |

Each row is one run of every sample with no failures; scores are means over all
345 samples. 0.6B and 4B name the Qwen3-Embedding model behind the dense
ranking; both ran with the model's query instruction and a 2,048-token limit,
as described [below](#retrieval-methods). `hybrid` fuses BM25 with the 0.6B
ranking. The control row reruns ARB's `eval-baseline --ranker bm25` on the same
machine and reproduces ARB's published BM25 row. Graph expansion reads more
than the released corpus, so it is a separate condition rather than a peer of
the other rows.

Reranking raises Recall@20 and BCY@8k over either dense ranking. After
reranking, the larger embedding model adds only 0.017 Recall@20, within noise,
so the 0.6B ranking with the reranker gives nearly the same result in under
half the embedding build time. Fusing BM25 into the 0.6B ranking does not raise Recall@20
significantly and lowers BCY@8k. Paired differences, with 95% bootstrap
intervals over repositories:

| Comparison | Recall@20 | MRR | BCY@8k |
| --- | --- | --- | --- |
| `rerank` over 0.6B − `dense` 0.6B | +0.100 (+0.065 to +0.131) | +0.042 (−0.015 to +0.087) | +0.133 (+0.055 to +0.204) |
| `rerank` over 4B − `dense` 4B | +0.076 (+0.042 to +0.120) | +0.047 (+0.006 to +0.079) | +0.123 (+0.037 to +0.202) |
| `rerank` over 4B − `rerank` over 0.6B | +0.017 (−0.005 to +0.036) | +0.010 (−0.027 to +0.036) | +0.015 (−0.035 to +0.047) |
| `dense` 4B − `dense` 0.6B | +0.041 (−0.002 to +0.074) | +0.005 (−0.033 to +0.040) | +0.024 (−0.037 to +0.071) |
| `hybrid` − `dense` 0.6B | +0.008 (−0.028 to +0.044) | −0.033 (−0.078 to +0.004) | −0.064 (−0.090 to −0.019) |

Recall@20 by task:

| Method | code2test (106) | comment2context (80) | trace2code (101) | edit2ripple (58) |
| --- | ---: | ---: | ---: | ---: |
| `rerank` over `dense` 4B | 0.835 | 0.663 | 0.926 | 0.711 |
| `rerank` over `dense` 0.6B | 0.797 | 0.694 | 0.896 | 0.687 |
| `dense` 4B | 0.780 | 0.579 | 0.799 | 0.697 |
| `hybrid` (0.6B) | 0.659 | 0.662 | 0.767 | 0.664 |
| `dense` 0.6B | 0.726 | 0.571 | 0.744 | 0.658 |
| `bm25` | 0.286 | 0.533 | 0.541 | 0.470 |
| `zoekt` | 0.304 | 0.325 | 0.607 | 0.556 |
| `graph_expansion` | 0.299 | 0.544 | 0.620 | 0.532 |

Earlier runs on these samples informed three settings: the dense token limit
and query instruction, compared on all samples, and rerank's fusion with the
base order rather than the reranker's order alone, chosen on the dev half.
Nothing else was tuned on ARB. Recall@20 / MRR on the dev and held-out halves
(164 and 181 samples, defined [below](#metric-contract)):

| Method | Dev | Held-out |
| --- | ---: | ---: |
| `rerank` over `dense` 4B | 0.8057 / 0.3507 | 0.7960 / 0.3099 |
| `rerank` over `dense` 0.6B | 0.7876 / 0.3280 | 0.7799 / 0.3108 |
| `dense` 4B | 0.7193 / 0.2805 | 0.7297 / 0.2848 |
| `hybrid` (0.6B) | 0.7133 / 0.2549 | 0.6736 / 0.2354 |
| `dense` 0.6B | 0.6728 / 0.2793 | 0.6943 / 0.2757 |
| `bm25` | 0.4776 / 0.1665 | 0.4231 / 0.1452 |
| `zoekt` | 0.4542 / 0.1589 | 0.4273 / 0.1619 |
| `graph_expansion` | 0.5139 / 0.1729 | 0.4664 / 0.1515 |

On the held-out half alone, reranking still raises Recall@20 over either dense
ranking (+0.086 over 0.6B, +0.066 over 4B, both intervals above zero), and the
4B base still adds 0.016 after reranking (−0.009 to +0.032).

Graph expansion built a code graph for 185 of the 218 snapshots, covering 279
samples. Seventeen transformers snapshots and six diffusers snapshots hit
CodeNib's 600-second scip-python limit. The limit is wall-clock time, so
coverage depends on host load: five of those diffusers snapshots finished
within it in the earlier run, when the host was less busy. The Java indexer
produced no graph for five spring-boot snapshots and one mockito snapshot; two
scrapy, one gin, and one numpy snapshot also produced none. Those samples rank
by BM25. On the 279 samples with a graph, expansion raised Recall@20 by 0.049
against BM25 (95% bootstrap interval −0.017 to +0.094), MRR by 0.008 (−0.001
to +0.014), and BCY@8k by 0.004 (−0.014 to +0.030). Almost all of the Recall@20
gain is in gin, etcd, and caddy, Go repositories whose graphs now include test
files; in the earlier run, before they did, expansion lowered Recall@20 by
0.047. Elsewhere, adding 15 reference neighbors after five BM25 seeds still
displaces better BM25 candidates about as often as it finds new gold files;
deciding when to follow references is left to an interactive agent.

Costs. Cold builds are the total over all 218 corpus snapshots; the largest
index is the biggest single snapshot; query times are per sample:

| Method | Cold builds, total | Largest index | Query median / p95 | Tool calls per sample |
| --- | ---: | --- | --- | ---: |
| `rerank` (either base) | 2 s, loading released text | None; scores 50 of up to 11k files | 7.6 s / 14.5 s | 1, plus the base run's 1 |
| `dense` 4B | 8,266 s | 162k vectors, 1.66 GB, in memory | 0.15 s / 1.83 s | 1 |
| `dense` 0.6B | 3,892 s | 162k vectors, 665 MB, in memory | 0.15 s / 2.01 s | 1 |
| `hybrid` (0.6B) | 4,489 s | BM25 and 0.6B indexes | 0.76 s / 14.74 s | 2 |
| `bm25` | 710 s | 162k chunks, 14.3M tokens, in memory | 0.44 s / 12.04 s | 1 |
| `zoekt` | 1,409 s | 276 MB on disk (9.2 GB for all snapshots) | 0.67 s / 2.32 s | 14.3 |
| `graph_expansion` | 25,399 s, plus 613 s fetching source | graph of 160k vertices, 343k reference edges | 0.46 s / 14.02 s | 1.8 |

A reranked run adds its stage's cost to the base run's. Dense builds embed each
distinct chunk text once per run: 0.90M of the 6.2M released chunks were
embedded and the rest reused, so a snapshot's cost depends on which snapshots
of its repository came before it. No method made a model API call; the
embedding models and the reranker ran locally.

The runs used tag [`arb/2026-10-24/methods`](https://github.com/sysevol-ai/CodeNib/tree/arb/2026-10-24/methods)
on one host (Intel Xeon Gold 5416S, 64 cores, one H100 shared with other jobs),
with CPU and GPU methods running at the same time, so treat timings as
indicative. The [results receipt](../assets/arb_full.json) holds every
method's configuration, per-sample metrics, ranking depths, and costs; the
full rankings are published at [`sysevol-ai/codenib-arb-rankings`](https://huggingface.co/datasets/sysevol-ai/codenib-arb-rankings/tree/9ac10c350738533b5444cf4e68ac9a3b83531d24) with checksums for offline
rescoring.

ARB's [community results](https://github.com/eyuansu62/agent-retrieval-bench/blob/main/docs/community_results.md#codenib)
list CodeNib's earlier five-method run (tag `arb/2026-10-22/methods`), which
its maintainers rescored independently; that run cut dense queries at 512
tokens and predates the BM25 tokenizer fix. An earlier eight-sample
compatibility check is kept in the [smoke receipt](../assets/arb_smoke.json).

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

The evaluator runs in an isolated subprocess against a clean ARB checkout,
rejecting hidden Git index flags as well as ordinary local changes. The
checkout can be the pinned revision or a later one that contains it, provided
every file the scorer imports is unchanged from the pinned revision; changes to
ARB's other evaluators do not matter. Results record both the checkout's
revision and the pinned scoring revision. The evaluator needs no installation
in CodeNib's environment. Optionally set
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

## Retrieval methods

`codenib-arb run --method` chooses how files are ranked. Every method reads
only the released query, indexes each corpus snapshot once, and is scored by
the same pinned evaluator. All methods except `graph_expansion` read only the
released corpus; `graph_expansion` also reads the full source, a separate
condition described below.

| Method | Index per snapshot | Ranking |
| --- | --- | --- |
| `bm25` (default) | CodeNib BM25 over every released file and symbol chunk | Every chunk ranked; each file takes the rank of its first chunk |
| `dense` | Exact inner-product search over Qwen3-Embedding vectors of every chunk (0.6B unless another model is chosen) | Same first-hit file ranking |
| `hybrid` | Both indexes above | CodeNib's file-level weighted RRF (k = 60) of the two file rankings |
| `zoekt` | Zoekt trigram index of every file and symbol chunk | One Zoekt query per code-like term in the query; files ordered by matched terms, then first match |
| `graph_expansion` | BM25 as above, plus a CodeNib code graph of the full source | The top five BM25 files, then up to 15 files connected to them by code references, then the rest in BM25 order |
| `rerank` | None; it reads a completed run | The base run's top 50 files rescored by Qwen3-Reranker-4B and fused with the base order by file-level RRF (k = 60); the other files keep the base order |

By default the dense model uses no query or document instruction, as ARB's own
embedding baseline does, and applies a 512-token limit to queries as well as
chunks. With the pinned tokenizer that cuts 124 of the 345 queries: 15 of 106
code2test, 2 of 80 comment2context, 52 of 101 trace2code, and 55 of 58
edit2ripple. The published results instead pass `--max-seq-length 2048
--embedding-batch-size 16 --query-prompt registry`, which prepends the
model's registered query instruction; at 2,048 tokens only 12 of the 58
edit2ripple queries are cut. BM25 and graph expansion read the full query, and
`hybrid` cuts it only in its dense branch. On a GPU the model runs in bfloat16. `--embedding-model` with
`--embedding-revision` (a 40-character commit) selects another
SentenceTransformers model, and `--max-seq-length` and `--embedding-batch-size`
change the token limit and batch size; each run records them. `--query-prompt registry` prepends the query instruction that CodeNib registers for the model, given as text or as the name of one of the model's own prompts, and the run records it. Models registered with a document instruction as well are refused, because chunks are embedded without one. Each distinct chunk text is embedded once per run, because
snapshots of the same repository share most files, and `index_size` records
how many chunks a build encoded and how many it reused. Dense methods need the
`semantic` extra; a missing extra stops the run before any case.

`rerank` is a second stage over a completed run. `--rerank-base` names that
run's output directory; it must have written its final report and ranked the
same prepared cases with released-corpus access, and a run still in progress is
refused. The model scores each of the top files as its path and released text,
truncated with the query to 8,192 tokens, under CodeNib's default reranker
instruction; another cross-encoder keeps its own length limit and takes no
instruction. It never sees gold labels. A file it cannot score within GPU
memory fails the case rather than scoring zero. `--rerank-model` with
`--rerank-revision`, `--rerank-top-k`, `--rerank-max-length`, and
`--rerank-batch-size` change the model and its limits. The receipt records them
with digests of the base run's receipt and predictions. The output holds
exactly the base ranking's files, duplicates removed. `tool_calls` counts the
stage's one scoring call, and `base_tool_calls` the base run's, recorded even
when the base failed that case and left empty when the base did not record it.
It needs the `semantic` extra.

The Zoekt method takes its terms from the query's string values: file names,
backticked spans, and identifiers written with `_`, camelCase, or capitals, up
to 32 per sample. Each term is one tool call. Files that match no term are not
ranked, so its rankings can be shorter than the corpus. It needs `make
zoekt-tool` or `--zoekt-bin-dir`.

### Full-source graph expansion

A code graph records which symbols reference which, as resolved by the
language's compiler-grade indexer (SCIP, or a language server). The indexer
needs the real repository, so `graph_expansion` fetches each sample's
`base_commit` into `--workspace` and builds the graph from that checkout:

```bash
python -m pip install -e '.[datasets,graph]'
make scip    # SCIP indexers for Python, TypeScript, Go, Rust, Java, and more
codenib-arb run --method graph_expansion --workspace /tmp/arb-src \
  --prepared /tmp/arb-prepared.json --upstream /tmp/arb --output /tmp/arb-graph
```

The rules for this condition follow the
[ARB maintainers' reply](https://github.com/eyuansu62/agent-retrieval-bench/issues/2#issuecomment-6334640769):

- The checkout is a depth-one fetch of `base_commit`, so neither the resolving
  change nor any later commit is in its object store. Every use resets it to
  that commit and removes untracked and ignored files; hidden Git index flags
  are rejected.
- Only the released query reaches retrieval; gold and annotations never do.
- Graph neighbors outside the released candidate set are dropped and counted
  in `removed_non_candidate_paths`. Scores and canonical BCY use released text.

The graph covers each language holding at least 10% of the snapshot's source
files and is built in a child process with a 30-minute limit. It never installs
dependencies or runs project setup, but the Rust and Java indexers run the
project's own build tooling. If no graph can be built, the sample is ranked by
BM25 and recorded with `graph_used: false`. Fetching source is reported as
`source_fetch_seconds`, apart from the build.

Current graph loading also rejects cyclic source containment and records the
failure before using BM25. A compiler provider can conflate nested definitions
([#871](https://github.com/sysevol-ai/CodeNib/issues/871)); accepting its artifact
does not establish correct symbol identity. The frozen scores above predate
this integrity gate and have not been recomputed.

Rankings produced elsewhere from the full source can be scored under the same
rule with `codenib-arb evaluate --source-access full-source`.

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
per-task and per-split summaries, failure IDs, and per-case errors. Commands
exit nonzero on failure. Always report coverage and the selected case set with
a score; truncated external rankings can lower MRR relative to a complete
ranking.

The split assigns each sample to `dev` when the first byte of
`sha256(sample_id)` is even and to `heldout` otherwise: 164 and 181 of the 345
samples. ARB community results use the same rule, so a method tuned on `dev`
can report `heldout` separately.

## Cost accounting

A native run separates the cost of building an index from the cost of using
it. Each corpus snapshot is indexed once, and samples that share the snapshot
reuse that index. The 345 samples use 218 distinct corpora.

| Quantity | Where it is recorded |
| --- | --- |
| Cold index build | `index_build_seconds` and `index_size` on the sample that triggered the build; `corpus_load_seconds` and, for `graph_expansion`, `source_fetch_seconds` separately |
| Query latency | `query_seconds` on every sample |
| Tool calls | `tool_calls` per sample: one search for `bm25` and `dense`, two for `hybrid` and for `graph_expansion` with a graph, one per term for `zoekt`, one scoring call for `rerank`, which also records the base run's as `base_tool_calls` |
| Model usage | No API calls (`model_calls` is 0). `dense` and `hybrid` run a local embedding model; `retrieval_usage.embedding` records its revision, loads, and encoded and reused chunks. `rerank` runs a local cross-encoder recorded in its method configuration |
| Context budget | Not applicable: every method returns a file ranking, and BCY packs released text at its fixed budgets |

`index_size` depends on the method: BM25 documents, tokens, vocabulary, and
indexed text bytes; dense documents, dimension, and vector bytes; the Zoekt
index's documents and bytes on disk; and the code graph's vertices, edges,
reference edges, and build time.

`results.json` summarizes these under `retrieval_usage` (count, total, median,
p95, and maximum) and records the CPU, core count, and peak memory under
`environment`. A resumed run keeps the timings recorded before the
interruption, and its peak memory covers only the final process. Offline
rescoring reports no cost, because it cannot observe how external rankings
were produced.

## Extending retrieval experiments

The imported-ranking boundary can score additional methods on the same cases.
These routes are candidates for later adapters:

| Existing CodeNib capability | Proposed comparison |
| --- | --- |
| Zoekt and graph tools inside an agent loop | Multi-round exact symbol, path, trace, and relationship search |

Before comparing agent strategies, fix the model, tool permissions, query
budget, and final context budget. Report index build time and size separately
from query latency and model cost.

Full-source results are a separate condition from released-corpus results;
report them in separate rows, as the [graph expansion rules](#full-source-graph-expansion)
describe.

### Indexed tools in ARB's closed-tool loop

ARB's `indexed_interactive` mode lets a policy call index lookups next to
`list_dir`, `grep`, `read_file`, and `submit`, under the same budgets. CodeNib
supplies the lookups as an ARB tool provider. The mode was added in
[agent-retrieval-bench#12](https://github.com/eyuansu62/agent-retrieval-bench/pull/12)
(`ae4091a2`), after the revision pinned above for static scores, so run it
from a checkout that includes that commit:

```bash
arb eval-closed-tool-llm ... --mode indexed_interactive \
  --indexed-tools symbol_search,references \
  --tool-provider codenib.eval.benchmarks.agent_retrieval_bench_tools:CodeNibToolProvider \
  --tool-provider-option workspace=/path/to/checkouts \
  --tool-provider-option cache=/path/to/tool-graphs
```

- `symbol_search(symbol)` returns definitions with that name. A dotted or
  `::` name matches the end of the qualified name.
- `references(path, symbol)` returns symbols in other files that reference, or
  are referenced by, that symbol or the symbols it contains, ordered by the
  number of connecting references.

Both return paths with 1-based line ranges, never file content; a reference
made at file scope reports the line where it occurs. Imports and other
reference-only names are not returned as definitions, and parameter lists such
as `run(String)` are ignored when matching names. The graph is built as for
[full-source graph expansion](#full-source-graph-expansion), so these runs are
full-source runs. Hits are limited to released files.

Graphs are cached per snapshot under `cache/graphs`, apart from the checkouts
under `workspace`; `workspace` must be neither `cache` nor `cache/graphs`. A
checkout never replaces a directory it did not publish, such as a graph or
another Git checkout, nor a graph a checkout, so roots that overlap across
providers fail instead of deleting each other's data, and neither root may lie
inside a published graph or checkout. A later run reuses a build, and reports
its original build and checkout-fetch times with `cached: true`, only when the
build matches: the CodeNib sources, Python and package versions, each graph
backend's command and executable, and the graph settings. A backend that its
launcher loads indirectly, such as `python -m`, `npx`, or `rustup run`, is
identified only by its command, so use a new cache after upgrading one. A
failed or partial build is retried on the next run, and a retry that covers
fewer languages keeps the earlier graph, with the retry's cost and, under
`retry`, its outcome in the status; set `--tool-provider-option
retry_failed=false` to keep failures, so that arms sharing a prebuilt cache see
the same index availability.

Each snapshot's status reports `released_source_files`, the released files in
the graph's languages, and `covered_source_files`, how many of them the graph
contains. Coverage is incomplete in known cases:

- Java snapshots build no graph, because project preparation is disabled;
- a Go repository builds no graph without a root `go.mod`, and a nested Go
  module that fails to index is left out (see
  [the Go provider notes](../scip_index.md#language-provider-map)).

A snapshot without a graph offers the policy only the minimal tools. ARB's
[indexed tools guide](https://github.com/eyuansu62/agent-retrieval-bench/blob/main/docs/adapters/indexed_tools.md)
describes the provider interface and what the evaluator enforces.
