# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Utility types and functions for code search QA dataset generation.

This module defines the data structures for multi-level code search queries,
ground truth definitions, and output formats compatible with LocBench and SWE-bench.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class QueryType(str, Enum):
    """
    Query types for code search prompts.

    Types describe the specificity and kind of hints the query can include:
    - BEHAVIORAL: Pure natural language, no code identifiers
    - FILE_HINT: Mentions file paths but not specific symbols
    - SYMBOL_HINT: Mentions function/class names directly
    - REASONING: Mentions symbols but requires understanding relationships/call chains
    """

    BEHAVIORAL = "behavioral"
    MODULE_HINT = "module_hint"
    FILE_HINT = "file_hint"
    SYMBOL_HINT = "symbol_hint"
    REASONING = "reasoning"


# Backward-compatible alias (deprecated).
QueryDifficultyLevel = QueryType


@dataclass
class CodeLocation:
    """
    A specific location in the codebase that answers a query.

    Follows the LocBench/SWE-bench convention for specifying code locations.
    """

    file_path: str
    """Relative path to the file from repository root (e.g., 'src/utils/cache.py')"""

    symbol: Optional[str] = None
    """Symbol name if applicable (e.g., 'CacheManager.invalidate')"""

    start_line: Optional[int] = None
    """Starting line number (1-indexed)"""

    end_line: Optional[int] = None
    """Ending line number (1-indexed)"""

    symbol_type: Optional[str] = None
    """Type of symbol: 'function', 'method', 'class', or None for file-level"""

    @property
    def node_id(self) -> str:
        """
        Generate a unique node identifier in the format used by gt_locate.py.

        Format: "file_path:SymbolName" or "file_path:ClassName.method_name()"
        """
        if self.symbol:
            suffix = "()" if self.symbol_type in ("function", "method") else ""
            return f"{self.file_path}:{self.symbol}{suffix}"
        return self.file_path

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "file_path": self.file_path,
            "symbol": self.symbol,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "symbol_type": self.symbol_type,
            "node_id": self.node_id,
        }


@dataclass
class GroundTruth:
    """
    Ground truth for a code search query.

    Specifies the expected code locations that answer the query.
    A query may have multiple valid locations (e.g., a function and its callers).
    """

    primary_locations: List[CodeLocation]
    """Primary locations that directly answer the query (must find at least one)"""

    secondary_locations: List[CodeLocation] = field(default_factory=list)
    """Secondary/related locations (finding these is a bonus)"""

    context_locations: List[CodeLocation] = field(default_factory=list)
    """Context locations needed to understand the issue (e.g., callers, tests)"""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "primary_locations": [loc.to_dict() for loc in self.primary_locations],
            "secondary_locations": [loc.to_dict() for loc in self.secondary_locations],
            "context_locations": [loc.to_dict() for loc in self.context_locations],
            # Flat lists for compatibility with existing evaluation scripts
            "target_files": sorted({loc.file_path for loc in self.primary_locations}),
            "target_symbols": [
                loc.node_id for loc in self.primary_locations if loc.symbol
            ],
        }


