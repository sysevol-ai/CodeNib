<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Incremental Graph - Interactive Demo

This walkthrough mirrors the current symbol-mode patcher: modified definitions
are classified as `affected`, stable vertices are updated in place, and
full-file fallback remaps one record per six-field anchored-edge tuple. The
C/C++ backend follows a separate `.idx` refresh path but shares the final
range-index contract.

<style>
.md-content { max-width: none; }
.md-content__inner { padding: 0 !important; }
.demo-frame {
  display: block;
  width: 100%;
  height: min(52rem, calc(100vh - 5.5rem));
  min-height: 42rem;
  background: var(--codenib-surface);
  border: 1px solid var(--codenib-line);
  border-radius: 0.8rem;
  box-shadow: var(--codenib-shadow);
}
@media screen and (max-width: 44.984375em) {
  .demo-frame {
    height: 78vh;
    min-height: 38rem;
    border-radius: 0.6rem;
  }
}
</style>

<iframe class="demo-frame" src="../incremental_interactive.html"></iframe>
