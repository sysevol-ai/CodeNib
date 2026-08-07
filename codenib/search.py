# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional

from .agent.extract_agent import KeywordExtraction, extract_keywords_from_statement
from .graph.code_graph import CodeGraph
from .index.sparse_idx.bm25_index import BM25CodeIndexer
from .llm.litellm_chat import LiteLLMChat
from .log_utils import get_logger
from .ls_router import LSIndexer
from .paths import user_state_dir
from .types import NODE_TYPE_FILE, NodeInfo, is_symbol_node

logger = get_logger(__name__)

_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_CONTEXT_LINES = 1_000


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

        output_path = str(user_state_dir() / (instance_id or self.repo_name))
        os.makedirs(output_path, exist_ok=True)
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
    ) -> List[NodeInfo]:
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
        result: Mapping[str, Any] | NodeInfo,
    ) -> Optional[str]:
        """
        Get source code context for a search result.

        Args:
            result: A single search result from the search method

        Returns:
            Source code context as a string, or None if not available
        """
        normalized = self._result_mapping(result)
        path = self._resolve_source_file(normalized.get("file"))
        if path is None:
            return None
        content = self._read_source(path)
        if content is None:
            return None

        type_of_result = normalized.get("type")
        if type_of_result == NODE_TYPE_FILE:
            return content
        if not is_symbol_node(type_of_result):
            return None
        return self._source_range(
            content,
            normalized.get("start_line"),
            normalized.get("end_line"),
        )

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
        if attributes.get("type") == NODE_TYPE_FILE:
            attributes = dict(attributes)
            attributes["file"] = attributes.get("name")
        return self.get_code_context(attributes)

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
        self,
        result: Mapping[str, Any] | NodeInfo,
        context_lines: int = 5,
    ) -> Dict[str, Any]:
        """
        Enhance a search result with source code context.

        Args:
            result: A single search result from the search method
            context_lines: Number of context lines to include before and after

        Returns:
            Enhanced result with source code context
        """
        if (
            not isinstance(context_lines, int)
            or isinstance(context_lines, bool)
            or not 0 <= context_lines <= _MAX_CONTEXT_LINES
        ):
            raise ValueError(
                f"context_lines must be between 0 and {_MAX_CONTEXT_LINES}"
            )
        enhanced_result = self._result_mapping(result)
        path = self._resolve_source_file(enhanced_result.get("file"))
        if path is None:
            return enhanced_result
        content = self._read_source(path)
        if content is None:
            return enhanced_result

        start_line = self._line_number(enhanced_result.get("start_line"))
        end_line = self._line_number(enhanced_result.get("end_line"))
        if start_line is None or end_line is None or end_line < start_line:
            return enhanced_result
        lines = content.splitlines(keepends=True)
        context_start = max(0, start_line - context_lines)
        context_end = min(len(lines) - 1, end_line + context_lines)
        if context_start > context_end:
            return enhanced_result
        enhanced_result["context_code"] = "".join(
            lines[context_start : context_end + 1]
        )
        enhanced_result["context_range"] = {
            "start": context_start,
            "end": context_end,
        }
        return enhanced_result

    @staticmethod
    def _result_mapping(result: Mapping[str, Any] | NodeInfo) -> Dict[str, Any]:
        if isinstance(result, NodeInfo):
            return result.model_dump(exclude_none=True)
        if isinstance(result, Mapping):
            return dict(result)
        raise TypeError("search result must be a NodeInfo or mapping")

    def _resolve_source_file(self, value: object) -> Optional[Path]:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            return None
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or value != relative.as_posix()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            return None
        root = Path(self.repo_path).expanduser().resolve()
        try:
            candidate = root.joinpath(*relative.parts).resolve(strict=True)
        except OSError:
            return None
        if root != candidate and root not in candidate.parents:
            return None
        return candidate

    @staticmethod
    def _read_source(path: Path) -> Optional[str]:
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                content = handle.read(_MAX_SOURCE_BYTES + 1)
        except OSError:
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(content) > _MAX_SOURCE_BYTES:
            return None
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def _line_number(value: object) -> Optional[int]:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        return value

    @classmethod
    def _source_range(
        cls,
        content: str,
        raw_start: object,
        raw_end: object,
    ) -> Optional[str]:
        start_line = cls._line_number(raw_start)
        end_line = cls._line_number(raw_end)
        if start_line is None or end_line is None or end_line < start_line:
            return None
        lines = content.splitlines(keepends=True)
        return "".join(lines[start_line : end_line + 1])

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
