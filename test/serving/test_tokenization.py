# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the text↔token bridge (:mod:`codenib.serving.server.tokenization`)."""

from __future__ import annotations

from typing import List

from codenib.serving.server.tokenization import Tokenizer

# Sentinel ids for the fake's "special" tokens.
_BOS = 1
_EOS = 2


class _FakeHF:
    """A tiny stand-in for an HF tokenizer: char code <-> id, with specials.

    Mirrors the real ``encode``/``decode``/``eos_token_id`` surface the wrapper
    delegates to, so the bridge is tested without downloading a model.
    """

    eos_token_id = _EOS

    def encode(self, text: str, add_special_tokens: bool = False) -> List[int]:
        ids = [ord(c) for c in text]
        return [_BOS, *ids] if add_special_tokens else ids

    def decode(self, ids: List[int], skip_special_tokens: bool = True) -> str:
        keep = [i for i in ids if not (skip_special_tokens and i in (_BOS, _EOS))]
        return "".join(chr(i) for i in keep)


def test_encode_omits_special_tokens_by_default() -> None:
    tok = Tokenizer(_FakeHF())
    assert tok.encode("ab") == [ord("a"), ord("b")]


def test_encode_can_add_special_tokens() -> None:
    tok = Tokenizer(_FakeHF())
    assert tok.encode("ab", add_special_tokens=True) == [_BOS, ord("a"), ord("b")]


def test_decode_round_trips_encoded_text() -> None:
    tok = Tokenizer(_FakeHF())
    text = "def add(a, b): return a + b"
    assert tok.decode(tok.encode(text)) == text


def test_decode_skips_special_tokens_by_default() -> None:
    tok = Tokenizer(_FakeHF())
    assert tok.decode([_BOS, ord("x"), _EOS]) == "x"


def test_decode_can_keep_special_tokens() -> None:
    tok = Tokenizer(_FakeHF())
    out = tok.decode([_BOS, ord("x")], skip_special_tokens=False)
    assert out == chr(_BOS) + "x"


def test_eos_token_id_is_exposed() -> None:
    tok = Tokenizer(_FakeHF())
    assert tok.eos_token_id == _EOS


def test_hf_tokenizer_is_reachable_for_extras() -> None:
    # The raw tokenizer is exposed so callers can use e.g. apply_chat_template.
    fake = _FakeHF()
    assert Tokenizer(fake).hf is fake
