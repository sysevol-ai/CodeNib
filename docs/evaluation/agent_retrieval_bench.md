# Agent Retrieval Bench

[Agent Retrieval Bench](https://github.com/eyuansu62/agent-retrieval-bench)
(ARB) asks a retriever to rank the files a coding agent needs next for four
workflow signals. `codenib-arb` runs five CodeNib retrieval methods on all four
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
| Evidence | [Measured](#results); five methods on all 345 samples |
| Upstream | <https://github.com/eyuansu62/agent-retrieval-bench> |
<!-- END CODENIB_SUPPORT_MATRIX:card:arb -->

## Results

On all 345 positive samples, dense retrieval puts gold files earliest (the best
Recall@5, MRR, and BCY), and fusing it with BM25 raises Recall@20 but lowers MRR. CodeNib's BM25 trails ARB's
own BM25 baseline; the cause is a tokenizer gap
([#856](https://github.com/sysevol-ai/CodeNib/issues/856)), not the protocol.

| Method | Source | Recall@5 | Recall@20 | MRR | BCY@8k |
| --- | --- | ---: | ---: | ---: | ---: |
| `dense` | Released corpus | 0.2940 | 0.5736 | 0.2425 | 0.3355 |
| `hybrid` | Released corpus | 0.2804 | 0.5972 | 0.2144 | 0.3101 |
| `zoekt` | Released corpus | 0.2498 | 0.4400 | 0.1605 | 0.2569 |
| `bm25` | Released corpus | 0.1543 | 0.3889 | 0.1211 | 0.1659 |
| `graph_expansion` | Full source | 0.1543 | 0.3498 | 0.1197 | 0.1659 |
| ARB BM25 (control) | Released corpus | 0.1899 | 0.4452 | 0.1520 | 0.2051 |

Each row is one run of every sample with no failures; scores are means over all
345 samples. Graph expansion ran as two concurrent shards of the same commit on
disjoint snapshots, scored together with `codenib-arb evaluate --source-access
full-source`. The control row reruns ARB's `eval-baseline --ranker bm25` on the
same machine and reproduces ARB's published BM25 row. Graph expansion reads more
than the released corpus, so it is a separate condition rather than a peer of
the other rows.

Recall@20 by task:

| Method | code2test (106) | comment2context (80) | trace2code (101) | edit2ripple (58) |
| --- | ---: | ---: | ---: | ---: |
| `dense` | 0.690 | 0.550 | 0.480 | 0.556 |
| `hybrid` | 0.607 | 0.627 | 0.561 | 0.601 |
| `zoekt` | 0.304 | 0.325 | 0.607 | 0.556 |
| `bm25` | 0.299 | 0.529 | 0.391 | 0.356 |
| `graph_expansion` | 0.209 | 0.506 | 0.416 | 0.276 |

No method was tuned on ARB. Recall@20 / MRR on the dev and held-out halves
(164 and 181 samples, defined [below](#metric-contract)):

| Method | Dev | Held-out |
| --- | ---: | ---: |
| `dense` | 0.5526 / 0.2340 | 0.5925 / 0.2502 |
| `hybrid` | 0.6105 / 0.2342 | 0.5852 / 0.1964 |
| `zoekt` | 0.4542 / 0.1589 | 0.4273 / 0.1619 |
| `bm25` | 0.4233 / 0.1359 | 0.3577 / 0.1077 |
| `graph_expansion` | 0.3704 / 0.1329 | 0.3310 / 0.1077 |

Graph expansion built a code graph for 190 of the 218 snapshots, covering 290
samples. Seventeen transformers snapshots and one diffusers snapshot hit
CodeNib's 600-second scip-python limit; the Java indexer produced no graph for
five spring-boot snapshots and one mockito snapshot; two scrapy, one gin, and
one numpy snapshot also produced none. Those samples rank by BM25. On the 290 samples with a graph, it
lowered Recall@20 by 0.047 against BM25 (95% bootstrap interval −0.085 to
−0.007). MRR fell by 0.002 (−0.005 to +0.002), and BCY@8k showed no net change
(−0.016 to +0.014). It helped trace2code (0.416 against
0.391) and hurt code2test, partly because the Go graphs in this run omitted
test files.
Adding 15 reference neighbors after five BM25 seeds displaces better BM25
candidates; deciding when to follow references is left to an interactive
agent.

Costs. Cold builds are the total over all 218 corpus snapshots; the largest
index is the biggest single snapshot; query times are per sample:

| Method | Cold builds, total | Largest index | Query median / p95 | Tool calls per sample |
| --- | ---: | --- | --- | ---: |
| `bm25` | 437 s | 160k chunks, 13.4M tokens, in memory | 0.40 s / 11.97 s | 1 |
| `dense` | 4,035 s | 160k vectors, 665 MB, in memory | 0.15 s / 1.76 s | 1 |
| `hybrid` | 4,365 s | both of the above | 0.55 s / 11.96 s | 2 |
| `zoekt` | 1,315 s | 276 MB on disk (9.2 GB for all snapshots) | 0.67 s / 2.37 s | 14.4 |
| `graph_expansion` | 22,541 s, plus 419 s fetching source | graph of 160k vertices, 343k reference edges | 0.33 s / 9.16 s | 1.8 |

Dense builds embed each distinct chunk text once per run: 0.90M of the 6.2M
released chunks were embedded and the rest reused, so a snapshot's cost depends
on which snapshots of its repository came before it. No method made a model
API call; dense and hybrid ran Qwen3-Embedding-0.6B locally.

The runs used tag [`arb/2026-10-22/methods`](https://github.com/sysevol-ai/CodeNib/tree/arb/2026-10-22/methods)
on one host (Intel Xeon Gold 5416S, 64 cores, one H100 shared with other jobs),
with CPU and GPU methods running at the same time, so treat timings as
indicative. The [results receipt](../assets/arb_full.json) holds every
method's configuration, per-sample metrics, ranking depths, and costs; the
full rankings are published at [`sysevol-ai/codenib-arb-rankings`](https://huggingface.co/datasets/sysevol-ai/codenib-arb-rankings/tree/8be160b7258a803bc7e6d21a7f3ef0f981bdd382) with checksums for offline
rescoring.

ARB lists these results, with the rankings and rescoring instructions, under
its [community results](https://github.com/eyuansu62/agent-retrieval-bench/blob/main/docs/community_results.md#codenib);
its maintainers rescored all five methods independently. An earlier
eight-sample compatibility check is kept in the
[smoke receipt](../assets/arb_smoke.json).

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
| `dense` | Exact inner-product search over Qwen3-Embedding-0.6B vectors of every chunk | Same first-hit file ranking |
| `hybrid` | Both indexes above | CodeNib's file-level weighted RRF (k = 60) of the two file rankings |
| `zoekt` | Zoekt trigram index of every file and symbol chunk | One Zoekt query per code-like term in the query; files ordered by matched terms, then first match |
| `graph_expansion` | BM25 as above, plus a CodeNib code graph of the full source | The top five BM25 files, then up to 15 files connected to them by code references, then the rest in BM25 order |

The dense model uses no query or document instruction, as ARB's own embedding
baseline does, and applies a 512-token limit to queries as well as chunks.
With the pinned tokenizer, 124 of the 345 queries are longer and are cut: 15 of
106 code2test, 2 of 80 comment2context, 52 of 101 trace2code, and 55 of 58
edit2ripple. BM25 and graph expansion read the full query, and `hybrid` cuts
it only in its dense branch. On a GPU the model runs in bfloat16. `--embedding-model` with
`--embedding-revision` (a 40-character commit) selects another
SentenceTransformers model, and `--max-seq-length` and `--embedding-batch-size`
change the token limit and batch size; each run records them. Each distinct chunk text is embedded once per run, because
snapshots of the same repository share most files, and `index_size` records
how many chunks a build encoded and how many it reused. Dense methods need the
`semantic` extra; a missing extra stops the run before any case.

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
`source_fetch_seconds`, apart from the build. The results above were
measured when CodeNib's Go graph omitted `_test.go` files, so expansion there
could not propose Go tests; Go graphs now include them.

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
| Tool calls | `tool_calls` per sample: one search for `bm25` and `dense`, two for `hybrid` and for `graph_expansion` with a graph, one per term for `zoekt` |
| Model usage | No API calls (`model_calls` is 0). `dense` and `hybrid` run a local embedding model; `retrieval_usage.embedding` records its revision, loads, and encoded and reused chunks |
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
| Retrieve → rerank | Candidate ordering with a model at fixed budgets |

Before comparing agent strategies, fix the model, tool permissions, query
budget, and final context budget. Report index build time and size separately
from query latency and model cost.

Full-source results are a separate condition from released-corpus results;
report them in separate rows, as the [graph expansion rules](#full-source-graph-expansion)
describe.

### Indexed tools in ARB's closed-tool loop

ARB's `indexed_interactive` mode lets a policy call index lookups next to
`list_dir`, `grep`, `read_file`, and `submit`, under the same budgets. CodeNib
supplies the lookups as an ARB tool provider. The mode is not in the ARB
revision pinned above for static scores; it is proposed in
[agent-retrieval-bench#12](https://github.com/eyuansu62/agent-retrieval-bench/pull/12),
so run it from a checkout that includes that change:

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

Graphs are cached per snapshot under `cache`, which must differ from
`workspace`. A later run reuses a build, and reports its original build and
checkout-fetch times with `cached: true`, only when the CodeNib sources and
graph settings match the ones that built it. A failed build is retried on the
next run; set `--tool-provider-option retry_failed=false` to keep failures, so
that arms sharing a prebuilt cache see the same index availability.

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
