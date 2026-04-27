"""ServerContext - loads a RepoManifest and hydrates index objects from disk."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from ..compiler.manifest import RepoManifest
from ..graph.code_graph import CodeGraph
from ..index.regex_idx import RegexNodeIndex
from ..index.sparse_idx import BM25CodeIndexer

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    """Runtime context for the MCP server.

    Holds the loaded manifest and hydrated index objects.  Missing or
    failed indexes stay ``None``; tools check at call time and return
    descriptive errors.
    """

    manifest: RepoManifest
    symbol_graph: Optional[CodeGraph] = None
    bm25: Optional[BM25CodeIndexer] = None
    regex_index: Optional[RegexNodeIndex] = None
    # ROISubgraph and vector store will be added by graph / vector tool phases.
    errors: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, manifest_path: str | Path) -> ServerContext:
        """Load a manifest and hydrate all available indexes.

        Each index is loaded independently; a failure in one does not
        block the others.  Failed indexes are recorded in ``errors``.
        """
        manifest = RepoManifest.load(manifest_path)
        ctx = cls(manifest=manifest)

        ctx._load_symbol_graph()
        ctx._load_bm25()
        ctx._load_regex_index()

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
