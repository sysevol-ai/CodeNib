# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Types shared by Guardian's cycle and investigation agents."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentExit:
    """Mechanical reason a model-owned agent stopped."""

    status: str
    reasoning: str = ""
