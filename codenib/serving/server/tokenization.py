# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The text↔token seam for the serving layer.

Everything in :mod:`codenib.serving` speaks token-ids (``TokenId = int``) and is
deliberately tokenizer-agnostic. A serving endpoint, however, receives text and
must return text, and the CodeNib retrieval backend must tokenize the snippets
it fetches. This module is the single place that bridges the two — a thin wrapper
over an HF ``AutoTokenizer`` exposing exactly what the rest of the stack needs:
``encode``, ``decode``, and the ``eos_token_id`` that
:class:`~codenib.serving.server.sglang.SGLangVerifier` uses to stop.

The tokenizer is injected (like the model in :class:`HFTreeEngine`) so tests can
pass a tiny fake; use :meth:`from_pretrained` for a real checkpoint. Importing
this module requires ``transformers`` (the ``serve``/``bench`` extra), which the
serving layer needs anyway; injection keeps the *tests* free of it.
"""

from __future__ import annotations

from typing import Any, List, Optional, Protocol, Sequence

from codenib.serving.types import TokenId


class TokenizerLike(Protocol):
    """The exact slice of an HF tokenizer that :class:`Tokenizer` delegates to.

    Declaring it structurally keeps the injection seam open — an HF
    ``PreTrainedTokenizer*`` and a hand-rolled test fake both satisfy it — while
    still letting the delegating calls below be type-checked.
    """

    def encode(self, text: str, *, add_special_tokens: bool = ...) -> Sequence[int]: ...

    # ``ids`` is List, not Sequence: the wrapper always materializes a list
    # before delegating, and HF tokenizers/fakes annotate the narrower type.
    def decode(self, ids: List[int], *, skip_special_tokens: bool = ...) -> str: ...

    @property
    def eos_token_id(self) -> Optional[int]: ...


class Tokenizer:
    """Minimal encode/decode bridge around an HF tokenizer.

    Args:
        tokenizer: any object satisfying :class:`TokenizerLike` — an HF
            ``PreTrainedTokenizer*`` or a compatible fake.
    """

    def __init__(self, tokenizer: TokenizerLike) -> None:
        #: The underlying HF tokenizer, deliberately untyped. Exposed so callers
        #: that need extra capabilities (e.g. ``apply_chat_template``) can reach
        #: them directly without this wrapper re-exporting — or this module
        #: re-declaring — the whole tokenizer surface.
        self.hf: Any = tokenizer
        #: The same object under its checked, narrow type. The three delegating
        #: methods go through this so they stay type-checkable.
        self._tok: TokenizerLike = tokenizer

    @classmethod
    def from_pretrained(cls, model_name: str, **kwargs: Any) -> "Tokenizer":
        """Load ``model_name``'s tokenizer (like ``HFTreeEngine.from_pretrained``)."""
        # Imported here, not at module scope, to match the rest of the serving
        # package: wrapping an injected tokenizer must not require the
        # ``serving`` extra, so only the *loading* path pulls in transformers.
        from transformers import AutoTokenizer

        return cls(AutoTokenizer.from_pretrained(model_name, **kwargs))

    def encode(self, text: str, *, add_special_tokens: bool = False) -> List[TokenId]:
        """Encode ``text`` to token ids.

        Defaults to ``add_special_tokens=False``: the serving layer builds its
        prompt via a chat template (which already inserts the model's special
        tokens), so re-adding them here would double them.
        """
        return list(self._tok.encode(text, add_special_tokens=add_special_tokens))

    def decode(
        self, ids: Sequence[TokenId], *, skip_special_tokens: bool = True
    ) -> str:
        """Decode token ids back to text."""
        return self._tok.decode(list(ids), skip_special_tokens=skip_special_tokens)

    @property
    def eos_token_id(self) -> Optional[TokenId]:
        """The end-of-sequence id, passed to ``SGLangVerifier`` to halt generation."""
        return self._tok.eos_token_id
