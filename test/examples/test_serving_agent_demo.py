# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for the example agent (``examples/serving_agent_demo.py``).

CodeNib and the OpenAI client are both injected as fakes, so the two-call
flow (retrieve context -> generate) is exercised with no index and no network.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import List

# Load examples/serving_agent_demo.py by path (``examples/`` is not a package).
_MODULE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "examples" / "serving_agent_demo.py"
)
_spec = importlib.util.spec_from_file_location("serving_agent_demo", _MODULE_PATH)
assert _spec and _spec.loader
serving_agent_demo = importlib.util.module_from_spec(_spec)
sys.modules["serving_agent_demo"] = serving_agent_demo
_spec.loader.exec_module(serving_agent_demo)

build_messages = serving_agent_demo.build_messages
generate = serving_agent_demo.generate
retrieve_context = serving_agent_demo.retrieve_context


class _Hit:
    __slots__ = ("content", "file")

    def __init__(self, content: str, file: str = "") -> None:
        self.content = content
        self.file = file


class _FakeBM25:
    def __init__(self, hits: List[_Hit]) -> None:
        self.hits = hits
        self.last_query = None

    def search(self, query, top_k=5, return_code_content=False):
        self.last_query = query
        return self.hits


def _ctx(hits, has_bm25: bool = True):
    return SimpleNamespace(bm25=_FakeBM25(hits) if has_bm25 else None)


class _FakeCompletions:
    def __init__(self, text: str, pieces: List[str]) -> None:
        self.text = text
        self.pieces = pieces
        self.calls: List[dict] = []

    def create(self, model, messages, stream=False):
        self.calls.append({"model": model, "messages": messages, "stream": stream})
        if stream:
            return iter(
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=p))]
                )
                for p in self.pieces
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))]
        )


def _client(text="DONE", pieces=("wor", "ld")):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(text, list(pieces)))
    )


# --- retrieve_context --------------------------------------------------------


def test_retrieve_context_joins_snippets_with_location() -> None:
    ctx = _ctx([_Hit("def a(): ...", file="a.py"), _Hit("def b(): ...", file="b.py")])
    out = retrieve_context("do something", ctx=ctx, top_k=2)
    assert "a.py" in out and "def a()" in out
    assert "b.py" in out and "def b()" in out


def test_retrieve_context_passes_query_to_index() -> None:
    ctx = _ctx([_Hit("x")])
    retrieve_context("my query", ctx=ctx)
    assert ctx.bm25.last_query == "my query"


def test_retrieve_context_without_index_returns_empty() -> None:
    assert retrieve_context("q", ctx=_ctx([], has_bm25=False)) == ""


def test_retrieve_context_loads_only_bm25_view(monkeypatch) -> None:
    ctx = _ctx([_Hit("def f(): ...")])
    seen = {}

    class _ServerContext:
        @staticmethod
        def load(path, *, views=None):
            seen["path"] = path
            seen["views"] = views
            return ctx

    module = types.ModuleType("codenib.mcp.context")
    module.ServerContext = _ServerContext
    monkeypatch.setitem(sys.modules, "codenib.mcp.context", module)

    assert "def f" in retrieve_context("q", manifest_path="/manifest.json")
    assert seen == {"path": "/manifest.json", "views": {"bm25"}}


# --- build_messages ----------------------------------------------------------


def test_build_messages_includes_context_and_task() -> None:
    msgs = build_messages("fix the bug", "some context")
    assert msgs[0]["role"] == "system"
    assert "some context" in msgs[1]["content"]
    assert "fix the bug" in msgs[1]["content"]


def test_build_messages_without_context_uses_task_only() -> None:
    msgs = build_messages("just this", "")
    assert msgs[1]["content"] == "just this"


# --- generate ----------------------------------------------------------------


def test_generate_non_stream_returns_message_content() -> None:
    client = _client(text="RESULT")
    out = generate(
        [{"role": "user", "content": "hi"}],
        base_url="http://x/v1",
        model="m",
        client=client,
    )
    assert out == "RESULT"
    assert client.chat.completions.calls[0]["stream"] is False


def test_generate_stream_concatenates_deltas() -> None:
    client = _client(pieces=["wor", "ld"])
    out = generate(
        [{"role": "user", "content": "hi"}],
        base_url="http://x/v1",
        model="m",
        stream=True,
        client=client,
    )
    assert out == "world"
    assert client.chat.completions.calls[0]["stream"] is True


# --- end-to-end flow ---------------------------------------------------------


def test_full_flow_retrieve_then_generate() -> None:
    ctx = _ctx([_Hit("def parse(): ...", file="cfg.py")])
    client = _client(text="added a docstring")
    context = retrieve_context("document parse", ctx=ctx)
    messages = build_messages("document parse", context)
    out = generate(messages, base_url="http://x/v1", model="m", client=client)
    assert out == "added a docstring"
    # The retrieved snippet made it into the prompt the model saw.
    assert "def parse()" in client.chat.completions.calls[0]["messages"][1]["content"]
