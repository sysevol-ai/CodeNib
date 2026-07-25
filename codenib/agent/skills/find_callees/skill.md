<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# find_callees

"What does this function call?" — outgoing call-graph edges. Use to follow a
symbol into its dependencies. Returns a compact list (name, file:line); call
file_read for the bodies you care about.
