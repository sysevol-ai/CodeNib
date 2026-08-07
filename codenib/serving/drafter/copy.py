# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Copy-based speculation (CopySpec / prompt-lookup).

Find the most recent earlier occurrence of the current context suffix and
speculate that the tokens which followed it last time will follow again. This is
the exact-match draft path: zero model cost, no extra GPU memory, and very high
acceptance on repetitive text such as code (boilerplate, repeated signatures,
edit-then-restate patterns).

See: "CopySpec: Accelerating LLMs with Speculative Copy-and-Paste" (2502.08923).
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from codenib.serving.drafter.base import Drafter
from codenib.serving.types import DraftTree, TokenId

#: Copy-drafting defaults, named so the server and the offline acceptance
#: harness provably draft with the same parameters — the harness exists to
#: predict serving throughput, so a silent divergence would invalidate it.
DEFAULT_MIN_MATCH = 2
DEFAULT_MAX_MATCH = 32


class CopyDrafter(Drafter):
    """Suffix-match copy drafter.

    For decreasing n-gram lengths in ``[min_match, max_match]`` it takes the last
    ``n`` context tokens as a pattern and looks for the most recent earlier
    occurrence of that pattern. The longest pattern that matches wins (longer
    match -> higher-precision copy), and the tokens following the matched
    occurrence become a single linear draft branch.
    """

    source = "copy"

    def __init__(
        self,
        min_match: int = DEFAULT_MIN_MATCH,
        max_match: int = DEFAULT_MAX_MATCH,
        max_draft: int = 16,
    ) -> None:
        if min_match < 1:
            raise ValueError("min_match must be >= 1")
        if max_match < min_match:
            raise ValueError("max_match must be >= min_match")
        self.min_match = min_match
        self.max_match = max_match
        self.max_draft = max_draft

    def draft(self, context: Sequence[TokenId], max_tokens: int) -> DraftTree:
        tree = DraftTree()
        budget = min(max_tokens, self.max_draft)
        if budget <= 0:
            return tree

        ctx = list(context)
        continuation = self._lookup(ctx, budget)
        if continuation:
            tree.add_sequence(continuation, source=self.source)
        return tree

    def _lookup(self, ctx: List[TokenId], budget: int) -> Optional[List[TokenId]]:
        n = len(ctx)
        if n < self.min_match + 1:
            return None

        upper = min(self.max_match, n - 1)
        for match_len in range(upper, self.min_match - 1, -1):
            pattern = ctx[n - match_len :]
            # Search backwards for the most recent earlier occurrence, excluding
            # the suffix itself (search space ends before the pattern's start).
            start = self._rfind(ctx, pattern, n - match_len - 1)
            if start == -1:
                continue
            after = start + match_len
            continuation = ctx[after : after + budget]
            if continuation:
                return continuation
        return None

    @staticmethod
    def _rfind(seq: List[TokenId], pattern: List[TokenId], last_start: int) -> int:
        """Index of the last occurrence of ``pattern`` in ``seq[: last_start+m]``.

        ``last_start`` is the highest index at which a match may *begin*. Returns
        -1 if not found. Linear scan from the right — adequate for per-step
        context sizes; swap for a suffix-automaton if profiling demands it.
        """
        m = len(pattern)
        for i in range(min(last_start, len(seq) - m), -1, -1):
            if seq[i : i + m] == pattern:
                return i
        return -1
