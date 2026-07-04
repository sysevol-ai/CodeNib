# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent-compile adapter for graph verify-expand helpers."""

from __future__ import annotations

import os
from typing import Optional

from codeminer.eval.agent_runner.verify_expand import (
    GraphNav,
    Verdict,
    expansion_seeds_from_candidates,
    graph_verify,
    render_expansion,
)


def load_graph_nav(prebuilt_dir: str, instance_id: str) -> Optional[GraphNav]:
    path = os.path.join(prebuilt_dir, instance_id, "graph.pkl")
    if not os.path.exists(path):
        return None
    try:
        from scripts.agent_compile.lib.prebuilt import load_prebuilt_code_graph

        graph = load_prebuilt_code_graph(prebuilt_dir, instance_id)
    except Exception:  # noqa: BLE001 — missing/corrupt graph just disables verify
        return None
    g = getattr(graph, "graph", None)
    return GraphNav(g) if g is not None else None


__all__ = [
    "GraphNav",
    "Verdict",
    "expansion_seeds_from_candidates",
    "graph_verify",
    "load_graph_nav",
    "render_expansion",
]
