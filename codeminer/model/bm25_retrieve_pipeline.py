from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..index.sparse_idx.bm25_index import BM25CodeIndexer
from ..log_utils import get_logger
from ..scip_interface import SCIPIndexerBase as SCIPIndexer
from ..types import QueriedNode

logger = get_logger(__name__)


class BM25RetrievePipeline:
    """BM25-only retrieval pipeline.

    Builds a code graph via SCIP indexing, then retrieves top-K nodes
    using BM25 sparse search. Useful as a baseline to isolate whether
    poor graph retrieval performance is due to bad BM25 seeds or bad
    graph expansion.

    Args:
        repo_path: Repository root to index.
        index_path: Directory used for SCIP index caches.
        top_k: Number of results to retrieve (default: 50).
        project_name: Project name for SCIP indexing; defaults to index_path basename.
    """

    def __init__(
        self,
        repo_path: str,
        index_path: str,
        *,
        top_k: int = 50,
        project_name: Optional[str] = None,
    ) -> None:
        self.top_k = top_k

        pname = project_name or Path(index_path).name

        # Build CodeGraph via SCIP indexing
        repo_indexer = SCIPIndexer(repo_path, output_dir=index_path)
        self.code_graph = repo_indexer.run_pipeline(
            project_name=pname, skip_level="graph"
        )
        if self.code_graph is None:
            raise RuntimeError(f"Failed to build code graph for {repo_path!r}")

        # Build BM25 index from graph
        self.bm25_index = BM25CodeIndexer(max_k=top_k, language="english")
        self.bm25_index.build_index_from_graph(self.code_graph)

    def query(self, query: str, top_k: Optional[int] = None) -> List[QueriedNode]:
        """Retrieve top-K nodes using BM25 sparse search.

        Args:
            query: The search query (e.g., problem statement).
            top_k: Number of results; overrides the instance default if provided.

        Returns:
            List of QueriedNode sorted by BM25 score.
        """
        k = top_k if top_k is not None else self.top_k
        bm25_results = self.bm25_index.search(query, top_k=k)
        return [
            QueriedNode(
                node_name=n.node_name,
                type=n.type,
                file=n.file,
                node_id=n.node_id or n.node_name,
                start_line=n.start_line,
                end_line=n.end_line,
                score=n.score,
                content=n.content,
            )
            for n in bm25_results
        ]

    def close(self) -> None:
        """Release pipeline resources."""
        self.bm25_index = None
        self.code_graph = None
