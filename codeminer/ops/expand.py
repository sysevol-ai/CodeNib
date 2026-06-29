# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence

from ..graph.code_graph import CodeGraph
from ..log_utils import get_logger
from ..types import NodeInfo, QueriedNode, is_symbol_node

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


def expand_graph_neighbors(
    context: ExpandContext,
    seeds: Sequence[QueriedNode],
    *,
    per_seed: int = 5,
    direction: str = "both",
    repo_path: Optional[str] = None,
    include_content: bool = False,
    symbol_only: bool = True,
    require_span: bool = True,
    score_scale: float = 0.5,
) -> List[QueriedNode]:
    """Return graph neighbors for retrieval candidates.

    This is the retrieval-pipeline counterpart to the agent-facing graph
    navigation helpers: keep dense candidates as seeds, append compact graph
    neighbors, and leave ranking to the downstream reranker. Only one-hop
    predecessor/successor expansion is performed; no BFS/PPR is hidden here.
    """
    graph = context.code_graph
    if graph is None or not seeds:
        return []

    per_seed = max(0, int(per_seed or 0))
    if per_seed == 0:
        return []
    if direction not in ("callees", "successors", "callers", "predecessors", "both"):
        raise ValueError(f"Unsupported graph expansion direction: {direction!r}")

    out: List[QueriedNode] = []
    for seed_rank, seed in enumerate(seeds, 1):
        canonical = _resolve_seed(graph, seed)
        if canonical is None:
            continue
        neighbor_ids: List[int] = []
        if direction in ("callees", "successors", "both"):
            neighbor_ids.extend(graph.get_successors(canonical))
        if direction in ("callers", "predecessors", "both"):
            neighbor_ids.extend(graph.get_predecessors(canonical))

        added_for_seed = 0
        for vid in neighbor_ids:
            node = _neighbor_to_queried(
                graph,
                vid,
                seed=seed,
                seed_rank=seed_rank,
                repo_path=repo_path,
                include_content=include_content,
                score_scale=score_scale,
            )
            if node is None:
                continue
            if symbol_only and not is_symbol_node(node.type):
                continue
            if require_span and (
                node.file is None or node.start_line is None or node.end_line is None
            ):
                continue
            out.append(node)
            added_for_seed += 1
            if added_for_seed >= per_seed:
                break
    return out


def _resolve_seed(graph: CodeGraph, seed: QueriedNode) -> Optional[str]:
    for value in (seed.node_id, seed.node_name):
        if not value:
            continue
        canonical, _ = graph.resolve_symbol(value)
        if canonical is not None:
            return canonical
    if seed.file and seed.node_name:
        canonical, _ = graph.resolve_symbol(f"{seed.file}:{seed.node_name}")
        if canonical is not None:
            return canonical
    return None


def _neighbor_to_queried(
    graph: CodeGraph,
    vertex_id: int,
    *,
    seed: QueriedNode,
    seed_rank: int,
    repo_path: Optional[str],
    include_content: bool,
    score_scale: float,
) -> Optional[QueriedNode]:
    info: dict[str, Any] = graph.get_node_info_by_id(vertex_id) or {}
    name = info.get("name")
    if not name:
        return None
    file_path = info.get("file")
    display = info.get("unified_name") or name
    score = (seed.score if seed.score is not None else 1.0 / seed_rank) or 0.0
    content = None
    if include_content:
        content = _read_span_content(
            repo_path=repo_path,
            file_path=file_path,
            start_line=info.get("start_line"),
            end_line=info.get("end_line"),
        )
    return QueriedNode(
        node_name=display,
        type=info.get("type", ""),
        file=file_path,
        node_id=_node_id(file_path, display),
        start_line=info.get("start_line"),
        end_line=info.get("end_line"),
        score=float(score) * score_scale,
        content=content,
    )


def _node_id(file_path: Optional[str], display: str) -> str:
    if not file_path:
        return display
    if "/" in display or ":" in display:
        return display
    return f"{file_path}:{display}"


def _read_span_content(
    *,
    repo_path: Optional[str],
    file_path: Optional[str],
    start_line: Any,
    end_line: Any,
) -> Optional[str]:
    if not file_path or start_line is None or end_line is None:
        return None
    try:
        start = int(start_line)
        end = int(end_line)
    except (TypeError, ValueError):
        return None
    path = Path(file_path)
    if not path.is_absolute():
        if not repo_path:
            return None
        path = Path(repo_path) / file_path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
    except OSError:
        return None
    start = max(0, start)
    end = min(len(lines) - 1, end)
    if end < start:
        return None
    return "".join(lines[start : end + 1])
