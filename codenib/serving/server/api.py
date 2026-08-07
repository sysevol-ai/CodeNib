# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible HTTP endpoint in front of the speculative-decoding loop.

This is the serving seam: a code agent (or LiteLLM in front of it) calls
``POST /v1/chat/completions`` exactly as it would call OpenAI, and CodeNib serving
answers by running the existing ``SpeculativeServer`` loop and streaming the
result back. Nothing about speculation is visible on the wire.

Scope (v1): **greedy only** — the loop is lossless greedy decoding, so a request
with ``temperature > 0`` (or ``top_p < 1``) is rejected rather than silently
served as greedy. Concurrency is single-flight: ``CachedHFTreeEngine`` keeps a
per-request KV cache, so a fresh engine is built per request over the shared,
read-only model and generations are serialized by one lock. Batching is future
work.

``main()`` wires the real HF engine + tokenizer; tests build :class:`AppState`
with fakes and drive :func:`create_app` via ``TestClient`` — no GPU, no model.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from codenib.serving.drafter.base import Drafter
from codenib.serving.drafter.copy import CopyDrafter
from codenib.serving.drafter.retrieval import (
    DEFAULT_RETRIEVAL_K,
    CodeNibBackend,
    RetrievalDrafter,
)
from codenib.serving.server.hf_engine import CachedHFTreeEngine, HFTreeEngine
from codenib.serving.server.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChunkChoice,
    DeltaMessage,
    ModelCard,
    ModelList,
    Usage,
)
from codenib.serving.server.sglang import SGLangVerifier, TargetEngine
from codenib.serving.server.tokenization import Tokenizer
from codenib.serving.server.worker import SpeculativeConfig, SpeculativeServer
from codenib.serving.types import TokenId


@dataclass
class AppState:
    """Everything the endpoint needs to serve requests.

    ``engine_factory`` returns a fresh :class:`TargetEngine` per request (the
    cached HF engine is per-request stateful); ``drafters`` are stateless and
    shared. ``lock`` serializes generations; :func:`create_app` rebinds it so
    each app gets its own, but it is never ``None`` — the request handlers rely
    on that, and an ``Optional`` here would only push the check to every use.
    """

    served_model_name: str
    tokenizer: Tokenizer
    engine_factory: Callable[[], TargetEngine]
    drafters: List[Drafter]
    config: SpeculativeConfig = field(default_factory=SpeculativeConfig)
    default_max_new_tokens: int = 256
    # asyncio primitives bind to the running loop lazily (3.10+), so building
    # this outside a loop is safe.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _check_greedy(req: ChatCompletionRequest) -> None:
    """Reject non-greedy requests — v1 serves lossless greedy only."""
    if req.temperature not in (None, 0, 0.0):
        raise HTTPException(
            status_code=400,
            detail="only greedy decoding (temperature=0) is supported in v1",
        )
    if req.top_p is not None and req.top_p < 1.0:
        raise HTTPException(
            status_code=400,
            detail="only greedy decoding (top_p=1) is supported in v1",
        )


def _build_prompt_ids(state: AppState, messages: List[ChatMessage]) -> List[TokenId]:
    """Turn chat messages into prompt token ids via the model's chat template.

    Falls back to a plain ``role: content`` concatenation for tokenizers without
    a chat template.
    """
    msgs = [{"role": m.role, "content": m.content} for m in messages]
    try:
        text = state.tokenizer.hf.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=True
        )
    except Exception:  # noqa: BLE001 - any templating failure falls back to plain text
        text = "".join(f"{m['role']}: {m['content']}\n" for m in msgs) + "assistant: "
    return state.tokenizer.encode(text)


def _max_new_tokens(state: AppState, req: ChatCompletionRequest) -> int:
    return req.max_completion_tokens or req.max_tokens or state.default_max_new_tokens


def _run_full(state: AppState, prompt_ids: List[TokenId], max_new: int):
    """Blocking full generation; returns (completion_ids, finish_reason)."""
    engine = state.engine_factory()
    verifier = SGLangVerifier(engine, eos_token_id=state.tokenizer.eos_token_id)
    server = SpeculativeServer(drafters=list(state.drafters), config=state.config)
    result = server.run(prompt_ids, verifier, max_new_tokens=max_new)
    finish = "length" if len(result.tokens) >= max_new else "stop"
    return result.tokens, finish


#: U+FFFD REPLACEMENT CHARACTER — what a UTF-8 decoder emits for bytes it cannot
#: decode, i.e. what ``decode()`` returns when a token ends mid-character.
#: Spelled as an escape, not the glyph: the literal is indistinguishable from
#: real mojibake in a diff, and would be silently destroyed by a bad re-encode.
_REPLACEMENT = "\ufffd"