@dataclass
class CodeSearchQuery:
    """
    A single code search query with its ground truth.

    This is the core data structure for the QA dataset.
    """

    query_id: str
    """Unique identifier for this query"""

    query_text: str
    """The natural language query text"""

    query_type: QueryType
    """Query type (behavioral, module hint, file hint, symbol hint, reasoning)"""

    ground_truth: GroundTruth
    """Ground truth locations for evaluation"""

    # Metadata
    repo: str
    """Repository name (e.g., 'django/django')"""

    base_commit: str
    """Commit hash at which the query applies"""

    source_instance_id: Optional[str] = None
    """Original SWE-bench/LocBench instance ID if derived from existing dataset"""

    # Optional fields for richer queries
    focus_area: Optional[str] = None
    """Short phrase describing the focus (e.g., 'caching behavior')"""

    hints: List[str] = field(default_factory=list)
    """Optional hints that could be revealed progressively"""

    related_queries: List[str] = field(default_factory=list)
    """IDs of related queries at different query types"""

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary in a format compatible with LocBench/SWE-bench.

        Output format:
        {
            "query_id": "django__django-12345_L1",
            "instance_id": "django__django-12345",  # for compatibility
            "repo": "django/django",
            "base_commit": "abc123...",
            "query": "...",
            "query_type": "behavioral",
            "difficulty": "behavioral",  # compatibility alias
            "ground_truth": {...},
            "focus": "...",
            "hints": [...],
            "related_queries": [...]
        }
        """
        return {
            # Core fields
            "query_id": self.query_id,
            "instance_id": self.source_instance_id or self.query_id,  # compatibility
            "repo": self.repo,
            "base_commit": self.base_commit,
            # Query content
            "query": self.query_text,
            "query_type": self.query_type.value,
            "difficulty": self.query_type.value,
            # Ground truth (expanded format)
            "ground_truth": self.ground_truth.to_dict(),
            # Flat fields for backward compatibility with existing eval scripts
            "target_files": self.ground_truth.to_dict()["target_files"],
            "target_symbols": self.ground_truth.to_dict()["target_symbols"],
            # Optional metadata
            "focus": self.focus_area,
            "hints": self.hints if self.hints else None,
            "related_queries": self.related_queries if self.related_queries else None,
        }


@dataclass
class CodeSearchDataset:
    """
    A complete code search QA dataset.

    Contains multiple queries organized by query type and repository.
    """

    name: str
    """Dataset name (e.g., 'codenib-qa-v1')"""

    version: str
    """Dataset version"""

    queries: List[CodeSearchQuery]
    """All queries in the dataset"""

    description: Optional[str] = None
    """Optional description of the dataset"""

    def filter_by_query_type(
        self, query_type: Union[QueryType, str]
    ) -> List[CodeSearchQuery]:
        """Filter queries by query type."""
        if isinstance(query_type, str):
            query_type = QueryType(query_type)
        return [q for q in self.queries if q.query_type == query_type]

    def filter_by_difficulty(
        self, level: Union[QueryType, str]
    ) -> List[CodeSearchQuery]:
        """Backward-compatible alias for filter_by_query_type."""
        return self.filter_by_query_type(level)

    def filter_by_repo(self, repo: str) -> List[CodeSearchQuery]:
        """Filter queries by repository."""
        return [q for q in self.queries if q.repo == repo]

    def get_stats(self) -> Dict[str, Any]:
        """Get dataset statistics."""
        by_query_type = {}
        by_repo = {}

        for q in self.queries:
            level = q.query_type.value
            by_query_type[level] = by_query_type.get(level, 0) + 1
            by_repo[q.repo] = by_repo.get(q.repo, 0) + 1

        return {
            "name": self.name,
            "version": self.version,
            "total_queries": len(self.queries),
            "by_query_type": by_query_type,
            "by_difficulty": by_query_type,
            "by_repo": by_repo,
            "unique_repos": len(by_repo),
        }

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary format for JSON serialization.

        Output format follows SWE-bench/LocBench conventions:
        {
            "metadata": {
                "name": "...",
                "version": "...",
                "description": "...",
                "stats": {...}
            },
            "instances": [...]  # List of query dicts
        }
        """
        return {
            "metadata": {
                "name": self.name,
                "version": self.version,
                "description": self.description,
                "stats": self.get_stats(),
            },
            "instances": [q.to_dict() for q in self.queries],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodeSearchDataset":
        """Load dataset from dictionary representation."""
        metadata = data.get("metadata", {})
        instances = data.get("instances", [])

        queries = []
        for inst in instances:
            # Parse ground truth
            gt_data = inst.get("ground_truth", {})
            primary_locs = [
                CodeLocation(**loc) for loc in gt_data.get("primary_locations", [])
            ]
            secondary_locs = [
                CodeLocation(**loc) for loc in gt_data.get("secondary_locations", [])
            ]
            context_locs = [
                CodeLocation(**loc) for loc in gt_data.get("context_locations", [])
            ]

            ground_truth = GroundTruth(
                primary_locations=primary_locs,
                secondary_locations=secondary_locs,
                context_locations=context_locs,
            )

            query = CodeSearchQuery(
                query_id=inst["query_id"],
                query_text=inst["query"],
                query_type=QueryType(inst.get("query_type") or inst.get("difficulty")),
                ground_truth=ground_truth,
                repo=inst["repo"],
                base_commit=inst["base_commit"],
                source_instance_id=inst.get("instance_id"),
                focus_area=inst.get("focus"),
                hints=inst.get("hints") or [],
                related_queries=inst.get("related_queries") or [],
            )
            queries.append(query)

        return cls(
            name=metadata.get("name", "unknown"),
            version=metadata.get("version", "0.0.0"),
            queries=queries,
            description=metadata.get("description"),
        )


# Query generation prompt templates by query type
QUERY_TYPE_PROMPTS: Dict[QueryType, str] = {
    QueryType.BEHAVIORAL: (
        "Craft a natural-language question describing the observed behavior needing a fix. "
        "Describe the BEHAVIOR, INTENT, and user-visible SYMPTOM in plain English; focus on "
        "WHAT goes wrong (inputs, outputs, error conditions) without revealing WHERE in the "
        "code it lives. "
        "Do NOT copy identifier, function, class, method, variable, module, or file/path "
        "names verbatim from the code, and do NOT reuse their distinctive sub-words (e.g. if "
        "the code defines `parse_votable_header`, do not write 'votable' or 'header parser'). "
        "Paraphrase domain terminology into everyday wording or describe the concept by its "
        "effect instead of naming it. The reader should be able to understand the symptom "
        "without knowing any code identifier."
    ),
    QueryType.MODULE_HINT: (
        "Craft a natural-language question describing the issue. You MAY mention module or "
        "package names (e.g., 'the caching module', 'the authentication package') but avoid "
        "specific file paths or symbol names. Focus on the behavioral aspect while giving a "
        "hint about which part of the system is affected."
    ),
    QueryType.FILE_HINT: (
        "Craft a natural-language question describing the issue. You MAY mention specific "
        "file paths (e.g., 'in utils/cache.py') but avoid mentioning specific function or "
        "class names. The question should describe the behavior while pointing to the "
        "relevant file(s)."
    ),
    QueryType.SYMBOL_HINT: (
        "Craft a natural-language question describing the issue. You MAY mention specific "
        "function names, class names, or methods (e.g., 'the CacheManager.get() method'). "
        "The question should clearly identify which symbol(s) are involved while describing "
        "the problematic behavior."
    ),
    QueryType.REASONING: (
        "Craft a natural-language question that requires understanding code relationships. "
        "You MAY mention some symbols but the question should require the reader to reason "
        "about call chains, inheritance, or control flow. For example: 'What calls X that "
        "could cause Y?' or 'Which subclasses of A override B incorrectly?'"
    ),
}


def get_prompt_for_query_type(query_type: QueryType) -> str:
    """Get the query generation prompt for a query type."""
    return QUERY_TYPE_PROMPTS[query_type]


def get_prompt_for_difficulty(level: QueryType) -> str:
    """Backward-compatible alias for get_prompt_for_query_type."""
    return get_prompt_for_query_type(level)
