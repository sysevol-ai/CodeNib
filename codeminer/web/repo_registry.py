# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Load pre-indexed demo repos and expose one agent per repo.

Each repo gets its **own** ``SkillRegistry`` instance (not the global
singleton) so that skill executors stay bound to that repo's indexes. This
gives clean per-repo isolation and lets us build a ready-to-run ``AgentRunner``
once at startup and reuse it for every request — retrieval is read-only, so
concurrent queries are safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..agent.runner import AgentRunner
from ..agent.skills.loader import SkillLoader
from ..agent.skills.registry import SkillRegistry
from ..compiler.manifest import RepoManifest
from ..compiler.params import SessionContext
from ..index.embedding.vector_store import CodeVectorStore
from ..index.sparse_idx.bm25_index import BM25CodeIndexer
from ..llm.litellm_chat import LiteLLMChat
from ..log_utils import get_logger
from ..ops.rerank import RerankContext
from ..ops.retrieve import RetrieveContext
from .config import DemoConfig, RepoConfig
from .schemas import RepoInfo

logger = get_logger(__name__)

_SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent",
    "skills",
)


def _fresh_registry() -> SkillRegistry:
    """Create an isolated registry that bypasses the global singleton."""
    reg = object.__new__(SkillRegistry)
    reg._skills = {}
    return reg


@dataclass
class RepoBundle:
    """Everything needed to answer questions about one repo."""

    config: RepoConfig
    manifest: RepoManifest
    runner: AgentRunner

    def info(self) -> RepoInfo:
        return RepoInfo(
            id=self.config.id,
            name=self.config.name,
            description=self.config.description,
            languages=self.manifest.languages,
            file_count=self.manifest.file_count,
            capabilities=self.manifest.capabilities,
        )


class RepoRegistry:
    """Holds the loaded :class:`RepoBundle` objects, keyed by repo id."""

    def __init__(self, config: DemoConfig) -> None:
        self._config = config
        self._bundles: Dict[str, RepoBundle] = {}

    def load_all(self) -> None:
        """Load every configured repo whose index manifest is present."""
        for repo in self._config.repos:
            manifest_path = self._config.manifest_path(repo)
            if not os.path.exists(manifest_path):
                logger.warning(
                    "Skipping %r: manifest not found at %s "
                    "(run scripts/build_demo_index.py first)",
                    repo.id,
                    manifest_path,
                )
                continue
            try:
                self._bundles[repo.id] = self._load_repo(repo, manifest_path)
                logger.info("Loaded repo %r (%s)", repo.id, repo.name)
            except Exception as exc:  # noqa: BLE001 - demo: keep other repos alive
                logger.error("Failed to load repo %r: %s", repo.id, exc, exc_info=True)

    def _load_repo(self, repo: RepoConfig, manifest_path: str) -> RepoBundle:
        manifest = RepoManifest.load(manifest_path)

        bm25_index: Optional[BM25CodeIndexer] = None
        vector_store: Optional[CodeVectorStore] = None

        bm25_entry = manifest.indexes.get("bm25")
        if bm25_entry is not None and bm25_entry.status == "fresh":
            bm25_index = BM25CodeIndexer()
            bm25_index.load_index(bm25_entry.path)

        vec_entry = manifest.indexes.get("vector")
        if vec_entry is not None and vec_entry.status == "fresh":
            emb_model = vec_entry.config.get(
                "embedding_model", self._config.embedding_model
            )
            emb_dim = vec_entry.config.get(
                "embedding_dimension", self._config.embedding_dimension
            )
            vector_store = CodeVectorStore(
                embedding_model=emb_model,
                embedding_provider="huggingface",
                dimension=emb_dim,
                store_path=vec_entry.path,
            )
            vector_store.load(vec_entry.path)

        retrieve_ctx = RetrieveContext(
            bm25=bm25_index,
            vector_store=vector_store,
            default_top_k=10,
            default_level="l2",
        )
        contexts: Dict[str, object] = {"retrieve": retrieve_ctx}
        if vector_store is not None:
            contexts["rerank"] = RerankContext(embedding_store=vector_store)

        registry = _fresh_registry()
        loader = SkillLoader()
        loader.load_all(_SKILLS_DIR, contexts=contexts, registry=registry)

        session_ctx = SessionContext(
            repo_path=manifest.repo_path,
            repo_size=manifest.file_count,
            primary_language=(
                manifest.languages[0] if manifest.languages else "python"
            ),
        )

        llm = LiteLLMChat(
            model=self._config.model,
            temperature=0.0,
            max_tokens=self._config.max_tokens,
        )
        runner = AgentRunner(
            llm=llm,
            registry=registry,
            max_turns=self._config.max_turns,
            manifest=manifest,
            session_ctx=session_ctx,
        )
        return RepoBundle(config=repo, manifest=manifest, runner=runner)

    # -- queries --

    def list_infos(self) -> List[RepoInfo]:
        return [b.info() for b in self._bundles.values()]

    def get(self, repo_id: str) -> Optional[RepoBundle]:
        return self._bundles.get(repo_id)
