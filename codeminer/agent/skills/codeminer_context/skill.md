<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# codeminer_context — GraphRAG context

Given the task/problem statement, it returns the relevant
code in a single call: it searches for entry-point symbols (keyword + semantic)
and **expands them along the call graph** (callers + callees), returning a
compact, deduped, budget-capped set (name · file:line · kind · relation). No
code bodies — `file_read` the few you want to confirm.

This replaces the usual fan-out of repeated grep/search/read: one call gives
you entry points *and* their structural neighbourhood, so you can answer or
pinpoint the edit location with far fewer tool calls.

## When to use
- First step for any "where is / what handles / what breaks if I change X".
- Whenever you would otherwise grep a name and then chase its callers/callees.
