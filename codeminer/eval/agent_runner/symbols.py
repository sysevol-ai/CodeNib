# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Symbol-name helpers for agent-runner evaluation."""

from __future__ import annotations

import os
from typing import Any, Dict, Tuple

from codeminer.eval.agent_runner.prebuilt import load_prebuilt_code_graph
from codeminer.eval.retrieval_eval import normalize_file_path


def symbol_leaf(name: Any) -> str:
    """Return the trailing symbol leaf used by answer/graph span lookup."""
    value = str(name or "").strip()
    if not value:
        return ""
    if ":" in value:
        value = value.rsplit(":", 1)[1]
    value = value.replace("#", ".")
    value = value.split("(", 1)[0]
    return value.split("/")[-1].split(".")[-1].strip()


def build_symbol_span_index(graph: Any) -> Dict[Tuple[str, str], Tuple[int, int]]:
    """Build ``{(file, leaf): (start_1based, end_1based)}`` from a symbol graph.

    Graph vertices carry 0-based tree-sitter line numbers. The returned spans
    are 1-based so they can be used directly by answer-span scoring.
    """
    index: Dict[Tuple[str, str], Tuple[int, int]] = {}
    igraph_obj = getattr(graph, "graph", graph)
    if igraph_obj is None:
        return index
    for vertex in igraph_obj.vs:
        attrs = vertex.attributes()
        file_path = attrs.get("file")
        start = attrs.get("start_line")
        end = attrs.get("end_line")
        if file_path is None or start is None or end is None:
            continue
        normalized_file = normalize_file_path(file_path)
        if not normalized_file:
            continue
        for label in (attrs.get("name"), attrs.get("unified_name")):
            leaf = symbol_leaf(label)
            if not leaf:
                continue
            index.setdefault((normalized_file, leaf), (int(start) + 1, int(end) + 1))
    return index


def build_prebuilt_symbol_span_index(
    prebuilt_root: str, instance_id: str
) -> Dict[Tuple[str, str], Tuple[int, int]]:
    """Build a symbol span index from ``<prebuilt_root>/<instance_id>/graph.pkl``."""
    path = os.path.join(prebuilt_root, instance_id, "graph.pkl")
    if not os.path.exists(path):
        return {}
    try:
        graph = load_prebuilt_code_graph(prebuilt_root, instance_id)
    except Exception:  # noqa: BLE001 - missing/corrupt graph disables this helper
        return {}
    return build_symbol_span_index(graph)
