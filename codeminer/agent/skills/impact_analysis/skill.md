<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# impact_analysis

Transitive call-graph impact / dependency analysis — the multi-hop structural
question grep cannot answer.

- `impact_analysis(symbol, direction="impact")` — **blast radius**: every
  function that *transitively* calls `symbol`. "If I change this, what might
  break?"
- `impact_analysis(symbol, direction="dependencies")` — what `symbol`
  *transitively* relies on (transitive callees).
- `max_depth` controls how many call hops to follow (default 3).

Returns compact nodes (name, file:line, kind) tagged with their hop-distance —
no code bodies; `file_read` the ones you care about. Use this for impact
assessment and cross-function reasoning, not for first-pass localization (search
+ grep are better for that). Unresolved/ambiguous symbols return candidates.
