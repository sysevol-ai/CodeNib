# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""HF-transformers tree-attention ``TargetEngine``.

The first *real* :class:`~codenib.serving.server.sglang.TargetEngine`: it runs an HF
``AutoModelForCausalLM`` over a whole flattened draft tree in **one forward pass**
and returns each position's greedy next-token prediction, which is exactly what
:class:`~codenib.serving.server.sglang.SGLangVerifier` consumes. Unlike the linear
``RealModelVerifier`` (which verifies one branch), this verifies every branch of
a fused tree simultaneously via a tree-attention mask.

How the single forward encodes the tree (all metadata comes from
:func:`~codenib.serving.server.sglang.flatten`):

* ``input_ids`` = ``context ++ flat.tokens``.
* ``position_ids`` = ``range(context_len) ++ flat.positions`` — sibling draft
  tokens share a position id (they are alternatives for the same slot), so every
  root-to-leaf branch sees a contiguous ``0..n`` position sequence.
* a **4D additive attention mask** (:meth:`HFTreeEngine._tree_allow`, a
  vectorized equivalent of :func:`~codenib.serving.server.sglang.attention_mask`):
  context is causal and each draft token attends to all context plus its own
  ancestor chain (never sibling or cousin branches). Allowed pairs map to
  ``0.0``, disallowed to the dtype's most-negative value.

The model is loaded with ``attn_implementation="eager"`` so the custom 4D mask is
honored — fused/flash kernels ignore an explicit mask and re-derive a plain
causal one, which would let branches see each other.

