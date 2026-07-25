#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deterministic baseline predictors for the Repository Guardian evaluation ladder.

Each function returns a ``List[Finding]`` scored by ``guardian_eval.py`` using
the same precision@k / recall / lead-time harness as Guardian's own episodes.

Baseline hierarchy (expected order, lowest→highest):
    random < churn_ranker ≤ graph_diff_only ≤ memoryless ≤ memory  (oracle = ceiling)

Usage:
    from scripts.guardian_baselines import churn_ranker, random_baseline

    findings = churn_ranker("/path/to/repo", since="90 days ago", top_k=10)
"""

from __future__ import annotations

import random as _random
import subprocess
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class BaselineFinding:
    """Evaluation prediction, deliberately distinct from a Guardian finding."""

    kind: str
    title: str
    detail: str = ""


Finding = BaselineFinding


# ---------------------------------------------------------------------------
# churn_ranker — deterministic, no LLM, no graph
# ---------------------------------------------------------------------------


def churn_ranker(
    repo_path: str,
    since: str = "90 days ago",
    *,
    top_k: int = 10,
) -> List[Finding]:
    """Emit the top-k churn hotspots as Findings, no LLM, no graph.

    This is the simplest non-trivial baseline: rank files by commit frequency
    and declare the hottest ones as risks.
    """
    from codeminer.guardian.signals import churn_hotspots

    hotspots = churn_hotspots(repo_path, since=since, top_n=top_k)
    return [
        Finding(
            kind="churn",
            title=f"High-churn file: {h.path}",
            detail=f"commit_count={h.commit_count}",
        )
        for h in hotspots[:top_k]
    ]


# ---------------------------------------------------------------------------
# graph_diff_only — structural analysis, no LLM
# ---------------------------------------------------------------------------


def graph_diff_only(
    repo_path: str,
    *,
    memory_dir: Optional[str] = None,
    prior_graph=None,
    current_graph=None,
    top_k: int = 10,
) -> List[Finding]:
    """Emit graph-diff drift signals as Findings, no LLM.

    Requires a prior graph (from ``memory_dir`` or the ``prior_graph`` arg)
    and a current graph (``current_graph`` arg or a ``graph.pkl`` in
    ``memory_dir``).  Returns ``[]`` when either graph is unavailable.

    Args:
        repo_path: Path to the repository (unused here but kept for API symmetry).
        memory_dir: Path to the Guardian memory store; auto-loads prior graph.
        prior_graph: Pre-loaded CodeGraph; overrides memory_dir lookup.
        current_graph: Pre-loaded CodeGraph for the current commit.  When
            omitted and memory_dir is set, attempts to load the second-most-
            recent snapshot as a proxy for comparison.
        top_k: Maximum number of drift findings to return.
    """
    if prior_graph is None:
        if memory_dir is None:
            return []
        try:
            from codeminer.guardian.memory import MemoryStore

            store = MemoryStore(memory_dir)
            prior_graph = store.load_prior_graph(memory_dir)
        except Exception:  # noqa: BLE001
            return []

    if prior_graph is None:
        return []

    if current_graph is None:
        return []

    try:
        from codeminer.guardian.signals.graph_diff import (
            compute_drift_signals, diff_graphs)

        edge_changes = diff_graphs(prior_graph, current_graph)
        signals = compute_drift_signals(edge_changes, current_graph, prior_graph)
        all_findings = [
            BaselineFinding(
                kind="drift",
                title=(
                    f"Graph-diff drift: {signal.kind} — "
                    f"{signal.file}: `{signal.symbol}`"
                ),
                detail=signal.detail,
            )
            for signal in signals
        ]
        return all_findings[:top_k]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# random_baseline — floor (no domain knowledge)
# ---------------------------------------------------------------------------


def get_tracked_files(repo_path: str) -> List[str]:
    """Return all files tracked by git in repo_path."""
    try:
        out = subprocess.check_output(
            ["git", "ls-files"],
            cwd=repo_path,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        return []


def random_baseline(
    all_files: List[str],
    top_k: int = 10,
    *,
    seed: int = 42,
) -> List[Finding]:
    """Pick top_k files uniformly at random (floor baseline).

    Uses a fixed seed so results are reproducible across eval runs.
    """
    rng = _random.Random(seed)
    sampled = rng.sample(all_files, min(top_k, len(all_files)))
    return [
        Finding(
            kind="churn",
            title=f"High-churn file: {path}",
            detail="random baseline",
        )
        for path in sampled
    ]


# ---------------------------------------------------------------------------
# oracle_baseline — ceiling (post-t knowledge, never deployable)
# ---------------------------------------------------------------------------


def oracle_baseline(
    gt_files: List[str],
    top_k: int = 10,
) -> List[Finding]:
    """Return all GT files as Findings (ceiling; requires post-t knowledge).

    This is the upper bound on precision@k and recall achievable without error.
    It is never deployable — it cheats by using future ground truth — but gives
    a reference ceiling for the evaluation ladder.
    """
    return [
        Finding(
            kind="churn",
            title=f"High-churn file: {path}",
            detail="oracle baseline",
        )
        for path in gt_files[:top_k]
    ]
