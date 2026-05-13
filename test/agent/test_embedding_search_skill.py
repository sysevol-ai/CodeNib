# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unit tests for the embedding_search skill executor and loader.

- TestEmbeddingSearchExecutor: pure unit tests using mock RetrieveContext.
- TestEmbeddingSearchLoader: smoke test that the real skill package loads.
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
    """Return the absolute path to the embedding_search skill package."""
    import codeminer.agent.skills as pkg

    return str(Path(pkg.__file__).parent / "embedding_search")


def _make_context(
    *,
    vector_store: Any = None,
    default_level: str = "l2",
    masks: dict | None = None,
) -> MagicMock:
    """Build a lightweight mock that quacks like RetrieveContext."""
    ctx = MagicMock()
    ctx.vector_store = vector_store
    ctx.default_level = default_level
    ctx.masks = masks if masks is not None else {}
    return ctx


def _make_store() -> MagicMock:
    """Build a mock CodeVectorStore with search methods."""
    store = MagicMock()
    store.search.return_value = []
    store.search_with_content.return_value = []
    return store


@pytest.fixture(autouse=True)
def reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


class TestEmbeddingSearchExecutor:
    """Tests that target executor.py directly via create_executor()."""

    def _load_executor(self, context: Any):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "embedding_search.executor",
            os.path.join(_skill_dir(), "executor.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.create_executor(context)

    def test_raises_when_vector_store_is_none(self):
        """execute() must raise RuntimeError if context.vector_store is None."""
        ctx = _make_context(vector_store=None)
        execute = self._load_executor(ctx)
        with pytest.raises(RuntimeError, match="Vector store not available"):
            execute("any query")

    def test_search_routing_by_return_content(self):
        """return_content=True routes to search_with_content, False to search."""
        store = _make_store()
        ctx = _make_context(vector_store=store)
        execute = self._load_executor(ctx)

        execute("q", top_k=5, return_content=True)
        store.search_with_content.assert_called_once()
        store.search.assert_not_called()

        store.reset_mock()

        execute("q", top_k=5, return_content=False)
        store.search.assert_called_once()
        store.search_with_content.assert_not_called()

    def test_params_forwarded(self):
        """top_k, level override, score_threshold, and mask_name are forwarded."""
        store = _make_store()
        mask_ids = {"node_1", "node_2"}
        ctx = _make_context(
            vector_store=store, default_level="l2", masks={"my_mask": mask_ids}
        )
        execute = self._load_executor(ctx)

        execute(
            "q",
            top_k=7,
            level="l0",
            score_threshold=0.75,
            mask_name="my_mask",
            return_content=True,
        )

        _, kwargs = store.search_with_content.call_args
        assert kwargs["top_k"] == 7
        assert kwargs["level"] == "l0"
        assert kwargs["score_threshold"] == 0.75
        assert kwargs["mask_node_ids"] == mask_ids


class TestEmbeddingSearchLoader:
    """Smoke test: the real skill package loads and produces a working executor."""

    def test_loads_and_executes(self):
        mock_ctx = _make_context(vector_store=_make_store())
        loader = SkillLoader()
        meta = loader.load_skill(_skill_dir(), contexts={"retrieve": mock_ctx})

        assert meta is not None
        assert meta.skill_id == "embedding_search"
        assert meta.executor_fn is not None
        assert callable(meta.executor_fn)

        result = meta.executor_fn("test query", top_k=3)
        assert isinstance(result, list)
