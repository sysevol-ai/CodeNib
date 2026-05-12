"""
ServerContext: loads RepoManifest and hydrates index objects from disk.

Manages lifecycle of CodeMiner indexes for MCP server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from ..compiler.manifest import RepoManifest
from ..graph.code_graph import CodeGraph
from ..index.embedding.vector_store import CodeVectorStore
from ..index.sparse_idx.bm25_index import BM25CodeIndexer

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    """
    Runtime context for the MCP server.

    Holds the loaded manifest and hydrated index objects. Missing or
    failed indexes stay None; tools check at call time and return
    descriptive errors.
    """

    manifest: RepoManifest
    symbol_graph: Optional[CodeGraph] = None
    bm25: Optional[BM25CodeIndexer] = None
    vector: Optional[CodeVectorStore] = None
    # Track loading errors for diagnostics
    errors: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, manifest_path: str | Path) -> ServerContext:
        """
        Load a manifest and hydrate all available indexes.

        Each index is loaded independently; a failure in one does not
        block the others. Failed indexes are recorded in errors.

        Args:
            manifest_path: Path to repo_manifest.json

        Returns:
            ServerContext with hydrated indexes
        """
        manifest = RepoManifest.load(manifest_path)
        ctx = cls(manifest=manifest)

        # Load all available indexes
        ctx._load_vector()

        # Log summary
        cap_summary = {k: v for k, v in manifest.capabilities.items() if v}
        logger.info(
            "ServerContext ready  repo=%s  commit=%s  capabilities=%s  errors=%s",
            manifest.repo_path,
            manifest.commit[:8] if manifest.commit else "N/A",
            cap_summary or "none",
            list(ctx.errors) or "none",
        )
        return ctx

    # ------------------------------------------------------------------
    # Private loaders
    # ------------------------------------------------------------------

    def _load_vector(self) -> None:
        """Load vector embedding index if available."""
        entry = self.manifest.indexes.get("vector")
        if not entry or entry.status != "fresh":
            return

        try:
            cfg = entry.config

            # Create CodeVectorStore instance with embedding model from manifest
            self.vector = CodeVectorStore(
                embedding_model=cfg["embedding_model"],
                embedding_provider=cfg["embedding_provider"],
                dimension=cfg.get("dimension"),
                index_metric=cfg.get("index_metric", "ip"),
                store_path=entry.path,
            )

            # Load FAISS index from disk
            self.vector.load()

            # Validate model consistency
            loaded_model = self.vector.embedding_model
            manifest_model = cfg["embedding_model"]
            if loaded_model != manifest_model:
                raise RuntimeError(
                    f"Embedding model mismatch: manifest specifies "
                    f"{manifest_model!r}, but loaded store has {loaded_model!r}. "
                    f"Re-run indexing with the correct model."
                )

            stats = self.vector.get_stats()
            logger.info(
                "Loaded vector index from %s (%d docs, model=%s)",
                entry.path,
                stats["total_documents"],
                cfg["embedding_model"],
            )
        except Exception as exc:
            self.vector = None
            self.errors["vector"] = str(exc)
            logger.warning("Failed to load vector index: %s", exc)