**Scope:** greedy verification. :class:`HFTreeEngine` re-forwards the whole
context each step — a faithful *tokens/forward-pass* (tau) reference but not a
wall-clock one. :class:`CachedHFTreeEngine` adds committed-prefix KV reuse so
only the draft (plus newly committed tokens) is forwarded per step, which is the
wall-clock engine. The SGLang tree-attention kernel remains a follow-up; see
``docs/superpowers/specs/2026-07-03-hf-tree-engine-design.md``.
"""

from __future__ import annotations

from typing import List, Sequence

from codenib.serving.server.sglang import FlatDraft, TargetEngine
from codenib.serving.types import TokenId


class HFTreeEngine(TargetEngine):
    """Tree-attention target forward backed by an HF causal LM.

    The model is injected so the engine is testable with a tiny random-weights
    model on CPU; use :meth:`from_pretrained` for a real checkpoint.

    Args:
        model: a loaded HF ``*ForCausalLM`` returning ``.logits`` of shape
            ``[batch, seq, vocab]``. Should be loaded with
            ``attn_implementation="eager"`` (see :meth:`from_pretrained`).
        device: torch device string for the input tensors; must match the model.
    """

    def __init__(self, model: object, *, device: str = "cuda") -> None:
        self.model = model
        self.device = device
        #: Running count of token-positions pushed through the model. Lets a
        #: benchmark quantify the work saved by KV-cache reuse
        #: (see :class:`CachedHFTreeEngine`).
        self.forwarded_positions = 0

    @classmethod
    def from_pretrained(
        cls,
        model_name: str,
        *,
        device: str = "cuda",
        dtype: object = None,
        model_class: object = None,
    ) -> "HFTreeEngine":
        """Load ``model_name`` as an eager-attention causal LM on ``device``.

        Shares :func:`~codenib.serving.server.real_model._load_lm` with
        ``RealModelVerifier``, so the same ``model_class`` escape hatch applies
        (pass an explicit auto-class for a target the ``AutoModelForCausalLM``
        mapping rejects). ``attn_implementation="eager"`` is required by the
        custom 4D tree mask and is always forced here.
        """
        import torch

        from codenib.serving.server.real_model import _load_lm

        model = (
            _load_lm(
                model_name,
                dtype=dtype or torch.bfloat16,
                model_class=model_class,
                attn_implementation="eager",
            )
            .to(device)
            .eval()
        )
        return cls(model, device=device)

    def _tree_allow(self, flat: FlatDraft):
        """Boolean allow-matrix ``[total_len, total_len]`` for the full tree.

        Identical to :func:`~codenib.serving.server.sglang.attention_mask` but built
        with vectorized torch ops: the context-causal block (the only
        O(context^2) part) is a single ``tril`` instead of a Python double loop,
        so a long context no longer costs O(context^2) *Python* work every step.
        The remaining loop is over draft tokens only (bounded by the speculation
        budget), independent of context length.
        """
        import torch

        c = flat.context_len
        total = flat.total_len
        allow = torch.zeros((total, total), dtype=torch.bool, device=self.device)
        allow[:c, :c] = torch.tril(
            torch.ones((c, c), dtype=torch.bool, device=self.device)
        )
        if flat.size:
            allow[c:, :c] = True  # every draft token attends to all context
            for off in range(flat.size):  # self + ancestor chain among drafts
                a = c + off
                while a >= c:
                    allow[c + off, a] = True
                    a = flat.parents[a - c]
        return allow

    def predict(self, context: Sequence[TokenId], flat: FlatDraft) -> List[TokenId]:
        import torch

        n = flat.total_len
        input_ids = torch.tensor(
            [list(context) + list(flat.tokens)], dtype=torch.long, device=self.device
        )
        position_ids = torch.tensor(
            [list(range(flat.context_len)) + list(flat.positions)],
            dtype=torch.long,
            device=self.device,
        )

        # Boolean allow-matrix (context causal + each draft token's ancestor
        # chain) -> 4D additive float mask the model consumes as an attention bias.
        param_dtype = next(self.model.parameters()).dtype
        allow = self._tree_allow(flat)
        bias = torch.zeros((1, 1, n, n), dtype=param_dtype, device=self.device)
        bias.masked_fill_(~allow, torch.finfo(param_dtype).min)

        with torch.no_grad():
            out = self.model(
                input_ids=input_ids,
                attention_mask=bias,
                position_ids=position_ids,
            )
        self.forwarded_positions += n
        return out.logits[0].argmax(dim=-1).tolist()


class CachedHFTreeEngine(HFTreeEngine):
    """:class:`HFTreeEngine` that reuses the committed context's KV cache.

    The stateless base re-forwards the whole ``context`` every step — the KV of
    every already-generated token is recomputed from scratch, so wall-clock
    tokens/s is dominated by re-prefill and the measured speedup is muted (this
    is the caveat throughout the speculative path). This subclass keeps a
    ``DynamicCache`` of the committed prefix and, each step, forwards only the
    **newly committed tokens plus the draft** against that cache — O(draft) work
    per step instead of O(context).

    It is behaviourally identical to the base engine (same greedy predictions,
    hence the same accepted path and bonus token, up to bf16 ties); only the
    amount of compute differs. The mechanics:

    * **Reset** when ``context`` no longer extends the cached prefix — a new run
      (or first call). The cache is per-run state.
    * **Incremental forward** of ``context[cached_len:] ++ flat.tokens`` with a
      compact ``[m, kv_len]`` mask: newly committed tokens are causal; draft
      tokens attend to all committed context plus their own ancestor chain. RoPE
      ``position_ids`` are absolute (siblings share a slot); ``cache_position``
      is the contiguous write slot.
    * **Crop** the cache back to the committed length after each step, discarding
      the KV of every rejected draft branch. Accepted tokens are re-forwarded
      (cheaply) as "newly committed" on the next step, which keeps the cache a
      simple linear prefix rather than a tree.

    Within a run ``context`` must extend monotonically (the speculation loop
    guarantees this).
    """

    def __init__(self, model: object, *, device: str = "cuda") -> None:
        super().__init__(model, device=device)
        self._cache: object = None
        self._committed: List[TokenId] = []
        self._cached_len: int = 0

    def _reset(self) -> None:
        from transformers import DynamicCache

        self._cache = DynamicCache()
        self._committed = []
        self._cached_len = 0

    def _incremental_allow(self, flat: FlatDraft, p: int, m: int, kv_len: int):
        """Boolean allow-matrix ``[m, kv_len]`` for the new tokens only.

        Keys are global positions ``0..kv_len-1`` (cached context, then newly
        committed, then draft). Row ``j`` is the query at global position
        ``p + j``. Committed rows are causal; draft rows attend to all committed
        context (cols ``< context_len``) plus their ancestor chain among the
        draft tokens.
        """
        import torch

        length = flat.context_len
        n_committed = length - p
        allow = torch.zeros((m, kv_len), dtype=torch.bool, device=self.device)
        for j in range(m):
            g = p + j
            if j < n_committed:
                allow[j, : g + 1] = True  # causal over cached + prior committed
            else:
                allow[j, :length] = True  # all committed context
                a = g
                while a >= length:  # self + draft ancestor chain
                    allow[j, a] = True
                    a = flat.parents[a - length]
        return allow

    def predict(self, context: Sequence[TokenId], flat: FlatDraft) -> List[TokenId]:
        import torch

        ctx = list(context)
        length = flat.context_len  # == len(context)

        if (
            self._cache is None
            or self._cached_len > length
            or ctx[: self._cached_len] != self._committed[: self._cached_len]
        ):
            self._reset()

        p = self._cached_len
        new_tokens = ctx[p:length] + list(flat.tokens)
        m = len(new_tokens)
        if m == 0:
            # Nothing new to forward: the context did not advance and the draft
            # tree is empty, so the only read position (the bonus slot at the
            # last context token) already lives in the committed KV cache and its
            # logits are not recomputed here. The speculation loop always appends
            # >= 1 token before the next predict(), so this signals that
            # monotonic-growth precondition was violated by the caller.
            raise ValueError(
                "CachedHFTreeEngine.predict called with no new tokens to "
                "forward (context did not advance and the draft tree is empty)"
            )
        kv_len = p + m

        param_dtype = next(self.model.parameters()).dtype
        input_ids = torch.tensor([new_tokens], dtype=torch.long, device=self.device)
        position_ids = torch.tensor(
            [list(range(p, length)) + list(flat.positions)],
            dtype=torch.long,
            device=self.device,
        )
        cache_position = torch.arange(p, p + m, device=self.device)

        allow = self._incremental_allow(flat, p, m, kv_len)
        bias = torch.zeros((1, 1, m, kv_len), dtype=param_dtype, device=self.device)
        bias.masked_fill_(~allow, torch.finfo(param_dtype).min)

        with torch.no_grad():
            out = self.model(
                input_ids=input_ids,
                attention_mask=bias,
                position_ids=position_ids,
                past_key_values=self._cache,
                cache_position=cache_position,
                use_cache=True,
            )
        self.forwarded_positions += m
        argmax = out.logits[0].argmax(dim=-1).tolist()

        # Full-length result addressed by global index; only the last context
        # position and the draft nodes are read by SGLangVerifier. New token j
        # sits at global index p + j.
        preds: List[TokenId] = [-1] * flat.total_len
        for j in range(m):
            preds[p + j] = argmax[j]

        # Commit the context KV, drop every draft branch, advance the prefix.
        self._cache = out.past_key_values
        self._cache.crop(length)
        self._committed = ctx[:length]
        self._cached_len = length
        return preds
