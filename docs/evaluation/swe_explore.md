# SWE-Explore Compatibility Validation

This validation runs CodeNib's native repository explorer against the
published data and official metrics from
[SWE-Explore](https://github.com/Qiushao-E/SWE-Explore-Bench). The reported run
is the explicit `bm25` compatibility control, not CodeNib's native multi-view
`auto` policy. It does not measure patch generation or SWE-bench issue
resolution.


<!-- BEGIN CODENIB_SUPPORT_MATRIX:card:swe_explore -->
| Benchmark card | |
| --- | --- |
| Layer | L1 Localization: the method's final ranked files, symbols, code blocks, or line regions |
| Origin | External |
| Task | Rank the source regions an issue needs |
| Ground truth | Released core and optional line regions |
| Granularity | Line region, file |
| Languages | Seven languages in the fixed 20-case set |
| Size | 848 released rows; a fixed 20-case validation set |
| Pinned revisions | Benchmark `3c12dc5a`, dataset `bdb0ae45` |
| Scorer | Official 17 metrics, reproduced exactly |
| Setup | Released JSONL and clean checkouts at joined base commits |
| CodeNib entry point | `codenib-swe-explore-benchmark` |
| Evidence | [Pinned](swe_explore.md); scorer agrees with the official evaluator |
| Upstream | <https://github.com/Qiushao-E/SWE-Explore-Bench> |
<!-- END CODENIB_SUPPORT_MATRIX:card:swe_explore -->

## Pinned Inputs

| Input | Revision |
| --- | --- |
| [SWE-Explore code](https://github.com/Qiushao-E/SWE-Explore-Bench) | `3c12dc5a551937038afcbdb6eb6bbf19f3ddd8c1` |
| SWE-Explore release | `bdb0ae45d7c337d9e1dc3ebfe2a0af6bc7c1fbd9` |
| Public benchmark SHA-256 | `dc4f114ececd0bfb987361c26ae5e2440456e2cccb36adfccb09ea5385aec202` |
| SWE-bench Verified | `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` |
| SWE-bench Multilingual | `2b7aced941b4873e9cad3e76abbae93f481d1beb` |
| SWE-bench Pro | `2dd05cab1572ce1d59fdc699b386692ff8e0bd29` |

The released SWE-Explore rows omit `problem_statement` and `base_commit`.
CodeNib joins all 848 rows to the three source datasets above. The join is
complete: 451 Verified, 182 Multilingual, and 215 Pro rows resolve uniquely.

`codenib-swe-explore-benchmark` reproduces this run; the command is in
[Harnesses and metrics](harnesses.md#one-shot-retrieval). By default it
rejects a `--bench` file whose SHA-256 differs from the pinned public release;
`--bench-sha256` overrides the expected digest. `--planning-budget` and
`--retrieval-level` configure the planner and the dense chunk level.

## Run It From Python

`CodeNibSWEExploreExplorer` wraps CodeNib's `RepositoryContextExplorer`, which
plans the route, retrieves, expands over the graph, reranks, and validates the
source. It converts the resulting 0-based evidence into the benchmark's
repository-relative, 1-based inclusive `ContextRegion` records:

```python
from codenib.integrations.swe_explore import CodeNibSWEExploreExplorer

explorer = CodeNibSWEExploreExplorer.from_repository(
    "/path/to/checkout",
    policy="auto",
)
results = explorer.explore(
    instance_id="org__repo-123",
    query="issue text",
    top_k=20,
)
```

`auto` plans against the views the index advertises (BM25, vector, and symbol
graph) and loads only the route it picks. The fixed comparison policies are
`bm25`, `dense`, `hybrid`, `hybrid_rerank`, and `graph`. Building the index is a
separate step; the strict runner builds and records exactly the views the
chosen policy needs.

Two cutoffs stay separate. `top_k` limits how many ranked regions the explorer
returns, while Recall and nDCG at 100, 300, or 500 cap the accumulated source
lines in the evaluator. The upstream SWE-Explore runner can also call CodeNib
with `--explorers codenib`.

## Fixed Case Set

The checked-in [case set](../assets/swe_explore_cases.json) fixes 20 cases before
execution: two Python cases and three each for Go, Rust, TypeScript,
JavaScript, C, and C++. Every repository is a clean detached checkout at the
joined `base_commit`. The audit rejects tracked changes plus untracked or
gitignored source files that CodeNib's chunker would index. The runner also
requires the default BM25 artifact profile and records it per case. The current
runner can separately execute `dense`, `hybrid`, `hybrid_rerank`, `graph`, or
`auto`; those arms require new measurements and are not backfilled here.

Coverage always uses all requested cases. Quality metrics are conditioned on
successful cases because zero-imputation would make lower-is-better noise
rates look artificially good; the report records that metric denominator and
lists every failed case separately.

## Contract Results

| Gate | Result |
| --- | ---: |
| Snapshot revision and cleanliness | 20/20 |
| BM25 view construction | 20/20 |
| Selective BM25-only loading (`--policy bm25`) | 20/20 |
| Ranked region query | 20/20 |
| Official runner completion | 20/20 |
| Generated differential metric cells | 4,250/4,250 |
| Real-output official metric cells | 1,020/1,020 |

The generated differential test covers 250 region sets across all 17 official
metrics. The real-output check re-scores 20 cases at region cutoffs 5, 10, and
20 with the pinned official `ExploreEvaluator`.

## Timing

These machine-local measurements separate construction, runtime loading, and
query serving.

| Stage | Mean | Median | Maximum |
| --- | ---: | ---: | ---: |
| BM25 construction | 1.05 s | 0.81 s | 3.08 s |
| BM25 view load | 94 ms | 64 ms | 238 ms |
| Top-20 query | 104 ms | 67 ms | 343 ms |

## Localization Summary

The table reports the CodeNib BM25 control only. Values are means over the fixed 20
cases and characterize this compatibility subset rather than a repository
population.

| Regions | Hit file | Hit region | Line recall | Context efficiency | nDCG@300 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | 0.265 | 0.181 | 0.030 | 0.263 | 0.342 |
| 10 | 0.390 | 0.245 | 0.124 | 0.287 | 0.265 |
| 20 | 0.482 | 0.317 | 0.162 | 0.245 | 0.207 |

The official repository's BM25 arm was also executed on all 20 cases as a
runner-level control. It uses a Python/document-oriented source-extension
allowlist, whereas this subset intentionally contains seven languages.
Consequently, its cross-language aggregate is not an algorithmically fair
baseline and is not used for a quality claim.

## Dataset Audit

All 3,992 core regions are well-formed. Optional trajectory labels contain 455
reversed ranges and 8 ranges ending at zero. The pinned official evaluator
treats these as empty or non-overlapping. CodeNib preserves that behavior and
reports it rather than silently repairing labels, which keeps scores exactly
comparable to upstream.
