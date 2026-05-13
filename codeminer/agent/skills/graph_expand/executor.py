# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Callable, List, Optional


def create_executor(context: Any) -> Callable[..., List[Any]]:
    """Factory: returns a callable that expands seed nodes via the symbol graph.

    Parameters
    ----------
    context:
        An ``ExpandContext`` instance (from ``codeminer.ops.expand``)
        that carries the loaded ``CodeGraph``.
    """
    from ....graph.roi_subgraph import ROISubgraph
    from ....ops.expand import nodeinfo_to_queried

    def execute(
        seed_nodes: List[Any],
        method: str = "bfs",
        top_k: int = 50,
        **kwargs: Any,
    ) -> List[Any]:
        if context.code_graph is None:
            raise RuntimeError("Symbol graph not available")

        hops: int = kwargs.get("hops", 2)
        direction: str = kwargs.get("direction", "both")
        damping: float = kwargs.get("damping", 0.85)
        filter_tests: bool = kwargs.get("filter_tests", True)
        edge_types: Optional[List[str]] = kwargs.get("edge_types")
        node_types: Optional[List[str]] = kwargs.get("node_types")

        # Extract seed names
        seed_names = []
        for node in seed_nodes:
            name = getattr(node, "node_name", None) or (
                node if isinstance(node, str) else None
            )
            if name:
                seed_names.append(name)

        if not seed_names:
            return []

        roi = ROISubgraph(context.code_graph)

        if method == "ppr":
            node_infos = roi.expand_ppr(
                node_names=seed_names,
                top_k=top_k,
                damping=damping,
                filter_tests=filter_tests,
            )
            seed_set = set(seed_names)
            node_infos = [n for n in node_infos if n.node_name not in seed_set]
        else:
            subgraph = roi.extract_subgraph(
                node_names=seed_names,
                k_hop=hops,
                edge_types=edge_types,
                direction=direction,
            )
            node_infos = roi.get_filtered_subgraph_nodes(
                subgraph,
                exclude_nodes=seed_names,
                filter_tests=filter_tests,
                node_types=node_types,
            )

        return nodeinfo_to_queried(node_infos)[:top_k]

    return execute
