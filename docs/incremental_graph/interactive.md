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
.demo-frame { width: 100%; height: 90vh; border: none; }
</style>

<iframe class="demo-frame" src="../incremental_interactive.html"></iframe>
