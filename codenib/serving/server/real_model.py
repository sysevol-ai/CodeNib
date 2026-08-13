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
directly (verified on transformers 5.12). A model outside that mapping loads
only if its ``model_type`` is in :data:`VERIFIED_NON_CAUSAL_MODEL_TYPES` — an
allowlist of architectures whose ``input_ids``-only forward is known to emit
causal-LM ``.logits`` — and each registry load is validated with a one-token
text forward before serving. Anything else fails loudly; the ``model_class``
parameter remains as the explicit escape hatch (and is how tests inject stubs).

torch/transformers are imported lazily so this module (and ``best_linear_path``)
stay importable without the ``bench`` extra installed.
"""

from __future__ import annotations

from typing import List, Sequence

from codenib.serving.server.sglang import _normalize_eos_token_ids
from codenib.serving.server.worker import Verifier, VerifyResult
from codenib.serving.types import DraftTree, TokenId

#: ``model_type`` values that live outside the transformers causal-LM
#: auto-mapping but whose ``input_ids``-only forward is verified to emit
#: causal-LM ``.logits``. Values name the auto-class attribute on
#: ``transformers`` (resolved lazily, so this module imports without the
#: serving extra). Add a type here only after checking its text-only forward;
#: every registry load is additionally validated by
#: :func:`_validate_text_logits` at startup.
VERIFIED_NON_CAUSAL_MODEL_TYPES = {
    "muse_glimmer": "AutoModelForImageTextToText",
}


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


def _resolve_model_class(model_name: str):
    """Pick the auto-class for ``model_name`` from its config ``model_type``.

    Resolution is fail-closed, mirroring how the compiler picks index builders
    from :class:`~codenib.compiler.index_builders.IndexBuilderRegistry`: the
    causal-LM auto-mapping wins, then the
    :data:`VERIFIED_NON_CAUSAL_MODEL_TYPES` allowlist (the returned marker makes
    the caller run the one-token forward validation), and an unknown type raises
    rather than guessing — a class that merely *loads* can still verify
    speculation against the wrong logits.

    Returns:
        ``(auto_class, needs_validation)``.
    """
    import transformers
    from transformers import AutoConfig, AutoModelForCausalLM
    from transformers.models.auto.modeling_auto import MODEL_FOR_CAUSAL_LM_MAPPING_NAMES

    model_type = getattr(AutoConfig.from_pretrained(model_name), "model_type", None)
    if model_type in MODEL_FOR_CAUSAL_LM_MAPPING_NAMES:
        return AutoModelForCausalLM, False
    registered = VERIFIED_NON_CAUSAL_MODEL_TYPES.get(model_type)
    if registered is not None:
        return getattr(transformers, registered), True
    raise RuntimeError(
        f"model_type {model_type!r} of {model_name!r} is neither in the "
        "transformers causal-LM auto-mapping nor in "
        "VERIFIED_NON_CAUSAL_MODEL_TYPES; verify its text-only forward emits "
        "causal-LM logits, then add it to the registry (or pass an explicit "
        "model_class)"
    )


def _validate_text_logits(model, model_name: str) -> None:
    """Assert ``model`` emits ``[batch, seq, vocab]`` logits from a text forward.

    Turns the registry's behavioral assumption into a startup invariant: a
    registry-listed class that needs pixel inputs, or that routes text through a
    head with different logits semantics, fails here — before the server binds a
    port — instead of silently corrupting speculation verification. One token on
    the load-time device; for large models this is a one-off startup cost.
    """
    import torch

    token = getattr(model.config, "bos_token_id", None) or 0
    try:
        with torch.no_grad():
            out = model(input_ids=torch.tensor([[token]], dtype=torch.long))
    except Exception as exc:
        raise RuntimeError(
            f"{model_name!r} rejected a text-only forward; its registry entry "
            "in VERIFIED_NON_CAUSAL_MODEL_TYPES is wrong for this checkpoint"
        ) from exc
    logits = getattr(out, "logits", None)
    if logits is None or getattr(logits, "ndim", None) != 3:
        raise RuntimeError(
            f"text-only forward of {model_name!r} did not return "
            "[batch, seq, vocab] logits; the model cannot back greedy "
            "speculation verification"
        )


def _load_lm(
    model_name: str,
    *,
    dtype: object,
    model_class: object = None,
    **from_pretrained_kwargs,
):
    """Load a model that exposes causal-LM ``.logits`` from a text-only forward.

    The auto-class comes from :func:`_resolve_model_class`: the causal-LM
    auto-mapping (covers every target we run, including ``Qwen/Qwen3.5-4B`` —
    its ``Qwen3_5ForCausalLM`` is in that mapping on transformers >= 5.2), then
    the :data:`VERIFIED_NON_CAUSAL_MODEL_TYPES` allowlist, whose loads are
    validated with a one-token text forward. An explicit ``model_class`` skips
    both resolution and validation — the caller asserts the class is right
    (tests use this to inject stubs).

    Extra ``from_pretrained_kwargs`` are forwarded verbatim, so callers with
    stricter loading needs can pass them through — e.g. the tree engine loads
    with ``attn_implementation="eager"`` (see :meth:`HFTreeEngine.from_pretrained`).
    """
    if model_class is not None:
        cls, needs_validation = model_class, False
    else:
        cls, needs_validation = _resolve_model_class(model_name)
    # ``torch_dtype`` is supported by the oldest transformers version in the
    # serving extra (4.44).  The shorter ``dtype`` spelling was added later and
    # is forwarded to model constructors by older releases, where it fails.
    model = cls.from_pretrained(
        model_name,
        torch_dtype=dtype,
        **from_pretrained_kwargs,
    )
    if needs_validation:
        _validate_text_logits(model, model_name)
    return model


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
        generation_config = getattr(self.model, "generation_config", None)
        eos = getattr(generation_config, "eos_token_id", None)
        if eos is None:
            eos = self.model.config.eos_token_id
        if eos is None:
            eos = self.tokenizer.eos_token_id
        self._eos = _normalize_eos_token_ids(eos)

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
            # Do not accept an EOS draft and then read a post-EOS prediction as
            # the bonus token.  EOS controls termination and is not emitted as
            # completion content by this serving loop.
            if pred in self._eos:
                return VerifyResult(accepted=accepted, bonus=None)
            if pred != drafted:
                break
            accepted.append(drafted)

        bonus = int(logits[offset + len(accepted)].argmax(-1))
        return VerifyResult(
            accepted=accepted,
            bonus=None if bonus in self._eos else bonus,
        )
