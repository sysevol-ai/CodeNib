# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Query synthesis facade for SWE-bench instances.

Orchestrates the three-stage pipeline:
  1. **ContextLoader** — checkout repo, build code graph, sample blocks
  2. **QueryCurator** — generate natural-language queries at various modes
  3. **Verifier** — verify query quality and target alignment

Query Types:
- BEHAVIORAL: Pure natural language, no code identifiers
- MODULE_HINT: May mention module/package names
- FILE_HINT: May mention file paths
- SYMBOL_HINT: May mention specific function/class names
- REASONING: Requires reasoning about code relationships
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Union

from codeminer.dataset.utils import CodeLocation, GroundTruth, QueryType
from codeminer.log_utils import get_logger
from codeminer.types import NodeInfo

from ._agent import AgentRunner
from ._types import RepoSnapshot, SampledCodeBlock, TargetDiscoveryResult
from .context_loader import ContextLoader
from .query_curator import QueryCurator
from .verifier import Verifier

logger = get_logger(__name__)


class ClaudeQuerySynthesizer:
    """Synthesize natural-language queries using a Claude-backed LLM.

    Thin facade that wires together :class:`ContextLoader`,
    :class:`QueryCurator`, and :class:`Verifier`.
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
        # Parse query type (difficulty_level is a deprecated alias).
        if difficulty_level is not None:
            query_type = difficulty_level
        if isinstance(query_type, str):
            self.query_type = QueryType(query_type)
        else:
            self.query_type = query_type

        self.num_queries = max(1, num_queries)

        # --- Agent runners with per-stage tool configs ---
        context_agent = AgentRunner(
            model=model,
            max_turns=max_turns,
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode=permission_mode,
        )
        curator_agent = AgentRunner(
            model=model,
            max_turns=max_turns,
            allowed_tools=allowed_tools or [],
            permission_mode=permission_mode,
        )
        verifier_agent = AgentRunner(
            model=model,
            max_turns=max_turns,
            allowed_tools=[],
            permission_mode=permission_mode,
        )

        # --- Sub-modules ---
        self._loader = ContextLoader(
            agent=context_agent,
            max_top_level=max_top_level,
            max_extensions=max_extensions,
            min_block_chars=min_block_chars,
            max_candidate_blocks=max_candidate_blocks,
            max_neighbor_blocks=max_neighbor_blocks,
            neighbor_k_hop=neighbor_k_hop,
            sampling_seed=sampling_seed,
        )
        self._curator = QueryCurator(
            agent=curator_agent,
            query_type=self.query_type,
            system_prompt=system_prompt,
            max_block_chars_in_prompt=max_block_chars_in_prompt,
            behavioral_consensus_runs=behavioral_consensus_runs,
        )
        self._verifier = Verifier(
            agent=verifier_agent,
            verification_mode=verification_mode,
            max_block_chars_in_prompt=max_block_chars_in_prompt,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def synthesize_query(
        self,
        instance: Dict[str, Any],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
        ground_truth: Optional[Dict[str, Any]] = None,
        query_index: int = 0,
    ) -> Dict[str, Any]:
        repo_path, snapshot = self._loader.checkout_and_snapshot(
            instance, repo_root=repo_root, cache_dir=cache_dir
        )
        behavioral_context = self._loader.prepare_behavioral_context(
            instance, repo_path, cache_dir=cache_dir, query_index=query_index
        )

        if self.query_type == QueryType.BEHAVIORAL and behavioral_context is not None:
            result = self._curator.generate_with_consensus(
                instance=instance,
                snapshot=snapshot,
                behavioral_context=behavioral_context,
            )
            runs = result.pop("_runs", [])
            result = self._verifier.verify(
                result, runs, behavioral_context, cwd=str(snapshot.root)
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
            discovered = self._loader.discover_targets(instance, snapshot)
            result = self._curator.generate_question(
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

        return self._build_output(
            instance=instance,
            result=result,
            gt=gt,
            discovered=discovered,
            target_symbol_nodes=target_symbol_nodes,
            snapshot=snapshot,
            query_index=query_index,
        )

    async def synthesize_query_async(
        self,
        instance: Dict[str, Any],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
        ground_truth: Optional[Dict[str, Any]] = None,
        query_index: int = 0,
    ) -> Dict[str, Any]:
        repo_path, snapshot = self._loader.checkout_and_snapshot(
            instance, repo_root=repo_root, cache_dir=cache_dir
        )
        behavioral_context = self._loader.prepare_behavioral_context(
            instance, repo_path, cache_dir=cache_dir, query_index=query_index
        )

        if self.query_type == QueryType.BEHAVIORAL and behavioral_context is not None:
            result = await self._curator.generate_with_consensus_async(
                instance=instance,
                snapshot=snapshot,
                behavioral_context=behavioral_context,
            )
            runs = result.pop("_runs", [])
            result = await self._verifier.verify_async(
                result, runs, behavioral_context, cwd=str(snapshot.root)
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
            discovered = await self._loader.discover_targets_async(instance, snapshot)
            result = await self._curator.generate_question_async(
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

        return self._build_output(
            instance=instance,
            result=result,
            gt=gt,
            discovered=discovered,
            target_symbol_nodes=target_symbol_nodes,
            snapshot=snapshot,
            query_index=query_index,
        )

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

    # ------------------------------------------------------------------
    # Output assembly
    # ------------------------------------------------------------------

    def _build_output(
        self,
        *,
        instance: Dict[str, Any],
        result: Dict[str, Any],
        gt: Optional[GroundTruth],
        discovered: TargetDiscoveryResult,
        target_symbol_nodes: List[NodeInfo],
        snapshot: RepoSnapshot,
        query_index: int,
    ) -> Dict[str, Any]:
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

    # ------------------------------------------------------------------
    # Ground truth helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_discovery_from_blocks(
        blocks: List[SampledCodeBlock],
    ) -> TargetDiscoveryResult:
        files = list(dict.fromkeys(block.file_path for block in blocks))
        symbols = list(dict.fromkeys(block.node_name for block in blocks))
        return TargetDiscoveryResult(
            target_files=files,
            target_symbols=symbols,
            target_symbol_nodes=[
                block.to_node_info(include_content=True) for block in blocks
            ],
            rationale="graph-sampled behavioral blocks",
        )

    @staticmethod
    def _build_ground_truth_from_blocks(
        blocks: List[SampledCodeBlock],
    ) -> Optional[GroundTruth]:
        if not blocks:
            return None

        primary_locations: List[CodeLocation] = []
        for block in blocks:
            loc = block.to_code_location()
            loc.symbol = ClaudeQuerySynthesizer._extract_symbol_name(
                block.node_name, block.file_path
            )
            primary_locations.append(loc)
        return GroundTruth(primary_locations=primary_locations)

    @staticmethod
    def _extract_symbol_name(node_name: str, file_path: str) -> Optional[str]:
        if ":" in node_name:
            prefix, symbol = node_name.split(":", 1)
            if prefix.endswith(file_path) or prefix == file_path:
                return symbol.rstrip("()") or None
            return symbol.rstrip("()") or None
        return node_name.rstrip("()") or None

    @staticmethod
    def _build_symbol_nodes_from_blocks(
        blocks: List[SampledCodeBlock],
    ) -> List[NodeInfo]:
        return [block.to_node_info(include_content=True) for block in blocks]

    @staticmethod
    def _build_symbol_nodes_from_ground_truth(
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

    @staticmethod
    def _dump_node_info(node: NodeInfo) -> Dict[str, Any]:
        return node.model_dump(exclude_none=True)

    @staticmethod
    def _build_ground_truth(
        instance: Dict[str, Any],
        ground_truth: Optional[Dict[str, Any]] = None,
        discovered_targets: Optional[TargetDiscoveryResult] = None,
    ) -> Optional[GroundTruth]:
        primary_locations: List[CodeLocation] = []

        if ground_truth:
            target_files = ground_truth.get("target_files", [])
            symbols_modified = ground_truth.get("symbols_modified", [])
            symbols_added = ground_truth.get("symbols_added", [])

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

        if not primary_locations and instance.get("patch"):
            from codeminer.dataset.gt_locate import GTLocator

            locator = GTLocator()
            target_files = locator.get_target_files(instance["patch"])
            for file_path in target_files:
                primary_locations.append(CodeLocation(file_path=file_path))

        if not primary_locations:
            return None

        return GroundTruth(primary_locations=primary_locations)
