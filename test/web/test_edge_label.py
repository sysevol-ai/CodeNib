# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the on-demand edge-label service."""

from __future__ import annotations

import json
import os
from typing import Dict, Optional

import pytest

from codenib.web.edge_label import EdgeLabeler, _sanitize

# --- fake LLM ------------------------------------------------------------------


class _Msg:
    __slots__ = ("content",)

    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    __slots__ = ("message",)

    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    __slots__ = ("choices",)

    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _FakeLLM:
    """Records calls; returns a canned completion (or raises)."""

    def __init__(self, reply: str = "Validates user input.", raise_exc: bool = False):
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("boom")
        return _Resp(self.reply)


def _patch_llm(monkeypatch, fake: _FakeLLM) -> None:
    import litellm

    monkeypatch.setattr(litellm, "completion", fake)


# --- helpers -------------------------------------------------------------------


def _make_labeler(tmp_path, sources: Optional[Dict[str, str]] = None, model="test/m"):
    sources = sources or {}

    def source_fn(file, start, end):
        if file in sources:
            return {"content": sources[file]}
        return None

    return EdgeLabeler(
        source_fn=source_fn,
        model=model,
        cache_dir=str(tmp_path),
        cache_namespace="inst@abc123",
    )


_SRC = {
    "a.py": "def router():\n    return validate(x)\n",
    "b.py": "def validate(x):\n    return x\n",
}


def _label(labeler):
    return labeler.label(
        "a.py",
        1,
        2,
        "router",
        "b.py",
        1,
        2,
        "validate",
        anchors=[{"file": "a.py", "line": 2}],
    )


# --- sanitize ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("```\nValidates user input.\n```", "validates user input"),
        ('"Loads DB config"', "loads db config"),
        (
            "dispatches request to handler quickly and safely now",
            "dispatches request to handler quickly and",
        ),
        ("Calls validate().", "calls validate()"),
        ("", ""),
        ("   ", ""),
        ("`parses config`", "parses config"),
        # thinking-model output: strip the reasoning block
        ("<think>hmm let me see</think>\nvalidates user input", "validates user input"),
        ("<think>reasoning truncated with no close", ""),
        ("done reasoning</think> loads config", "loads config"),
    ],
)
def test_sanitize(raw, expected):
    assert _sanitize(raw) == expected


# --- generation + caching ------------------------------------------------------


def test_generates_then_caches(tmp_path, monkeypatch):
    fake = _FakeLLM("Validates user input.")
    _patch_llm(monkeypatch, fake)
    labeler = _make_labeler(tmp_path, _SRC)

    label, cached = _label(labeler)
    assert label == "validates user input"
    assert cached is False
    assert fake.calls == 1

    # second call: served from the in-memory cache, no new LLM call
    label2, cached2 = _label(labeler)
    assert label2 == "validates user input"
    assert cached2 is True
    assert fake.calls == 1


def test_cache_key_changes_with_code(tmp_path, monkeypatch):
    fake = _FakeLLM("validates input")
    _patch_llm(monkeypatch, fake)
    labeler = _make_labeler(tmp_path, dict(_SRC))
    _label(labeler)
    assert fake.calls == 1

    # mutate the source symbol's code -> different content hash -> regenerate
    labeler._source = lambda f, s, e: (  # type: ignore
        {"content": "def router():\n    return other()\n"}
        if f == "a.py"
        else {"content": _SRC["b.py"]}
    )
    _label(labeler)
    assert fake.calls == 2


def test_llm_error_returns_empty_and_not_cached(tmp_path, monkeypatch):
    fake = _FakeLLM(raise_exc=True)
    _patch_llm(monkeypatch, fake)
    labeler = _make_labeler(tmp_path, _SRC)

    label, cached = _label(labeler)
    assert label == ""
    assert cached is False
    # a transient failure must not be cached -> next click retries
    label2, _ = _label(labeler)
    assert fake.calls == 2


def test_no_source_skips_llm(tmp_path, monkeypatch):
    fake = _FakeLLM("should not run")
    _patch_llm(monkeypatch, fake)
    labeler = _make_labeler(tmp_path, sources={})  # source_fn returns None for all

    label, cached = labeler.label("x.py", 1, 2, "a", "y.py", 1, 2, "b", anchors=[])
    assert label == ""
    assert fake.calls == 0


def test_persists_to_disk_and_reloads(tmp_path, monkeypatch):
    fake = _FakeLLM("loads db config")
    _patch_llm(monkeypatch, fake)
    labeler = _make_labeler(tmp_path, _SRC)
    _label(labeler)
    assert fake.calls == 1

    # the cache file exists and holds the phrase + model
    files = [f for f in os.listdir(tmp_path) if f.startswith("edge_labels_")]
    assert files, "cache file was not written"
    with open(os.path.join(tmp_path, files[0])) as fh:
        payload = json.load(fh)
    assert any(v.get("label") == "loads db config" for v in payload.values())

    # a fresh labeler (same dir/namespace) serves from disk, no LLM call
    fake2 = _FakeLLM("DIFFERENT")
    _patch_llm(monkeypatch, fake2)
    labeler2 = _make_labeler(tmp_path, _SRC)
    label, cached = _label(labeler2)
    assert label == "loads db config"
    assert cached is True
    assert fake2.calls == 0
