# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""ServerContext - loads a RepoManifest and hydrates index objects from disk.

Holds vector, symbol_graph, BM25, regex, and Zoekt indexes. Each loads
independently; failures land in ``ctx.errors`` and the corresponding
attribute stays ``None`` so tools can surface a clear error at call time
rather than blocking server startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from ..compiler.manifest import RepoManifest
from ..graph.code_graph import CodeGraph
from ..index.embedding.vector_store import CodeVectorStore
from ..index.regex_idx import RegexNodeIndex
from ..index.sparse_idx import BM25CodeIndexer
from ..index.trigram import ZoektSearcher, ZoektUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    """Runtime context for the MCP server.

    Holds the loaded manifest and hydrated index objects. Missing or
    failed indexes stay ``None``; tools check at call time and return
    descriptive errors.
    """

    manifest: RepoManifest
    symbol_graph: Optional[CodeGraph] = None
    bm25: Optional[BM25CodeIndexer] = None
    regex_index: Optional[RegexNodeIndex] = None
    zoekt: Optional[ZoektSearcher] = None
    vector: Optional[CodeVectorStore] = None
    errors: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, manifest_path: str | Path) -> ServerContext:
        """Load a manifest and hydrate all available indexes.

        Each index is loaded independently; a failure in one does not
        block the others. Failed indexes are recorded in ``errors``.
        """
        manifest = RepoManifest.load(manifest_path)
        ctx = cls(manifest=manifest)

        ctx._load_symbol_graph()
        ctx._load_bm25()
        ctx._load_regex_index()
        ctx._load_zoekt()
        ctx._load_vector()

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

    def _load_symbol_graph(self) -> None:
        entry = self.manifest.indexes.get("symbol_graph")
        if not entry or entry.status != "fresh":
            return
        try:
            graph_path = Path(entry.path)
            pkl = graph_path / "graph.pkl" if graph_path.is_dir() else graph_path
            self.symbol_graph = CodeGraph.load_graph(str(pkl))
            logger.info("Loaded symbol_graph from %s", pkl)
        except Exception as exc:
            self.errors["symbol_graph"] = str(exc)
            logger.warning("Failed to load symbol_graph: %s", exc)

    def _load_bm25(self) -> None:
        entry = self.manifest.indexes.get("bm25")
        if not entry or entry.status != "fresh":
            return
        try:
            indexer = BM25CodeIndexer()
            indexer.load_index(entry.path)
            self.bm25 = indexer
            logger.info("Loaded BM25 index from %s", entry.path)
        except Exception as exc:
            self.errors["bm25"] = str(exc)
            logger.warning("Failed to load BM25 index: %s", exc)

    def _load_regex_index(self) -> None:
        """Regex index piggybacks on symbol_graph; no separate manifest entry."""
        if self.symbol_graph is None:
            return
        try:
            self.regex_index = RegexNodeIndex(self.symbol_graph)
            logger.info("Built RegexNodeIndex (%d nodes)", len(self.regex_index.nodes))
        except Exception as exc:
            self.errors["regex_index"] = str(exc)
            logger.warning("Failed to build RegexNodeIndex: %s", exc)

    def _load_zoekt(self) -> None:
        """Spawn a ``zoekt-webserver`` against the indexed shard directory.

        The Zoekt binary is a soft dependency. When the binary is missing
        or the webserver fails to come up, the failure is recorded in
        :attr:`errors` and ``self.zoekt`` stays ``None`` so the
        ``search_zoekt`` MCP tool can return a clear error.
        """
        entry = self.manifest.indexes.get("zoekt")
        if not entry or entry.status != "fresh":
            return
        try:
            searcher = ZoektSearcher(index_dir=entry.path)
            searcher.start()
            self.zoekt = searcher
            logger.info(
                "Started zoekt-webserver  shards=%s  port=%d",
                entry.path,
                searcher.port,
            )
        except ZoektUnavailableError as exc:
            self.errors["zoekt"] = str(exc)
            logger.warning("Zoekt unavailable: %s", exc)
        except Exception as exc:
            self.errors["zoekt"] = str(exc)
            logger.warning("Failed to start zoekt-webserver: %s", exc)

    def _load_vector(self) -> None:
        """Load vector embedding index if available."""
        entry = self.manifest.indexes.get("vector")
        if not entry or entry.status != "fresh":
            return

        try:
            cfg = entry.config

            # Create CodeVectorStore with embedding model from manifest
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
