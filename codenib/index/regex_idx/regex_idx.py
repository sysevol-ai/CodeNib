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
MAX_REGEX_SCANNED_NODES = 100_000
MAX_REGEX_CANDIDATES = 25_000
REGEX_SEARCH_TIMEOUT_SECONDS = 2.0


class RegexSearchTimeoutError(TimeoutError):
    """Raised when regex work exceeds the request-wide deadline."""


class RegexSearchBudgetError(RuntimeError):
    """Raised when regex work exceeds a deterministic node budget."""


def _timeout_error() -> RegexSearchTimeoutError:
    return RegexSearchTimeoutError(
        "Regex search exceeded the "
        f"{REGEX_SEARCH_TIMEOUT_SECONDS:g}-second request deadline; "
        "use a simpler pattern, a narrower filter, or plain-string search"
    )


def _remaining_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _timeout_error()
    return remaining


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
        top_k: Optional[int] = None,
    ) -> List[NodeInfo]:
        r"""
        Search for pattern in node content (grep-like functionality).

        Args:
            pattern: Search pattern (regex or plain string)
            file_glob: Optional glob to filter by file path (e.g., '*.py', '**/calc.py')
            node_type: Optional node type to filter (e.g., 'function', 'class', 'file')
            case_sensitive: Whether search is case-sensitive (default: False)
            use_regex: Whether to use regex (default: True) or plain string matching
            top_k: Optional result limit. Regex searches stop as soon as it is met.

        Returns:
            List of NodeInfo objects matching the pattern

        Examples:
            >>> idx.search(r'def\s+\w+', file_glob='*.py')  # Find function defs
            >>> idx.search('calculator', use_regex=False)  # Plain string search
            >>> idx.search('class', node_type='file')  # Search in file nodes only
        """
        if use_regex:
            if len(pattern) > MAX_REGEX_PATTERN_CHARS:
                raise ValueError(
                    "Regex pattern exceeds "
                    f"the {MAX_REGEX_PATTERN_CHARS}-character limit"
                )
            if top_k is not None and (
                isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1
            ):
                raise ValueError("top_k must be a positive integer")

            # Start the request-wide clock before compiling or evaluating either
            # user-controlled structural filter. A single filtered-out scan is
            # still work charged to this request.
            deadline = time.monotonic() + REGEX_SEARCH_TIMEOUT_SECONDS
            flags = 0 if case_sensitive else regex.IGNORECASE
            try:
                compiled = regex.compile(pattern, flags)
            except regex.error as exc:
                logger.error("Invalid regex pattern %r: %s", pattern, exc)
                raise ValueError(f"Invalid regex pattern: {exc}") from exc
            _remaining_time(deadline)

            matches: List[NodeInfo] = []
            scanned_nodes = 0
            candidate_nodes = 0
            for node in self.nodes:
                _remaining_time(deadline)
                scanned_nodes += 1
                if scanned_nodes > MAX_REGEX_SCANNED_NODES:
                    raise RegexSearchBudgetError(
                        "Regex search exceeded the "
                        f"{MAX_REGEX_SCANNED_NODES}-node scan budget; "
                        "use file_glob or node_type to narrow the search"
                    )

                if file_glob:
                    path_matches = bool(node.file) and fnmatch(node.file, file_glob)
                    _remaining_time(deadline)
                    if not path_matches:
                        continue
                if node_type and node.type != node_type:
                    continue
                if not node.content:
                    continue

                candidate_nodes += 1
                if candidate_nodes > MAX_REGEX_CANDIDATES:
                    raise RegexSearchBudgetError(
                        "Regex search exceeded the "
                        f"{MAX_REGEX_CANDIDATES}-candidate match budget; "
                        "use file_glob or node_type to narrow the search"
                    )

                remaining = _remaining_time(deadline)
                try:
                    matched = compiled.search(node.content, timeout=remaining)
                except TimeoutError as exc:
                    raise _timeout_error() from exc
                _remaining_time(deadline)
                if matched:
                    matches.append(node)
                    if top_k is not None and len(matches) >= top_k:
                        break

            logger.debug(
                "Regex search pattern=%r file_glob=%r node_type=%r: "
                "found %d matches after scanning %d nodes and %d candidates",
                pattern,
                file_glob,
                node_type,
                len(matches),
                scanned_nodes,
                candidate_nodes,
            )
            return matches

        # Preserve the unrestricted plain-string path for direct index users.
        candidates = self.nodes
        if file_glob:
            candidates = [
                node
                for node in candidates
                if node.file and fnmatch(node.file, file_glob)
            ]
        if node_type:
            candidates = [node for node in candidates if node.type == node_type]

        if case_sensitive:
            matches = [
                node for node in candidates if node.content and pattern in node.content
            ]
        else:
            pattern_lower = pattern.lower()
            matches = [
                node
                for node in candidates
                if node.content and pattern_lower in node.content.lower()
            ]
        if top_k is not None:
            matches = matches[:top_k]

        logger.debug(
            "Plain search pattern=%r file_glob=%r node_type=%r: "
            "found %d matches from %d candidates",
            pattern,
            file_glob,
            node_type,
            len(matches),
            len(candidates),
        )

        return matches
