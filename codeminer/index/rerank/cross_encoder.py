# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-encoder reranker wrappers.

Two backends share a common ``score(query, docs) -> List[float]`` interface:

  - ``STCrossEncoderWrapper``: thin wrapper around
    :class:`sentence_transformers.CrossEncoder`. Works for mxbai-rerank-*,
    BAAI/bge-reranker-*, jinaai/jina-reranker-* and any model that ships a
    standard sentence-transformers cross-encoder config.

  - ``QwenRerankerWrapper``: implements the Qwen3-Reranker family API. These
    models are decoder-only causal LMs trained to emit ``yes`` or ``no`` for
    a (Instruct, Query, Document) prompt. The relevance score is the
    softmax-normalized probability of the ``yes`` token.

Use :func:`build_reranker` as a factory; it dispatches by model name.
"""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Standard sentence-transformers CrossEncoder (mxbai / bge / jina-rerank)
# ---------------------------------------------------------------------------


class STCrossEncoderWrapper:
    """Thin wrapper around ``sentence_transformers.CrossEncoder``.

    ``score(query, docs)`` returns one relevance scalar per ``doc``; sign
    convention: higher = more relevant.
    """

    def __init__(
        self,
        model_name: str,
        *,
        max_length: Optional[int] = None,
        batch_size: int = 16,
        trust_remote_code: bool = True,
        device: Optional[str] = None,
        torch_dtype: Optional[str] = None,
        instruction: Optional[
            str
        ] = None,  # ignored — ST CrossEncoders don't take instructions
    ) -> None:
        from sentence_transformers import CrossEncoder

        self.model_name = model_name
        self.batch_size = batch_size
        if instruction:
            logger.debug(
                "STCrossEncoderWrapper(%s) ignoring instruction=%r — "
                "standard CrossEncoder models do not consume an instruction.",
                model_name,
                instruction,
            )

        kwargs = {"trust_remote_code": trust_remote_code}
        if max_length is not None:
            kwargs["max_length"] = max_length
        if device is not None:
            kwargs["device"] = device
        # CrossEncoder forwards ``model_kwargs`` to the underlying HF model.
        if torch_dtype is not None:
            kwargs["model_kwargs"] = {"torch_dtype": torch_dtype}

        self._model = CrossEncoder(model_name, **kwargs)
        logger.info(
            "Loaded ST CrossEncoder: %s (max_length=%s, batch_size=%d)",
            model_name,
            max_length,
            batch_size,
        )

    def score(self, query: str, docs: List[str]) -> List[float]:
        if not docs:
            return []
        pairs = [(query, d) for d in docs]
        scores = self._model.predict(
            pairs,
            batch_size=self.batch_size,
            show_progress_bar=False,
        )
        return [float(s) for s in scores]

    def close(self) -> None:
        # Best-effort teardown; never raise from close() but keep cleanup
        # failures observable at debug level for diagnostics.
        try:
            del self._model
        except Exception:
            logger.debug(
                "STCrossEncoderWrapper.close: model already absent or " "non-deletable",
                exc_info=True,
            )
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            logger.debug(
                "STCrossEncoderWrapper.close: gc/cuda cleanup raised; " "continuing",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Qwen3-Reranker (decoder-only, yes/no-token scoring)
# ---------------------------------------------------------------------------


_QWEN_DEFAULT_INSTRUCTION = (
    "Given a github issue, identify the code that needs to be changed "
    "to fix the issue."
)
_QWEN_PREFIX = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or '
    '"no".<|im_end|>\n<|im_start|>user\n'
)
_QWEN_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


class QwenRerankerWrapper:
    """Score (query, doc) pairs with Qwen3-Reranker (yes/no logit trick).

    Implements the inference recipe from the model card: feed a prompt of
    ``<Instruct>: <task>\\n<Query>: <q>\\n<Document>: <d>``, take the logits
    at the last position, and compute
    ``softmax([logit_yes, logit_no])[0]`` as the relevance score. Higher =
    more relevant.

    Scoring is done in batches; long documents are truncated from the *front*
    so the trailing portion (typically the most informative for our
    issue→code task) is preserved.
    """

    def __init__(
        self,
        model_name: str,
        *,
        max_length: int = 8192,
        batch_size: int = 8,
        instruction: str = _QWEN_DEFAULT_INSTRUCTION,
        device: Optional[str] = None,
        torch_dtype: Optional[str] = "auto",
        trust_remote_code: bool = True,
    ) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.model_name = model_name
        self.max_length = max_length
        self.batch_size = batch_size
        self.instruction = instruction

        self._tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            padding_side="left",
            trust_remote_code=trust_remote_code,
        )

        model_kwargs = {"trust_remote_code": trust_remote_code}
        if torch_dtype is not None:
            model_kwargs["torch_dtype"] = torch_dtype
        self._model = AutoModelForCausalLM.from_pretrained(model_name, **model_kwargs)

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = device
        self._model = self._model.to(device).eval()

        # Pre-resolve yes/no token ids and the prompt prefix/suffix tokens
        # so each scoring call only tokenizes the pair body.
        self._yes_id = self._tokenizer.convert_tokens_to_ids("yes")
        self._no_id = self._tokenizer.convert_tokens_to_ids("no")
        if self._yes_id is None or self._no_id is None:
            raise RuntimeError(
                f"Tokenizer for {model_name} did not yield 'yes'/'no' ids. "
                "Check whether this model is a Qwen3-Reranker variant."
            )

        self._prefix_ids = self._tokenizer.encode(
            _QWEN_PREFIX, add_special_tokens=False
        )
        self._suffix_ids = self._tokenizer.encode(
            _QWEN_SUFFIX, add_special_tokens=False
        )

        logger.info(
            "Loaded Qwen reranker: %s (device=%s, max_length=%d, "
            "batch_size=%d, instruction=%r)",
            model_name,
            device,
            max_length,
            batch_size,
            instruction,
        )

    def _format_pair(self, query: str, doc: str) -> str:
        return (
            f"<Instruct>: {self.instruction}\n"
            f"<Query>: {query}\n"
            f"<Document>: {doc}"
        )

    def _build_input_ids(self, pair_texts: List[str]) -> dict:
        # Tokenize the pair body, then reserve room for prefix+suffix.
        body_budget = max(
            8,
            self.max_length - len(self._prefix_ids) - len(self._suffix_ids),
        )
        body_enc = self._tokenizer(
            pair_texts,
            padding=False,
            truncation="longest_first",
            max_length=body_budget,
            add_special_tokens=False,
            return_attention_mask=False,
        )
        # Splice prefix + body + suffix per example, then left-pad as a batch.
        full = [
            self._prefix_ids + ids + self._suffix_ids for ids in body_enc["input_ids"]
        ]
        return self._tokenizer.pad(
            {"input_ids": full},
            padding=True,
            return_tensors="pt",
        )

    def score(self, query: str, docs: List[str]) -> List[float]:
        """Score (query, doc) pairs as P(yes-token) at the last position.

        Defends against CUDA OOM on long-context instances (long L2 chunks
        from large repos like prometheus, jq, ruff) by halving the batch
        size on each ``OutOfMemoryError`` and retrying the same chunk. At
        batch=1, a single still-OOMing doc is assigned score 0.0 so the
        rest of the candidates still get ranked instead of poisoning the
        whole run.
        """
        if not docs:
            return []
        import torch

        pair_texts = [self._format_pair(query, d) for d in docs]
        out: List[float] = []
        i = 0
        batch_size = self.batch_size

        with torch.inference_mode():
            while i < len(pair_texts):
                chunk = pair_texts[i : i + batch_size]
                try:
                    out.extend(self._score_chunk(chunk))
                    i += len(chunk)
                except torch.cuda.OutOfMemoryError:
                    torch.cuda.empty_cache()
                    if batch_size > 1:
                        new_bs = max(1, batch_size // 2)
                        logger.warning(
                            "QwenRerankerWrapper OOM at batch=%d; halving "
                            "to %d and retrying (%d docs remaining)",
                            batch_size,
                            new_bs,
                            len(pair_texts) - i,
                        )
                        batch_size = new_bs
                        continue
                    logger.error(
                        "QwenRerankerWrapper OOM at batch=1 on doc %d "
                        "(content len=%d); assigning score 0.0 and "
                        "continuing",
                        i,
                        len(docs[i]) if i < len(docs) else 0,
                    )
                    out.append(0.0)
                    i += 1
        return out

    def _score_chunk(self, pair_texts: List[str]) -> List[float]:
        """Single forward pass + yes/no-token softmax for a batch."""
        import torch

        inputs = self._build_input_ids(pair_texts)
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        logits = self._model(**inputs).logits  # (B, T, V)
        last = logits[:, -1, :]  # (B, V)
        yes_no = torch.stack([last[:, self._yes_id], last[:, self._no_id]], dim=-1)
        probs = torch.softmax(yes_no.float(), dim=-1)
        return probs[:, 0].tolist()

    def close(self) -> None:
        # Best-effort teardown; never raise from close() but keep cleanup
        # failures observable at debug level for diagnostics.
        try:
            del self._model
            del self._tokenizer
        except Exception:
            logger.debug(
                "QwenRerankerWrapper.close: model/tokenizer already absent or "
                "non-deletable",
                exc_info=True,
            )
        try:
            import gc

            import torch

            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            logger.debug(
                "QwenRerankerWrapper.close: gc/cuda cleanup raised; " "continuing",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_reranker(
    model_name: str,
    *,
    backend: Optional[str] = None,
    **kwargs,
):
    """Build a reranker wrapper for ``model_name``.

    ``backend`` may be ``"qwen"`` or ``"st"`` to force a backend; when
    omitted, the choice is inferred from the model name.
    """
    if backend is None:
        backend = "qwen" if "Qwen3-Reranker" in model_name else "st"

    if backend == "qwen":
        return QwenRerankerWrapper(model_name, **kwargs)
    if backend == "st":
        return STCrossEncoderWrapper(model_name, **kwargs)
    raise ValueError(f"Unknown reranker backend: {backend!r}")
