# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Context loading for query synthesis — graph sampling + agent exploration."""

from __future__ import annotations

import hashlib
import math
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from codeminer.dataset.swebench import SwebenchDataset
from codeminer.dataset.swebench_multilingual import SwebenchMultilingualDataset
from codeminer.graph.roi_subgraph import ROISubgraph
from codeminer.log_utils import get_logger
from codeminer.ls_router import LSIndexer
from codeminer.types import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FUNCTION,
    NODE_TYPE_METHOD,
    is_symbol_node,
)
from codeminer.utils import is_test_file

from ._agent import AgentRunner
from ._types import (
    BehavioralContext,
    RepoSnapshot,
    SampledCodeBlock,
    TargetDiscoveryResult,
)

logger = get_logger(__name__)


class ContextLoader:
    """Load and prepare code context for query synthesis.

    Responsibilities:
      - Check out the target repo at a specific commit.
      - Snapshot the repo structure (top-level entries, file extensions).
      - Build a SCIP code graph via ``LSIndexer``.
      - Sample candidate code blocks from the graph.
      - Select a core block and its k-hop neighborhood.
      - Optionally explore the repo with the agent for richer context.
      - Discover target files/symbols for non-behavioral queries.
    """

    def __init__(
        self,
        *,
        agent: AgentRunner,
        max_top_level: int = 40,
        max_extensions: int = 8,
        min_block_chars: int = 100,
        max_candidate_blocks: int = 24,
        max_neighbor_blocks: int = 8,
        neighbor_k_hop: int = 1,
        sampling_seed: Optional[int] = None,
    ) -> None:
        self.agent = agent
        self.max_top_level = max_top_level
        self.max_extensions = max_extensions
        self.min_block_chars = min_block_chars
        self.max_candidate_blocks = max_candidate_blocks
        self.max_neighbor_blocks = max_neighbor_blocks
        self.neighbor_k_hop = neighbor_k_hop
        self.sampling_seed = sampling_seed

    # ------------------------------------------------------------------
    # Checkout & snapshot
    # ------------------------------------------------------------------

    def checkout_instance(
        self,
        instance: Dict[str, Any],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ) -> str:
        """Clone/fetch the repo and checkout ``base_commit``.  Returns repo path."""
        dataset_cls = (
            SwebenchMultilingualDataset
            if instance.get("language_group")
            else SwebenchDataset
        )
        dataset = dataset_cls(root=cache_dir, repo_root=repo_root, log=False)
        dataset.process_instance(instance, repo_root=repo_root)
        return dataset.get_repo_path(instance, repo_root=repo_root)

    def snapshot_repo(self, repo_path: str) -> RepoSnapshot:
        """Collect lightweight repo metadata (top-level entries, extensions)."""
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

    def checkout_and_snapshot(
        self,
        instance: Dict[str, Any],
        *,
        repo_root: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ) -> Tuple[str, RepoSnapshot]:
        """Convenience: checkout + snapshot in one call."""
        repo_path = self.checkout_instance(
            instance, repo_root=repo_root, cache_dir=cache_dir
        )
        snapshot = self.snapshot_repo(repo_path)
        return repo_path, snapshot

    # ------------------------------------------------------------------
    # Agent-based repo exploration (new)
    # ------------------------------------------------------------------

    async def explore_repo_with_agent_async(
        self, snapshot: RepoSnapshot
    ) -> RepoSnapshot:
        """Use the agent (with file tools) to explore the repo and produce
        a richer context summary.  Mutates and returns *snapshot*."""
        prompt = (
            "You are exploring a code repository to build context for generating "
            "code-search evaluation queries.\n\n"
            f"Repo structure:\n{snapshot.format_summary()}\n\n"
            "Please:\n"
            "1. Read the README (if present) and summarize the project purpose.\n"
            "2. Identify the main source directories and their roles.\n"
            "3. List the key modules/packages and briefly describe each.\n"
            "4. Note any important architectural patterns.\n\n"
            "Return a concise summary (under 800 chars)."
        )
        try:
            summary = await self.agent.run_async(prompt, cwd=str(snapshot.root))
            snapshot.agent_summary = summary.strip()[:800] if summary else None
        except Exception as exc:
            logger.warning("Agent repo exploration failed: %s", exc)
        return snapshot

    def explore_repo_with_agent(self, snapshot: RepoSnapshot) -> RepoSnapshot:
        """Synchronous wrapper for :meth:`explore_repo_with_agent_async`."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.explore_repo_with_agent_async(snapshot))
        raise RuntimeError(
            "Running inside an active event loop. Use explore_repo_with_agent_async."
        )

    # ------------------------------------------------------------------
    # Graph-based behavioral context
    # ------------------------------------------------------------------

    def prepare_behavioral_context(
        self,
        instance: Dict[str, Any],
        repo_path: str,
        *,
        cache_dir: Optional[str] = None,
        query_index: int = 0,
    ) -> Optional[BehavioralContext]:
        """Build a ``BehavioralContext`` by indexing the repo, sampling blocks,
        and selecting a core block with its neighborhood."""
        graph = self.load_code_graph(
            instance=instance,
            repo_path=repo_path,
            cache_dir=cache_dir,
        )
        if graph is None:
            return None

        candidates = self.sample_candidate_blocks(graph, instance)
        if not candidates:
            return None

        core_block = self.pick_core_block(candidates, graph, query_index=query_index)
        neighborhood = self.collect_neighborhood_blocks(
            graph=graph,
            core_block=core_block,
            candidate_blocks=candidates,
        )
        return BehavioralContext(
            core_block=core_block,
            candidate_blocks=candidates,
            neighborhood_blocks=neighborhood,
        )

    def load_code_graph(
        self,
        instance: Dict[str, Any],
        repo_path: str,
        *,
        cache_dir: Optional[str] = None,
    ):
        """Build SCIP code graph via ``LSIndexer``.  Returns ``CodeGraph`` or ``None``."""
        language = self.infer_language(instance)
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

    @staticmethod
    def infer_language(instance: Dict[str, Any]) -> str:
        """Infer the primary language from instance metadata."""
        language_group = (instance.get("language_group") or "").lower()
        if "rust" in language_group:
            return "rust"
        if "javascript" in language_group or "typescript" in language_group:
            return "ts"
        if "go" in language_group:
            return "go"
        if "c++" in language_group or language_group == "c":
            return "cpp"
        return "python"

    def sample_candidate_blocks(
        self,
        code_graph,
        instance: Dict[str, Any],
    ) -> List[SampledCodeBlock]:
        """Sample symbol nodes from the graph as candidate code blocks."""
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

    # Core-block selection heuristics
    _MAX_CORE_LINES = 400
    _MIN_CORE_LINES = 8

    @staticmethod
    def _is_repetitive_block(content: str, line_count: int) -> bool:
        """Detect blocks that are mostly repetitive mappings or enum tables.

        If > 70% of non-empty lines share the same leading token the block is
        likely a lookup table, match arm list, or enum variant listing.
        """
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if len(lines) < 15:
            return False
        leading_tokens: Dict[str, int] = {}
        for ln in lines:
            token = re.split(r"[\s({\[,.:=>#|]", ln, maxsplit=1)[0]
            if token:
                leading_tokens[token] = leading_tokens.get(token, 0) + 1
        if not leading_tokens:
            return False
        most_common_count = max(leading_tokens.values())
        return most_common_count / len(lines) > 0.70

    @classmethod
    def _score_core_block(cls, blk: SampledCodeBlock, graph_degree: int) -> float:
        """Score a candidate block for core-block selection.

        Balances size, connectivity, and behavioral richness while filtering
        out blocks that are too large, too small, or repetitive.
        """
        if blk.line_count > cls._MAX_CORE_LINES:
            return -1.0
        if blk.line_count < cls._MIN_CORE_LINES:
            return -1.0
        if cls._is_repetitive_block(blk.content, blk.line_count):
            return -1.0

        type_bonus = 1.0
        if blk.node_type in (NODE_TYPE_FUNCTION, NODE_TYPE_METHOD):
            type_bonus = 1.3
        elif blk.node_type == NODE_TYPE_CLASS:
            type_bonus = 0.8

        size_score = math.log(blk.char_count + 1)
        degree_score = min(graph_degree, 15) * 3.0

        return (size_score + degree_score) * type_bonus

    @classmethod
    def pick_core_block(
        cls, blocks: List[SampledCodeBlock], code_graph, *, query_index: int = 0
    ) -> SampledCodeBlock:
        """Select the best core block for behavioral query synthesis.

        Filters out non-behavioral blocks (config tables, huge enums, tiny
        stubs) and scores the rest by a balance of size, graph connectivity,
        and node type.  Falls back to size-based ranking when all blocks are
        filtered out.
        """
        graph = code_graph.get_graph()

        scored = [
            (blk, cls._score_core_block(blk, graph.degree(blk.node_id)))
            for blk in blocks
        ]
        valid = [(blk, s) for blk, s in scored if s >= 0]

        if not valid:
            logger.warning(
                "All %d candidate blocks filtered out; "
                "falling back to size-based selection.",
                len(blocks),
            )
            valid = sorted(
                [(blk, float(blk.char_count)) for blk in blocks],
                key=lambda t: t[1],
                reverse=True,
            )
        else:
            valid.sort(key=lambda t: t[1], reverse=True)

        idx = min(query_index, len(valid) - 1)
        return valid[idx][0]

    def collect_neighborhood_blocks(
        self,
        *,
        graph,
        core_block: SampledCodeBlock,
        candidate_blocks: List[SampledCodeBlock],
    ) -> List[SampledCodeBlock]:
        """Collect k-hop neighbors of *core_block* from the code graph."""
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

    # ------------------------------------------------------------------
    # Non-behavioral target discovery
    # ------------------------------------------------------------------

    def discover_targets(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
    ) -> TargetDiscoveryResult:
        """Synchronous target discovery."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.discover_targets_async(instance, snapshot))
        raise RuntimeError(
            "Running inside an active event loop. Use discover_targets_async."
        )

    async def discover_targets_async(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
    ) -> TargetDiscoveryResult:
        """Ask the agent to explore the repo and identify target files/symbols."""
        context_parts = ["Repo summary:\n" + snapshot.format_summary()]
        context_parts.append(
            "Explore the repository and identify likely implementation targets.\n"
            "Return strict JSON with keys: target_files (array of relative file paths), "
            "target_symbols (array of symbol identifiers in repository-native format), "
            "rationale (string or null). Keep arrays concise (<= 8)."
        )

        raw_payload = await self.agent.run_async(
            "\n\n".join(context_parts),
            cwd=str(snapshot.root),
        )
        payload = self.agent.extract_json(raw_payload)
        try:
            return TargetDiscoveryResult.model_validate_json(payload)
        except ValidationError:
            heuristic = self._parse_discovery_from_text(raw_payload)
            if heuristic.target_files or heuristic.target_symbols:
                return heuristic
            logger.warning("Target discovery returned non-JSON payload; continuing.")
            return TargetDiscoveryResult()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

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

    @staticmethod
    def _is_hidden(path: Path) -> bool:
        return any(part.startswith(".") for part in path.parts)

    @staticmethod
    def _parse_discovery_from_text(text: str) -> TargetDiscoveryResult:
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
