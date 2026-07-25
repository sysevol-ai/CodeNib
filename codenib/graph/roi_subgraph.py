# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from collections import deque
from typing import List, Optional, Set

import igraph as ig

from ..log_utils import get_logger
from ..types import NodeInfo
from ..utils import is_test_file
from .code_graph import CodeGraph

logger = get_logger(__name__)


class ROISubgraph:
    """
    Region of Interest (ROI) subgraph extraction for code graphs.

    This class takes search results from CodeSearchEngine and extracts a focused
    subgraph containing the most relevant code elements and their neighborhood.
    """

    def __init__(self, code_graph: CodeGraph):
        """
        Initialize the ROI subgraph extractor.

        Args:
            code_graph: The full code graph from which to extract subgraphs
        """
        self.code_graph = code_graph
        self.full_graph = code_graph.get_graph()

    def expand_ppr(
        self,
        node_names: List[str],
        top_k: int = 50,
        damping: float = 0.85,
        filter_tests: bool = True,
    ) -> List[NodeInfo]:
        """Expand seed nodes using Personalized PageRank.

        Runs PPR on the full graph with seed nodes as the personalization
        vector, then returns the top-k nodes ranked by PPR score.

        Args:
            node_names: Seed node names (e.g. from BM25).
            top_k: Maximum number of nodes to return.
            damping: PPR damping factor (0–1). Higher = more global.
            filter_tests: Whether to exclude test files.

        Returns:
            List of NodeInfo objects sorted by PPR score (descending).
        """
        n_vertices = self.full_graph.vcount()
        if n_vertices == 0:
            return []

        # Build personalization vector: uniform weight on seeds, 0 elsewhere
        reset = [0.0] * n_vertices
        seed_count = 0
        for name in node_names:
            vid = self.code_graph.name_to_vertex.get(name)
            if vid is not None:
                reset[vid] = 1.0
                seed_count += 1
            else:
                logger.warning("PPR seed '%s' not found in graph", name)

        if seed_count == 0:
            logger.warning("No valid PPR seeds — returning empty results")
            return []

        # Run Personalized PageRank
        scores = self.full_graph.personalized_pagerank(
            directed=True, damping=damping, reset=reset
        )

        # Pair each vertex with its PPR score and sort descending
        scored = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

        results: List[NodeInfo] = []
        for vid, ppr_score in scored:
            if len(results) >= top_k:
                break

            node_attrs = self.get_node_info_by_id(vid)

            # Filter test files
            if filter_tests and node_attrs.file and is_test_file(node_attrs.file):
                continue

            # Skip zero-span or missing line info
            if (
                node_attrs.start_line is None
                or node_attrs.end_line is None
                or node_attrs.start_line == node_attrs.end_line
            ):
                continue

            content = self.get_node_content(vid) or ""
            results.append(
                NodeInfo(
                    node_name=node_attrs.node_name,
                    type=node_attrs.type,
                    file=node_attrs.file,
                    start_line=node_attrs.start_line,
                    end_line=node_attrs.end_line,
                    score=ppr_score,
                    content=content,
                )
            )

        logger.info(
            "PPR expansion: %d seeds -> %d nodes (damping=%.2f)",
            seed_count,
            len(results),
            damping,
        )
        return results

    def extract_subgraph(
        self,
        node_names: List[str],
        k_hop: int = 2,
        edge_types: Optional[List[str]] = None,
        direction: str = "both",
    ) -> ig.Graph:
        """
        Extract a subgraph around specified node names.

        Args:
            node_names: List of node names to use as seeds for the subgraph
            k_hop: Number of hops to expand from each seed node
            edge_types: Optional list of edge types to consider (None for all types)
            direction: Direction to traverse - "forward", "backward", or "both"

        Returns:
            An igraph Graph object representing the extracted subgraph
        """
        # Track visited nodes to avoid duplicates
        visited_nodes = set()
        # Track nodes to include in the subgraph
        subgraph_nodes = set()

        # BFS for each seed node
        for node_name in node_names:
            try:
                # Convert node name to node ID
                if node_name not in self.code_graph.name_to_vertex:
                    logger.warning(f"Node name {node_name!r} not found in graph")
                    continue

                node_id = self.code_graph.name_to_vertex[node_name]

                # Skip if we've already processed this node
                if node_id in visited_nodes:
                    continue

                # Add this seed node to the subgraph and mark as visited
                subgraph_nodes.add(node_id)
                visited_nodes.add(node_id)

                # Initialize queue for BFS with (node, depth) pairs
                queue = deque([(node_id, 0)])

                # BFS traversal to find k-hop neighborhood
                while queue:
                    current_node, depth = queue.popleft()

                    # Stop expanding if we've reached k hops
                    if depth >= k_hop:
                        continue

                    # Get neighbors
                    neighbors = self._get_neighbors(current_node, edge_types, direction)

                    # Process neighbors
                    for neighbor in neighbors:
                        if neighbor not in visited_nodes:
                            visited_nodes.add(neighbor)
                            subgraph_nodes.add(neighbor)
                            queue.append((neighbor, depth + 1))

            except Exception as e:
                logger.error(f"Error processing node {node_name}: {e}")
                continue

        # Create subgraph from the collected nodes
        return self._create_subgraph(subgraph_nodes)

    def get_filtered_subgraph_nodes(
        self,
        subgraph: ig.Graph,
        exclude_nodes: Optional[List[str]] = None,
        filter_tests: bool = True,
        node_types: Optional[List[str]] = None,
    ) -> List[NodeInfo]:
        """
        Extract useful nodes from a subgraph, filtering out nodes where
        start_line equals end_line (unless explicitly included).

        Args:
            subgraph: An igraph Graph object (from extract_subgraph or
                extract_roi_from_search_results)
            node_types: Optional list of node types to include (None for all types)

        Returns:
            List of NodeInfo objects including the node content
        """
        filtered_nodes = []

        for node in subgraph.vs:
            try:
                # Get original node ID and attributes
                original_id = node["original_id"]
                node_attrs = self.get_node_info_by_id(original_id)

                # Filter out test files if specified
                if filter_tests and node_attrs.file and is_test_file(node_attrs.file):
                    continue

                # Filter by node type if specified
                if node_types and node_attrs.type not in node_types:
                    continue

                # Skip nodes with zero line span or None values
                if (
                    node_attrs.start_line is None
                    or node_attrs.end_line is None
                    or node_attrs.start_line == node_attrs.end_line
                ):
                    continue

                # Get the node content
                content = self.get_node_content(original_id) or ""

                # Create NodeInfo and add to the list
                node_with_content = NodeInfo(
                    node_name=node_attrs.node_name,
                    type=node_attrs.type,
                    file=node_attrs.file,
                    start_line=node_attrs.start_line,
                    end_line=node_attrs.end_line,
                    content=content,
                )

                filtered_nodes.append(node_with_content)

            except Exception as e:
                logger.error(f"Error processing node {node.index}: {e}")
                continue

        # remove exclude_nodex in filtered_nodes
        if exclude_nodes:
            filtered_nodes = [
                node for node in filtered_nodes if node.node_name not in exclude_nodes
            ]

        logger.info(f"Extracted {len(filtered_nodes)} useful nodes from subgraph")
        return filtered_nodes

    def _get_neighbors(
        self,
        node_id: int,
        edge_types: Optional[List[str]] = None,
        direction: str = "both",
    ) -> List[int]:
        """
        Get neighbors of a node, optionally filtered by edge type and direction.

        Iterates over edges (not unique neighbors) so multi-edges between
        the same pair — possible after the anchor-aware schema upgrade —
        are each inspected for type filtering. Returned neighbor list may
        contain duplicates only if multiple matching edges exist between
        the same pair; callers using sets handle this naturally.

        Args:
            node_id: ID of the node
            edge_types: Optional list of edge types to filter by
            direction: Direction to traverse - "forward", "backward", or "both"

        Returns:
            List of neighbor node IDs
        """
        # Map direction to the edge-set we should iterate over.
        if direction == "forward":
            edge_ids = self.full_graph.incident(node_id, mode="out")
        elif direction == "backward":
            edge_ids = self.full_graph.incident(node_id, mode="in")
        else:  # "both"
            edge_ids = self.full_graph.incident(node_id, mode="all")

        # No edge-type filter: return unique neighbors directly.
        if not edge_types:
            seen = set()
            result = []
            for eid in edge_ids:
                e = self.full_graph.es[eid]
                neighbor = e.target if e.source == node_id else e.source
                if neighbor not in seen:
                    seen.add(neighbor)
                    result.append(neighbor)
            return result

        # With edge-type filter: include each neighbor at most once but only
        # if at least one of the edges between them passes the type filter.
        filtered_neighbors: List[int] = []
        seen = set()
        for eid in edge_ids:
            try:
                e = self.full_graph.es[eid]
                if "type" not in e.attributes():
                    continue
                if e["type"] not in edge_types:
                    continue
                neighbor = e.target if e.source == node_id else e.source
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                filtered_neighbors.append(neighbor)
            except Exception as exc:
                logger.error(f"Error inspecting edge {eid}: {exc}")
                continue

        return filtered_neighbors

    def _create_subgraph(self, node_ids: Set[int]) -> ig.Graph:
        """
        Create a subgraph from a set of node IDs.

        Args:
            node_ids: Set of node IDs to include in the subgraph

        Returns:
            An igraph Graph object representing the subgraph
        """
        node_list = list(node_ids)

        # Create a subgraph from the original graph
        subgraph = self.full_graph.subgraph(node_list)

        # Add original node IDs as an attribute to make mapping back easier
        for i, original_id in enumerate(node_list):
            subgraph.vs[i]["original_id"] = original_id

        return subgraph

    def get_node_info_by_id(self, node_id: int) -> NodeInfo:
        """
        Get information about a node in the original graph.

        Args:
            node_id: ID of the node in the original graph

        Returns:
            NodeInfo containing the node attributes
        """
        try:
            vertex = self.full_graph.vs[node_id]
            attributes = vertex.attributes()

            # In igraph, the vertex name is stored in the "name" attribute
            node_name = vertex["name"]

            node_info_dict = {
                "node_name": node_name,
                "type": attributes.get("type", ""),
                "file": attributes.get("file", None),
                "start_line": attributes.get("start_line", None),
                "end_line": attributes.get("end_line", None),
            }

            return NodeInfo(**node_info_dict)
        except Exception as e:
            logger.error(f"Error getting node info for node {node_id}: {e}")
            return NodeInfo(type="")

    def get_node_content(self, node_id: int) -> Optional[str]:
        """
        Get the content of a node in the original graph.

        Args:
            node_id: ID of the node in the original graph
        Returns:
            The content of the node, or None if not available
        """
        node_content = self.code_graph.get_node_content(node_id)
        if node_content is None:
            logger.warning(f"No content found for node {node_id}")
            return None
        return node_content
