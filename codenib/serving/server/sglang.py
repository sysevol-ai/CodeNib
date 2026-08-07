# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""SGLang-backed tree verification.

This is the GPU-side verifier that turns CodeNib serving's *projected* speedup into a
*measured* one. ``OracleVerifier`` (in :mod:`codenib.serving.server.worker`) answers
"is this draft token correct?" by comparing to a known ``truth`` array — only
possible when replaying existing files. In real serving there is no truth array;
the only authority on the next token is the **target model itself**. This module
runs that model over the whole draft tree in **one forward pass** and accepts a
draft token iff it equals what the model would have produced anyway, which keeps
output identical to non-speculative greedy decoding.

The work splits cleanly into two layers:

* **Engine-agnostic plumbing (real, tested here):** flatten a :class:`DraftTree`
  into a linear sequence with per-token *position ids* and a *tree-attention*
  parent map, then recover the longest accepted root-to-leaf path from the
  model's per-position next-token predictions. All CPU, no GPU, unit-tested with
  a fake engine.
* **The model forward (``TargetEngine`` boundary):** given the flattened tree,
  return each position's greedy next-token prediction. The production
  implementation (:class:`SGLangTreeEngine`) submits the flattened tree to
  SGLang's tree-attention verify kernel; it is the one piece that needs a live
  GPU engine and is left as a documented stub.

Scope of this first cut: **greedy** verification (argmax). Sampling-based
verification (modified rejection sampling that preserves the target's exact
distribution) is future work and noted at the call site.

See: speculative decoding (Leviathan et al. 2211.17192), tree attention
verification (Medusa/SpecInfer), and RASD tree fusion (2503.03434).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence

from codenib.serving.server.worker import Verifier, VerifyResult
from codenib.serving.types import DraftNode, DraftTree, TokenId


@dataclass
class FlatDraft:
    """A :class:`DraftTree` flattened for a single batched forward pass.

    The model is fed ``context`` followed by ``tokens`` (the draft nodes in
    pre-order). Each draft token carries the metadata a tree-attention kernel
    needs to verify every branch in one pass:

    * ``positions[i]`` — the position id of draft token ``i``. Siblings at the
      same tree depth share a position id (they are alternative tokens *for the
      same slot*), so each branch sees a contiguous 0..n position sequence.
    * ``parents[i]`` — the *global* sequence index (into ``context`` ++
      ``tokens``) that draft token ``i`` attends back to: its tree parent, or the
      last context token for a depth-1 node. This is the tree-attention mask in
      compressed form (see :func:`attention_mask`).

    ``node_index`` maps each :class:`DraftNode` to its global sequence index so
    the accepted path can be read back after the forward pass.
    """

    context_len: int
    tokens: List[TokenId] = field(default_factory=list)
    positions: List[int] = field(default_factory=list)
    parents: List[int] = field(default_factory=list)
    # id(DraftNode) -> global sequence index (context_len + offset into tokens).
    node_index: Dict[int, int] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.tokens)

    @property
    def total_len(self) -> int:
        """Length of the full fed sequence (context + draft tokens)."""
        return self.context_len + len(self.tokens)


def flatten(context_len: int, tree: DraftTree) -> FlatDraft:
    """Flatten ``tree`` into a :class:`FlatDraft` for a forward pass over a
    sequence of length ``context_len`` followed by the draft tokens.

    Pre-order DFS keeps every root-to-node path contiguous in attention terms
    (a node's ancestors always precede it). Depth-1 nodes attend to the last
    context token (``context_len - 1``); deeper nodes attend to their tree
    parent.
    """
    flat = FlatDraft(context_len=context_len)

    def visit(node: DraftNode, parent_global: int, depth: int) -> None:
        for child in node.children:
            g = context_len + len(flat.tokens)
            flat.node_index[id(child)] = g
            flat.tokens.append(child.token)
            # Depth d node fills generation slot d-1 -> position context_len+d-1.
            flat.positions.append(context_len + depth - 1)
            flat.parents.append(parent_global)
            visit(child, g, depth + 1)

    # Root's children attend back to the final context token.
    visit(tree.root, context_len - 1, 1)
    return flat


def attention_mask(flat: FlatDraft) -> List[List[bool]]:
    """Expand ``flat``'s parent links into a full boolean attention mask.

    ``mask[i][j]`` is True when query position ``i`` may attend to key position
    ``j``. Context tokens are causal; each draft token attends to all context
    plus its own ancestor chain (and itself) — never to sibling or cousin
    branches. Provided for adapters/engines that consume a dense mask; the
    compressed ``parents`` map is the source of truth.
    """
    n = flat.total_len
    c = flat.context_len
    mask = [[False] * n for _ in range(n)]
    # Causal block over the context.
    for i in range(c):
        for j in range(i + 1):
            mask[i][j] = True
    # Each draft token: all context + ancestor chain + self.
    for off in range(flat.size):
        i = c + off
        for j in range(c):
            mask[i][j] = True
        anc = i
        while anc >= c:
            mask[i][anc] = True
            anc = flat.parents[anc - c]
        # anc is now the last context token (parent of the depth-1 ancestor).
    return mask


