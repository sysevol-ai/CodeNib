# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Real-model verifier for the HF-transformers POC.

This is the first non-stub :class:`~codenib.serving.server.worker.Verifier`: instead of
replaying a known continuation (``OracleVerifier``) it loads a real target model
and verifies drafts against the model's own greedy continuation. It is the seam
the real benchmark numbers come from.

**Losslessness.** In exact arithmetic the loop is equivalent to vanilla
autoregressive generation: a drafted token is accepted only when it equals the
model's argmax at that position, and the bonus token is the model's argmax at the
next position. So the emitted sequence is identical to greedy AR — drafting only
changes *how many* tokens come out per forward pass, never *which*. The
vanilla-AR baseline is the same object driven with no drafters
(``SpeculativeServer(drafters=[]).run(prompt, verifier)``), which makes the
speedup comparison fair within one runtime.

The one caveat is numerical, not logical: verifying a *chunk* runs the model over
``context + draft`` (a prefill-shaped forward), while AR decodes one token at a
time, and in bf16 those two paths can produce last-bit-different logits (GPU
matmul reductions are non-associative in the sequence dimension). At an argmax
*tie* — two tokens with equal top logits — that last bit flips which one wins, so
the drafted and AR streams can diverge at a tied position. Empirically this is
rare and only at genuine ties; it is the standard
"lossless up to numerics" property of real speculative decoding, and it does not
affect the acceptance metrics (the drafted run is self-consistent).

**POC scope.** ``verify`` flattens the (usually linear) draft tree to a single
best-scored branch and runs one full forward over ``context + draft`` with no KV
cache reuse. That keeps the mean-accepted-length (tau) measurement exact;
wall-clock tokens/s is muted by the missing cache and is only meaningful once KV
reuse / tree attention lands (a follow-up). Multi-branch tree verification is
also deferred.

**Multimodal targets.** Targets load as ``AutoModelForCausalLM``. Even
``Qwen/Qwen3.5-4B`` — whose config architecture is
``Qwen3_5ForConditionalGeneration`` with a ``vision_config`` — maps cleanly:
``transformers`` (>= 5.2) registers ``Qwen3_5ForCausalLM`` in the causal-LM
auto-mapping, so ``AutoModelForCausalLM.from_pretrained`` returns the text model
directly (verified on transformers 5.12). For any model the causal-LM mapping
genuinely rejects, pass an explicit ``model_class`` (e.g.
``AutoModelForImageTextToText``, whose ``input_ids``-only forward also emits
next-token text ``.logits``); ``RealModelVerifier`` forwards it to :func:`_load_lm`.

torch/transformers are imported lazily so this module (and ``best_linear_path``)
stay importable without the ``bench`` extra installed.
"""

from __future__ import annotations

from typing import List, Sequence

from codenib.serving.server.worker import Verifier, VerifyResult
from codenib.serving.types import DraftTree, TokenId


def best_linear_path(tree: DraftTree) -> List[TokenId]:
    """Flatten a draft tree to one linear branch for single-path verification.

    Walks from the root, always taking the highest-scored child (ties broken by
    insertion order), down to a leaf. Our drafters mostly emit linear trees, so
    this is usually loss-free; for genuinely branching fused trees it picks the
    most-confident branch and leaves the rest to a future tree-attention verify.
    """
    path: List[TokenId] = []
    node = tree.root
    while node.children:
        node = max(node.children, key=lambda c: c.score)
        path.append(node.token)
    return path


def _load_lm(
    model_name: str,
    *,
    dtype: object,
    model_class: object = None,
    **from_pretrained_kwargs,
):
    """Load a model that exposes causal-LM ``.logits`` from a text-only forward.

    Uses ``AutoModelForCausalLM`` by default (covers every target we run,
    including ``Qwen/Qwen3.5-4B`` — its ``Qwen3_5ForCausalLM`` is in the causal-LM
    auto-mapping on transformers >= 5.2). For a model that mapping genuinely
    rejects, pass an explicit ``model_class`` (e.g. ``AutoModelForImageTextToText``,
    whose ``input_ids``-only forward also emits next-token text logits) — it wins
    over the default. Either way the result returns ``.logits`` from a text-only
    forward, which is all :meth:`RealModelVerifier.verify` needs.

    Extra ``from_pretrained_kwargs`` are forwarded verbatim, so callers with
    stricter loading needs can pass them through — e.g. the tree engine loads
    with ``attn_implementation="eager"`` (see :meth:`HFTreeEngine.from_pretrained`).
    """
    from transformers import AutoModelForCausalLM

    cls = model_class if model_class is not None else AutoModelForCausalLM
    return cls.from_pretrained(model_name, dtype=dtype, **from_pretrained_kwargs)


class RealModelVerifier(Verifier):
    """Verify drafts against a real HF ``AutoModelForCausalLM`` (greedy, lossless).

    Args:
        model_name: HF id or local path of the target model.
        device: torch device for the model and inputs (default ``"cuda"``).
        dtype: parameter dtype; defaults to ``torch.bfloat16``.
        model_class: optional explicit auto-class to load with, for a target the
            ``AutoModelForCausalLM`` mapping rejects (e.g.
            ``AutoModelForImageTextToText``); forwarded to :func:`_load_lm`.
    """

    def __init__(
        self,
        model_name: str,
        *,
        device: str = "cuda",
        dtype: object = None,
        model_class: object = None,
    ) -> None:
        import torch
        from transformers import AutoTokenizer

        self.device = device
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = (
            _load_lm(
                model_name,
                dtype=dtype or torch.bfloat16,
                model_class=model_class,
            )
            .to(device)
            .eval()
        )

        # eos can be a single id or a list; normalise to a set for membership.
        eos = self.model.config.eos_token_id
        if eos is None:
            eos = self.tokenizer.eos_token_id
        if eos is None:
            self._eos = set()
        elif isinstance(eos, int):
            self._eos = {eos}
        else:
            self._eos = set(eos)

    def verify(self, context: Sequence[TokenId], tree: DraftTree) -> VerifyResult:
        import torch

        if len(context) == 0:
            raise ValueError("RealModelVerifier requires a non-empty context")

        draft = best_linear_path(tree)
        input_ids = torch.tensor(
            [list(context) + draft], dtype=torch.long, device=self.device
        )
        with torch.no_grad():
            logits = self.model(input_ids).logits[0]  # [T, vocab], T == L + k

        offset = len(context) - 1  # logits[offset] predicts the first new token
        accepted: List[TokenId] = []
        for j, drafted in enumerate(draft):
            pred = int(logits[offset + j].argmax(-1))
            if pred != drafted:
                break
            accepted.append(drafted)

        bonus = int(logits[offset + len(accepted)].argmax(-1))
        return VerifyResult(
            accepted=accepted,
            bonus=None if bonus in self._eos else bonus,
        )
