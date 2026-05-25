# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from typing import Any, Callable, List, Optional


def _symbols_in_files(code_graph: Any, files: List[str]) -> List[str]:
    """Resolve file paths to the qualified names of symbols defined in them.

    Uses ``CodeGraph._file_nodes`` (file -> [(start, end, vid)]). Matches a
    requested path against graph file keys by exact / suffix / basename so the
    agent can pass whatever path form it read (repo-relative or absolute).
    """
    file_nodes = getattr(code_graph, "_file_nodes", None) or {}
    if not file_nodes:
        return []
    graph = code_graph.graph
    names: List[str] = []
    for raw in files:
        want = (raw or "").strip().strip("`'\"").lstrip("./")
        if not want:
            continue
        keys = [
            k
            for k in file_nodes
            if k == want or k.endswith("/" + want) or want.endswith(k)
        ]
        if not keys:  # last resort: basename match
            base = os.path.basename(want)
            keys = [k for k in file_nodes if os.path.basename(k) == base]
        for k in keys:
            for _s, _e, vid in file_nodes[k]:
                try:
                    nm = graph.vs[vid]["name"]
                except (KeyError, IndexError):
                    nm = None
                if nm:
                    names.append(nm)
    return names


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
        seed_files: Optional[List[str]] = None,
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

        # Two ergonomic ways to seed (the agent rarely has exact node_names):
        #   - seed_files: a file the agent already read → expand from every
        #     symbol defined in it (the LSP "what references this file" move).
        #   - seed_symbols: exact node_name strings from prior search results.
        # ``seed_nodes`` is a back-compat shim (QueriedNode objects / strings).
        raw_seeds: List[Any] = list(seed_symbols or []) + list(seed_nodes or [])
        seed_names: List[str] = []
        for node in raw_seeds:
            name = getattr(node, "node_name", None) or (
                node if isinstance(node, str) else None
            )
            if name:
                seed_names.append(name)
        if seed_files:
            seed_names.extend(_symbols_in_files(context.code_graph, seed_files))

        if not seed_names:
            raise ValueError(
                "graph_expand needs seeds. Easiest: pass seed_files=[path] for "
                "a file you already read, to expand from its symbols. Or pass "
                "seed_symbols as exact node_name strings from prior search "
                "results."
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
