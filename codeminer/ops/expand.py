# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..graph.code_graph import CodeGraph
from ..log_utils import get_logger
from ..types import NodeInfo, QueriedNode

logger = get_logger(__name__)


@dataclass
class ExpandContext:
    """Shared resources for graph expansion operators."""

    code_graph: Optional[CodeGraph] = None
    default_top_k: int = 50
    default_hops: int = 2
    default_direction: str = "both"
    default_method: str = "bfs"
    default_damping: float = 0.85
    filter_tests: bool = True


# ---------------------------------------------------------------------------
# Helpers (used by skill executors)
# ---------------------------------------------------------------------------


def nodeinfo_to_queried(nodes: List[NodeInfo]) -> List[QueriedNode]:
    """Convert NodeInfo list to QueriedNode list, assigning rank-based scores."""
    results: List[QueriedNode] = []
    for rank, ni in enumerate(nodes):
        score = ni.score if ni.score is not None else 1.0 / (rank + 1)
        results.append(
            QueriedNode(
                node_name=ni.node_name,
                type=ni.type,
                file=ni.file,
                start_line=ni.start_line,
                end_line=ni.end_line,
                score=score,
                content=ni.content,
            )
        )
    return results
