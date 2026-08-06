---
title: Benchmarks & Evaluation
hide:
  - toc
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Benchmarks & Evaluation

<div class="codenib-section-lede" markdown>

Build reproducible benchmark inputs, prepare ground truth for source-location
retrieval evaluation, and carry the configuration and provenance needed to
interpret every result.

</div>

## Dataset And Benchmark Support

Dataset loading, benchmark scoring, and agent-policy compatibility are
different surfaces. A loader entry means CodeNib can prepare snapshots and
ground truth; it does not imply that every retrieval route or external policy
has been evaluated on that dataset. See the separate
[agent integration matrix](../agent_integrations.md) for policy boundaries.

| Dataset or benchmark | CodeNib surface | Evaluation use | Validated evidence and boundary |
| --- | --- | --- | --- |
| [CodeNib Base](https://huggingface.co/datasets/fishmingyu/codenib-base-dataset) | `CodeNibBaseDataset` | Retrieval, graph, rerank, LSP, and agent-context studies | Frozen 100-row, five-language-group test split; complete five-embedding and rerank result sets. |
| [CodeNib Synthesis](https://huggingface.co/datasets/sysevol-ai/codenib-synthesis) | `CodeNibSynthesisDataset` plus quality audit | Behavioral, hint, reasoning, and traversal queries | Five language configurations and 500 retained rows; synthetic queries complement rather than replace issue-derived CodeNib Base labels. |
| [SWE-bench Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite) and [Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified) | `SwebenchDataset`, collection, checkout, and GT locator | Retrieval baselines and fixed-case localization-policy preparation | Loader, patch-to-location ground truth, clean-checkout, and policy preflight paths are covered; individual agent support remains policy-specific. |
| [SWE-bench Multilingual](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual) | `SwebenchMultilingualDataset`, collection, and GT locator | Multilingual source for CodeNib Base and benchmark joins | Dedicated schema and repository preparation; all 182 rows used by the pinned SWE-Explore release join resolve uniquely. |
| [Loc-Bench v1](https://huggingface.co/datasets/czlll/Loc-Bench_V1) | `LocbenchDataset` | Retrieval and shared localization runners | Dataset, checkout, and location-label adapters are implemented; no paper-wide aggregate is claimed. |
| [SWE-Explore](https://github.com/Qiushao-E/SWE-Explore-Bench) | `CodeNibSWEExploreExplorer` and compatible 17-metric scorer | Ranked source-region benchmark | Fixed 20-case, seven-language run; 1,020/1,020 real-output metric cells match the pinned official evaluator. [Full validation](swe_explore.md). |
| [SWE-bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) | Pinned source join for SWE-Explore | Supplies issue text and commit identity omitted by the released region rows | Join-only boundary: all 215 referenced rows resolve uniquely; this is not a general Pro task runner. |
| Local JSON | `LocalJsonDataset` | Custom repository/query/location inputs | Schema adapter with inline file and symbol ground truth; reproducibility is owned by the caller's input file. |

<div class="codenib-section-grid" markdown>

-   <span class="codenib-card-eyebrow">Collect</span>

    **SWE-bench instances**

    Sample representative tasks across repositories and languages without
    losing their source metadata.

    [Collect benchmark inputs →](../collect_swebench.md)

-   <span class="codenib-card-eyebrow">Synthesize</span>

    **Query datasets**

    Generate traversal, behavioral, and multi-step queries with explicit
    curation and verification stages.

    [Run the synthesis pipeline →](../synthesis_pipeline.md)

-   <span class="codenib-card-eyebrow">Prepare</span>

    **Ground-truth locations**

    Extract expected files, symbols, and one-based source ranges from benchmark
    patches.

    [Use the GT locator →](../gt_locator.md)

-   <span class="codenib-card-eyebrow">Package</span>

    **Artifact bundles**

    Preserve manifests, metrics, diagnostics, and provenance as one verifiable
    evaluation output.

    [Build an artifact bundle →](../evaluation_artifacts.md)

</div>
