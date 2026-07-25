# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Shared data classes for the query synthesis pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from codenib.dataset.utils import CodeLocation
from codenib.types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NodeInfo,
)


class QuerySynthesisResult(BaseModel):
    """Structured output for synthesized queries."""

    question: str = Field(
        description="Single natural-language question describing the issue."
    )
    focus: Optional[str] = Field(
        default=None,
        description="Optional short phrase highlighting the behavior focus.",
    )
    hints: Optional[List[str]] = Field(
        default=None,
        description="Optional progressive hints that could help locate the code.",
    )


class TargetDiscoveryResult(BaseModel):
    """Structured output for repository target discovery."""

    target_files: List[str] = Field(default_factory=list)
    target_symbols: List[str] = Field(default_factory=list)
    target_symbol_nodes: List[NodeInfo] = Field(default_factory=list)
    rationale: Optional[str] = Field(default=None)


class BehavioralSelectionResult(BaseModel):
    """Structured output for behavior-first synthesis from sampled code blocks."""

    question: str = Field(description="Natural-language behavioral question.")
    focus: Optional[str] = Field(default=None)
    required_block_ids: List[str] = Field(default_factory=list)
    rationale: Optional[str] = Field(default=None)


@dataclass
class RepoSnapshot:
    root: Path
    top_level: List[str]
    languages: List[Tuple[str, int]]
    agent_summary: Optional[str] = None

    def format_summary(self) -> str:
        parts: List[str] = []
        if self.top_level:
            parts.append("Top-level entries: " + ", ".join(self.top_level))
        if self.languages:
            lang_summary = ", ".join(
                f"{ext} ({count})" for ext, count in self.languages
            )
            parts.append(f"File extensions (top): {lang_summary}")
        if self.agent_summary:
            parts.append(f"Agent exploration summary:\n{self.agent_summary}")
        return "\n\n".join(parts)


@dataclass
class SampledCodeBlock:
    block_id: str
    node_id: int
    node_name: str
    file_path: str
    node_type: str
    start_line: int
    end_line: int
    content: str
    char_count: int
    line_count: int

    def to_node_info(self, *, include_content: bool = True) -> NodeInfo:
        return NodeInfo(
            node_name=self.node_name,
            type=self.node_type,
            file=self.file_path,
            start_line=self.start_line,
            end_line=self.end_line,
            content=self.content if include_content else None,
        )

    def to_code_location(self) -> CodeLocation:
        symbol_type = None
        if self.node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
            symbol_type = "function"
        elif self.node_type == NODE_TYPE_CLASS:
            symbol_type = "class"
        return CodeLocation(
            file_path=self.file_path,
            symbol_type=symbol_type,
            start_line=self.start_line,
            end_line=self.end_line,
        )


@dataclass
class BehavioralContext:
    core_block: SampledCodeBlock
    candidate_blocks: List[SampledCodeBlock]
    neighborhood_blocks: List[SampledCodeBlock]
    # {(file_path, leaf_symbol): (start_line, end_line)} over ALL graph symbol
    # nodes (0-based, graph attrs) — used to resolve real line spans for the
    # hint/reasoning path, whose ground truth carries only file:symbol strings.
    # NOT candidate_blocks: those are randomly downsampled to max_candidate_blocks.
    symbol_spans: Dict[Tuple[str, str], Tuple[int, int]] = field(default_factory=dict)


def format_prompt_block(
    block: SampledCodeBlock,
    *,
    is_core: bool = False,
    max_block_chars: int = 1800,
) -> str:
    """Format a SampledCodeBlock for inclusion in an LLM prompt."""
    content = block.content
    if len(content) > max_block_chars:
        content = (
            content[:max_block_chars] + "\n# ... truncated for synthesis context ..."
        )
    core_tag = " **CORE**" if is_core else ""
    return (
        f"[{block.block_id}]{core_tag} type={block.node_type} "
        f"lines={block.start_line}-{block.end_line} "
        f"chars={block.char_count}\n"
        f"{content}"
    )