class _IncrementalDecoder:
    """Turn a growing token-id list into *stable* text deltas.

    ``decode(prefix)`` is not guaranteed to be a character prefix of
    ``decode(prefix + more)``. A token can end mid-way through a multi-byte
    UTF-8 sequence, which decodes to ``U+FFFD`` until its continuation arrives
    and then collapses into a different character entirely — so computing a
    delta by slicing off ``len(previous_text)`` emits mojibake and then
    mis-aligns every delta after it.

    This holds back the trailing undecodable run instead, so only text that can
    no longer change is emitted. :meth:`flush` releases whatever is left when
    the stream ends, so nothing is dropped.
    """

    def __init__(self, tokenizer: Tokenizer) -> None:
        self._tok = tokenizer
        self._ids: List[TokenId] = []
        self._emitted = ""

    def push(self, ids: Sequence[TokenId]) -> str:
        """Append ``ids`` and return the delta that is now safe to send."""
        self._ids.extend(ids)
        stable = self._tok.decode(self._ids).rstrip(_REPLACEMENT)
        if len(stable) < len(self._emitted):
            return ""  # nothing new has become stable yet
        return self._advance(stable)

    def flush(self) -> str:
        """Return the tail held back at end of stream (replacement chars incl.)."""
        return self._advance(self._tok.decode(self._ids))

    def _advance(self, text: str) -> str:
        if not text.startswith(self._emitted):
            # Defensive: a tokenizer that rewrites already-emitted characters.
            # Resync on the common prefix rather than replaying the whole text.
            i = 0
            limit = min(len(text), len(self._emitted))
            while i < limit and text[i] == self._emitted[i]:
                i += 1
            self._emitted = text[:i]
        delta = text[len(self._emitted) :]
        self._emitted = text
        return delta


def _chunk_json(
    cid: str,
    created: int,
    model: str,
    *,
    role: Optional[str] = None,
    content: Optional[str] = None,
    finish_reason: Optional[str] = None,
) -> str:
    chunk = ChatCompletionChunk(
        id=cid,
        created=created,
        model=model,
        choices=[
            ChunkChoice(
                delta=DeltaMessage(role=role, content=content),
                finish_reason=finish_reason,
            )
        ],
    )
    return chunk.model_dump_json()


def create_app(state: AppState) -> FastAPI:
    """Build the FastAPI app bound to ``state``."""
    app = FastAPI(title="CodeNib Serving", version="0.1.0")
    state.lock = asyncio.Lock()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models() -> ModelList:
        return ModelList(
            data=[ModelCard(id=state.served_model_name, created=int(time.time()))]
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest):
        _check_greedy(req)
        prompt_ids = _build_prompt_ids(state, req.messages)
        max_new = _max_new_tokens(state, req)
        model_name = req.model or state.served_model_name

        if req.stream:
            return EventSourceResponse(_stream(state, prompt_ids, max_new, model_name))

        async with state.lock:
            completion_ids, finish = await asyncio.to_thread(
                _run_full, state, prompt_ids, max_new
            )
        text = state.tokenizer.decode(completion_ids)
        return ChatCompletionResponse(
            id=_completion_id(),
            created=int(time.time()),
            model=model_name,
            choices=[
                Choice(
                    message=ChatMessage(role="assistant", content=text),
                    finish_reason=finish,
                )
            ],
            usage=Usage(
                prompt_tokens=len(prompt_ids),
                completion_tokens=len(completion_ids),
                total_tokens=len(prompt_ids) + len(completion_ids),
            ),
        )

    return app


