# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Regex-based in-memory index for CodeGraph nodes.
"""

from __future__ import annotations

import time
from fnmatch import fnmatch
from typing import TYPE_CHECKING, List, Optional

import regex

from ...log_utils import get_logger
from ...types import NODE_TYPE_FILE, NodeInfo

if TYPE_CHECKING:
    from ...graph.code_graph import CodeGraph

logger = get_logger(__name__)

MAX_CONTENT_CHARS = 8000  # Content truncation to avoid memory overflow
MAX_REGEX_PATTERN_CHARS = 4096
REGEX_SEARCH_TIMEOUT_SECONDS = 2.0


class RegexSearchTimeoutError(TimeoutError):
    """Raised when regex matching exceeds the index-wide request deadline."""


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
        content_failures = 0

        for v in vs:
            vid = v.index
            attrs = v.attributes()

            # Get content from the node
            content: Optional[str] = None
            has_source_range = (
                attrs.get("start_line") is not None
                and attrs.get("end_line") is not None
            )
            if attrs.get("type") == NODE_TYPE_FILE or has_source_range:
                try:
                    content = self.code_graph.get_node_content(vid)
                    if content:
                        content = content[:MAX_CONTENT_CHARS]
                except Exception:  # malformed source metadata is non-fatal here
                    content_failures += 1

            # Create NodeInfo object
            node_type = attrs.get("type") or ""
            node = NodeInfo(
                node_name=v["name"],
                type=node_type,
                file=v["name"] if node_type == NODE_TYPE_FILE else attrs.get("file"),
                start_line=attrs.get("start_line"),
                end_line=attrs.get("end_line"),
                content=content,
            )
            self.nodes.append(node)

        logger.info(f"Built index with {len(vs)} nodes")
        if content_failures:
            logger.debug(
                "Skipped content for %d nodes with unreadable source metadata",
                content_failures,
            )

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
            if len(pattern) > MAX_REGEX_PATTERN_CHARS:
                raise ValueError(
                    "Regex pattern exceeds "
                    f"the {MAX_REGEX_PATTERN_CHARS}-character limit"
                )
            flags = 0 if case_sensitive else regex.IGNORECASE
            try:
                compiled = regex.compile(pattern, flags)
            except regex.error as exc:
                logger.error("Invalid regex pattern %r: %s", pattern, exc)
                raise ValueError(f"Invalid regex pattern: {exc}") from exc

            deadline = time.monotonic() + REGEX_SEARCH_TIMEOUT_SECONDS
            for node in candidates:
                if not node.content:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RegexSearchTimeoutError(
                        "Regex search exceeded the "
                        f"{REGEX_SEARCH_TIMEOUT_SECONDS:g}-second execution limit; "
                        "use a simpler pattern or plain-string search"
                    )
                try:
                    matched = compiled.search(node.content, timeout=remaining)
                except TimeoutError as exc:
                    raise RegexSearchTimeoutError(
                        "Regex search exceeded the "
                        f"{REGEX_SEARCH_TIMEOUT_SECONDS:g}-second execution limit; "
                        "use a simpler pattern or plain-string search"
                    ) from exc
                if matched:
                    matches.append(node)
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
