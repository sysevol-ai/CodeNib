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
from types import SimpleNamespace
from typing import List, Sequence

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

from fastapi.testclient import TestClient  # noqa: E402

from codenib.serving.server.api import AppState  # noqa: E402
from codenib.serving.server.api import (
    _REPLACEMENT,
    MAX_REQUEST_BODY_BYTES,
    _IncrementalDecoder,
    _RequestBodyLimitMiddleware,
    _stream,
    create_app,
)
from codenib.serving.server.schemas import (  # noqa: E402
    MAX_COMPLETION_TOKENS,
    MAX_PROMPT_CHARACTERS,
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
        "/v1/chat/completions",
        json={"model": "codenib-test", "messages": _MESSAGES},
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
        json={
            "model": "codenib-test",
            "messages": _MESSAGES,
            "temperature": 0.7,
        },
    )
    assert resp.status_code == 400
    assert "greedy" in resp.json()["detail"]


def test_top_p_below_one_is_rejected() -> None:
    client = _make_client()
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "codenib-test", "messages": _MESSAGES, "top_p": 0.5},
    )
    assert resp.status_code == 400


def test_wrong_model_is_rejected_instead_of_echoed() -> None:
    resp = _make_client().post(
        "/v1/chat/completions",
        json={"model": "not-served", "messages": _MESSAGES},
    )

    assert resp.status_code == 404
    assert "codenib-test" in resp.json()["detail"]


@pytest.mark.parametrize(
    "unsupported",
    [
        {"tools": [{"type": "function", "function": {"name": "search"}}]},
        {"tool_choice": "auto"},
        {"unknown_option": True},
    ],
)
def test_unsupported_request_fields_are_rejected(unsupported: dict) -> None:
    payload = {
        "model": "codenib-test",
        "messages": _MESSAGES,
        **unsupported,
    }

    assert _make_client().post("/v1/chat/completions", json=payload).status_code == 422


@pytest.mark.parametrize(
    "value",
    [0, -1, True, "8", MAX_COMPLETION_TOKENS + 1],
)
@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_completion_token_limits_are_strict(field: str, value: int) -> None:
    payload = {
        "model": "codenib-test",
        "messages": _MESSAGES,
        field: value,
    }

    assert _make_client().post("/v1/chat/completions", json=payload).status_code == 422


def test_tool_role_is_rejected_when_tool_calls_are_unsupported() -> None:
    resp = _make_client().post(
        "/v1/chat/completions",
        json={
            "model": "codenib-test",
            "messages": [{"role": "tool", "content": "result"}],
        },
    )

    assert resp.status_code == 422


