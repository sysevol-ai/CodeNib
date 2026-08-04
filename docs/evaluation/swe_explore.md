# SWE-Explore Compatibility Validation

This validation checks whether CodeNib can participate in the official
SWE-Explore explorer and evaluator contracts. It does not measure patch
generation or SWE-bench issue resolution.

## Pinned Inputs

| Input | Revision |
| --- | --- |
| SWE-Explore code | `3c12dc5a551937038afcbdb6eb6bbf19f3ddd8c1` |
| SWE-Explore release | `bdb0ae45d7c337d9e1dc3ebfe2a0af6bc7c1fbd9` |
| SWE-bench Verified | `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` |
| SWE-bench Multilingual | `2b7aced941b4873e9cad3e76abbae93f481d1beb` |
| SWE-bench Pro | `2dd05cab1572ce1d59fdc699b386692ff8e0bd29` |

The released SWE-Explore rows omit `problem_statement` and `base_commit`.
CodeNib joins all 848 rows to the three source datasets above. The join is
complete: 451 Verified, 182 Multilingual, and 215 Pro rows resolve uniquely.

## Fixed Case Set

The checked-in [case set](swe_explore_cases.json) fixes 20 cases before
execution: two Python cases and three each for Go, Rust, TypeScript,
JavaScript, C, and C++. Every repository is a clean detached checkout at the
joined `base_commit`; no failed case is removed from the denominator.
Runner-level failures contribute zero to aggregate quality metrics rather than
being silently averaged away.

## Contract Results

| Gate | Result |
| --- | ---: |
| Snapshot revision and cleanliness | 20/20 |
| BM25 view construction | 20/20 |
| Selective BM25-only loading | 20/20 |
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

The table reports the CodeNib arm only. Values are means over the fixed 20
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
