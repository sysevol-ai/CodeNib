# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the bm25_search skill executor and loader.

- TestBM25SearchExecutor: pure unit tests using mock RetrieveContext.
- TestBM25SearchLoader: smoke test that the real skill package loads.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from codeminer.agent.skills.loader import SkillLoader
from codeminer.agent.skills.registry import SkillRegistry


def _skill_dir() -> str:
    """Return the absolute path to the bm25_search skill package."""
    import codeminer.agent.skills as pkg

    return str(Path(pkg.__file__).parent / "bm25_search")


def _make_context(*, bm25: Any = None) -> MagicMock:
    """Build a lightweight mock that quacks like RetrieveContext."""
    ctx = MagicMock()
    ctx.bm25 = bm25
    return ctx


def _make_bm25() -> MagicMock:
    """Build a mock BM25CodeIndexer with a search method."""
    bm25 = MagicMock()
    bm25.search.return_value = []
    return bm25


@pytest.fixture(autouse=True)
def reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


class TestBM25SearchExecutor:
    """Tests that target executor.py directly via create_executor()."""

    def _load_executor(self, context: Any):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "bm25_search.executor",
            os.path.join(_skill_dir(), "executor.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.create_executor(context)

    def test_raises_when_bm25_is_none(self):
        """execute() must raise RuntimeError if context.bm25 is None."""
        ctx = _make_context(bm25=None)
        execute = self._load_executor(ctx)
        with pytest.raises(RuntimeError, match="BM25 index not available"):
            execute("any query")

    def test_search_is_called(self):
        """A basic call must invoke bm25.search() with query forwarded."""
        bm25 = _make_bm25()
        ctx = _make_context(bm25=bm25)
        execute = self._load_executor(ctx)

        execute("find function foo")

        bm25.search.assert_called_once()
        _, kwargs = bm25.search.call_args
        assert kwargs["query"] == "find function foo"

    def test_params_forwarded(self):
        """top_k, filter_test, return_content, wrap_with_line_numbers are forwarded."""
        bm25 = _make_bm25()
        ctx = _make_context(bm25=bm25)
        execute = self._load_executor(ctx)

        execute(
            "q",
            top_k=7,
            filter_test=True,
            return_content=False,
            wrap_with_line_numbers=False,
        )

        _, kwargs = bm25.search.call_args
        assert kwargs["top_k"] == 7
        assert kwargs["filter_test"] is True
        assert kwargs["return_code_content"] is False
        assert kwargs["wrap_with_ln"] is False


class TestBM25SearchLoader:
    """Smoke test: the real skill package loads and produces a working executor."""

    def test_loads_and_executes(self):
        mock_ctx = _make_context(bm25=_make_bm25())
        loader = SkillLoader()
        meta = loader.load_skill(_skill_dir(), contexts={"retrieve": mock_ctx})

        assert meta is not None
        assert meta.skill_id == "bm25_search"
        assert meta.executor_fn is not None
        assert callable(meta.executor_fn)

        result = meta.executor_fn("test query", top_k=3)
        assert isinstance(result, list)