async def _stream(
    state: AppState, prompt_ids: List[TokenId], max_new: int, model_name: str
):
    """Async SSE generator bridging the sync decode loop through a queue.

    The blocking ``run_iter`` runs in a worker thread and pushes decoded text
    deltas onto an asyncio queue; this coroutine drains it and emits OpenAI
    ``chat.completion.chunk`` events. The lock is held for the whole stream, so
    only one generation runs at a time — including when the client disconnects
    mid-stream (see the acquire/release comment below).
    """
    created = int(time.time())
    cid = _completion_id()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()
    stop = threading.Event()

    def produce() -> None:
        try:
            engine = state.engine_factory()
            verifier = SGLangVerifier(engine, eos_token_id=state.tokenizer.eos_token_id)
            server = SpeculativeServer(
                drafters=list(state.drafters), config=state.config
            )
            decoder = _IncrementalDecoder(state.tokenizer)
            produced = 0
            for step in server.run_iter(prompt_ids, verifier, max_new_tokens=max_new):
                if stop.is_set():
                    # The consumer is gone; stop occupying the GPU. Checked per
                    # step because a step itself is not interruptible.
                    return
                if not step.emitted:
                    continue
                produced += len(step.emitted)
                delta = decoder.push(step.emitted)
                if delta:
                    loop.call_soon_threadsafe(queue.put_nowait, ("delta", delta))
            tail = decoder.flush()
            if tail:
                loop.call_soon_threadsafe(queue.put_nowait, ("delta", tail))
            finish = "length" if produced >= max_new else "stop"
            loop.call_soon_threadsafe(queue.put_nowait, ("finish", finish))
        except Exception as exc:  # noqa: BLE001 - surfaced to the consumer below
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, done)

    # Acquired and released by hand rather than with ``async with``: a client
    # disconnect cancels this generator at an ``await``, and a lexical
    # ``async with`` would unwind and hand the lock to the next request while
    # this request's worker thread is still generating on the GPU — two
    # concurrent generations, which is exactly what the lock exists to prevent.
    # The release below is therefore deferred until the worker has stopped.
    await state.lock.acquire()
    fut: Optional[asyncio.Future] = None
    try:
        yield {"data": _chunk_json(cid, created, model_name, role="assistant")}
        fut = loop.run_in_executor(None, produce)
        error: Optional[BaseException] = None
        finish = "stop"
        while True:
            item = await queue.get()
            if item is done:
                break
            kind, payload = item
            if kind == "delta":
                yield {"data": _chunk_json(cid, created, model_name, content=payload)}
            elif kind == "finish":
                finish = payload
            elif kind == "error":
                error = payload
        await fut
        if error is not None:
            raise error
        yield {"data": _chunk_json(cid, created, model_name, finish_reason=finish)}
        yield {"data": "[DONE]"}
    finally:
        stop.set()
        if fut is None or fut.done():
            state.lock.release()
        else:
            fut.add_done_callback(lambda _f: state.lock.release())


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse ``codenib-serve`` arguments (``argv`` is injected by tests)."""
    parser = argparse.ArgumentParser(
        description="CodeNib OpenAI-compatible serving endpoint"
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("CODENIB_SERVE_MODEL", "Qwen/Qwen2.5-Coder-0.5B"),
    )
    parser.add_argument("--served-model-name", default=None)
    parser.add_argument(
        "--device", default=os.environ.get("CODENIB_SERVE_DEVICE", "cuda")
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--max-draft-tokens", type=int, default=16)
    parser.add_argument(
        "--manifest",
        default=os.environ.get("CODENIB_SERVE_MANIFEST"),
        help=(
            "path to <repo>/.codenib_cache/repo_manifest.json. When given, a "
            "retrieval drafter over that index is fused in alongside the copy "
            "drafter; without it, serving is copy-only."
        ),
    )
    return parser.parse_args(argv)


def _build_drafters(args: argparse.Namespace, tokenizer: Tokenizer) -> List[Drafter]:
    """Ordered draft sources for ``args``.

    :class:`~codenib.serving.drafter.copy.CopyDrafter` comes first — it is near-free and
    owns shared prefixes, so retrieval grafts onto it during fusion. The retrieval
    drafter is opt-in: it needs a prebuilt CodeNib index, and loading one costs a
    ``codenib`` install plus startup time, so it is only built when
    ``--manifest`` is supplied.

    ``tokenizer`` must be the *serving* tokenizer: the backend decodes the context
    tail into a query and re-encodes retrieved snippets, so its ids have to line up
    with the ones the target model is emitting.
    """
    drafters: List[Drafter] = [CopyDrafter(max_draft=args.max_draft_tokens)]
    if args.manifest:
        backend = CodeNibBackend.from_manifest(args.manifest, tokenizer)
        drafters.append(
            RetrievalDrafter(
                backend, k=DEFAULT_RETRIEVAL_K, max_draft=args.max_draft_tokens
            )
        )
    return drafters


def main() -> None:
    """CLI entry point (``codenib-serve``): load the HF engine and serve."""
    args = _parse_args()

    # Imported here, not at module scope: this module only *defines* the app, so
    # embedding it behind another ASGI server should not require uvicorn. It is
    # the one import ``tests/test_api.py`` does not declare via importorskip.
    import uvicorn

    base = HFTreeEngine.from_pretrained(args.model, device=args.device)
    model = base.model
    tokenizer = Tokenizer.from_pretrained(args.model)
    state = AppState(
        served_model_name=args.served_model_name or args.model,
        tokenizer=tokenizer,
        engine_factory=lambda: CachedHFTreeEngine(model, device=args.device),
        drafters=_build_drafters(args, tokenizer),
        config=SpeculativeConfig(max_draft_tokens=args.max_draft_tokens),
        default_max_new_tokens=args.max_new_tokens,
    )
    app = create_app(state)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
