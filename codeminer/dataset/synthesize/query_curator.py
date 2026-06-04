# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query generation (curation) for the synthesis pipeline."""

from __future__ import annotations

import hashlib
import math
import random
import re
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from codeminer.dataset.utils import QueryType, get_prompt_for_query_type
from codeminer.log_utils import get_logger

from ._agent import AgentRunner
from ._types import (
    BehavioralContext,
    BehavioralSelectionResult,
    QuerySynthesisResult,
    RepoSnapshot,
    TargetDiscoveryResult,
    format_prompt_block,
)

logger = get_logger(__name__)


class QueryCurator:
    """Generate natural-language queries from code context.

    Supports multiple modes via ``query_type``:
      - BEHAVIORAL: pure natural language, no code identifiers
      - MODULE_HINT: may mention module/package names
      - FILE_HINT: may mention file paths
      - SYMBOL_HINT: may mention specific function/class names
      - REASONING: requires reasoning about code relationships
    """

    def __init__(
        self,
        *,
        agent: AgentRunner,
        query_type: QueryType = QueryType.BEHAVIORAL,
        system_prompt: Optional[str] = None,
        max_block_chars_in_prompt: int = 1800,
        behavioral_consensus_runs: int = 3,
    ) -> None:
        self.agent = agent
        self.query_type = query_type
        self.max_block_chars_in_prompt = max_block_chars_in_prompt
        self.behavioral_consensus_runs = max(1, behavioral_consensus_runs)

        base_prompt = (
            "You are a codebase assistant helping create "
            "code search evaluation queries. "
        )
        level_prompt = get_prompt_for_query_type(self.query_type)
        self.system_prompt = system_prompt or (base_prompt + level_prompt)
        # Push system prompt into the agent runner
        self.agent.system_prompt = self.system_prompt

    # ------------------------------------------------------------------
    # Behavioral query generation
    # ------------------------------------------------------------------

    def generate_behavioral(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
        run_index: int = 0,
    ) -> Dict[str, Any]:
        """Generate a single behavioral question (sync)."""
        prompt = self._build_behavioral_prompt(
            instance=instance,
            snapshot=snapshot,
            behavioral_context=behavioral_context,
            run_index=run_index,
        )
        payload = self.agent.run(prompt, cwd=str(snapshot.root))
        return self._parse_behavioral_payload(payload, behavioral_context)

    async def generate_behavioral_async(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
        run_index: int = 0,
    ) -> Dict[str, Any]:
        """Generate a single behavioral question (async)."""
        prompt = self._build_behavioral_prompt(
            instance=instance,
            snapshot=snapshot,
            behavioral_context=behavioral_context,
            run_index=run_index,
        )
        payload = await self.agent.run_async(prompt, cwd=str(snapshot.root))
        return self._parse_behavioral_payload(payload, behavioral_context)

    def generate_with_consensus(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        """Run N behavioral generation passes and aggregate via majority vote (sync).

        Returns consensus result dict.  Verification is done separately by
        the Verifier.
        """
        runs = []
        for idx in range(self.behavioral_consensus_runs):
            runs.append(
                self.generate_behavioral(
                    instance=instance,
                    snapshot=snapshot,
                    behavioral_context=behavioral_context,
                    run_index=idx,
                )
            )
        result = self._aggregate_consensus(runs, behavioral_context)
        result["_runs"] = runs  # pass runs through for the verifier
        return result

    async def generate_with_consensus_async(
        self,
        *,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        """Run N behavioral generation passes and aggregate (async)."""
        runs = []
        for idx in range(self.behavioral_consensus_runs):
            runs.append(
                await self.generate_behavioral_async(
                    instance=instance,
                    snapshot=snapshot,
                    behavioral_context=behavioral_context,
                    run_index=idx,
                )
            )
        result = self._aggregate_consensus(runs, behavioral_context)
        result["_runs"] = runs
        return result

    # ------------------------------------------------------------------
    # Non-behavioral query generation
    # ------------------------------------------------------------------

    def generate_question(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        *,
        ground_truth: Optional[Dict[str, Any]] = None,
        discovered_targets: Optional[TargetDiscoveryResult] = None,
    ) -> Dict[str, Any]:
        """Generate a question at the configured query type (sync)."""
        user_content = self._build_question_prompt(
            instance, snapshot, ground_truth, discovered_targets
        )
        payload = self.agent.run(user_content, cwd=str(snapshot.root))
        return self._parse_question_payload(payload, discovered_targets)

    async def generate_question_async(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        *,
        ground_truth: Optional[Dict[str, Any]] = None,
        discovered_targets: Optional[TargetDiscoveryResult] = None,
    ) -> Dict[str, Any]:
        """Generate a question at the configured query type (async)."""
        user_content = self._build_question_prompt(
            instance, snapshot, ground_truth, discovered_targets
        )
        payload = await self.agent.run_async(user_content, cwd=str(snapshot.root))
        return self._parse_question_payload(payload, discovered_targets)

    # ------------------------------------------------------------------
    # Private: behavioral prompt & parsing
    # ------------------------------------------------------------------

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

        core_block_text = format_prompt_block(
            core, is_core=True, max_block_chars=self.max_block_chars_in_prompt
        )

        context_block_text = ""
        if neighborhood:
            context_block_text = "\n\n".join(
                format_prompt_block(
                    block, max_block_chars=self.max_block_chars_in_prompt
                )
                for block in neighborhood
            )

        parts = [
            "You are generating a behavioral code-search query from sampled code blocks.\n",
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
            f"1) The question MUST describe what the CORE BLOCK"
            f" ({core.block_id}) does behaviorally -- its INTENT and the"
            " user-visible SYMPTOM, in plain English.\n"
            "2) The question MUST NOT mention function names, class names,"
            " method names, variable names, signatures, module names, or file"
            " paths -- and MUST NOT reuse their distinctive sub-words (e.g. do"
            " not write 'votable' just because the code names a `VOTable`"
            " symbol). Paraphrase domain terminology instead of copying it.\n"
            "3) Pick required blocks (IDs) needed to answer the question. "
            f"Always include {core.block_id}.\n"
            "4) In your rationale, explain how your question relates to"
            " the CORE BLOCK's behavior.\n"
            "5) Return strict JSON only with keys:"
            " question, focus, required_block_ids, rationale."
        )

        return "\n\n".join(parts)

    def _parse_behavioral_payload(
        self,
        payload: str,
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        blob = self.agent.extract_json(payload)
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

    def _aggregate_consensus(
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

    # ------------------------------------------------------------------
    # Private: non-behavioral prompt & parsing
    # ------------------------------------------------------------------

    def _build_question_prompt(
        self,
        instance: Dict[str, Any],
        snapshot: RepoSnapshot,
        ground_truth: Optional[Dict[str, Any]],
        discovered_targets: Optional[TargetDiscoveryResult],
    ) -> str:
        problem_statement = (instance.get("problem_statement") or "").strip()
        synthesis_run_id = instance.get("synthesis_run_id")

        constraint_clause = self._build_constraint_clause(
            problem_statement, ground_truth
        )

        context_parts = ["Repo summary:\n" + snapshot.format_summary()]

        if synthesis_run_id is not None:
            context_parts.append(
                "Diversity run id: "
                f"{synthesis_run_id}. Produce a distinct query focus from other runs."
            )

        if ground_truth and self.query_type in (
            QueryType.MODULE_HINT,
            QueryType.FILE_HINT,
            QueryType.SYMBOL_HINT,
            QueryType.REASONING,
        ):
            anchor_block = self._build_anchor_block(ground_truth)
            if anchor_block:
                context_parts.append(anchor_block)
            else:
                gt_info = []
                if ground_truth.get("target_files"):
                    gt_info.append(
                        f"Target files: {', '.join(ground_truth['target_files'])}"
                    )
                if ground_truth.get("symbols_modified"):
                    gt_info.append(
                        "Modified symbols: "
                        + ", ".join(ground_truth["symbols_modified"][:5])
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

        return "\n\n".join(context_parts)

    def _parse_question_payload(
        self,
        payload: str,
        discovered_targets: Optional[TargetDiscoveryResult],
    ) -> Dict[str, Any]:
        payload = self.agent.extract_json(payload)

        try:
            result = QuerySynthesisResult.model_validate_json(payload)
            question = result.question.strip()
            focus = result.focus
            hints = result.hints
        except ValidationError:
            question = self._coerce_question_text(payload)
            focus = None
            hints = None

        if self.query_type == QueryType.BEHAVIORAL:
            question = self._sanitize_question(question)

        if not question:
            question = self._fallback_question(discovered_targets)
        if not question.endswith("?"):
            question = question.rstrip(".") + "?"

        return {"question": question, "focus": focus, "hints": hints}

    # ------------------------------------------------------------------
    # Private: anchor block (non-behavioral grounding)
    # ------------------------------------------------------------------

    def _build_anchor_block(self, ground_truth: Dict[str, Any]) -> Optional[str]:
        # Returns None when ground_truth has no anchor_content — callers fall
        # back to the weak "Ground truth info" hint in that case.
        content = ground_truth.get("anchor_content")
        if not content:
            return None

        anchor_file = ground_truth.get("anchor_file") or (
            (ground_truth.get("target_files") or [""])[0]
        )
        anchor_symbol = ground_truth.get("anchor_symbol") or (
            (ground_truth.get("symbols_modified") or [""])[0]
        )

        max_chars = self.max_block_chars_in_prompt
        snippet = content if len(content) <= max_chars else content[:max_chars] + "..."

        dotted_module = self._derive_dotted_module(anchor_file)
        module_rule = (
            f"Your question MUST explicitly name the module in dotted form "
            f"`{dotted_module}` (or a clearly-recognizable sub-module of it)."
            if dotted_module
            else "Your question MUST explicitly name the module or package in "
            "dotted form (e.g. `astropy.stats`, `numpy.linalg`)."
        )

        type_rules = {
            QueryType.MODULE_HINT: (
                f"{module_rule} It MUST NOT mention the file path or any "
                "specific function/class names."
            ),
            QueryType.FILE_HINT: (
                "Your question SHOULD mention the file path shown above, but MUST "
                "NOT mention specific function or class names."
            ),
            QueryType.SYMBOL_HINT: (
                "Your question SHOULD mention the specific function/class/method "
                "name shown above and describe its behavior."
            ),
            QueryType.REASONING: (
                "Your question SHOULD mention the symbol shown above and frame "
                "the problem in terms of call chains, inheritance, or control "
                "flow that involve it."
            ),
        }
        rule = type_rules.get(self.query_type, "Describe the behavior of this code.")

        header_lines = ["=== ANCHOR CODE (your query MUST describe this) ==="]
        if anchor_file:
            header_lines.append(f"File: {anchor_file}")
        if anchor_symbol:
            header_lines.append(f"Symbol: {anchor_symbol}")
        header = "\n".join(header_lines)

        return (
            f"{header}\n\n```\n{snippet}\n```\n\n"
            f"BINDING RULE: {rule} Do not invent unrelated bugs about other "
            "parts of the repository."
        )

    @staticmethod
    def _derive_dotted_module(anchor_file: str) -> str:
        """Convert ``astropy/stats/funcs.py`` -> ``astropy.stats``."""
        if not anchor_file:
            return ""
        parts = anchor_file.replace("\\", "/").split("/")
        if len(parts) < 2:
            return ""
        return ".".join(p for p in parts[:-1] if p)

    # ------------------------------------------------------------------
    # Private: constraint clause
    # ------------------------------------------------------------------

    def _build_constraint_clause(
        self,
        problem_statement: str,
        ground_truth: Optional[Dict[str, Any]] = None,
    ) -> str:
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

    # ------------------------------------------------------------------
    # Private: text helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_avoid_terms(text: str) -> List[str]:
        if not text:
            return []
        file_like = re.findall(
            r"\b[\w/\.-]+\.(?:py|js|ts|rs|go|java|cpp|c|h|hpp)\b", text
        )
        func_like = re.findall(r"\b[A-Za-z_]\w*\(\)", text)
        names = set(file_like + func_like)
        return [name for name in names if len(name) > 2]

    @staticmethod
    def _sanitize_question(text: str) -> str:
        text = re.sub(r"`[^`]+`", "", text)
        text = re.sub(
            r"\b[\w/\.-]+\.(?:py|js|ts|rs|go|java|cpp|c|h|hpp)\b",
            "a module",
            text,
        )
        text = re.sub(r"\b[A-Za-z_]\w*\(\)", "a function", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text

    @staticmethod
    def _coerce_question_text(text: str) -> str:
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

    @staticmethod
    def _fallback_question(
        discovered_targets: Optional[TargetDiscoveryResult],
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
        # Default behavioral fallback
        if first_file:
            return f"What logic in {first_file} is responsible for this behavior?"
        if first_symbol:
            return f"How does {first_symbol} implement this behavior?"
        return (
            "Which code path controls this behavior, and why can an automatic "
            "rewrite change runtime semantics?"
        )
