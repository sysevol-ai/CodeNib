<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# read_code_block

Read a symbol's source **by its graph node** — the structured, LocAgent-style
alternative to `file_read`. Given a symbol name (fuzzy-matched), it resolves the
node and returns its code block (a `file:line` header plus the body).

Use it after `bm25_search` / `find_callers` / `find_callees` / `trace` /
`impact_analysis` surface a symbol you want to inspect. Ambiguous or unknown
symbols return candidate names so you can re-query with an exact one.