def test_combined_message_characters_are_bounded() -> None:
    resp = _make_client().post(
        "/v1/chat/completions",
        json={
            "model": "codenib-test",
            "messages": [
                {"role": "user", "content": "x" * (MAX_PROMPT_CHARACTERS // 2)},
                {
                    "role": "user",
                    "content": "y" * (MAX_PROMPT_CHARACTERS // 2 + 1),
                },
            ],
        },
    )

    assert resp.status_code == 422


def test_http_request_body_is_bounded_before_schema_parsing() -> None:
    resp = _make_client().post(
        "/v1/chat/completions",
        content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 413


def test_chunked_http_request_body_is_bounded() -> None:
    async def scenario() -> None:
        chunk = b"x" * (MAX_REQUEST_BODY_BYTES // 2 + 1)
        incoming = iter(
            [
                {"type": "http.request", "body": chunk, "more_body": True},
                {"type": "http.request", "body": chunk, "more_body": False},
            ]
        )
        sent = []

        async def receive():
            return next(incoming)

        async def send(message):
            sent.append(message)

        async def read_body(_scope, wrapped_receive, _send):
            while True:
                message = await wrapped_receive()
                if not message.get("more_body"):
                    return

        middleware = _RequestBodyLimitMiddleware(
            read_body,
            max_bytes=MAX_REQUEST_BODY_BYTES,
        )
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [],
            },
            receive,
            send,
        )

        start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        assert start["status"] == 413

    asyncio.run(scenario())


def test_prompt_plus_completion_must_fit_model_context() -> None:
    tok = Tokenizer(_FakeTok())
    truth = tok.encode("hi") + tok.encode(_COMPLETION)
    state = AppState(
        served_model_name="codenib-test",
        tokenizer=tok,
        engine_factory=lambda: _GreedyTruthEngine(truth),
        drafters=[],
        config=SpeculativeConfig(max_draft_tokens=2),
        default_max_new_tokens=1,
        max_context_tokens=3,
    )
    resp = TestClient(create_app(state)).post(
        "/v1/chat/completions",
        json={
            "model": "codenib-test",
            "messages": _MESSAGES,
            "max_tokens": 2,
        },
    )

    assert resp.status_code == 400
    assert "model context" in resp.json()["detail"]


def test_streaming_chunks_reassemble_to_completion() -> None:
    client = _make_client()
    content = ""
    finish_seen = False
    done_seen = False
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "codenib-test", "messages": _MESSAGES, "stream": True},
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
        json={"model": "codenib-test", "messages": _MESSAGES, "stream": True},
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


def test_stream_queue_applies_backpressure_to_fast_producer() -> None:
    """A stalled client must not let decoded deltas accumulate without a bound."""
    tok = Tokenizer(_FakeTok())
    prompt_ids = tok.encode("hi")
    truth = prompt_ids + tok.encode("x" * 100)

    class _CountingEngine(_GreedyTruthEngine):
        calls = 0

        def predict(self, context, flat):
            self.calls += 1
            return super().predict(context, flat)

    engine = _CountingEngine(truth)
    state = AppState(
        served_model_name="codenib-test",
        tokenizer=tok,
        engine_factory=lambda: engine,
        drafters=[],
        config=SpeculativeConfig(max_draft_tokens=1),
        default_max_new_tokens=100,
        stream_queue_items=1,
    )

    async def scenario() -> None:
        agen = _stream(state, prompt_ids, 100, "codenib-test")
        await agen.asend(None)  # role chunk; acquires the generation lock
        await agen.asend(None)  # first content delta; client now stops draining
        await asyncio.sleep(0.2)

        # One item can be queued and at most one further generation step can be
        # in progress while its enqueue waits for a slot.
        assert engine.calls <= 3

        await agen.aclose()
        for _ in range(100):
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


def test_parse_args_binds_to_loopback_by_default() -> None:
    from codenib.serving.server.api import _parse_args

    assert _parse_args([]).host == "127.0.0.1"


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--port", "0"),
        ("--port", "65536"),
        ("--max-new-tokens", "0"),
        ("--max-new-tokens", str(MAX_COMPLETION_TOKENS + 1)),
        ("--max-draft-tokens", "0"),
        ("--max-context-tokens", "0"),
    ],
)
def test_parse_args_rejects_unsafe_numeric_limits(flag: str, value: str) -> None:
    from codenib.serving.server.api import _parse_args

    with pytest.raises(SystemExit) as exc_info:
        _parse_args([flag, value])

    assert exc_info.value.code == 2


def test_context_limit_uses_smallest_credible_capacity() -> None:
    from codenib.serving.server.api import _model_context_capacity

    model = SimpleNamespace(config=SimpleNamespace(max_position_embeddings=32_768))
    tokenizer = SimpleNamespace(model_max_length=16_384)

    assert _model_context_capacity(model, tokenizer) == 16_384


def test_context_limit_ignores_tokenizer_unknown_sentinel() -> None:
    from codenib.serving.server.api import _model_context_capacity

    model = SimpleNamespace(config=SimpleNamespace(max_position_embeddings=8_192))
    tokenizer = SimpleNamespace(model_max_length=10**30)

    assert _model_context_capacity(model, tokenizer) == 8_192


def test_context_limit_rejects_override_above_capacity() -> None:
    from codenib.serving.server.api import _resolve_context_limit

    model = SimpleNamespace(config=SimpleNamespace(max_position_embeddings=8_192))
    tokenizer = SimpleNamespace(model_max_length=8_192)

    with pytest.raises(SystemExit, match="exceeds"):
        _resolve_context_limit(model, tokenizer, 8_193)


def test_context_limit_defaults_to_safe_tree_attention_budget() -> None:
    from codenib.serving.server.api import _resolve_context_limit

    model = SimpleNamespace(config=SimpleNamespace(max_position_embeddings=32_768))
    tokenizer = SimpleNamespace(model_max_length=32_768)

    assert _resolve_context_limit(model, tokenizer, None) == 8_192


def test_generation_default_must_fit_context_limit() -> None:
    from codenib.serving.server.api import _validate_generation_limit

    with pytest.raises(SystemExit, match="max-new-tokens"):
        _validate_generation_limit(257, 256)


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