class TargetEngine(ABC):
    """The target model's forward pass over a flattened draft tree.

    Implementations run one batched forward and return, for each fed sequence
    position, the model's **greedy next-token id** — i.e. ``argmax`` of the
    logits at that position. CodeNib serving reads only the predictions it needs (the
    last context token and each accepted draft node), but returning the full
    aligned vector keeps the contract simple and matches what a real engine
    computes anyway.
    """

    @abstractmethod
    def predict(self, context: Sequence[TokenId], flat: FlatDraft) -> List[TokenId]:
        """Return greedy next-token predictions aligned to the fed sequence.

        The result has length ``flat.total_len``; ``result[i]`` is the token the
        model would emit immediately after sequence position ``i`` (context
        positions ``0..context_len-1`` followed by the draft tokens). Only
        ``result[context_len-1]`` and the draft-node positions are consulted.
        """
        raise NotImplementedError


def _normalize_eos_token_ids(
    value: Optional[TokenId | Iterable[TokenId]],
) -> FrozenSet[TokenId]:
    """Normalize one or many model termination ids into an immutable set."""
    if value is None:
        return frozenset()
    values = (
        (value,) if isinstance(value, int) and not isinstance(value, bool) else value
    )
    if isinstance(values, (str, bytes)):
        raise ValueError("eos token ids must be integers")
    try:
        normalized = frozenset(values)
    except TypeError as exc:
        raise ValueError("eos token ids must be an integer or iterable") from exc
    if any(
        isinstance(token, bool) or not isinstance(token, int) for token in normalized
    ):
        raise ValueError("eos token ids must be integers")
    return normalized


@dataclass
class SGLangVerifier(Verifier):
    """Greedy tree verifier driven by a :class:`TargetEngine`.

    Walks the fused draft tree along the model's own greedy predictions,
    accepting each draft token that matches what the model would have generated,
    and emits the model's next token as the always-present bonus (this is the
    free token standard speculative decoding yields per step). Output is
    therefore token-identical to non-speculative greedy decoding.

    ``eos_token_ids`` maps every model termination prediction to a ``None``
    bonus so :meth:`SpeculativeServer.run` halts.
    """

    engine: TargetEngine
    eos_token_ids: Optional[TokenId | Iterable[TokenId]] = None
    _eos: FrozenSet[TokenId] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._eos = _normalize_eos_token_ids(self.eos_token_ids)

    def verify(self, context: Sequence[TokenId], tree: DraftTree) -> VerifyResult:
        if not context:
            raise ValueError("SGLangVerifier requires a non-empty context")
        c = len(context)
        flat = flatten(c, tree)
        preds = self.engine.predict(context, flat)
        if len(preds) != flat.total_len:
            raise ValueError(
                f"engine returned {len(preds)} predictions; "
                f"expected {flat.total_len}"
            )

        accepted: List[TokenId] = []
        node = tree.root
        pred_idx = c - 1  # prediction after the last context token = next slot
        while node.children:
            want = preds[pred_idx]
            # EOS terminates generation; it is control flow, not completion
            # content.  Check before matching a drafted child so an EOS copied
            # from prior chat-template separators is never accepted/emitted and
            # no prediction conditioned on the post-EOS branch becomes a bonus.
            if want in self._eos:
                return VerifyResult(accepted=accepted, bonus=None)
            child = next((ch for ch in node.children if ch.token == want), None)
            if child is None:
                break  # model diverges from every drafted branch here
            accepted.append(child.token)
            node = child
            pred_idx = flat.node_index[id(child)]

        bonus: Optional[TokenId] = preds[pred_idx]
        if bonus in self._eos:
            bonus = None
        return VerifyResult(accepted=accepted, bonus=bonus)


class SGLangTreeEngine(TargetEngine):
    """Production :class:`TargetEngine` backed by a live SGLang engine. **Stub.**

    Planned wiring (the one remaining GPU-side task):

    1. Build the batched forward inputs from a :class:`FlatDraft`: ``input_ids``
       = context ++ ``flat.tokens``, ``positions`` = context range ++
       ``flat.positions``, and a tree-attention bias from ``flat.parents`` (or
       the dense :func:`attention_mask`) so each draft token attends only to its
       ancestor chain.
    2. Run SGLang's tree-attention verify kernel once, reusing the prefilled
       context KV cache.
    3. Return per-position ``argmax`` of the output logits as the greedy
       predictions (sampling-based rejection verification is a later upgrade).

    Kept as an explicit stub so the verifier logic above stays importable and
    fully unit-tested without sglang or a GPU.
    """

    def __init__(self, engine: object | None = None) -> None:
        self.engine = engine

    def predict(self, context: Sequence[TokenId], flat: FlatDraft) -> List[TokenId]:
        raise NotImplementedError(
            "SGLangTreeEngine needs a live SGLang engine. Build the tree-attention "
            "forward from FlatDraft (input_ids/positions/parents), run one verify "
            "pass, and return per-position argmax. SGLangVerifier already drives "
            "any TargetEngine — see the fake engine in the tests for the contract."
        )
