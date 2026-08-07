# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the OpenAI-compatible endpoint (:mod:`codenib.serving.server.api`).

Driven with a fake tokenizer (char <-> id) and a ``_GreedyTruthEngine`` that
continues a known ``truth`` sequence, so the whole request path runs on CPU with
no model download. The fakes mirror the patterns in ``test_sglang_verifier.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import List, Sequence

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

from fastapi.testclient import TestClient  # noqa: E402

from codenib.serving.server.api import AppState  # noqa: E402
from codenib.serving.server.api import (
    _REPLACEMENT,
    _IncrementalDecoder,
    _stream,
    create_app,
)
from codenib.serving.server.sglang import TargetEngine  # noqa: E402
from codenib.serving.server.tokenization import Tokenizer  # noqa: E402
from codenib.serving.server.worker import SpeculativeConfig  # noqa: E402
from codenib.serving.types import TokenId  # noqa: E402

_EOS = 0
_COMPLETION = " world"


class _FakeTok:
    """Char-code tokenizer with a trivial chat template (concatenate contents)."""

    eos_token_id = _EOS

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=True):
        return "".join(m["content"] for m in msgs)

    def encode(self, text: str, add_special_tokens: bool = False) -> List[int]:
        return [ord(c) for c in text]

    def decode(self, ids: List[int], skip_special_tokens: bool = True) -> str:
        return "".join(chr(i) for i in ids if not (skip_special_tokens and i == _EOS))


class _GreedyTruthEngine(TargetEngine):
    """Greedily continues ``truth``; predicts ``eos`` once past its end."""

    def __init__(self, truth: Sequence[TokenId], eos: int = _EOS) -> None:
        self.truth = list(truth)
        self.eos = eos

    def predict(self, context: Sequence[TokenId], flat) -> List[TokenId]:
        positions = list(range(flat.context_len)) + list(flat.positions)
        n = len(self.truth)
        return [self.truth[p + 1] if p + 1 < n else self.eos for p in positions]


def _make_client() -> TestClient:
    tok = Tokenizer(_FakeTok())
    prompt_ids = tok.encode("hi")  # matches apply_chat_template of the message below
    truth = prompt_ids + tok.encode(_COMPLETION)
    state = AppState(
        served_model_name="codenib-test",
        tokenizer=tok,
        engine_factory=lambda: _GreedyTruthEngine(truth),
        drafters=[],
        config=SpeculativeConfig(max_draft_tokens=8),
        default_max_new_tokens=64,
    )
    return TestClient(create_app(state))


_MESSAGES = [{"role": "user", "content": "hi"}]


def test_health() -> None:
    client = _make_client()
    assert client.get("/health").json() == {"status": "ok"}


def test_models_lists_served_model() -> None:
    client = _make_client()
    data = client.get("/v1/models").json()
    assert data["object"] == "list"
    assert data["data"][0]["id"] == "codenib-test"


def test_chat_completion_returns_greedy_continuation() -> None:
    client = _make_client()
    resp = client.post(
        "/v1/chat/completions", json={"model": "m", "messages": _MESSAGES}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == _COMPLETION
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["completion_tokens"] == len(_COMPLETION)
    assert data["usage"]["prompt_tokens"] == 2


def test_temperature_above_zero_is_rejected() -> None:
    client = _make_client()
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": _MESSAGES, "temperature": 0.7},
    )
    assert resp.status_code == 400
    assert "greedy" in resp.json()["detail"]


def test_top_p_below_one_is_rejected() -> None:
    client = _make_client()
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": _MESSAGES, "top_p": 0.5},
    )
    assert resp.status_code == 400


def test_streaming_chunks_reassemble_to_completion() -> None:
    client = _make_client()
    content = ""
    finish_seen = False
    done_seen = False
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "m", "messages": _MESSAGES, "stream": True},
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                done_seen = True
                continue
            chunk = json.loads(payload)
            assert chunk["object"] == "chat.completion.chunk"
            delta = chunk["choices"][0]["delta"]
            if delta.get("content"):
                content += delta["content"]
            if chunk["choices"][0]["finish_reason"] == "stop":
                finish_seen = True
    assert content == _COMPLETION
    assert finish_seen
    assert done_seen


# --- regression: multi-byte-safe streaming deltas -------------------------


class _ByteTok:
    """UTF-8 *byte* tokenizer: one id per byte, so characters straddle tokens.

    This is the shape that breaks naive ``full[len(text_so_far):]`` slicing —
    decoding a prefix that ends mid-character yields U+FFFD, which then vanishes
    once the continuation byte arrives.
    """

    eos_token_id = _EOS

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=True):
        return "".join(m["content"] for m in msgs)

    def encode(self, text: str, add_special_tokens: bool = False) -> List[int]:
        return list(text.encode("utf-8"))

    def decode(self, ids: List[int], skip_special_tokens: bool = True) -> str:
        keep = bytes(i for i in ids if not (skip_special_tokens and i == _EOS))
        return keep.decode("utf-8", errors="replace")


_MULTIBYTE = " héllo 😀!"


def test_incremental_decoder_holds_back_incomplete_multibyte() -> None:
    tok = Tokenizer(_ByteTok())
    decoder = _IncrementalDecoder(tok)
    # Feed one byte at a time — the worst case for prefix-slicing.
    out = "".join(decoder.push([i]) for i in tok.encode(_MULTIBYTE))
    out += decoder.flush()
    assert out == _MULTIBYTE
    assert _REPLACEMENT not in out


