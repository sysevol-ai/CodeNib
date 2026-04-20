"""
ServerContext: loads RepoManifest and hydrates index objects from disk.

Manages lifecycle of CodeMiner indexes for MCP server.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..compiler.manifest import RepoManifest
from ..graph.code_graph import CodeGraph
from ..index.embedding.vector_store import CodeVectorStore
from ..index.sparse_idx.bm25_index import BM25CodeIndexer
from ..log_utils import get_logger

logger = get_logger(__name__)


@dataclass
class ServerContext:
    """
    MCP server context holding all loaded indexes.

    Indexes are loaded from a RepoManifest. Missing or failed indexes
    result in None attributes; tools check and return clear errors.
    """

    manifest: RepoManifest
    symbol_graph: Optional[CodeGraph] = None
    bm25: Optional[BM25CodeIndexer] = None
    vector: Optional[CodeVectorStore] = None

    @classmethod
    def load(cls, manifest_path: str) -> "ServerContext":
        """
        Load ServerContext from a manifest file.

        Args:
            manifest_path: Path to repo_manifest.json

        Returns:
            ServerContext with hydrated indexes
        """
        manifest = RepoManifest.load(manifest_path)
        ctx = cls(manifest=manifest)

        # Load vector store if available
        if "vector" in manifest.indexes:
            try:
                vector_entry = manifest.indexes["vector"]
                cfg = vector_entry.config

                # Create CodeVectorStore instance with embedding model from manifest
                ctx.vector = CodeVectorStore(
                    embedding_model=cfg["embedding_model"],
                    embedding_provider=cfg["embedding_provider"],
                    dimension=cfg.get("dimension"),
                    index_metric=cfg.get("index_metric", "ip"),
                    store_path=vector_entry.path,
                )

                # Load FAISS index from disk (requires self.embedding)
                ctx.vector.load()

                logger.info(
                    f"Loaded vector store: {cfg['embedding_model']} "
                    f"({ctx.vector.get_stats()['total_documents']} docs)"
                )
            except Exception as e:
                logger.warning(f"Failed to load vector index: {e}")
                ctx.vector = None

        return ctx
