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
