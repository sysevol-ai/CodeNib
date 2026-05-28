<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# bm25_names

Lexical search that returns **symbol NAME tags only** — `name`, `file:line`,
`kind`, with **no code bodies**. The LocAgent-style entry point: you get
candidate symbol names, then navigate the call graph (`find_callers` /
`find_callees` / `trace` / `impact_analysis`) from those names and
`read_code_block` the ones you want to inspect.

Names are readable (`unified_name` when the canonical id is a content hash), so
you can pass them straight to the graph tools.
