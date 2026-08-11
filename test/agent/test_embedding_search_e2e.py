# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end smoke test for the embedding_search skill.

This test builds (or reloads) a real FAISS index over the httpie/cli repository,
then exercises the full skill loading and execution path.

Run:

    pytest test/agent/test_embedding_search_e2e.py -v

The index is written to /tmp/embedding_e2e_index/ and is reused across runs.
Delete that directory to force a rebuild.

Environment variable CODENIB_INDEX_PATH can override the cache location:

    CODENIB_INDEX_PATH=/my/path pytest test/agent/test_embedding_search_e2e.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codenib.index.embedding.artifact_integrity import capture_authenticated_vector_view
from codenib.native_index_authorization import _mint_trusted_local_admin_authorization


def _cuda_available() -> bool:
    try:
        import torch
    except Exception:
        return False
    return bool(torch.cuda.is_available())


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _cuda_available(),
        reason="embedding_search e2e requires a CUDA-capable torch install",
    ),
]

DEFAULT_EMBEDDING_MODEL = "nomic-ai/CodeRankEmbed"
DEFAULT_EMBEDDING_DIM = 768
DEFAULT_EMBEDDING_PROVIDER = "huggingface"

EMBEDDING_INDEX_PATH = "/tmp/embedding_e2e_index"


def _load_test_owned_vector(store, path):
    """Authorize the session-owned local E2E cache, never artifact input."""

    semantic_contract = dict(store.artifact_metadata)
    with capture_authenticated_vector_view(path) as vector_view:
        authorization = _mint_trusted_local_admin_authorization(
            vector_view.ownership,
            view_type="vector",
            semantic_contract=semantic_contract,
            evidence=(
                "embedding-search-e2e-local-cache",
                "captured-vector-tree-subject",
            ),
        )
        store.load(
            str(path),
            native_index_authorization=authorization,
        )


@pytest.fixture(scope="session")
def vector_store(httpie_cli_repo):
    """Build or load a CodeVectorStore for the httpie/cli repo."""
    from codenib.index.embedding import CodeVectorStore, build_hierarchical_vector_store

    repo_path = str(httpie_cli_repo)
    store_root = Path(EMBEDDING_INDEX_PATH)
    l0_dir = store_root / "l0"
    l2_dir = store_root / "l2"

    if l0_dir.exists() and l2_dir.exists():
        print(f"\n[e2e] Loading cached index from {EMBEDDING_INDEX_PATH}")
        vs = CodeVectorStore(
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_provider=DEFAULT_EMBEDDING_PROVIDER,
            dimension=DEFAULT_EMBEDDING_DIM,
            store_path=EMBEDDING_INDEX_PATH,
            model_kwargs={
                "trust_remote_code": True,
                "device": "cuda",
                "model_kwargs": {"torch_dtype": "float16"},
            },
            encode_kwargs={
                "batch_size": 4,
                "normalize_embeddings": True,
            },
        )
        _load_test_owned_vector(vs, EMBEDDING_INDEX_PATH)
    else:
        print(f"\n[e2e] Building index for {repo_path} into {EMBEDDING_INDEX_PATH}")
        store_root.mkdir(parents=True, exist_ok=True)
        vs = build_hierarchical_vector_store(
            repo_path=repo_path,
            index_path=EMBEDDING_INDEX_PATH,
            languages=["python"],
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_provider=DEFAULT_EMBEDDING_PROVIDER,
            embedding_dimension=DEFAULT_EMBEDDING_DIM,
            embedding_kwargs={
                "model_kwargs": {
                    "trust_remote_code": True,
                    "device": "cuda",
                    "model_kwargs": {"torch_dtype": "float16"},
                },
                "encode_kwargs": {
                    "batch_size": 4,
                    "normalize_embeddings": True,
                },
            },
        )

    return vs


@pytest.fixture(scope="session")
def retrieve_context(vector_store):
    """Construct a real RetrieveContext backed by the session-scoped vector store."""
    from codenib.ops.retrieve import RetrieveContext

    return RetrieveContext(vector_store=vector_store)


@pytest.fixture(scope="session")
def executor_fn(retrieve_context):
    """Load the embedding_search skill and return the bound executor callable."""
    import codenib.agent.skills as pkg
    from codenib.agent.skills.loader import SkillLoader
    from codenib.agent.skills.registry import SkillRegistry

    skill_dir = str(Path(pkg.__file__).parent / "embedding_search")

    SkillRegistry.reset()
    loader = SkillLoader()
    meta = loader.load_skill(skill_dir, contexts={"retrieve": retrieve_context})
    assert meta is not None, f"SkillLoader returned None for {skill_dir}"
    assert meta.executor_fn is not None, "executor_fn is None after loading"
    return meta.executor_fn


class TestEmbeddingSearchE2E:
    """Full-stack tests using a real FAISS index over httpie/cli."""

    def setup_method(self):
        import torch

        if torch.cuda.is_available():
            allocated = torch.cuda.memory_allocated() / 1024**3
            reserved = torch.cuda.memory_reserved() / 1024**3
            print(
                f"\n[GPU] Before test: {allocated:.2f}GB allocated, {reserved:.2f}GB reserved"
            )

    def teardown_method(self):
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    @classmethod
    def teardown_class(cls):
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def test_basic_query_returns_well_formed_results(self, executor_fn):
        """A broad query returns non-empty results with expected fields."""
        results = executor_fn("HTTP request handling", top_k=5, return_content=True)
        assert isinstance(results, list)
        assert len(results) > 0

        for node in results:
            assert isinstance(node.score, float)
            assert node.file
            assert node.content
            assert isinstance(node.start_line, int)
            assert isinstance(node.end_line, int)

    def test_return_content_flag(self, executor_fn):
        """return_content=True populates content; False omits it."""
        with_content = executor_fn("HTTP client", top_k=3, return_content=True)
        assert all(node.content for node in with_content)

        without_content = executor_fn("HTTP client", top_k=3, return_content=False)
        assert len(without_content) > 0
        for node in without_content:
            assert not getattr(node, "content", None)

    def test_both_levels_return_results(self, executor_fn):
        """Both l0 (file skeletons) and l2 (function/method) levels work."""
        l0 = executor_fn("HTTP client", top_k=3, level="l0")
        l2 = executor_fn("HTTP client", top_k=3, level="l2")
        assert len(l0) > 0
        assert len(l2) > 0

    def test_conceptual_query_finds_relevant_file(self, executor_fn):
        """A semantic query should surface files related to the concept."""
        results = executor_fn(
            "function that sends an HTTP request with authentication", top_k=10
        )
        assert len(results) > 0
        files = [node.file for node in results]
        relevant = [
            f
            for f in files
            if "auth" in f or "request" in f or "client" in f or "session" in f
        ]
        assert relevant, f"No auth/request file in top-10 results. Got: {files}"
