"""
Query synthesis agent for SWE-bench instances.

This agent checks out the target repo commit, summarizes the repo, and uses the
Claude code agent to produce natural-language queries at different query types.

Query Types:
- BEHAVIORAL: Pure natural language, no code identifiers
- MODULE_HINT: May mention module/package names
- FILE_HINT: May mention file paths
- SYMBOL_HINT: May mention specific function/class names
- REASONING: Requires reasoning about code relationships
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from claude_agent_sdk import ClaudeAgentOptions, query
from pydantic import BaseModel, Field, ValidationError

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.dataset.swebench_multilingual import SwebenchMultilingualDataset
from codeminer.dataset.utils import (
    CodeLocation,
    GroundTruth,
    QueryType,
    get_prompt_for_query_type,
)
from codeminer.graph.roi_subgraph import ROISubgraph
from codeminer.log_utils import get_logger
from codeminer.ls_router import LSIndexer
from codeminer.types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    NodeInfo,
    is_symbol_node,
)
from codeminer.utils import is_test_file

logger = get_logger(__name__)


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

    def format_summary(self) -> str:
        parts: List[str] = []
        if self.top_level:
            parts.append("Top-level entries: " + ", ".join(self.top_level))
        if self.languages:
            lang_summary = ", ".join(
                f"{ext} ({count})" for ext, count in self.languages
            )
            parts.append(f"File extensions (top): {lang_summary}")
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


class ClaudeQuerySynthesizer:
    """
    Synthesize natural-language queries using a Claude-backed LLM.

    Supports multiple query types for generating queries with varying
    amounts of code context revealed.
    """

    def __init__(
        self,
        *,
        model: str = "opus",
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        permission_mode: str = "bypassPermissions",
        system_prompt: Optional[str] = None,
        query_type: Union[QueryType, str] = QueryType.BEHAVIORAL,
        difficulty_level: Optional[Union[QueryType, str]] = None,
        max_readme_chars: int = 1500,
        max_metadata_chars: int = 800,
        max_top_level: int = 40,
        max_extensions: int = 8,
        min_block_chars: int = 100,
        max_candidate_blocks: int = 24,
        max_neighbor_blocks: int = 8,
        neighbor_k_hop: int = 1,
        max_block_chars_in_prompt: int = 1800,
        sampling_seed: Optional[int] = None,
        behavioral_consensus_runs: int = 3,
        num_queries: int = 1,
        verification_mode: str = "lenient",
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools or []
        self.permission_mode = permission_mode
        self.max_readme_chars = max_readme_chars
        self.max_metadata_chars = max_metadata_chars
        self.max_top_level = max_top_level
        self.max_extensions = max_extensions
        self.min_block_chars = min_block_chars
        self.max_candidate_blocks = max_candidate_blocks
        self.max_neighbor_blocks = max_neighbor_blocks
        self.neighbor_k_hop = neighbor_k_hop
        self.max_block_chars_in_prompt = max_block_chars_in_prompt
        self.sampling_seed = sampling_seed
        self.behavioral_consensus_runs = max(1, behavioral_consensus_runs)
        self.num_queries = max(1, num_queries)
        if verification_mode not in ("strict", "lenient", "none"):
            raise ValueError(
                f"verification_mode must be strict, lenient, or none; got {verification_mode!r}"
            )
        self.verification_mode = verification_mode

        # Parse query type (difficulty_level is a deprecated alias).
        if difficulty_level is not None:
            query_type = difficulty_level
        if isinstance(query_type, str):
            self.query_type = QueryType(query_type)
        else:
            self.query_type = query_type

        # Build system prompt based on query type
        base_prompt = (
            "You are a codebase assistant helping create "
            "code search evaluation queries. "
        )
        level_prompt = get_prompt_for_query_type(self.query_type)
        self.system_prompt = system_prompt or (base_prompt + level_prompt)

    def synthesize_query(
        self,
        instance: Dict[str, Any],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
        ground_truth: Optional[Dict[str, Any]] = None,
        query_index: int = 0,
    ) -> Dict[str, Any]:
        """
        Synthesize a query for a single instance.

        Args:
            instance: SWE-bench instance dictionary
            repo_root: Root directory for repository storage
            cache_dir: Cache directory for datasets
            ground_truth: Optional pre-computed ground truth from gt_locate.py

        Returns:
            Dictionary with query and metadata in CodeSearchQuery format
        """
        repo_path = self._checkout_instance(
            instance, repo_root=repo_root, cache_dir=cache_dir
        )
        snapshot = self._snapshot_repo(repo_path)
        behavioral_context = self._prepare_behavioral_context(
            instance, repo_path, cache_dir=cache_dir, query_index=query_index
        )
        if self.query_type == QueryType.BEHAVIORAL and behavioral_context is not None:
            result = self._generate_behavioral_question_with_consensus(
                instance=instance,
                snapshot=snapshot,
                behavioral_context=behavioral_context,
            )
            gt = self._build_ground_truth_from_blocks(
                result.get("selected_blocks") or [behavioral_context.core_block]
            )
            discovered = self._build_discovery_from_blocks(
                result.get("selected_blocks") or [behavioral_context.core_block]
            )
            target_symbol_nodes = self._build_symbol_nodes_from_blocks(
                result.get("selected_blocks") or [behavioral_context.core_block]
            )
        else:
            discovered = self._discover_targets(instance, snapshot)
            result = self._generate_question(
                instance,
                snapshot,
                ground_truth=ground_truth,
                discovered_targets=discovered,
            )
            gt = self._build_ground_truth(
                instance,
                ground_truth,
                discovered_targets=discovered,
            )
            target_symbol_nodes = self._build_symbol_nodes_from_ground_truth(gt)

        instance_id = instance.get("instance_id", "unknown")
        query_id = f"{instance_id}_{self.query_type.value}_q{query_index + 1}"

        return {
            "query_id": query_id,
            "instance_id": instance_id,
            "repo": instance.get("repo"),
            "base_commit": instance.get("base_commit"),
            "query": result["question"],
            "query_type": self.query_type.value,
            "difficulty": self.query_type.value,
            "ground_truth": gt.to_dict() if gt else None,
            "target_files": gt.to_dict()["target_files"] if gt else [],
            "target_symbols": gt.to_dict()["target_symbols"] if gt else [],
            "target_symbol_nodes": [
                self._dump_node_info(node) for node in target_symbol_nodes
            ],
            "focus": result.get("focus"),
            "hints": result.get("hints"),
            "repo_snapshot": snapshot.format_summary(),
            "discovered_target_files": discovered.target_files,
            "discovered_target_symbols": discovered.target_symbols,
            "discovered_target_symbol_nodes": [
                self._dump_node_info(node) for node in discovered.target_symbol_nodes
            ],
            "discovery_rationale": discovered.rationale,
            "core_block_id": result.get("core_block_id"),
            "selected_block_ids": result.get("selected_block_ids"),
            "verification_passed": result.get("verification_passed"),
            "verification_block_id": result.get("verification_block_id"),
        }

    async def synthesize_query_async(
        self,
        instance: Dict[str, Any],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
        ground_truth: Optional[Dict[str, Any]] = None,
        query_index: int = 0,
    ) -> Dict[str, Any]:
        """
        Synthesize a query for a single instance (async version).

        Args:
            instance: SWE-bench instance dictionary
            repo_root: Root directory for repository storage
            cache_dir: Cache directory for datasets
            ground_truth: Optional pre-computed ground truth from gt_locate.py

        Returns:
            Dictionary with query and metadata in CodeSearchQuery format
        """
        repo_path = self._checkout_instance(
            instance, repo_root=repo_root, cache_dir=cache_dir
        )
        snapshot = self._snapshot_repo(repo_path)
        behavioral_context = self._prepare_behavioral_context(
            instance, repo_path, cache_dir=cache_dir, query_index=query_index
        )
        if self.query_type == QueryType.BEHAVIORAL and behavioral_context is not None:
            result = await self._generate_behavioral_question_with_consensus_async(
                instance=instance,
                snapshot=snapshot,
                behavioral_context=behavioral_context,
            )
            gt = self._build_ground_truth_from_blocks(
                result.get("selected_blocks") or [behavioral_context.core_block]
            )
            discovered = self._build_discovery_from_blocks(
                result.get("selected_blocks") or [behavioral_context.core_block]
            )
            target_symbol_nodes = self._build_symbol_nodes_from_blocks(
                result.get("selected_blocks") or [behavioral_context.core_block]
            )
        else:
            discovered = await self._discover_targets_async(instance, snapshot)
            result = await self._generate_question_async(
                instance,
                snapshot,
                ground_truth=ground_truth,
                discovered_targets=discovered,
            )
            gt = self._build_ground_truth(
                instance,
                ground_truth,
                discovered_targets=discovered,
            )
            target_symbol_nodes = self._build_symbol_nodes_from_ground_truth(gt)

        instance_id = instance.get("instance_id", "unknown")
        query_id = f"{instance_id}_{self.query_type.value}_q{query_index + 1}"

        return {
            "query_id": query_id,
            "instance_id": instance_id,
            "repo": instance.get("repo"),
            "base_commit": instance.get("base_commit"),
            "query": result["question"],
            "query_type": self.query_type.value,
            "difficulty": self.query_type.value,
            "ground_truth": gt.to_dict() if gt else None,
            "target_files": gt.to_dict()["target_files"] if gt else [],
            "target_symbols": gt.to_dict()["target_symbols"] if gt else [],
            "target_symbol_nodes": [
                self._dump_node_info(node) for node in target_symbol_nodes
            ],
            "focus": result.get("focus"),
            "hints": result.get("hints"),
            "repo_snapshot": snapshot.format_summary(),
            "discovered_target_files": discovered.target_files,
            "discovered_target_symbols": discovered.target_symbols,
            "discovered_target_symbol_nodes": [
                self._dump_node_info(node) for node in discovered.target_symbol_nodes
            ],
            "discovery_rationale": discovered.rationale,
            "core_block_id": result.get("core_block_id"),
            "selected_block_ids": result.get("selected_block_ids"),
            "verification_passed": result.get("verification_passed"),
            "verification_block_id": result.get("verification_block_id"),
        }

    def synthesize_queries(
        self,
        instances: Iterable[Dict[str, Any]],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results = []
        for instance in instances:
            for qi in range(self.num_queries):
                try:
                    results.append(
                        self.synthesize_query(
                            instance,
                            repo_root=repo_root,
                            cache_dir=cache_dir,
                            query_index=qi,
                        )
                    )
                except Exception as exc:
                    instance_id = instance.get("instance_id", "unknown")
                    logger.error(
                        "Failed to synthesize query for %s (q%d): %s",
                        instance_id,
                        qi + 1,
                        exc,
                        exc_info=True,
                    )
                    results.append(
                        {
                            "instance_id": instance_id,
                            "repo": instance.get("repo"),
                            "base_commit": instance.get("base_commit"),
                            "error": str(exc),
                        }
                    )
        return results

    async def synthesize_queries_async(
        self,
        instances: Iterable[Dict[str, Any]],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for instance in instances:
            for qi in range(self.num_queries):
                try:
                    results.append(
                        await self.synthesize_query_async(
                            instance,
                            repo_root=repo_root,
                            cache_dir=cache_dir,
                            query_index=qi,
                        )
                    )
                except Exception as exc:
                    instance_id = instance.get("instance_id", "unknown")
                    logger.error(
                        "Failed to synthesize query for %s (q%d): %s",
                        instance_id,
                        qi + 1,
                        exc,
                        exc_info=True,
                    )
                    results.append(
                        {
                            "instance_id": instance_id,
                            "repo": instance.get("repo"),
                            "base_commit": instance.get("base_commit"),
                            "error": str(exc),
                        }
                    )
        return results

    def _checkout_instance(
        self,
        instance: Dict[str, Any],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ) -> str:
        dataset_cls = (
            SwebenchMultilingualDataset
            if instance.get("language_group")
            else SwebenchDataset
        )
        dataset = dataset_cls(root=cache_dir, repo_root=repo_root, log=False)
        dataset.process_instance(instance, repo_root=repo_root)
        return dataset.get_repo_path(instance, repo_root=repo_root)

    def _snapshot_repo(self, repo_path: str) -> RepoSnapshot:
        root = Path(repo_path)
        top_level = sorted(
            [entry.name for entry in root.iterdir() if not entry.name.startswith(".")]
        )[: self.max_top_level]

        extensions = self._collect_extensions(root)
        return RepoSnapshot(
            root=root,
            top_level=top_level,
            languages=extensions,
        )

    def _collect_extensions(self, root: Path) -> List[Tuple[str, int]]:
        counts: Dict[str, int] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if self._is_hidden(path):
                continue
            ext = path.suffix.lower() or "no_ext"
            counts[ext] = counts.get(ext, 0) + 1
        sorted_counts = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        return sorted_counts[: self.max_extensions]

    def _is_hidden(self, path: Path) -> bool:
        return any(part.startswith(".") for part in path.parts)

    def _prepare_behavioral_context(
        self,
        instance: Dict[str, Any],
        repo_path: str,
        *,
        cache_dir: Optional[str] = None,
        query_index: int = 0,
    ) -> Optional[BehavioralContext]:
        graph = self._load_or_build_code_graph(
            instance=instance,
            repo_path=repo_path,
            cache_dir=cache_dir,
        )
        if graph is None:
            return None

        candidates = self._sample_candidate_blocks(graph, instance)
        if not candidates:
            return None

        core_block = self._pick_core_block(candidates, graph, query_index=query_index)
        neighborhood = self._collect_neighborhood_blocks(
            graph=graph,
            core_block=core_block,
            candidate_blocks=candidates,
        )
        return BehavioralContext(
            core_block=core_block,
            candidate_blocks=candidates,
            neighborhood_blocks=neighborhood,
        )

    def _load_or_build_code_graph(
        self,
        instance: Dict[str, Any],
        repo_path: str,
        *,
        cache_dir: Optional[str] = None,
    ):
        language = self._infer_instance_language(instance)
        repo_name = (instance.get("repo") or "repo").replace("/", "__")
        graph_cache_root = (
            Path(cache_dir).expanduser() / "scip_graph_cache"
            if cache_dir
            else Path("/tmp") / "scip_graph_cache"
        )
        output_dir = graph_cache_root / repo_name
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            indexer = LSIndexer(
                project_root=repo_path,
                output_dir=output_dir,
                language=language,
            )
            return indexer.run_pipeline(skip_level="graph", report_profile=False)
        except Exception as exc:
            logger.warning(
                "Failed to build code graph for %s (%s): %s",
                instance.get("instance_id", "unknown"),
                language,
                exc,
            )
            return None

    def _infer_instance_language(self, instance: Dict[str, Any]) -> str:
        language_group = (instance.get("language_group") or "").lower()
        if "rust" in language_group:
            return "rust"
        if "javascript" in language_group or "typescript" in language_group:
            return "ts"
        if "c++" in language_group or language_group == "c":
            return "cpp"
        return "python"

    def _sample_candidate_blocks(
        self,
        code_graph,
        instance: Dict[str, Any],
    ) -> List[SampledCodeBlock]:
        graph = code_graph.get_graph()
        blocks: List[SampledCodeBlock] = []

        for node in graph.vs:
            attrs = node.attributes()
            node_type = attrs.get("type", "")
            if not is_symbol_node(node_type):
                continue

            file_path = attrs.get("file")
            if not file_path or is_test_file(file_path):
                continue
            if self._is_declaration_file(file_path):
                continue

            start_line = attrs.get("start_line")
            end_line = attrs.get("end_line")
            if (
                not isinstance(start_line, int)
                or not isinstance(end_line, int)
                or end_line <= start_line
            ):
                continue

            content = code_graph.get_node_content(node.index)
            if not content:
                continue
            content = content.strip()
            if len(content) < self.min_block_chars:
                continue

            line_count = content.count("\n") + 1
            block = SampledCodeBlock(
                block_id=f"blk_{len(blocks) + 1:03d}",
                node_id=node.index,
                node_name=node["name"],
                file_path=file_path,
                node_type=node_type,
                start_line=start_line,
                end_line=end_line,
                content=content,
                char_count=len(content),
                line_count=line_count,
            )
            blocks.append(block)

        if not blocks:
            return []

        run_id = instance.get("synthesis_run_id")
        if self.sampling_seed is None:
            rng = random.Random()
        else:
            seed_input = (
                f"{self.sampling_seed}:"
                f"{instance.get('instance_id') or instance.get('repo') or 'seed'}:"
                f"{run_id or 0}"
            )
            seed = int(hashlib.md5(seed_input.encode("utf-8")).hexdigest()[:8], 16)
            rng = random.Random(seed)
        if len(blocks) > self.max_candidate_blocks:
            blocks = rng.sample(blocks, self.max_candidate_blocks)

        return sorted(blocks, key=lambda blk: blk.char_count, reverse=True)

    # ---- Core-block selection heuristics ----
    # Blocks above this line count are likely config tables, enum mappings,
    # or generated dispatch code — not the kind of behavioral logic we want.
    _MAX_CORE_LINES = 400
    # Blocks below this line count are too small to produce rich questions.
    _MIN_CORE_LINES = 8
    # If the ratio of unique meaningful statements to total lines is below
    # this threshold, the block is likely a repetitive mapping/enum table.
    _MIN_STATEMENT_DIVERSITY = 0.12

    @staticmethod
    def _is_repetitive_block(content: str, line_count: int) -> bool:
        """Detect blocks that are mostly repetitive mappings or enum tables.

        Heuristic: count lines that start with a repeated pattern token.
        If > 70% of lines share the same leading token the block is likely a
        lookup table, match arm list, or enum variant listing — none of which
        make good behavioral query targets.
        """
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if len(lines) < 15:
            return False
        # Extract first "word" (token) from each line
        leading_tokens: Dict[str, int] = {}
        for ln in lines:
            token = re.split(r"[\s({\[,.:=>#|]", ln, maxsplit=1)[0]
            if token:
                leading_tokens[token] = leading_tokens.get(token, 0) + 1
        if not leading_tokens:
            return False
        most_common_count = max(leading_tokens.values())
        return most_common_count / len(lines) > 0.70

    def _score_core_block(self, blk: SampledCodeBlock, graph_degree: int) -> float:
        """Score a candidate block for core-block selection.

        Balances size, connectivity, and behavioral richness while penalizing
        blocks that are too large (config tables) or too repetitive (enum
        mappings).
        """
        # Hard filters — return negative score to exclude
        if blk.line_count > self._MAX_CORE_LINES:
            return -1.0
        if blk.line_count < self._MIN_CORE_LINES:
            return -1.0
        if self._is_repetitive_block(blk.content, blk.line_count):
            return -1.0

        # Prefer functions/methods over classes — they tend to have more
        # focused, describable behavior.
        type_bonus = 1.0
        if blk.node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
            type_bonus = 1.3
        elif blk.node_type == NODE_TYPE_CLASS:
            type_bonus = 0.8

        # Size component: use log to dampen the advantage of very large blocks.
        # A 200-line function should not score 10x higher than a 20-line one.
        size_score = math.log(blk.char_count + 1)

        # Connectivity component (graph degree): useful but capped to prevent
        # hub nodes (like giant dispatchers) from dominating.
        degree_score = min(graph_degree, 15) * 3.0

        return (size_score + degree_score) * type_bonus

    def _pick_core_block(
        self, blocks: List[SampledCodeBlock], code_graph, *, query_index: int = 0
    ) -> SampledCodeBlock:
        """Select the best core block for behavioral query synthesis.

        Filters out non-behavioral blocks (config tables, huge enums, tiny
        stubs) and scores the rest by a balance of size, graph connectivity,
        and node type.  Falls back to the original size-based ranking when
        all blocks are filtered out.
        """
        graph = code_graph.get_graph()

        scored = [
            (blk, self._score_core_block(blk, graph.degree(blk.node_id)))
            for blk in blocks
        ]
        # Keep only blocks that passed the hard filters (score >= 0)
        valid = [(blk, s) for blk, s in scored if s >= 0]

        if not valid:
            # Fallback: relax filters, just sort by size (original behavior)
            logger.warning(
                "All %d candidate blocks filtered out; falling back to size-based selection.",
                len(blocks),
            )
            valid = sorted(
                [(blk, float(blk.char_count)) for blk in blocks],
                key=lambda t: t[1],
                reverse=True,
            )

        valid.sort(key=lambda t: t[1], reverse=True)
        idx = min(query_index, len(valid) - 1)
        return valid[idx][0]

    def _collect_neighborhood_blocks(
        self,
        *,
        graph,
        core_block: SampledCodeBlock,
        candidate_blocks: List[SampledCodeBlock],
    ) -> List[SampledCodeBlock]:
        try:
            roi = ROISubgraph(graph)
            subgraph = roi.extract_subgraph(
                [core_block.node_name],
                k_hop=self.neighbor_k_hop,
                direction="both",
            )
            nodes = roi.get_filtered_subgraph_nodes(
                subgraph,
                exclude_nodes=[core_block.node_name],
                filter_tests=True,
                node_types=[NODE_TYPE_FUNCTION, NODE_TYPE_METHOD, NODE_TYPE_CLASS],
            )
        except Exception as exc:
            logger.warning(
                "Failed to collect neighborhood for core block %s: %s",
                core_block.node_name,
                exc,
            )
            nodes = []

        by_name = {blk.node_name: blk for blk in candidate_blocks}
        neighborhood: List[SampledCodeBlock] = []
        for node in nodes:
            matched = by_name.get(node.node_name)
            if matched is None:
                continue
            if matched.block_id == core_block.block_id:
                continue
            neighborhood.append(matched)
            if len(neighborhood) >= self.max_neighbor_blocks:
                break

        if not neighborhood:
            for blk in candidate_blocks:
                if blk.block_id == core_block.block_id:
                    continue
                neighborhood.append(blk)
                if len(neighborhood) >= self.max_neighbor_blocks:
                    break

        return neighborhood

    def _generate_behavioral_question(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
        run_index: int = 0,
    ) -> Dict[str, Any]:
        prompt = self._build_behavioral_prompt(
            instance=instance,
            snapshot=snapshot,
            behavioral_context=behavioral_context,
            run_index=run_index,
        )
        payload = self._run_agent(prompt, cwd=str(snapshot.root))
        return self._parse_behavioral_payload(payload, behavioral_context)

    async def _generate_behavioral_question_async(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
        run_index: int = 0,
    ) -> Dict[str, Any]:
        prompt = self._build_behavioral_prompt(
            instance=instance,
            snapshot=snapshot,
            behavioral_context=behavioral_context,
            run_index=run_index,
        )
        payload = await self._run_agent_async(prompt, cwd=str(snapshot.root))
        return self._parse_behavioral_payload(payload, behavioral_context)

    def _generate_behavioral_question_with_consensus(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        runs = []
        for idx in range(self.behavioral_consensus_runs):
            runs.append(
                self._generate_behavioral_question(
                    instance=instance,
                    snapshot=snapshot,
                    behavioral_context=behavioral_context,
                    run_index=idx,
                )
            )
        result = self._aggregate_behavioral_consensus(runs, behavioral_context)
        return self._apply_verification(
            result, runs, behavioral_context, cwd=str(snapshot.root)
        )

    async def _generate_behavioral_question_with_consensus_async(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        runs = []
        for idx in range(self.behavioral_consensus_runs):
            runs.append(
                await self._generate_behavioral_question_async(
                    instance=instance,
                    snapshot=snapshot,
                    behavioral_context=behavioral_context,
                    run_index=idx,
                )
            )
        result = self._aggregate_behavioral_consensus(runs, behavioral_context)
        return await self._apply_verification_async(
            result, runs, behavioral_context, cwd=str(snapshot.root)
        )

    def _aggregate_behavioral_consensus(
        self,
        runs: List[Dict[str, Any]],
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        if not runs:
            return {
                "question": self._fallback_question(None),
                "focus": None,
                "hints": None,
                "selected_blocks": [behavioral_context.core_block],
                "core_block_id": behavioral_context.core_block.block_id,
                "selected_block_ids": [behavioral_context.core_block.block_id],
            }

        vote_counts: Dict[str, int] = {}
        for run in runs:
            for block in run.get("selected_blocks", []):
                vote_counts[block.block_id] = vote_counts.get(block.block_id, 0) + 1

        core_id = behavioral_context.core_block.block_id
        majority = math.ceil(len(runs) / 2)

        pool = {
            blk.block_id: blk
            for blk in [behavioral_context.core_block]
            + behavioral_context.neighborhood_blocks
        }
        selected_blocks = [
            pool[block_id]
            for block_id, votes in sorted(
                vote_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
            if votes >= majority and block_id in pool
        ]
        if not selected_blocks:
            selected_blocks = [behavioral_context.core_block]
        if core_id not in {blk.block_id for blk in selected_blocks}:
            selected_blocks.insert(0, behavioral_context.core_block)

        best_run = max(
            runs,
            key=lambda run: sum(
                vote_counts.get(blk.block_id, 0)
                for blk in run.get("selected_blocks", [])
            ),
        )
        return {
            "question": best_run.get("question") or self._fallback_question(None),
            "focus": best_run.get("focus"),
            "hints": None,
            "selected_blocks": selected_blocks,
            "core_block_id": behavioral_context.core_block.block_id,
            "selected_block_ids": [blk.block_id for blk in selected_blocks],
        }

    # Cheap pre-filter: catch synthesis artifacts that never need an LLM call.
    _SYNTHESIS_ARTIFACT_RE = re.compile(r"blk_\d+")
    _MIN_QUERY_LENGTH = 60

    @classmethod
    def _is_trivially_invalid(cls, question: str) -> Tuple[bool, str]:
        """Fast pre-filter for obviously broken queries (no LLM needed).

        Returns ``(is_invalid, reason)``.
        """
        if not question or not question.strip():
            return True, "empty question"
        q = question.strip()
        if len(q) < cls._MIN_QUERY_LENGTH:
            return True, f"too short ({len(q)} chars, need >= {cls._MIN_QUERY_LENGTH})"
        if cls._SYNTHESIS_ARTIFACT_RE.search(q):
            return True, "contains synthesis block ID (blk_NNN)"
        return False, ""

    def _apply_verification(
        self,
        result: Dict[str, Any],
        runs: List[Dict[str, Any]],
        behavioral_context: BehavioralContext,
        *,
        cwd: str,
    ) -> Dict[str, Any]:
        """Run quality + alignment verification (sync)."""
        # ---- Cheap pre-filter for obvious garbage ----
        bad, reason = self._is_trivially_invalid(result["question"])
        if bad:
            logger.warning(
                "Trivially invalid query (%s): %r", reason, result["question"]
            )
            # Try to salvage from alternate runs
            for run in runs:
                alt_q = run.get("question", "")
                if not alt_q or alt_q == result["question"]:
                    continue
                alt_bad, _ = self._is_trivially_invalid(alt_q)
                if not alt_bad:
                    result["question"] = alt_q
                    result["focus"] = run.get("focus")
                    bad = False
                    break
            if bad:
                result["verification_passed"] = False
                result["verification_block_id"] = None
                return result

        if self.verification_mode == "none":
            result["verification_passed"] = None
            result["verification_block_id"] = None
            return result

        # ---- LLM verification: quality + alignment in one call ----
        vr = self._verify_question_alignment(
            result["question"], behavioral_context, cwd=cwd
        )
        if vr["passed"]:
            result["verification_passed"] = True
            result["verification_block_id"] = vr["block_id"]
            return result

        # Verification failed — try alternate runs in strict mode
        if self.verification_mode == "strict":
            for alt_run in runs:
                alt_q = alt_run.get("question", "")
                if not alt_q or alt_q == result["question"]:
                    continue
                alt_bad, _ = self._is_trivially_invalid(alt_q)
                if alt_bad:
                    continue
                alt_vr = self._verify_question_alignment(
                    alt_q, behavioral_context, cwd=cwd
                )
                if alt_vr["passed"]:
                    result["question"] = alt_q
                    result["focus"] = alt_run.get("focus")
                    result["verification_passed"] = True
                    result["verification_block_id"] = alt_vr["block_id"]
                    return result

        # All attempts failed or lenient mode — keep best question, mark failed
        result["verification_passed"] = False
        result["verification_block_id"] = vr["block_id"]
        return result

    async def _apply_verification_async(
        self,
        result: Dict[str, Any],
        runs: List[Dict[str, Any]],
        behavioral_context: BehavioralContext,
        *,
        cwd: str,
    ) -> Dict[str, Any]:
        """Run quality + alignment verification (async)."""
        # ---- Cheap pre-filter for obvious garbage ----
        bad, reason = self._is_trivially_invalid(result["question"])
        if bad:
            logger.warning(
                "Trivially invalid query (%s): %r", reason, result["question"]
            )
            for run in runs:
                alt_q = run.get("question", "")
                if not alt_q or alt_q == result["question"]:
                    continue
                alt_bad, _ = self._is_trivially_invalid(alt_q)
                if not alt_bad:
                    result["question"] = alt_q
                    result["focus"] = run.get("focus")
                    bad = False
                    break
            if bad:
                result["verification_passed"] = False
                result["verification_block_id"] = None
                return result

        if self.verification_mode == "none":
            result["verification_passed"] = None
            result["verification_block_id"] = None
            return result

        # ---- LLM verification: quality + alignment in one call ----
        vr = await self._verify_question_alignment_async(
            result["question"], behavioral_context, cwd=cwd
        )
        if vr["passed"]:
            result["verification_passed"] = True
            result["verification_block_id"] = vr["block_id"]
            return result

        # Verification failed — try alternate runs in strict mode
        if self.verification_mode == "strict":
            for alt_run in runs:
                alt_q = alt_run.get("question", "")
                if not alt_q or alt_q == result["question"]:
                    continue
                alt_bad, _ = self._is_trivially_invalid(alt_q)
                if alt_bad:
                    continue
                alt_vr = await self._verify_question_alignment_async(
                    alt_q, behavioral_context, cwd=cwd
                )
                if alt_vr["passed"]:
                    result["question"] = alt_q
                    result["focus"] = alt_run.get("focus")
                    result["verification_passed"] = True
                    result["verification_block_id"] = alt_vr["block_id"]
                    return result

        # All attempts failed or lenient mode — keep best question, mark failed
        result["verification_passed"] = False
        result["verification_block_id"] = vr["block_id"]
        return result

    def _build_verification_prompt(
        self,
        question: str,
        behavioral_context: BehavioralContext,
    ) -> str:
        """Build a blind verification prompt that checks both quality and alignment."""
        all_blocks = [behavioral_context.core_block] + list(
            behavioral_context.neighborhood_blocks
        )
        block_text = "\n\n".join(
            self._format_prompt_block(block, is_core=False) for block in all_blocks
        )
        return (
            "You are evaluating a code-search question for quality and correctness.\n\n"
            f"Question: {question!r}\n\n"
            f"Code blocks:\n{block_text}\n\n"
            "Evaluate the question on TWO criteria:\n\n"
            "1) QUALITY — Is this a valid behavioral code-search query?\n"
            "   A valid query describes specific, observable behavior of code "
            "(e.g., how inputs are processed, what conditions trigger certain logic, "
            "what transformations are applied).\n"
            "   REJECT if the query is:\n"
            "   - Chain-of-thought or internal reasoning (e.g., 'Let me examine...', "
            "'I need to understand...')\n"
            "   - Too vague or generic to locate any specific code\n"
            "   - About the synthesis process itself\n"
            "   - Contains code identifiers (function names, class names, variable names, "
            "file paths, or backtick-wrapped code spans like `foo()` or `bar.py`)\n"
            "   - References bugs, issues, or test failures instead of describing code behavior\n"  # noqa: B950
            "   - Not actually a question about code behavior\n\n"
            "2) ALIGNMENT — Which code block does the question primarily describe?\n"
            "   Identify the single block whose behavior the question is asking about.\n\n"
            "Return strict JSON with keys:\n"
            "  valid_query (bool): true if the question passes the QUALITY check\n"
            "  reject_reason (string): reason for rejection if valid_query is false, else empty\n"  # noqa: B950
            "  block_id (string): the block ID the question describes (even if invalid)\n"
            "  confidence (high|medium|low): how confident you are in the block_id match"
        )

    def _verify_question_alignment(
        self,
        question: str,
        behavioral_context: BehavioralContext,
        *,
        cwd: str,
    ) -> Dict[str, Any]:
        """Verify that a generated question aligns with the core block (sync)."""
        prompt = self._build_verification_prompt(question, behavioral_context)
        payload = self._run_agent(prompt, cwd=cwd)
        return self._parse_verification_result(payload, behavioral_context)

    async def _verify_question_alignment_async(
        self,
        question: str,
        behavioral_context: BehavioralContext,
        *,
        cwd: str,
    ) -> Dict[str, Any]:
        """Verify that a generated question aligns with the core block (async)."""
        prompt = self._build_verification_prompt(question, behavioral_context)
        payload = await self._run_agent_async(prompt, cwd=cwd)
        return self._parse_verification_result(payload, behavioral_context)

    def _parse_verification_result(
        self,
        payload: str,
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        """Parse verification LLM response — checks both quality and alignment."""
        blob = self._extract_json_blob(payload)
        core_id = behavioral_context.core_block.block_id
        try:
            data = json.loads(blob)
            valid_query = data.get("valid_query", True)
            reject_reason = data.get("reject_reason", "")
            block_id = data.get("block_id", "")
            confidence = data.get("confidence", "low")
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Verification returned non-JSON; assuming failed.")
            return {"block_id": "", "confidence": "low", "passed": False}

        if not valid_query:
            logger.warning(
                "Verification rejected query as low quality: %s", reject_reason
            )
            return {"block_id": block_id, "confidence": confidence, "passed": False}

        aligned = block_id == core_id
        if not aligned:
            logger.warning(
                "Verification mismatch: question maps to %s, expected %s (confidence=%s)",
                block_id,
                core_id,
                confidence,
            )
        else:
            logger.info(
                "Verification passed: question maps to core block %s (confidence=%s)",
                core_id,
                confidence,
            )
        return {"block_id": block_id, "confidence": confidence, "passed": aligned}

    def _build_behavioral_prompt(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
        run_index: int = 0,
    ) -> str:
        core = behavioral_context.core_block
        neighborhood = list(behavioral_context.neighborhood_blocks)
        if neighborhood:
            seed_src = (
                f"{instance.get('instance_id', 'unknown')}:"
                f"{instance.get('synthesis_run_id', 0)}:{run_index}"
            )
            seed = int(hashlib.md5(seed_src.encode("utf-8")).hexdigest()[:8], 16)
            random.Random(seed).shuffle(neighborhood)

        core_block_text = self._format_prompt_block(core, is_core=True)

        context_block_text = ""
        if neighborhood:
            context_block_text = "\n\n".join(
                self._format_prompt_block(block) for block in neighborhood
            )

        parts = [
            "You are generating a behavioral code-search query from sampled code blocks.\n"
            "The query will be used as a code-search benchmark: a developer types this\n"
            "query into a search tool to find the relevant code. Write it like a real\n"
            "developer search query — concise, focused on ONE key behavior or design\n"
            "decision, not an exhaustive summary of the entire function.\n",
            "=== PRIMARY TARGET (CORE BLOCK) ===\n"
            "Your question MUST describe the behavior of this block.\n\n"
            f"{core_block_text}",
        ]

        if context_block_text:
            parts.append(
                "=== CONTEXT BLOCKS (for understanding only) ===\n"
                "These help you understand the codebase but are NOT the primary focus.\n\n"
                f"{context_block_text}"
            )

        parts.append(
            f"Repository summary:\n{snapshot.format_summary()}\n\n"
            f"Source instance id: {instance.get('instance_id', 'unknown')}\n"
            f"Verification pass: {run_index + 1}\n\n"
            "RULES:\n"
            f"1) The question MUST describe a SPECIFIC behavior or design"
            f" decision in the CORE BLOCK ({core.block_id}). Focus on ONE"
            " interesting aspect — e.g., a particular edge case, a key"
            " branching condition, an important invariant, or how a specific"
            " category of input is handled. Do NOT summarize the entire"
            " function; pick the most distinctive behavioral aspect.\n"
            "2) The question MUST NOT mention any code identifiers:"
            " no function names, class names, variable names, method names,"
            " file paths, or module names. Describe behavior in plain English"
            " using generic terms (e.g., 'the entry point', 'the main"
            " orchestration routine', 'the validation logic').\n"
            "3) The question MUST focus ONLY on what the CORE BLOCK's code"
            " does. Do NOT reference bugs, issues, test failures, or any"
            " external problem context. Describe the code's normal behavior.\n"
            "4) Keep the question to 1 sentence.\n"
            "5) Pick required blocks (IDs) needed to answer the question. "
            f"Always include {core.block_id}.\n"
            "6) In your rationale, explain how your question relates to"
            " the CORE BLOCK's behavior.\n"
            "7) Return strict JSON only with keys:"
            " question, focus, required_block_ids, rationale."
        )

        return "\n\n".join(parts)

    def _format_prompt_block(
        self, block: SampledCodeBlock, *, is_core: bool = False
    ) -> str:
        content = block.content
        if len(content) > self.max_block_chars_in_prompt:
            content = (
                content[: self.max_block_chars_in_prompt]
                + "\n# ... truncated for synthesis context ..."
            )
        core_tag = " **CORE**" if is_core else ""
        return (
            f"[{block.block_id}]{core_tag} type={block.node_type} "
            f"lines={block.start_line}-{block.end_line} "
            f"chars={block.char_count}\n"
            f"{content}"
        )

    def _parse_behavioral_payload(
        self,
        payload: str,
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        blob = self._extract_json_blob(payload)
        try:
            parsed = BehavioralSelectionResult.model_validate_json(blob)
            question = parsed.question.strip()
            focus = parsed.focus
            selected_ids = parsed.required_block_ids
        except ValidationError:
            question = self._coerce_question_text(blob)
            focus = None
            selected_ids = []

        question = self._sanitize_question(question)
        if not question:
            question = self._fallback_question(None)
        if not question.endswith("?"):
            question = question.rstrip(".") + "?"

        pool = {
            blk.block_id: blk
            for blk in [behavioral_context.core_block]
            + behavioral_context.neighborhood_blocks
        }
        selected_blocks = [
            pool[block_id] for block_id in selected_ids if block_id in pool
        ]
        if behavioral_context.core_block.block_id not in {
            blk.block_id for blk in selected_blocks
        }:
            selected_blocks.insert(0, behavioral_context.core_block)

        return {
            "question": question,
            "focus": focus,
            "hints": None,
            "selected_blocks": selected_blocks,
            "core_block_id": behavioral_context.core_block.block_id,
            "selected_block_ids": [blk.block_id for blk in selected_blocks],
        }

    def _build_discovery_from_blocks(
        self,
        blocks: List[SampledCodeBlock],
    ) -> TargetDiscoveryResult:
        files = list(dict.fromkeys(block.file_path for block in blocks))
        symbols = list(dict.fromkeys(block.node_name for block in blocks))
        return TargetDiscoveryResult(
            target_files=files,
            target_symbols=symbols,
            target_symbol_nodes=self._build_symbol_nodes_from_blocks(blocks),
            rationale="graph-sampled behavioral blocks",
        )

    def _build_ground_truth_from_blocks(
        self,
        blocks: List[SampledCodeBlock],
    ) -> Optional[GroundTruth]:
        if not blocks:
            return None

        primary_locations: List[CodeLocation] = []
        for block in blocks:
            loc = block.to_code_location()
            loc.symbol = self._extract_symbol_name(block.node_name, block.file_path)
            primary_locations.append(loc)
        return GroundTruth(primary_locations=primary_locations)

    def _extract_symbol_name(self, node_name: str, file_path: str) -> Optional[str]:
        if ":" in node_name:
            prefix, symbol = node_name.split(":", 1)
            if prefix.endswith(file_path) or prefix == file_path:
                return symbol.rstrip("()") or None
            return symbol.rstrip("()") or None
        return node_name.rstrip("()") or None

    def _build_symbol_nodes_from_blocks(
        self,
        blocks: List[SampledCodeBlock],
    ) -> List[NodeInfo]:
        return [block.to_node_info(include_content=True) for block in blocks]

    def _build_symbol_nodes_from_ground_truth(
        self,
        gt: Optional[GroundTruth],
    ) -> List[NodeInfo]:
        if gt is None:
            return []
        nodes: List[NodeInfo] = []
        for loc in gt.primary_locations:
            nodes.append(
                NodeInfo(
                    node_name=loc.node_id,
                    type=loc.symbol_type or "",
                    file=loc.file_path,
                    start_line=loc.start_line,
                    end_line=loc.end_line,
                )
            )
        return nodes

    def _dump_node_info(self, node: NodeInfo) -> Dict[str, Any]:
        return node.model_dump(exclude_none=True)

    def _build_ground_truth(
        self,
        instance: Dict[str, Any],
        ground_truth: Optional[Dict[str, Any]] = None,
        discovered_targets: Optional[TargetDiscoveryResult] = None,
    ) -> Optional[GroundTruth]:
        """
        Build a GroundTruth object from instance data and/or pre-computed ground truth.

        Args:
            instance: SWE-bench instance dictionary
            ground_truth: Optional pre-computed ground truth from gt_locate.py

        Returns:
            GroundTruth object or None if no ground truth available
        """
        primary_locations: List[CodeLocation] = []

        if ground_truth:
            # Use pre-computed ground truth from gt_locate.py
            target_files = ground_truth.get("target_files", [])
            symbols_modified = ground_truth.get("symbols_modified", [])
            symbols_added = ground_truth.get("symbols_added", [])

            # Add modified symbols as primary locations
            for symbol_id in symbols_modified + symbols_added:
                if ":" in symbol_id:
                    file_path, symbol = symbol_id.split(":", 1)
                    symbol_type = "function" if symbol.endswith("()") else "class"
                    symbol_name = symbol.rstrip("()")
                    primary_locations.append(
                        CodeLocation(
                            file_path=file_path,
                            symbol=symbol_name,
                            symbol_type=symbol_type,
                        )
                    )

            # If no symbols, add file-level locations
            if not primary_locations:
                for file_path in target_files:
                    primary_locations.append(CodeLocation(file_path=file_path))

        if not primary_locations and discovered_targets is not None:
            for file_path in discovered_targets.target_files:
                if file_path:
                    primary_locations.append(CodeLocation(file_path=file_path))
            for symbol_id in discovered_targets.target_symbols:
                if ":" in symbol_id:
                    file_path, symbol = symbol_id.split(":", 1)
                    symbol_name = symbol.rstrip("()")
                    symbol_type = "function" if symbol.endswith("()") else None
                    primary_locations.append(
                        CodeLocation(
                            file_path=file_path,
                            symbol=symbol_name or None,
                            symbol_type=symbol_type,
                        )
                    )
                elif discovered_targets.target_files:
                    primary_locations.append(
                        CodeLocation(
                            file_path=discovered_targets.target_files[0],
                            symbol=symbol_id.rstrip("()") or None,
                        )
                    )

        # Fallback: extract from patch if no ground truth provided
        if not primary_locations and instance.get("patch"):
            from codeminer.dataset.gt_locate import GTLocator

            locator = GTLocator()
            target_files = locator.get_target_files(instance["patch"])
            for file_path in target_files:
                primary_locations.append(CodeLocation(file_path=file_path))

        if not primary_locations:
            return None

        return GroundTruth(primary_locations=primary_locations)

    def _build_constraint_clause(
        self,
        problem_statement: str,
        ground_truth: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Build constraint clause based on query type.

        For harder levels (BEHAVIORAL, MODULE_HINT), we restrict what can be mentioned.
        For easier levels (FILE_HINT, SYMBOL_HINT), we allow and even encourage specifics.
        For REASONING level, we require mentioning some symbols but asking about relationships.
        """
        level = self.query_type

        if level == QueryType.BEHAVIORAL:
            avoid_terms = self._extract_avoid_terms(problem_statement)
            if avoid_terms:
                return (
                    "STRICT: You MUST NOT mention any of these identifiers: "
                    + ", ".join(sorted(avoid_terms))
                    + ". Focus purely on observable behavior."
                )
            return (
                "STRICT: Avoid ALL code identifiers, file paths, and technical names."
            )

        elif level == QueryType.MODULE_HINT:
            return (
                "You MAY mention module or package names (e.g., 'the caching module') "
                "but AVOID specific file paths or function/class names."
            )

        elif level == QueryType.FILE_HINT:
            if ground_truth:
                files = ground_truth.get("target_files", [])
                if files:
                    return (
                        f"You SHOULD mention relevant file paths from: {', '.join(files)}. "
                        "But AVOID mentioning specific function or class names."
                    )
            return "You MAY mention specific file paths but AVOID function/class names."

        elif level == QueryType.SYMBOL_HINT:
            if ground_truth:
                symbols = ground_truth.get("symbols_modified", []) + ground_truth.get(
                    "symbols_added", []
                )
                if symbols:
                    return (
                        f"You SHOULD mention relevant symbols from: {', '.join(symbols[:5])}. "
                        "Be specific about which function/class/method is involved."
                    )
            return "You MAY and SHOULD mention specific function/class/method names."

        elif level == QueryType.REASONING:
            if ground_truth:
                symbols = ground_truth.get("symbols_modified", []) + ground_truth.get(
                    "symbols_added", []
                )
                if symbols:
                    return (
                        f"Mention some symbols from: {', '.join(symbols[:3])}. "
                        "But frame the question to require understanding of call chains, "
                        "inheritance, or control flow. Ask 'what calls X', 'which classes "
                        "inherit from Y', or 'how does A interact with B'."
                    )
            return (
                "Frame the question to require reasoning about code relationships "
                "(call chains, inheritance, data flow)."
            )

        return "Focus on the behavior being described."

    def _generate_question(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        ground_truth: Optional[Dict[str, Any]] = None,
        discovered_targets: Optional[TargetDiscoveryResult] = None,
    ) -> Dict[str, Any]:
        """
        Generate a question at the configured query type.

        Returns a dict with 'question', 'focus', and optionally 'hints'.
        """
        problem_statement = (instance.get("problem_statement") or "").strip()
        synthesis_run_id = instance.get("synthesis_run_id")

        constraint_clause = self._build_constraint_clause(
            problem_statement, ground_truth
        )

        # Build context based on query type
        context_parts = ["Repo summary:\n" + snapshot.format_summary()]

        if synthesis_run_id is not None:
            context_parts.append(
                "Diversity run id: "
                f"{synthesis_run_id}. Produce a distinct query focus from other runs."
            )

        # Add ground truth info for levels that need it
        if ground_truth and self.query_type in (
            QueryType.FILE_HINT,
            QueryType.SYMBOL_HINT,
            QueryType.REASONING,
        ):
            gt_info = []
            if ground_truth.get("target_files"):
                gt_info.append(
                    f"Target files: {', '.join(ground_truth['target_files'])}"
                )
            if ground_truth.get("symbols_modified"):
                gt_info.append(
                    f"Modified symbols: {', '.join(ground_truth['symbols_modified'][:5])}"
                )
            if gt_info:
                context_parts.append(
                    "Ground truth info (use as appropriate):\n" + "\n".join(gt_info)
                )
        elif discovered_targets and self.query_type in (
            QueryType.FILE_HINT,
            QueryType.SYMBOL_HINT,
            QueryType.REASONING,
        ):
            discovered_info = []
            if discovered_targets.target_files:
                discovered_info.append(
                    "Discovered files: "
                    + ", ".join(discovered_targets.target_files[:8])
                )
            if discovered_targets.target_symbols:
                discovered_info.append(
                    "Discovered symbols: "
                    + ", ".join(discovered_targets.target_symbols[:8])
                )
            if discovered_info:
                context_parts.append(
                    "Repository exploration targets:\n" + "\n".join(discovered_info)
                )

        context_parts.append(f"Constraints:\n{constraint_clause}")
        context_parts.append(
            "Output JSON with keys: question (string), focus (string or null), "
            "hints (array of strings or null). Return only JSON."
        )

        user_content = "\n\n".join(context_parts)

        payload = self._run_agent(user_content, cwd=str(snapshot.root))
        payload = self._extract_json_blob(payload)

        try:
            result = QuerySynthesisResult.model_validate_json(payload)
            question = result.question.strip()
            focus = result.focus
            hints = result.hints
        except ValidationError:
            question = self._coerce_question_text(payload)
            focus = None
            hints = None

        # Only sanitize for BEHAVIORAL level
        if self.query_type == QueryType.BEHAVIORAL:
            question = self._sanitize_question(question)

        if not question:
            question = self._fallback_question(discovered_targets)
        if not question.endswith("?"):
            question = question.rstrip(".") + "?"

        return {"question": question, "focus": focus, "hints": hints}

    async def _generate_question_async(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        ground_truth: Optional[Dict[str, Any]] = None,
        discovered_targets: Optional[TargetDiscoveryResult] = None,
    ) -> Dict[str, Any]:
        """
        Generate a question at the configured query type (async version).

        Returns a dict with 'question', 'focus', and optionally 'hints'.
        """
        problem_statement = (instance.get("problem_statement") or "").strip()
        synthesis_run_id = instance.get("synthesis_run_id")

        constraint_clause = self._build_constraint_clause(
            problem_statement, ground_truth
        )

        # Build context based on query type
        context_parts = ["Repo summary:\n" + snapshot.format_summary()]

        if synthesis_run_id is not None:
            context_parts.append(
                "Diversity run id: "
                f"{synthesis_run_id}. Produce a distinct query focus from other runs."
            )

        # Add ground truth info for levels that need it
        if ground_truth and self.query_type in (
            QueryType.FILE_HINT,
            QueryType.SYMBOL_HINT,
            QueryType.REASONING,
        ):
            gt_info = []
            if ground_truth.get("target_files"):
                gt_info.append(
                    f"Target files: {', '.join(ground_truth['target_files'])}"
                )
            if ground_truth.get("symbols_modified"):
                gt_info.append(
                    f"Modified symbols: {', '.join(ground_truth['symbols_modified'][:5])}"
                )
            if gt_info:
                context_parts.append(
                    "Ground truth info (use as appropriate):\n" + "\n".join(gt_info)
                )
        elif discovered_targets and self.query_type in (
            QueryType.FILE_HINT,
            QueryType.SYMBOL_HINT,
            QueryType.REASONING,
        ):
            discovered_info = []
            if discovered_targets.target_files:
                discovered_info.append(
                    "Discovered files: "
                    + ", ".join(discovered_targets.target_files[:8])
                )
            if discovered_targets.target_symbols:
                discovered_info.append(
                    "Discovered symbols: "
                    + ", ".join(discovered_targets.target_symbols[:8])
                )
            if discovered_info:
                context_parts.append(
                    "Repository exploration targets:\n" + "\n".join(discovered_info)
                )

        context_parts.append(f"Constraints:\n{constraint_clause}")
        context_parts.append(
            "Output JSON with keys: question (string), focus (string or null), "
            "hints (array of strings or null). Return only JSON."
        )

        user_content = "\n\n".join(context_parts)

        payload = await self._run_agent_async(user_content, cwd=str(snapshot.root))
        payload = self._extract_json_blob(payload)

        try:
            result = QuerySynthesisResult.model_validate_json(payload)
            question = result.question.strip()
            focus = result.focus
            hints = result.hints
        except ValidationError:
            question = self._coerce_question_text(payload)
            focus = None
            hints = None

        # Only sanitize for BEHAVIORAL level
        if self.query_type == QueryType.BEHAVIORAL:
            question = self._sanitize_question(question)

        if not question:
            question = self._fallback_question(discovered_targets)
        if not question.endswith("?"):
            question = question.rstrip(".") + "?"

        return {"question": question, "focus": focus, "hints": hints}

    def _discover_targets(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
    ) -> TargetDiscoveryResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._discover_targets_async(instance, snapshot))
        raise RuntimeError(
            "Running inside an active event loop. Use synthesize_query_async instead."
        )

    async def _discover_targets_async(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
    ) -> TargetDiscoveryResult:
        context_parts = ["Repo summary:\n" + snapshot.format_summary()]
        context_parts.append(
            "Explore the repository and identify likely implementation targets.\n"
            "Return strict JSON with keys: target_files (array of relative file paths), "
            "target_symbols (array of symbol identifiers in repository-native format), "
            "rationale (string or null). Keep arrays concise (<= 8)."
        )

        raw_payload = await self._run_agent_async(
            "\n\n".join(context_parts),
            cwd=str(snapshot.root),
        )
        payload = self._extract_json_blob(raw_payload)
        try:
            return TargetDiscoveryResult.model_validate_json(payload)
        except ValidationError:
            heuristic = self._parse_discovery_from_text(raw_payload)
            if heuristic.target_files or heuristic.target_symbols:
                return heuristic
            logger.warning("Target discovery returned non-JSON payload; continuing.")
            return TargetDiscoveryResult()

    def _run_agent(self, prompt: str, *, cwd: Optional[str] = None) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._run_agent_async(prompt, cwd=cwd))
        raise RuntimeError(
            "Running inside an active event loop. Use synthesize_query_async instead."
        )

    async def _run_agent_async(self, prompt: str, *, cwd: Optional[str] = None) -> str:
        options = ClaudeAgentOptions(
            max_turns=self.max_turns,
            system_prompt=self.system_prompt,
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            model=self.model,
            cwd=cwd,
        )
        text_chunks: List[str] = []
        query_gen = query(prompt=prompt, options=options)

        try:
            async for message in query_gen:
                msg_type = message.__class__.__name__
                logger.debug(f"Agent message type: {msg_type}")

                if msg_type == "ResultMessage":
                    # Get result directly from ResultMessage
                    result = getattr(message, "result", None)
                    if result and isinstance(result, str):
                        return result.strip()
                    # If no result in ResultMessage, return accumulated text
                    return "\n".join(text_chunks).strip()

                if msg_type == "ErrorMessage":
                    error_msg = getattr(message, "error", "Unknown error")
                    logger.error(f"Agent error message: {error_msg}")
                    raise RuntimeError(f"Agent returned error: {error_msg}")

                if msg_type == "AssistantMessage":
                    content = getattr(message, "content", None)
                    if not content:
                        continue
                    block_texts: List[str] = []
                    for block in content:
                        text = getattr(block, "text", None)
                        if isinstance(text, str) and text.strip():
                            block_texts.append(text.strip())
                    if block_texts:
                        text_chunks.append("\n".join(block_texts))
        finally:
            # Ensure generator is properly closed
            await query_gen.aclose()

        return "\n".join(text_chunks).strip()

    def _extract_json_blob(self, text: str) -> str:
        text = text.strip()
        if not text:
            return text

        fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
        if fenced:
            text = fenced.group(1).strip()

        decoder = json.JSONDecoder()
        for idx, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                _obj, end = decoder.raw_decode(text[idx:])
                return text[idx : idx + end]
            except json.JSONDecodeError:
                continue
        return text

    def _extract_avoid_terms(self, text: str) -> List[str]:
        if not text:
            return []
        file_like = re.findall(
            r"\b[\w/\.-]+\.(?:py|js|ts|rs|go|java|cpp|c|h|hpp)\b", text
        )
        func_like = re.findall(r"\b[A-Za-z_]\w*\(\)", text)
        names = set(file_like + func_like)
        return [name for name in names if len(name) > 2]

    def _sanitize_question(self, text: str) -> str:
        """Minimal sanitization for behavioral queries.

        We deliberately do NO regex-based identifier stripping.  The generation
        prompt already instructs the LLM to avoid identifiers, and the LLM
        verification step will reject queries that leak them.

        Any post-hoc removal of identifiers from natural language (whether via
        backtick stripping or bare-name regex) produces garbled text:
          - "functions like `exec` and `open`" → "functions like and"
          - "uses exec() or suggests" → "uses or suggests"

        The only cleanup we do is collapsing whitespace.
        """
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text

    def _coerce_question_text(self, text: str) -> str:
        if not text:
            return ""
        stripped = text.strip()
        if not stripped:
            return ""
        for line in stripped.splitlines():
            candidate = line.strip().strip("-* ").strip()
            if not candidate:
                continue
            candidate = re.sub(r"^(question|query)\s*:\s*", "", candidate, flags=re.I)
            if len(candidate) >= 12:
                if "?" in candidate:
                    return candidate.split("?", 1)[0].strip() + "?"
                return candidate
        return ""

    def _fallback_question(
        self, discovered_targets: Optional[TargetDiscoveryResult]
    ) -> str:
        first_file = (
            discovered_targets.target_files[0]
            if discovered_targets and discovered_targets.target_files
            else None
        )
        first_symbol = (
            discovered_targets.target_symbols[0]
            if discovered_targets and discovered_targets.target_symbols
            else None
        )
        if self.query_type == QueryType.BEHAVIORAL:
            return (
                "Which code path controls this behavior, and why can an automatic "
                "rewrite change runtime semantics?"
            )
        if self.query_type == QueryType.MODULE_HINT:
            module_hint = (
                first_file.split("/", 1)[0]
                if first_file and "/" in first_file
                else "the relevant module"
            )
            return f"In {module_hint}, where is the logic that governs this behavior?"
        if self.query_type == QueryType.FILE_HINT and first_file:
            return f"What logic in {first_file} is responsible for this behavior?"
        if self.query_type == QueryType.SYMBOL_HINT and first_symbol:
            return f"How does {first_symbol} implement this behavior?"
        if self.query_type == QueryType.REASONING:
            if first_symbol:
                return (
                    "What callers or control-flow paths around "
                    f"{first_symbol} determine this behavior?"
                )
            if first_file:
                return f"What interactions in {first_file} determine this behavior?"
        return "Where in the codebase is the logic responsible for this behavior?"

    def _parse_discovery_from_text(self, text: str) -> TargetDiscoveryResult:
        if not text:
            return TargetDiscoveryResult()
        file_pattern = r"\b[\w./-]+\.(?:py|rs|js|ts|go|java|cpp|c|h|hpp)\b"
        files = list(dict.fromkeys(re.findall(file_pattern, text)))
        symbol_patterns = [
            r"\b[\w./-]+\.(?:py|rs|js|ts|go|java|cpp|c|h|hpp):[A-Za-z_][\w.:]*(?:\(\))?\b",
            r"\b[\w/]+#[A-Za-z_]\w*(?:\(\))?\b",
            r"\b[\w/]+/[A-Za-z_]\w*(?:\(\))?\b",
        ]
        symbols: List[str] = []
        for pattern in symbol_patterns:
            symbols.extend(re.findall(pattern, text))
        symbols = list(dict.fromkeys(symbols))
        return TargetDiscoveryResult(
            target_files=files[:8],
            target_symbols=symbols[:8],
            rationale="parsed from non-JSON discovery output",
        )
