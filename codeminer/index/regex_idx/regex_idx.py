# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Regex-based in-memory index for CodeGraph nodes.
"""

import re
from fnmatch import fnmatch
from typing import List, Optional

from ...graph.code_graph import CodeGraph
from ...log_utils import get_logger
from ...types import NodeInfo

logger = get_logger(__name__)

MAX_CONTENT_CHARS = 8000  # Content truncation to avoid memory overflow


class RegexNodeIndex:
    """
    In-memory regex-based index for CodeGraph nodes.
    Supports regex pattern matching on node content with glob filtering.
    """

    def __init__(self, code_graph: CodeGraph):
        """
        Initialize RegexNodeIndex and build index from CodeGraph.

        Args:
            code_graph: CodeGraph instance containing nodes to index
        """
        self.code_graph = code_graph
        self.nodes: List[NodeInfo] = []
        self._build_index()

        logger.info(f"RegexNodeIndex initialized with {len(self.nodes)} nodes")

    def _build_index(self):
        """Build index from CodeGraph."""
        vs = self.code_graph.get_graph().vs

        for v in vs:
            vid = v.index
            attrs = v.attributes()

            # Get content from the node
            content: Optional[str] = None
            try:
                content = self.code_graph.get_node_content(vid)
                if content:
                    content = content[:MAX_CONTENT_CHARS]
            except Exception as e:
                logger.debug(f"Failed to get content for node {vid}: {e}")
                content = None

            # Create NodeInfo object
            node = NodeInfo(
                node_name=v["name"],
                type=attrs.get("type") or "",
                file=attrs.get("file"),
                start_line=attrs.get("start_line"),
                end_line=attrs.get("end_line"),
                content=content,
            )
            self.nodes.append(node)

        logger.info(f"Built index with {len(vs)} nodes")

    def search(
        self,
        pattern: str,
        file_glob: Optional[str] = None,
        node_type: Optional[str] = None,
        case_sensitive: bool = False,
        use_regex: bool = True,
    ) -> List[NodeInfo]:
        r"""
        Search for pattern in node content (grep-like functionality).

        Args:
            pattern: Search pattern (regex or plain string)
            file_glob: Optional glob to filter by file path (e.g., '*.py', '**/calc.py')
            node_type: Optional node type to filter (e.g., 'function', 'class', 'file')
            case_sensitive: Whether search is case-sensitive (default: False)
            use_regex: Whether to use regex (default: True) or plain string matching

        Returns:
            List of NodeInfo objects matching the pattern

        Examples:
            >>> idx.search(r'def\s+\w+', file_glob='*.py')  # Find function defs
            >>> idx.search('calculator', use_regex=False)  # Plain string search
            >>> idx.search('class', node_type='file')  # Search in file nodes only
        """
        # Step 1: Filter by structural attributes (file, type)
        candidates = self.nodes

        if file_glob:
            candidates = [
                n for n in candidates if n.file and fnmatch(n.file, file_glob)
            ]

        if node_type:
            candidates = [n for n in candidates if n.type == node_type]

        # Step 2: Search in content using regex or plain string
        matches = []

        if use_regex:
            # Regex matching
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                regex = re.compile(pattern, flags)
            except re.error as e:
                logger.error(f"Invalid regex pattern {pattern!r}: {e}")
                raise ValueError(f"Invalid regex pattern: {e}") from e

            matches = [n for n in candidates if n.content and regex.search(n.content)]
        else:
            # Plain string matching
            if case_sensitive:
                matches = [n for n in candidates if n.content and pattern in n.content]
            else:
                pattern_lower = pattern.lower()
                matches = [
                    n
                    for n in candidates
                    if n.content and pattern_lower in n.content.lower()
                ]

        logger.debug(
            f"Search pattern={pattern!r} file_glob={file_glob} node_type={node_type}: "
            f"found {len(matches)} matches from {len(candidates)} candidates"
        )

        return matches
