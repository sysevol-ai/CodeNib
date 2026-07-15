<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Dense-Seeded GraphRAG Ablation

## Question

Does CodeMiner's static code graph improve repository-level file localization
over the same dense embedding index, rather than merely replacing dense
candidates with a weaker graph retriever?

The historical ablation could not answer this cleanly. It mixed identifier,
BM25, and embedding seeds, then appended unranked neighbors. A second pilot used
only 30 dense chunks; 22 of 58 development instances had fewer than ten unique
dense files, so graph expansion could improve `files@10` simply by filling empty
output slots.

## Locked Method

The corrected pipeline is deliberately small:

1. Retrieve 300 L2 chunks with `Qwen/Qwen3-Embedding-0.6B`. Every evaluated
   instance supplies at least ten unique dense files.
2. Use only the first ten dense chunks as graph seeds. The full dense pool is
   preserved.
3. Expand one hop over `reference` edges in both directions, interleaving
   callers and callees with at most ten unique neighbors per seed.
4. Score the dense-plus-graph candidate set using vectors already persisted in
   the same embedding index. Duplicate persisted node IDs retain their best
   score before top-k truncation.
5. Fuse unique-file semantic and graph ranks with weighted RRF: semantic weight
   `1.0`, graph weight `0.5`, and `k=60`.

