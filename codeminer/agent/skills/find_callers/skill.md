<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# find_callers

"Who calls this function/class?" — incoming call-graph edges. Use for impact
("what breaks if I change X") and to walk a bug up into its caller. Returns a
compact list (name, file:line); call file_read for the bodies you care about.
