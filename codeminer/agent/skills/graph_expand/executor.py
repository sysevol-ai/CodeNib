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
        seed_symbols: Optional[List[str]] = None,
        seed_nodes: Optional[List[Any]] = None,
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

        # Primary input is ``seed_symbols`` (list of qualified name strings the
        # model copies from prior search results). ``seed_nodes`` is a
        # back-compat shim accepting QueriedNode-like objects or strings.
        raw_seeds: List[Any] = list(seed_symbols or []) + list(seed_nodes or [])
        seed_names: List[str] = []
        for node in raw_seeds:
            name = getattr(node, "node_name", None) or (
                node if isinstance(node, str) else None
            )
            if name:
                seed_names.append(name)

        if not seed_names:
            raise ValueError(
                "graph_expand needs seeds: pass seed_symbols as a list of "
                "exact node_name strings (e.g. 'module.ClassName.method') "
                "copied from prior bm25_search / embedding_search results, "
                "or use file_search / bm25_search to find them first."
            )

        # Surface seeds that don't exist in the graph so the model can re-seed
        # instead of getting a silent empty result.
        name_to_vertex = getattr(context.code_graph, "name_to_vertex", {}) or {}
        if name_to_vertex:
            unresolved = [n for n in seed_names if n not in name_to_vertex]
            if len(unresolved) == len(seed_names):
                raise ValueError(
                    "graph_expand: none of the seed_symbols resolve to graph "
                    f"nodes: {unresolved}. Copy the exact node_name from a "
                    "search result, or grep for the symbol with "
                    "file_search(mode='content')."
                )

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
