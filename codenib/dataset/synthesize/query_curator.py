# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query generation (curation) for the synthesis pipeline."""

from __future__ import annotations

import hashlib
import math
import random
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import ValidationError

from codenib.dataset.utils import QueryType, get_prompt_for_query_type
from codenib.log_utils import get_logger

from ._types import (
    BehavioralContext,
    BehavioralSelectionResult,
    QuerySynthesisResult,
    RepoSnapshot,
    TargetDiscoveryResult,
    format_prompt_block,
)

if TYPE_CHECKING:
    from ._agent import AgentRunner

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
            QueryType.FILE_HINT,
            QueryType.SYMBOL_HINT,
            QueryType.REASONING,
        ):
            gt_info = []
            src_files = [
                f
                for f in (ground_truth.get("target_files") or [])
                if self._is_source_path(str(f))
            ]
            if src_files:
                gt_info.append(f"Target files: {', '.join(src_files)}")
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

        # Ground the curator to the REAL anchor code. Without this the model only
        # sees the repo summary + a one-line constraint and free-associates a
        # plausible-but-wrong topic (module_hint drifted off-target 100% of the
        # time). Showing the actual target code forces the query to be about it.
        anchor_code = (ground_truth or {}).get("anchor_content")
        if anchor_code:
            snippet = str(anchor_code)[: self.max_block_chars_in_prompt]
            context_parts.append(
                "Target code — the query MUST be about the behavior in THIS exact "
                "code (it is the ground-truth answer a searcher has to land on). Do "
                "NOT write a query about some other part of the repo:\n"
                f"```\n{snippet}\n```"
            )

        context_parts.append(f"Constraints:\n{constraint_clause}")
        context_parts.append(
            "Phrasing: write `question` as ONE concise, realistic search query — "
            "what a developer would actually type to find this code (a single "
            "sentence, aim for under 30 words). Do NOT narrate your reasoning (no "
            "'Now I…', 'Let me…', 'Looking at…'), and do NOT pack in step-by-step "
            "analysis, speculative implementation details, or a multi-sentence "
            "problem report — the judge rejects verbose, narrated queries."
        )
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
        elif question:
            # Agent-thinking the model prepends to the query ("Now let me look at
            # X. <query>") leaks through the JSON path too, not just prose
            # fallback — strip it here so it is caught regardless of source.
            question = self._strip_leading_narration(question)

        if not question:
            question = self._fallback_question(discovered_targets)
        # Normalize trailing punctuation to exactly one '?' so salvaged prose
        # ending in ':' never becomes the malformed ':?' the judge flags.
        question = question.rstrip().rstrip("?").rstrip(" .:;,-—")
        if question:
            question = question + "?"

        return {"question": question, "focus": focus, "hints": hints}

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
            modules = self._modules_from_ground_truth(ground_truth)
            if modules:
                # Bind to the ACTUAL target module (like file_hint/symbol_hint do)
                # — the old generic prompt gave the curator no anchor, so it drifted
                # off-target (query about X, target an unrelated module) and leaked
                # file paths. That collapsed module_hint to ~9% judge-valid.
                return (
                    "Name ONLY the module/package, in dotted form, drawn from: "
                    f"{', '.join(modules)}. Describe the behavior implemented THERE so "
                    "the query is bound to that module. You MUST NOT mention any file "
                    "path (no '/', no '.py'/'.go'/'.ts'/'.rs'/'.c'/'.h' suffix) and MUST "
                    "NOT mention any function, class, or method name."
                )
            return (
                "Name the relevant module/package in dotted form (e.g. 'the "
                "pkg.subpkg module'). You MUST NOT mention file paths (no '/', no file "
                "extension) or any function/class/method name."
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

    # Non-source targets that poison MODULE_HINT/anchor binding: in-tree build
    # output, bundles, type-definition stubs, sourcemaps, lockfiles. Binding a
    # query to ``dist.esm.axios`` or ``index.d`` produces a target the judge
    # correctly rejects as "not the source implementation".
    _NON_SOURCE_DIR_RE = re.compile(
        r"(^|/)(dist|build|out|node_modules|vendor|third_party|\.git|__pycache__)/",
        re.I,
    )
    _NON_SOURCE_FILE_RE = re.compile(
        r"(\.min\.js|\.d\.[cm]?ts|\.map|\.lock|\.snap)$", re.I
    )

    @classmethod
    def _is_source_path(cls, path: str) -> bool:
        return not (
            cls._NON_SOURCE_DIR_RE.search(path) or cls._NON_SOURCE_FILE_RE.search(path)
        )

    @classmethod
    def _modules_from_ground_truth(
        cls, ground_truth: Optional[Dict[str, Any]]
    ) -> List[str]:
        """Dotted module paths derived from the target files (for MODULE_HINT).

        ``astropy/units/core.py`` -> ``astropy.units.core``. Gives the curator the
        real module to name (and to bind the described behavior to), instead of an
        unanchored "the X module". Skips non-source targets (bundles, type-def
        stubs) so we never bind to ``dist.esm.axios`` / ``index.d``.
        """
        files = (ground_truth or {}).get("target_files") or []
        mods: List[str] = []
        for f in files:
            f = str(f)
            if not cls._is_source_path(f):
                continue
            base = re.sub(r"\.[A-Za-z0-9]+$", "", f)  # drop extension
            dotted = base.replace("/", ".").strip(".")
            if dotted:
                mods.append(dotted)
        return list(dict.fromkeys(mods))[:3]

    @classmethod
    def _strip_leading_narration(cls, q: str) -> str:
        """Drop leading agent-thinking sentences the model prepends to a query
        ("Now let me look at X. <real query>"). Returns "" when the whole string
        is narration so the caller falls back to a clean question."""
        if not q:
            return q
        parts = re.split(r"(?<=[.:])\s+", q.strip())
        while parts and cls._NARRATION_RE.match(parts[0].strip()):
            parts.pop(0)
        return " ".join(parts).strip()

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

    # Agent chain-of-thought preambles that leak into the query field when the
    # model returns prose instead of JSON ("Now I understand the code...",
    # "Let me look at..."). Skip these lines so we fall back to a clean question
    # rather than salvaging reasoning narration as the query.
    _NARRATION_RE = re.compile(
        r"^(now\b|let me\b|let's\b|i'?ll\b|i now\b|i understand\b|i can see\b|"
        r"i need to\b|i'?ve\b|looking at\b|first,|okay\b|ok\b|so,|here'?s what)",
        re.I,
    )

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
            if QueryCurator._NARRATION_RE.match(candidate):
                continue
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
