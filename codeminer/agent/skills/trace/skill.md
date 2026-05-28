<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# trace

"How does X reach Y?" — the shortest call path between two symbols, following
call edges grep can't (incl. indirect hops). Returns the ordered path
(name, file:line per hop). Use to connect a symptom to its root cause across
functions.
