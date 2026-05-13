# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query verification for the synthesis pipeline."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from codeminer.log_utils import get_logger

from ._agent import AgentRunner
from ._types import BehavioralContext, format_prompt_block

logger = get_logger(__name__)


class Verifier:
    """Verify synthesized queries for quality and target alignment.

    Two checks:
      1. **Format compliance** — regex/length checks ensuring the query
         matches its mode (e.g., behavioral queries contain no code
         identifiers, questions are not too short, no chain-of-thought
         leaks).
      2. **Target alignment** — a blind LLM test: given the question and
         all blocks (unlabeled), does the LLM identify the core block?
    """

    # Patterns that indicate chain-of-thought leaks or meta-language.
    _BAD_QUERY_PATTERNS = re.compile(
        r"(?i)"
        r"(?:^let me |^I (?:will|need to|want to|should|can) |"
        r"examine (?:the |this )?(?:core )?block|"
        r"look (?:at|into) (?:the |this )?(?:core )?block|"
        r"understand (?:the |its |this )?(?:full )?content|"
        r"(?:core|context|sampled|candidate) block|"
        r"blk_\d+|"
        r"^(?:now|first|next),? (?:let|I)|"
        r"read (?:the |this )?(?:source |code )?(?:more )?closely)"
    )
    _MIN_QUERY_LENGTH = 60

    def __init__(
        self,
        *,
        agent: AgentRunner,
        verification_mode: str = "lenient",
        max_block_chars_in_prompt: int = 1800,
    ) -> None:
        if verification_mode not in ("strict", "lenient", "none"):
            raise ValueError(
                f"verification_mode must be strict, lenient, or none; got {verification_mode!r}"
            )
        self.agent = agent
        self.verification_mode = verification_mode
        self.max_block_chars_in_prompt = max_block_chars_in_prompt

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(
        self,
        result: Dict[str, Any],
        runs: List[Dict[str, Any]],
        behavioral_context: BehavioralContext,
        *,
        cwd: str,
    ) -> Dict[str, Any]:
        """Run quality check + post-consensus verification (sync).

        Mutates and returns *result* with ``verification_passed`` and
        ``verification_block_id`` keys added.
        """
        ok, reason = self.validate_query_quality(result["question"])
        if not ok:
            logger.warning(
                "Query quality check failed (%s): %r", reason, result["question"]
            )
            if not self._pick_best_valid_question(result, runs):
                result["verification_passed"] = False
                result["verification_block_id"] = None
                return result

        if self.verification_mode == "none":
            result["verification_passed"] = None
            result["verification_block_id"] = None
            return result

        vr = self._verify_alignment(result["question"], behavioral_context, cwd=cwd)
        if vr["passed"]:
            result["verification_passed"] = True
            result["verification_block_id"] = vr["block_id"]
            return result

        if self.verification_mode == "strict":
            ranked_runs = sorted(
                runs,
                key=lambda r: r.get("question", "") != result["question"],
            )
            for alt_run in ranked_runs:
                alt_q = alt_run.get("question", "")
                if not alt_q or alt_q == result["question"]:
                    continue
                alt_ok, _ = self.validate_query_quality(alt_q)
                if not alt_ok:
                    continue
                alt_vr = self._verify_alignment(alt_q, behavioral_context, cwd=cwd)
                if alt_vr["passed"]:
                    result["question"] = alt_q
                    result["focus"] = alt_run.get("focus")
                    result["verification_passed"] = True
                    result["verification_block_id"] = alt_vr["block_id"]
                    return result

        result["verification_passed"] = False
        result["verification_block_id"] = vr["block_id"]
        return result

    async def verify_async(
        self,
        result: Dict[str, Any],
        runs: List[Dict[str, Any]],
        behavioral_context: BehavioralContext,
        *,
        cwd: str,
    ) -> Dict[str, Any]:
        """Run quality check + post-consensus verification (async)."""
        ok, reason = self.validate_query_quality(result["question"])
        if not ok:
            logger.warning(
                "Query quality check failed (%s): %r", reason, result["question"]
            )
            if not self._pick_best_valid_question(result, runs):
                result["verification_passed"] = False
                result["verification_block_id"] = None
                return result

        if self.verification_mode == "none":
            result["verification_passed"] = None
            result["verification_block_id"] = None
            return result

        vr = await self._verify_alignment_async(
            result["question"], behavioral_context, cwd=cwd
        )
        if vr["passed"]:
            result["verification_passed"] = True
            result["verification_block_id"] = vr["block_id"]
            return result

        if self.verification_mode == "strict":
            ranked_runs = sorted(
                runs,
                key=lambda r: r.get("question", "") != result["question"],
            )
            for alt_run in ranked_runs:
                alt_q = alt_run.get("question", "")
                if not alt_q or alt_q == result["question"]:
                    continue
                alt_ok, _ = self.validate_query_quality(alt_q)
                if not alt_ok:
                    continue
                alt_vr = await self._verify_alignment_async(
                    alt_q, behavioral_context, cwd=cwd
                )
                if alt_vr["passed"]:
                    result["question"] = alt_q
                    result["focus"] = alt_run.get("focus")
                    result["verification_passed"] = True
                    result["verification_block_id"] = alt_vr["block_id"]
                    return result

        result["verification_passed"] = False
        result["verification_block_id"] = vr["block_id"]
        return result

    # ------------------------------------------------------------------
    # Quality validation (static — usable without an agent)
    # ------------------------------------------------------------------

    @classmethod
    def validate_query_quality(cls, question: str) -> Tuple[bool, str]:
        """Check whether *question* is a valid behavioral query.

        Returns ``(is_valid, reason)``.
        """
        if not question or not question.strip():
            return False, "empty question"
        q = question.strip()
        if len(q) < cls._MIN_QUERY_LENGTH:
            return False, f"too short ({len(q)} chars, need >= {cls._MIN_QUERY_LENGTH})"
        m = cls._BAD_QUERY_PATTERNS.search(q)
        if m:
            return False, f"meta-language detected: {m.group()!r}"
        return True, ""

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _pick_best_valid_question(
        self,
        result: Dict[str, Any],
        runs: List[Dict[str, Any]],
    ) -> bool:
        """Try to replace *result*'s question with a valid one from *runs*."""
        for run in runs:
            alt_q = run.get("question", "")
            if not alt_q or alt_q == result["question"]:
                continue
            ok, _ = self.validate_query_quality(alt_q)
            if ok:
                result["question"] = alt_q
                result["focus"] = run.get("focus")
                return True
        return False

    def _build_verification_prompt(
        self,
        question: str,
        behavioral_context: BehavioralContext,
    ) -> str:
        """Build a blind verification prompt (no core label)."""
        all_blocks = [behavioral_context.core_block] + list(
            behavioral_context.neighborhood_blocks
        )
        block_text = "\n\n".join(
            format_prompt_block(
                block, is_core=False, max_block_chars=self.max_block_chars_in_prompt
            )
            for block in all_blocks
        )
        return (
            "Given the following code-search question, identify which code block "
            "the question is primarily about.\n\n"
            f"Question: {question!r}\n\n"
            f"Code blocks:\n{block_text}\n\n"
            "Which block does this question primarily describe? "
            "Return strict JSON with keys: block_id (string), confidence (high|medium|low)."
        )

    def _verify_alignment(
        self,
        question: str,
        behavioral_context: BehavioralContext,
        *,
        cwd: str,
    ) -> Dict[str, Any]:
        prompt = self._build_verification_prompt(question, behavioral_context)
        payload = self.agent.run(prompt, cwd=cwd)
        return self._parse_verification_result(payload, behavioral_context)

    async def _verify_alignment_async(
        self,
        question: str,
        behavioral_context: BehavioralContext,
        *,
        cwd: str,
    ) -> Dict[str, Any]:
        prompt = self._build_verification_prompt(question, behavioral_context)
        payload = await self.agent.run_async(prompt, cwd=cwd)
        return self._parse_verification_result(payload, behavioral_context)

    def _parse_verification_result(
        self,
        payload: str,
        behavioral_context: BehavioralContext,
    ) -> Dict[str, Any]:
        blob = self.agent.extract_json(payload)
        core_id = behavioral_context.core_block.block_id
        try:
            data = json.loads(blob)
            block_id = data.get("block_id", "")
            confidence = data.get("confidence", "low")
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Verification returned non-JSON; assuming failed.")
            return {"block_id": "", "confidence": "low", "passed": False}

        passed = block_id == core_id
        if not passed:
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
        return {"block_id": block_id, "confidence": confidence, "passed": passed}
