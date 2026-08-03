<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Repository search

Search repository implementation with one focused query. The tool combines
lexical and semantic rankings with reciprocal-rank fusion when both views are
loaded, while reserving result slots for strong evidence unique to either
branch; on a fast BM25-only index it uses lexical ranking without changing the
interface. Every result includes a repository location and a bounded source
excerpt focused on the query, so one large function cannot displace the rest of
the evidence.

Use this as the first step for repository questions. Split broad questions into
focused follow-up searches for specific concepts, symbols, or lifecycle stages.
For questions about prevention, validation, or guarantees, search separately
for the predicate and for the loader/provider call site that acts on it; a
warning, test, or error message alone is not enforcement.
When following an exact symbol, keep the relevant object and action from the
original question in the query (for example, `symbol_name vector runtime load`)
rather than asking only for generic "usage".
By default, results omit tests, examples, documentation, generated files, and
validation harnesses so they are not mistaken for runtime mechanisms. Set
`include_supporting=true` only when the question explicitly asks about those
materials or when implementation evidence needs corroboration.
