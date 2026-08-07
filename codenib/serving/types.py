# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Core data structures shared across drafters and the serving worker.

A *draft* is a tree of candidate token ids that the target model verifies in a
single forward pass. We model it explicitly (rather than as a flat sequence) so
that multiple draft sources — copy, retrieval, base-model — can be fused into one
tree before verification, following RASD's tree-fusion design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

# A token id, kept as a plain int so drafters stay tokenizer-agnostic.
TokenId = int


@dataclass
class DraftNode:
    """A single node in a draft tree.

    Each node carries one candidate token and links to continuation children.
    The path from the root to any node is one speculated continuation.
    """

    token: TokenId
    children: List["DraftNode"] = field(default_factory=list)
    # Free-form provenance, e.g. "copy", "retrieval:bm25", "model". Useful for
    # acceptance-rate accounting per draft source.
    source: Optional[str] = None
    # Optional drafter confidence in [0, 1]; used to rank/prune branches.
    score: float = 1.0

    def add_path(
        self, tokens: Sequence[TokenId], source: Optional[str] = None
    ) -> "DraftNode":
        """Merge a linear continuation into the tree under this node.

        Shared prefixes reuse existing children (longest-prefix merge), so two
        candidate continuations that agree for k tokens only cost one branch for
        those k tokens. Returns the deepest node of the inserted path.
        """
        node = self
        for tok in tokens:
            match = next((c for c in node.children if c.token == tok), None)
            if match is None:
                match = DraftNode(token=tok, source=source)
                node.children.append(match)
            node = match
        return node

    def __iter__(self) -> Iterator["DraftNode"]:
        """Pre-order traversal over this node and its descendants."""
        yield self
        for child in self.children:
            yield from child


@dataclass
class DraftTree:
    """A verification tree rooted at the current generation position.

    The root is a sentinel that holds no token; its children are the first
    speculated tokens. ``size`` counts real (non-root) candidate tokens — the
    number of positions the target model must verify.
    """

    root: DraftNode = field(default_factory=lambda: DraftNode(token=-1, source="root"))

    @property
    def size(self) -> int:
        return sum(1 for _ in self.root) - 1  # exclude the sentinel root

    def is_empty(self) -> bool:
        return not self.root.children

    def add_sequence(
        self, tokens: Sequence[TokenId], source: Optional[str] = None
    ) -> None:
        """Add a linear candidate continuation from the root."""
        if tokens:
            self.root.add_path(tokens, source=source)
