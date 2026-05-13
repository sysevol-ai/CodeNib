# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .agent.extract_agent import KeywordExtraction, extract_keywords_from_statement
from .graph.code_graph import CodeGraph
from .index.sparse_idx.bm25_index import BM25CodeIndexer
from .llm.litellm_chat import LiteLLMChat
from .log_utils import get_logger
from .ls_router import LSIndexer

logger = get_logger(__name__)


class CodeSearchEngine:
    """
    Integrated search engine that combines SCIP indexing, code graph representation,
    BM25 indexing, and keyword extraction to provide context-aware code search.
    """

    def __init__(
        self,
        repo_path: str,
        llm_model: str = "gpt-4o",
        llm_temperature: float = 0.0,
        llm_max_tokens: int = 8192,
        top_k: int = 10,
        language: str = "english",
        instance_id: Optional[str] = None,
    ):
        """
        Initialize the CodeSearchEngine.

        Args:
            repo_path: Path to the repository to index and search.
            llm_model: Full litellm model identifier for keyword extraction
                (e.g., ``"gpt-4o"``, ``"vertex_ai/gemini-2.5-flash"``).
            llm_temperature: Temperature for LLM sampling.
            llm_max_tokens: Maximum tokens for LLM responses.
            top_k: Number of top results to return.
            language: Language for BM25 indexer (default is "english").
            instance_id: Optional instance ID for the dataset.
        """
        self.repo_path = os.path.abspath(repo_path)
        self.repo_name = os.path.basename(self.repo_path)

        self.llm = LiteLLMChat(
            model=llm_model,
            temperature=llm_temperature,
            max_tokens=llm_max_tokens,
        )
        self.top_k = top_k
        self.language = language

        # Components
        self.code_graph: CodeGraph = None
        self.bm25_indexer = None

        logger.info(f"Initialized CodeSearchEngine for repository: {self.repo_name}")
        self.index_repository(repo_name=self.repo_name, instance_id=instance_id)

    def index_repository(
        self,
        repo_name: Optional[str] = None,
        instance_id: Optional[str] = None,
    ) -> bool:
        """
        Index the repository using SCIP and build BM25 index.

        Args:
            repo_name: Optional name for the repository
            instance_id: Optional instance ID for the dataset

        Returns:
            bool: True if indexing was successful, False otherwise
        """
        # Create SCIP indexer
        logger.info(f"Indexing {repo_name} repository using SCIP...")

        # set output path with ~/.codeminer/instance_id
        output_path = str(Path.home()) + "/.codeminer/" + instance_id
        if not os.path.exists(output_path):
            os.makedirs(output_path)
        scip_indexer = LSIndexer(self.repo_path, output_dir=output_path)

        logger.info(f"Saved decoded scip index to {output_path}")

        # Build code graph
        # Run the indexing pipeline
        self.code_graph = scip_indexer.run_pipeline(project_name=instance_id)

        # Build BM25 index
        logger.info("Building BM25 index from code graph...")
        try:
            self.bm25_indexer = BM25CodeIndexer(
                code_graph=self.code_graph, max_k=self.top_k, language=self.language
            )
        except Exception as e:
            logger.error(f"Error building BM25 index: {e}")
            return False

        return True

    def extract_keywords(self, problem_statement: str) -> KeywordExtraction:
        """
        Extract keywords from a problem statement.

        Args:
            problem_statement: The problem statement to analyze

        Returns:
            KeywordExtraction object containing extracted keywords
        """
        logger.info("Extracting keywords from problem statement...")
        try:
            keywords = extract_keywords_from_statement(
                problem_statement,
                llm=self.llm,
            )
            logger.info(
                f"Extracted {len(keywords.keywords)} keywords: {', '.join(keywords.keywords)}"
            )
            return keywords
        except Exception as e:
            logger.error(f"Error extracting keywords: {e}")
            # Return empty keywords if extraction fails
            return KeywordExtraction(keywords=[])

    def search(
        self,
        problem_statement: str,
        use_keyword_extraction: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Search the repository for code relevant to the problem statement.

        Args:
            problem_statement: The problem statement or query
            use_keyword_extraction: Whether to use keyword extraction.

        Returns:
            List of search results with scores and locations
        """
        if self.bm25_indexer is None:
            logger.error("Repository is not indexed. Call index_repository() first.")
            return []

        search_query = problem_statement

        # Extract keywords if requested
        if use_keyword_extraction:
            extracted = self.extract_keywords(problem_statement)
            if extracted and extracted.keywords:
                # Combine extracted keywords into a search query
                search_query = " ".join(extracted.keywords)

        logger.info(f"Searching with query: {search_query}")

        # Perform search
        try:
            results = self.bm25_indexer.search(search_query)
            logger.info(f"Found {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"Error during search: {e}")
            return []

    def get_code_context(
        self,
        result: Dict[str, Any],
    ) -> Optional[str]:
        """
        Get source code context for a search result.

        Args:
            result: A single search result from the search method

        Returns:
            Source code context as a string, or None if not available
        """
        # check type first
        type_of_result = result.get("type")
        if type_of_result == "file":
            # If the result is a file, we can return the whole file content
            file_path = os.path.join(self.repo_path, result["file"])
            if not os.path.exists(file_path):
                return None

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception as e:
                logger.error(f"Error reading file {file_path}: {e}")
                return None
        elif type_of_result == "symbol":
            file_path = os.path.join(self.repo_path, result["file"])
            if not os.path.exists(file_path):
                return None

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

                start_line = result.get("start_line", 0)
                end_line = result.get("end_line", len(lines))

                # Extract the relevant lines
                context_code = "".join(lines[start_line:end_line])
                return context_code
            except Exception as e:
                logger.error(f"Error getting code context: {e}")
                return None

    def get_code_context_by_node_name(
        self,
        node_name: str,
    ) -> Optional[str]:
        """
        Get source code context for a node by its name.

        Args:
            node_name: The name of the graph node

        Returns:
            Source code context as a string, or None if not available
        """
        # Search for the node in the code graph
        attributes = self.code_graph.get_node_info_by_name(node_name)
        if not attributes:
            logger.warning(f"Node {node_name!r} not found in the code graph.")
            return None
        # Get the type of the node
        node_type = attributes.get("type")
        if node_type == "file":
            # If the node is a file, return the whole file content
            file_path = os.path.join(self.repo_path, attributes["name"])
            if not os.path.exists(file_path):
                logger.warning(f"File {file_path!r} does not exist.")
                return None

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception as e:
                logger.error(f"Error reading file {file_path}: {e}")
                return None
        elif node_type == "symbol":
            file_path = os.path.join(self.repo_path, attributes["file"])
            if not os.path.exists(file_path):
                logger.warning(f"File {file_path!r} does not exist.")
                return None

            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

                start_line = attributes.get("start_line", 0)
                end_line = attributes.get("end_line", len(lines))

                # Extract the relevant lines
                context_code = "".join(lines[start_line:end_line])
                return context_code
            except Exception as e:
                logger.error(f"Error getting code context: {e}")
                return None

    def get_predecessor_nodes(
        self,
        node_name: str,
    ) -> List[str]:
        """
        Get predecessor nodes for a given node name.

        Args:
            node_name: The name of the graph node

        Returns:
            List of predecessor node names
        """
        if self.code_graph is None:
            logger.error("Code graph is not initialized.")
            return []

        predecessors = self.code_graph.get_predecessors(node_name)
        if not predecessors:
            logger.warning(f"No predecessors found for node {node_name!r}.")

        return predecessors

    def get_rich_result_context(
        self, result: Dict[str, Any], context_lines: int = 5
    ) -> Dict[str, Any]:
        """
        Enhance a search result with source code context.

        Args:
            result: A single search result from the search method
            context_lines: Number of context lines to include before and after

        Returns:
            Enhanced result with source code context
        """
        if "file" not in result or not result["file"]:
            return result

        file_path = os.path.join(self.repo_path, result["file"])
        if not os.path.exists(file_path):
            return result

        # Copy the result to avoid modifying the original
        enhanced_result = dict(result)

        try:
            # Get line range from the result
            start_line = result.get("start_line")
            end_line = result.get("end_line")

            if start_line is not None and end_line is not None:
                # Calculate context range
                context_start = max(0, start_line - context_lines)

                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

                # Adjust context end based on file length
                context_end = min(len(lines), end_line + context_lines)

                # Extract the relevant lines
                context_code = "".join(lines[context_start:context_end])
                enhanced_result["context_code"] = context_code
                enhanced_result["context_range"] = {
                    "start": context_start,
                    "end": context_end,
                }
        except Exception as e:
            logger.error(f"Error getting code context: {e}")

        return enhanced_result

    def search_with_context(
        self,
        problem_statement: str,
        context_lines: int = 5,
        use_keyword_extraction: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Search and include source code context in the results.

        Args:
            problem_statement: The problem statement or query
            context_lines: Number of context lines to include
            use_keyword_extraction: Whether to use keyword extraction

        Returns:
            List of search results with code context
        """
        # First get the basic search results
        results = self.search(
            problem_statement,
            use_keyword_extraction=use_keyword_extraction,
        )

        # Enhance each result with context
        enhanced_results = []
        for result in results:
            enhanced_result = self.get_rich_result_context(result, context_lines)
            enhanced_results.append(enhanced_result)

        return enhanced_results
