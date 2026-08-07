# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Draft-tree fusion.

Merge several draft trees (copy, retrieval, base-model) into one verification
tree via longest-prefix matching, so candidate continuations that agree for the
first k tokens share a single branch for those tokens. This is RASD's "tree
fusion": one forward pass verifies all sources at once, and the fused tree is
pruned to the speculation budget.

See: "RASD: Retrieval-Augmented Speculative Decoding" (2503.03434).
"""

from __future__ import annotations

from typing import Iterable

from codenib.serving.types import DraftNode, DraftTree


def fuse(trees: Iterable[DraftTree], max_tokens: int | None = None) -> DraftTree:
    """Fuse ``trees`` into a single tree, optionally pruned to ``max_tokens``.

    Branches from later trees merge onto shared prefixes of earlier ones; a node
    keeps the ``source`` of whichever tree introduced it first. When pruning, a
    breadth-first frontier is kept so the highest, most-shared (most likely to be
    accepted) tokens survive over deep speculative tails.
    """
    fused = DraftTree()
    for tree in trees:
        _merge_into(fused.root, tree.root)
    if max_tokens is not None:
        _prune(fused, max_tokens)
    return fused


def _merge_into(dst: DraftNode, src: DraftNode) -> None:
    """Recursively merge ``src``'s children onto ``dst`` by token identity."""
    for src_child in src.children:
        match = next((c for c in dst.children if c.token == src_child.token), None)
        if match is None:
            match = DraftNode(
                token=src_child.token,
                source=src_child.source,
                score=src_child.score,
            )
            dst.children.append(match)
        _merge_into(match, src_child)


def _prune(tree: DraftTree, max_tokens: int) -> None:
    """Keep at most ``max_tokens`` nodes, breadth-first from the root.

    BFS favours shallow tokens — those nearest the current position, which the
    target model is most likely to accept — and drops deep speculative tails
    first. Surviving nodes have their out-of-budget children removed.
    """
    if max_tokens <= 0:
        tree.root.children = []
        return

    kept = 0
    frontier = [tree.root]
    while frontier:
        node = frontier.pop(0)
        survivors = []
        for child in node.children:
            if kept >= max_tokens:
                break
            survivors.append(child)
            kept += 1
            frontier.append(child)
        node.children = survivors
