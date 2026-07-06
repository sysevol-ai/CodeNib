# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph-backed LSP baseline adapters for agent-runner evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence

from codeminer.agent.lsp_graph import lsp_route
from codeminer.agent.route_context import (
    extract_lsp_symbol_seeds as extract_core_lsp_symbol_seeds,
)

from .baseline import (
    BaselineLocation,
    BaselineRunResult,
    BaselineTask,
    build_baseline_result_entry,
)


@dataclass(frozen=True)
class LSPRouteBaselineConfig:
    """Configuration for the static-graph LSP route baseline."""

    top_k: int = 12
    seed_limit: int = 8
    include_neighbors: bool = True
    agent_name: str = "lsp_route"


def extract_lsp_symbol_seeds(
    task: BaselineTask,
    *,
    limit: int = 8,
) -> List[str]:
    """Extract explicit symbol seeds from a task without using ground truth.

    The baseline intentionally does not search the repository and never reads
    ``target_files`` or ``target_symbols``. It routes from symbols already named
    by task metadata/context or code-like identifiers in the issue text.
    """

    explicit: List[Any] = []

    for source in (task.metadata, task.context):
        if not isinstance(source, Mapping):
            continue
        for key in ("lsp_symbol_seeds", "symbol_seeds", "symbols"):
            explicit.append(source.get(key))

    text_parts = [task.query]
    if isinstance(task.context, Mapping):
        for key in ("issue_title", "issue_body", "hints"):
            value = task.context.get(key)
            if isinstance(value, str):
                text_parts.append(value)
    return extract_core_lsp_symbol_seeds(
        *text_parts,
        explicit=explicit,
        limit=limit,
    )


def locations_from_lsp_nodes(nodes: Sequence[Any]) -> List[BaselineLocation]:
    """Convert LSP-shaped graph nodes into baseline output locations."""

    locations: List[BaselineLocation] = []
    for node in nodes:
        file_path = _node_value(node, "file") or _node_value(node, "file_path")
        node_name = str(
            _node_value(node, "node_name") or _node_value(node, "node_id") or ""
        )
        if not file_path:
            file_path, node_name = _split_file_symbol(node_name)
        name = _symbol_name(node_name, file_path)
        if not name or not file_path:
            continue
        locations.append(
            BaselineLocation(
                name=name,
                type=str(_node_value(node, "type") or ""),
                file_path=str(file_path),
                line_start=_agent_line(_node_value(node, "start_line")),
                line_end=_agent_line(_node_value(node, "end_line")),
                action="modify",
                description=str(_node_value(node, "content") or ""),
            )
        )
    return locations


def run_lsp_route_baseline(
    *,
    task: BaselineTask,
    graph: Any,
    config: LSPRouteBaselineConfig | None = None,
) -> BaselineRunResult:
    """Run the static-graph LSP route baseline for one task."""

    cfg = config or LSPRouteBaselineConfig()
    if graph is None:
        return BaselineRunResult(
            success=False,
            error_message="symbol graph unavailable",
            repo_path=task.repo_path,
        )

    seeds = extract_lsp_symbol_seeds(task, limit=cfg.seed_limit)
    if not seeds:
        return BaselineRunResult(
            success=True,
            locations=(),
            usage=_usage(cfg, seeds=seeds, route_count=0),
            repo_path=task.repo_path,
        )

    try:
        nodes = lsp_route(
            graph,
            symbols=seeds,
            query=task.query,
            top_k=cfg.top_k,
            include_neighbors=cfg.include_neighbors,
        )
    except Exception as exc:  # noqa: BLE001 - baseline records failures per task
        return BaselineRunResult(
            success=False,
            error_message=str(exc),
            repo_path=task.repo_path,
        )

    locations = tuple(locations_from_lsp_nodes(nodes))
    return BaselineRunResult(
        success=True,
        locations=locations,
        usage=_usage(cfg, seeds=seeds, route_count=len(nodes)),
        repo_path=task.repo_path,
        raw_output=nodes,
    )


def build_lsp_route_result_entry(
    *,
    task: BaselineTask,
    graph: Any,
    metrics_k: Sequence[int],
    config: LSPRouteBaselineConfig | None = None,
) -> dict[str, Any]:
    """Build a JSON-ready result row for one static-graph LSP baseline run."""

    cfg = config or LSPRouteBaselineConfig()
    result = run_lsp_route_baseline(task=task, graph=graph, config=cfg)
    return build_baseline_result_entry(
        task=task,
        agent_name=cfg.agent_name,
        model=None,
        result=result,
        metrics_k=metrics_k,
    )


def load_prebuilt_lsp_graph(prebuilt_root: str, instance_id: str) -> Any:
    """Load the prebuilt graph artifact used by LSP-route baselines."""

    from .prebuilt import load_prebuilt_code_graph

    return load_prebuilt_code_graph(prebuilt_root, instance_id)


def _usage(
    config: LSPRouteBaselineConfig,
    *,
    seeds: Sequence[str],
    route_count: int,
) -> dict[str, Any]:
    return {
        "backend": "static_graph_lsp_route",
        "top_k": config.top_k,
        "seed_limit": config.seed_limit,
        "include_neighbors": config.include_neighbors,
        "symbol_seeds": list(seeds),
        "route_count": route_count,
    }


def _node_value(node: Any, key: str) -> Any:
    if isinstance(node, Mapping):
        return node.get(key)
    return getattr(node, key, None)


def _agent_line(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value) + 1
    except (TypeError, ValueError):
        return None


def _split_file_symbol(text: str) -> tuple[Optional[str], str]:
    if ":" not in text:
        return None, text
    left, right = text.split(":", 1)
    if "/" in left or "." in left:
        return left, right
    return None, text


def _symbol_name(display: str, file_path: Any) -> str:
    text = (display or "").strip()
    file_text = str(file_path or "").strip()
    if file_text and text.startswith(file_text + ":"):
        text = text[len(file_text) + 1 :]
    return text.strip()


__all__ = [
    "LSPRouteBaselineConfig",
    "build_lsp_route_result_entry",
    "extract_lsp_symbol_seeds",
    "load_prebuilt_lsp_graph",
    "locations_from_lsp_nodes",
    "run_lsp_route_baseline",
]
