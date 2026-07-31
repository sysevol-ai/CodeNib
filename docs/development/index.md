---
title: Development
hide:
  - toc
---

<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Development

<div class="codenib-section-lede" markdown>

Extend CodeNib through its shared registries and contracts, verify the changed
surface at the right test tier, and keep evaluation artifacts reproducible.

</div>

<div class="codenib-section-grid" markdown>

-   <span class="codenib-card-eyebrow">Extend</span>

    **Contribute a language**

    Add chunking and graph capabilities through the language registry, then
    promote each backend only after its contract checks pass.

    [Follow the language workflow →](../contributing-a-language.md)

-   <span class="codenib-card-eyebrow">Verify</span>

    **CI/CD**

    Choose among unit, integration, serial, graph-consumer, core, and slow
    verification tiers.

    [Run the right checks →](../ci_cd.md)

-   <span class="codenib-card-eyebrow">Publish</span>

    **Releasing**

    Build and inspect distributions, run install smoke tests, and publish a
    release without bypassing artifact checks.

    [Prepare a release →](../releasing.md)

-   <span class="codenib-card-eyebrow">Measure</span>

    **Evaluation workflows**

    Collect benchmark inputs, synthesize queries, extract ground-truth source
    locations, and package reproducible result bundles.

    [Open evaluation workflows →](../evaluation/index.md)

</div>

## Project contracts

Use [Naming](../branding.md) for project and package terminology. Evaluation
inputs and outputs have their own reproducibility contracts; start with the
[Evaluation Workflows](../evaluation/index.md) overview before running them.