def test_incremental_decoder_emits_nothing_until_a_character_completes() -> None:
    tok = Tokenizer(_ByteTok())
    decoder = _IncrementalDecoder(tok)
    emoji = list("😀".encode("utf-8"))
    assert [decoder.push([b]) for b in emoji[:-1]] == ["", "", ""]
    assert decoder.push([emoji[-1]]) == "😀"


def _make_byte_client() -> TestClient:
    tok = Tokenizer(_ByteTok())
    prompt_ids = tok.encode("hi")
    truth = prompt_ids + tok.encode(_MULTIBYTE)
    state = AppState(
        served_model_name="codenib-test",
        tokenizer=tok,
        engine_factory=lambda: _GreedyTruthEngine(truth),
        drafters=[],
        config=SpeculativeConfig(max_draft_tokens=8),
        default_max_new_tokens=64,
    )
    return TestClient(create_app(state))


def test_streaming_does_not_corrupt_multibyte_characters() -> None:
    client = _make_byte_client()
    content = ""
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "m", "messages": _MESSAGES, "stream": True},
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                continue
            delta = json.loads(payload)["choices"][0]["delta"]
            if delta.get("content"):
                content += delta["content"]
    assert content == _MULTIBYTE
    assert _REPLACEMENT not in content


# --- regression: a disconnect must not release the single-flight lock -----


def test_disconnect_holds_lock_until_worker_stops() -> None:
    """Cancelling the stream must not hand the lock on mid-generation.

    Otherwise the next request starts a second generation while this one is
    still on the GPU, which is exactly what the single-flight lock exists to
    prevent.
    """
    started = threading.Event()
    release = threading.Event()

    tok = Tokenizer(_FakeTok())
    prompt_ids = tok.encode("hi")
    truth = prompt_ids + tok.encode(_COMPLETION)

    class _BlockingEngine(_GreedyTruthEngine):
        """Stands in for a generation step that is still occupying the GPU."""

        def predict(self, context, flat):
            started.set()
            release.wait(10)
            return super().predict(context, flat)

    state = AppState(
        served_model_name="codenib-test",
        tokenizer=tok,
        engine_factory=lambda: _BlockingEngine(truth),
        drafters=[],
        config=SpeculativeConfig(max_draft_tokens=8),
        default_max_new_tokens=64,
    )

    async def scenario() -> None:
        agen = _stream(state, prompt_ids, 64, "m")
        await agen.asend(None)  # role chunk; the lock is now held
        assert state.lock.locked()

        # Resuming past the first yield starts the worker thread. It blocks in
        # predict(), so this send never completes — it stands in for a consumer
        # waiting on deltas.
        pump = asyncio.create_task(agen.asend(None))
        await asyncio.to_thread(started.wait, 10)
        assert started.is_set()

        pump.cancel()  # the client disconnects
        with pytest.raises(asyncio.CancelledError):
            await pump

        # The worker is still inside predict(): the lock must NOT be free yet.
        assert state.lock.locked()

        release.set()
        for _ in range(1000):
            if not state.lock.locked():
                break
            await asyncio.sleep(0.01)
        assert not state.lock.locked()

    asyncio.run(scenario())


# --- CLI wiring: --manifest -> RetrievalDrafter ------------------
# `main()` is not directly testable (it calls uvicorn.run), so the two decisions
# it makes are factored out: argument parsing and drafter construction. These
# cover the wiring only; CodeNibBackend's own behaviour lives in
# test_codenib_backend.py.


def test_parse_args_defaults_to_no_manifest() -> None:
    from codenib.serving.server.api import _parse_args

    assert _parse_args([]).manifest is None


def test_parse_args_accepts_manifest() -> None:
    from codenib.serving.server.api import _parse_args

    args = _parse_args(["--manifest", "/repo/.codenib_cache/m.json"])
    assert args.manifest == "/repo/.codenib_cache/m.json"


def test_build_drafters_without_manifest_is_copy_only() -> None:
    from codenib.serving.drafter.copy import CopyDrafter
    from codenib.serving.server.api import _build_drafters, _parse_args

    drafters = _build_drafters(_parse_args([]), Tokenizer(_FakeTok()))

    assert len(drafters) == 1
    assert isinstance(drafters[0], CopyDrafter)


def test_build_drafters_with_manifest_appends_retrieval_drafter(monkeypatch) -> None:
    """A manifest must load a CodeNibBackend and fuse it in after copy.

    ``from_manifest`` is the one call that needs the ``codenib`` package and a
    prebuilt index on disk, so it is stubbed; everything either side is real.
    """
    from codenib.serving.drafter.copy import CopyDrafter
    from codenib.serving.drafter.retrieval import CodeNibBackend, RetrievalDrafter
    from codenib.serving.server.api import _build_drafters, _parse_args

    sentinel = object()
    seen = {}

    def _fake_from_manifest(path, tokenizer, **kwargs):
        seen["path"] = path
        seen["tokenizer"] = tokenizer
        return sentinel

    monkeypatch.setattr(CodeNibBackend, "from_manifest", _fake_from_manifest)

    tok = Tokenizer(_FakeTok())
    drafters = _build_drafters(_parse_args(["--manifest", "/m.json"]), tok)

    assert [type(d) for d in drafters] == [CopyDrafter, RetrievalDrafter]
    retr = drafters[1]
    assert isinstance(retr, RetrievalDrafter)  # narrows List[Drafter] for mypy
    assert retr.backend is sentinel  # the loaded index, not a fresh one
    assert seen["path"] == "/m.json"
    assert seen["tokenizer"] is tok  # serving tokenizer, so ids line up
