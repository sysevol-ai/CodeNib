# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for bm25_names — compact name-tag search for the LocAgent harness (#133)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeminer.agent.skills.bm25_names.executor import create_executor
from codeminer.types import QueriedNode


class _FakeBM25:
    def __init__(self):
        self.last = None

    def search(self, query, top_k, **kw):
        self.last = kw  # capture flags
        return [
            QueriedNode(
                node_name="hashA",
                node_id="a.c:hashA",
                file="a.c",
                content="void foo() { /* big body */ }",
            )
        ]


class _FakeGraph:
    def get_node_info_by_name(self, name):
        return {"unified_name": "a.c:foo()"} if name == "hashA" else None

    def display_name(self, name):
        info = self.get_node_info_by_name(name) or {}
        return info.get("unified_name") or name


def test_returns_names_only_no_bodies():
    bm = _FakeBM25()
    ex = create_executor(
        {
            "retrieve": SimpleNamespace(bm25=bm),
            "expand": SimpleNamespace(code_graph=_FakeGraph()),
        }
    )
    out = ex(query="foo", top_k=5)
    assert bm.last["return_code_content"] is False  # asked bm25 for no content
    assert all(n.content is None for n in out)  # no bodies leak
    assert out[0].node_name == "a.c:foo()"  # hash relabeled to readable unified_name
    assert out[0].node_id == "a.c:a.c:foo()" or out[0].node_id.endswith("foo()")


def test_raises_without_bm25():
    ex = create_executor({"retrieve": SimpleNamespace(bm25=None), "expand": None})
    with pytest.raises(RuntimeError, match="BM25"):
        ex(query="x")
