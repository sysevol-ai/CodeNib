# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Suffix-automaton drafting (SAM-Decoding / SuffixDecoding style).

CopySpec and prompt-lookup match a *fixed-length* n-gram suffix; ``CopyDrafter``
even scans candidate lengths one by one. A suffix automaton instead indexes
**every** substring of a reference string and finds the *exact longest* suffix of
the current context that occurs in it, in amortized O(1) per step — strictly more
powerful than fixed n-gram copy, and it scales to a whole-repo corpus.

This one drafter subsumes two of CodeNib serving's draft sources:

* a **static** automaton over a corpus (the rest of the repo) -> retrieval reach,
* a **dynamic** automaton over the generated context -> in-context copy,

and drafts the continuation that followed the most recent occurrence of the
longest match, picking whichever automaton offers the longer match. It drops into
``fuse`` exactly like ``CopyDrafter``/``RetrievalDrafter``.

See: "SAM-Decoding: Speculative Decoding via Suffix Automaton" (2411.10666) and
"SuffixDecoding" (2411.04975). Prototype notes: the dynamic automaton is rebuilt
per step (O(context)); a production build extends it incrementally as tokens are
emitted. Continuations stop at document boundaries (sentinel ids < 0).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from codenib.serving.drafter.base import Drafter
from codenib.serving.types import DraftTree, TokenId


class _SuffixAutomaton:
    """Online suffix automaton over a token string (Blumer et al.).

    State 0 is the initial state. ``end[s]`` holds a representative end position
    of an occurrence of the substrings recognised by ``s`` — after
    :meth:`finalize` it is the *most recent* (maximum) such position, so reading
    forward from it yields the latest continuation.
    """

    def __init__(self) -> None:
        self.trans: List[Dict[TokenId, int]] = [{}]
        self.link: List[int] = [-1]
        self.length: List[int] = [0]
        self.end: List[int] = [-1]
        self.last = 0

    def _new(self, length: int, end: int) -> int:
        self.trans.append({})
        self.link.append(-1)
        self.length.append(length)
        self.end.append(end)
        return len(self.length) - 1

    def extend(self, c: TokenId, pos: int) -> None:
        cur = self._new(self.length[self.last] + 1, pos)
        p = self.last
        while p != -1 and c not in self.trans[p]:
            self.trans[p][c] = cur
            p = self.link[p]
        if p == -1:
            self.link[cur] = 0
        else:
            q = self.trans[p][c]
            if self.length[p] + 1 == self.length[q]:
                self.link[cur] = q
            else:
                clone = self._new(self.length[p] + 1, self.end[q])
                self.trans[clone].update(self.trans[q])
                self.link[clone] = self.link[q]
                while p != -1 and self.trans[p].get(c) == q:
                    self.trans[p][c] = clone
                    p = self.link[p]
                self.link[q] = clone
                self.link[cur] = clone
        self.last = cur

    def finalize(self) -> None:
        """Propagate the most-recent end position up the suffix links."""
        # States in decreasing length: a state's end-position set is a subset of
        # its suffix-link parent's, so max-propagating gives each state the most
        # recent occurrence of any substring it recognises.
        order = sorted(
            range(len(self.length)), key=lambda s: self.length[s], reverse=True
        )
        for v in order:
            pl = self.link[v]
            if pl != -1 and self.end[v] > self.end[pl]:
                self.end[pl] = self.end[v]

    def longest_suffix_match(self, query: Sequence[TokenId]) -> Tuple[int, int]:
        """Return ``(state, length)`` of the longest suffix of ``query`` that is a
        substring of the reference. ``end[state]`` locates that occurrence."""
        v, length = 0, 0
        for c in query:
            while v != 0 and c not in self.trans[v]:
                v = self.link[v]
                length = self.length[v]
            nxt = self.trans[v].get(c)
            if nxt is not None:
                v = nxt
                length += 1
            else:
                v, length = 0, 0
        return v, length


def _build(reference: Sequence[TokenId]) -> _SuffixAutomaton:
    sam = _SuffixAutomaton()
    for pos, tok in enumerate(reference):
        sam.extend(tok, pos)
    sam.finalize()
    return sam


def _read_continuation(
    reference: Sequence[TokenId], end: int, budget: int
) -> List[TokenId]:
    """Tokens following position ``end`` in ``reference``, up to a doc boundary."""
    out: List[TokenId] = []
    i = end + 1
    n = len(reference)
    while i < n and len(out) < budget:
        tok = reference[i]
        if tok < 0:  # document-boundary sentinel
            break
        out.append(tok)
        i += 1
    return out


class SuffixAutomatonDrafter(Drafter):
    """Exact-longest-suffix drafter backed by suffix automata.

    Args:
        corpus: optional reference documents (e.g. the other files in the repo).
            Indexed once into a static automaton for retrieval reach. Omit for a
            pure in-context (copy-style) drafter.
        min_match: minimum matched suffix length to draft (precision floor).
        max_draft: cap on drafted tokens per step.
    """

    source = "suffix_automaton"

    def __init__(
        self,
        corpus: Optional[Sequence[Sequence[TokenId]]] = None,
        min_match: int = 2,
        max_draft: int = 16,
    ) -> None:
        if min_match < 1:
            raise ValueError("min_match must be >= 1")
        self.min_match = min_match
        self.max_draft = max_draft
        self._ref: List[TokenId] = []
        self._sam: Optional[_SuffixAutomaton] = None
        if corpus:
            for i, doc in enumerate(corpus):
                if i:
                    self._ref.append(-(i))  # distinct negative boundary sentinel
                self._ref.extend(doc)
            self._sam = _build(self._ref)

    def draft(self, context: Sequence[TokenId], max_tokens: int) -> DraftTree:
        tree = DraftTree()
        budget = min(max_tokens, self.max_draft)
        if budget <= 0:
            return tree

        ctx = list(context)
        best_len = self.min_match - 1
        best_cont: Optional[List[TokenId]] = None

        # Static corpus automaton: longest suffix of ctx occurring in the repo.
        if self._sam is not None and len(ctx) >= self.min_match:
            v, length = self._sam.longest_suffix_match(ctx)
            if length > best_len:
                cont = _read_continuation(self._ref, self._sam.end[v], budget)
                if cont:
                    best_len, best_cont = length, cont

        # Dynamic context automaton: most recent *earlier* occurrence of a context
        # suffix. Built over ctx[:-1] so the trailing self-match is excluded; the
        # full ctx is the read source. Ties prefer this (more local) source.
        if len(ctx) >= self.min_match + 1:
            dyn = _build(ctx[:-1])
            v, length = dyn.longest_suffix_match(ctx)
            if length >= self.min_match and length >= best_len:
                cont = _read_continuation(ctx, dyn.end[v], budget)
                if cont:
                    best_len, best_cont = length, cont

        if best_cont:
            tree.add_sequence(best_cont, source=self.source)
        return tree