The design follows the general code-graph motivation in
[DraCo](https://aclanthology.org/2024.acl-long.431/) and
[LocAgent](https://aclanthology.org/2025.acl-long.426/), while using the
score-scale-independent rank fusion introduced by
[Cormack et al.](https://doi.org/10.1145/1571941.1572114). This experiment does
not use an LLM or agent loop.

## Protocol

The 100 `codeminer-base-dataset` instances are split by repository, stratified
by language: 58 development instances and 42 held-out test instances. No
repository appears in both partitions. The graph weight was selected once on
development from `{0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0}`. The held-out set was
then evaluated once at `0.5`; no test-set retuning was performed.

Controls use identical queries, embedding model, vector artifacts, and
unique-file metrics:

- `dense`: the strong 300-chunk embedding baseline.
- `dense_graph_semantic`: graph candidate expansion followed only by the same
  embedding score.
- `dense_graph_fusion_w0p5`: the locked structural RRF method.

## Results

| partition | method | files@1 | files@5 | files@10 |
|---|---|---:|---:|---:|
| dev (58) | dense | 19 (32.8%) | 31 (53.4%) | 36 (62.1%) |
| dev (58) | GraphRAG | **20 (34.5%)** | **33 (56.9%)** | **38 (65.5%)** |
| held-out (42) | dense | 15 (35.7%) | **23 (54.8%)** | 31 (73.8%) |
| held-out (42) | GraphRAG | **17 (40.5%)** | 22 (52.4%) | **34 (81.0%)** |
| all (100) | dense | 34 (34.0%) | 54 (54.0%) | 67 (67.0%) |
| all (100) | dense + graph, semantic only | 34 (34.0%) | 54 (54.0%) | 67 (67.0%) |
| all (100) | GraphRAG | **37 (37.0%)** | **55 (55.0%)** | **72 (72.0%)** |

At `files@10`, the locked method has seven gains and two losses over all 100
instances. The held-out contribution is four gains and one loss. Candidate
expansion with semantic reranking alone is exactly equal to dense at every
reported cutoff; the measured gain comes from the structural rank signal, not
from a larger candidate list.

The all-instance `files@10` paired delta is `+5 pp`. A paired instance bootstrap
gives a 95% interval of `[-1 pp, +11 pp]`; the exact two-sided McNemar/binomial
test on the nine discordant pairs gives `p=0.18`. This is a replicated positive
pilot, not yet a statistically significant result. `files@5` improves only one
point overall and regresses on the held-out split, so the current method should
be described as recall-oriented at a ten-file context budget.

## Embedding and Cross-Encoder Matrix

The locked graph configuration was also run over all 100 instances with five
prebuilt embedding indexes. A `Qwen/Qwen3-Reranker-4B` cross-encoder arm scores
up to 50 unique files. Dense and GraphRAG use the same per-instance candidate
budget: the minimum number of eligible files available to both arms, never
below the largest reported cutoff of ten. The observed budget range is 10--50.

Cross-encoder candidates must either carry indexed content or be recoverable
from the dataset row's exact `base_commit`. Generated graph artifacts that do
not exist in that Git tree are excluded before calculating the paired budget
and recorded in the result. This affected seven of the 500 model-instance runs;
all 500 runs remained scoreable and both arms used exactly the recorded budget.

| embedding | Dense @1/5/10 | GraphRAG @1/5/10 | Dense+CE @1/5/10 | GraphRAG+CE @1/5/10 | GraphRAG median | GraphRAG+CE median |
|---|---:|---:|---:|---:|---:|---:|
| SweRank-Small | 36/58/65 | 38/60/65 | 42/61/77 | 44/63/77 | 27.7 ms | 3.78 s |
| Qwen3-0.6B | 34/54/67 | 37/55/72 | 45/67/77 | 44/67/79 | 58.3 ms | 3.55 s |
| Jina-Code-1.5B | 36/65/77 | 40/64/76 | 42/70/80 | 41/69/78 | 85.0 ms | 3.21 s |
| Qwen3-4B | 37/62/78 | 37/69/73 | 46/71/80 | 48/71/82 | 173.5 ms | 4.39 s |
| SweRank-Large | 46/65/78 | 47/72/79 | 52/71/78 | 53/70/77 | 257.6 ms | 6.28 s |

GraphRAG is not a uniform win across embeddings or cutoffs. It improves the
Qwen3-0.6B baseline by `+5 pp` at `files@10`, but Jina loses one point and
Qwen3-4B loses five points at the same cutoff before cross-encoding. The
cross-encoder is also not monotonic: it helps several configurations, but
GraphRAG+CE trails Dense+CE for Jina and trails non-CE GraphRAG at `files@10`
for SweRank-Large. These are model interactions, not evidence for one universal
GraphRAG gain. The full matrix should therefore be shown as a separate
ablation, with the millisecond graph-only and second-scale cross-encoder arms
visually separated.

## Cost

Across all 100 instances, median dense retrieval is `36.3 ms`, reference-graph
expansion is `7.0 ms`, indexed candidate rerank plus fusion is `4.8 ms`, and the
complete GraphRAG pipeline is `57.6 ms` (`p90 = 162.7 ms`). The vector store
reuses the dense stage's query embedding during restricted candidate reranking;
before that reuse, median reranking was `38.3 ms` and total latency was
`96.0 ms`. Ranked files and recall are identical before and after the change.
The median graph expansion adds 37 nodes; its p90 is 78 nodes. These are
warm-query measurements over prebuilt graph and vector artifacts and exclude
index construction.

The dedicated single-column `cm-draw` figure uses mean target-file recall rather
than the stricter all-target instance success rate in the table. Under that
plotting metric, the paired Qwen3-0.6B dense and GraphRAG values are `0.715` and
`0.765`; GraphRAG's mean warm-query latency is `102.8 ms`. The same panel shows
the historical Qwen3-0.6B plus 4B/8B cross-encoder sweeps as contextual points.
Those artifacts rank top-ten chunks rather than ten unique files, so the figure
does not draw a joint Pareto frontier and the controlled claim remains the
equal-budget Dense-versus-GraphRAG comparison above.

## Reproduce

Development sweep:

```bash
python -m scripts.retrieval_ablation.dense_graphrag_benchmark \
  --out /mnt/data/codeminer/results/dense_graphrag_dev.json \
  --partition dev --dense-pool-k 300 --graph-seed-k 10 \
  --neighbors-per-seed 10 --direction both --rerank-mode index \
  --graph-weight 0.25 0.5 0.75 1 1.25 1.5 2 --rrf-k 60
```

Locked held-out run:

```bash
python -m scripts.retrieval_ablation.dense_graphrag_benchmark \
  --out /mnt/data/codeminer/results/dense_graphrag_test.json \
  --partition test --dense-pool-k 300 --graph-seed-k 10 \
  --neighbors-per-seed 10 --direction both --rerank-mode index \
  --graph-weight 0.5 --rrf-k 60
```

Raw results for this run are under `/mnt/data/codeminer/results/` as
`dense_graphrag_dev_reference_rrf_sweep_v2.json`,
`dense_graphrag_test_locked_v1.json`, and
`dense_graphrag_all_locked_v1.json`. The canonical latency-optimized,
ranking-equivalent rerun is `dense_graphrag_all_locked_final.json`.

Full embedding and cross-encoder matrix:

```bash
python -m scripts.retrieval_ablation.run_dense_graphrag_matrix \
  --output-dir /mnt/data/codeminer/results/dense_graphrag_matrix_v4 \
  --partition all --prebuilt-dir /mnt/data/codeminer \
  --cuda-visible-devices 0
```

The matrix directory contains one resumable JSON checkpoint and log per
embedding plus `matrix_manifest.json`. The completed v4 manifest records five
successful cells, 100 scored instances per cell, and no errors.
