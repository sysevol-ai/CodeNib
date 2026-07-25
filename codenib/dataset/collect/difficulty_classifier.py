# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent-based difficulty classification for SWE-bench instances.

Uses a Claude agent to analyze the problem statement and patch content to
produce a difficulty level (low/medium/high) that reflects conceptual
complexity rather than just lines-of-code changed.
"""

from __future__ import annotations

import asyncio
import atexit
import gc
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_agent_sdk import ClaudeAgentOptions, query
from pydantic import BaseModel, Field

from codenib.log_utils import get_logger

logger = get_logger(__name__)

DIFFICULTY_SYSTEM_PROMPT = (
    "You are an expert software engineer who classifies the difficulty of "
    "bug-fix tasks. You will be given one or more tasks, each with a problem "
    "statement (issue description) and the patch that resolves it. Classify "
    "each task's difficulty as low, medium, or high based on the criteria "
    "below.\n\n"
    "Criteria:\n"
    "- **low**: Straightforward fix. Localised to a single function or "
    "module. Requires minimal domain knowledge. The root cause is obvious "
    "from the problem description (e.g. typo, missing null-check, wrong "
    "constant, off-by-one error).\n"
    "- **medium**: Requires understanding interactions between a few "
    "components. May involve non-trivial logic changes, adding a new code "
    "path, or fixing subtle data-flow issues. Moderate domain knowledge "
    "needed.\n"
    "- **high**: Requires deep understanding of the system architecture or "
    "domain. Involves changes across multiple modules/subsystems, complex "
    "algorithmic reasoning, concurrency issues, or subtle correctness "
    "constraints that are hard to identify from the problem description "
    "alone.\n\n"
    "Respond with ONLY a JSON object (no markdown fences) with this shape:\n"
    '{"results": [{"instance_id": "...", "difficulty_level": "low|medium|high"}, ...]}'
)

VALID_LEVELS = {"low", "medium", "high"}
DIFFICULTY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string"},
                    "difficulty_level": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
                "required": ["instance_id", "difficulty_level"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}
MAX_PATCH_CHARS = 6000
BATCH_SIZE = 10
BATCH_RETRY_LIMIT = 2
MAX_FALLBACK_SPLIT_DEPTH = 3


class DifficultyResult(BaseModel):
    """Structured output for agent difficulty classification."""

    difficulty_level: str = Field(description="low, medium, or high")


class AgentDifficultyClassifier:
    """Classify SWE-bench instance difficulty using a Claude agent."""

    def __init__(
        self,
        *,
        model: str = "opus",
        batch_size: int = BATCH_SIZE,
        cache_path: Optional[Path] = None,
    ) -> None:
        self.model = model
        self.batch_size = batch_size
        self.cache_path = cache_path
        self._cache: Dict[str, DifficultyResult] = {}
        self._runner: asyncio.Runner | None = None
        self._closed = False
        atexit.register(self.close)
        if cache_path and cache_path.exists():
            self._load_cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(
        self,
        instance_id: str,
        problem_statement: str,
        patch: str,
    ) -> DifficultyResult:
        """Classify a single instance's difficulty."""
        if instance_id in self._cache:
            logger.debug("Cache hit for %s", instance_id)
            return self._cache[instance_id]

        results = self._classify_items_with_fallback(
            [(instance_id, problem_statement, patch)]
        )
        result = results[instance_id]
        self._cache[instance_id] = result
        return result

    def classify_batch(
        self,
        instances: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Classify all instances, adding agent difficulty fields in-place.

        Each dict gets ``agent_difficulty``. Instances are sent to the agent in
        batches to minimize subprocess overhead.
        """
        # Separate cached vs uncached.
        uncached: List[Dict[str, Any]] = []
        for inst in instances:
            iid = inst["instance_id"]
            if iid in self._cache:
                result = self._cache[iid]
                inst["agent_difficulty"] = result.difficulty_level
            else:
                uncached.append(inst)

        logger.info(
            "Difficulty classification: %d cached, %d to classify",
            len(instances) - len(uncached),
            len(uncached),
        )

        # Classify uncached in batches.
        for batch_start in range(0, len(uncached), self.batch_size):
            batch = uncached[batch_start : batch_start + self.batch_size]
            items = [
                (
                    inst["instance_id"],
                    inst.get("problem_statement", ""),
                    inst.get("patch", ""),
                )
                for inst in batch
            ]
            results = self._classify_items_with_fallback(items)

            for inst in batch:
                iid = inst["instance_id"]
                result = results[iid]
                self._cache[iid] = result
                inst["agent_difficulty"] = result.difficulty_level

            logger.info(
                "Classified %d/%d instances",
                min(batch_start + self.batch_size, len(uncached)),
                len(uncached),
            )

        self._save_cache()
        logger.info("Difficulty classification complete: %d instances", len(instances))
        return instances

    def _classify_items_with_fallback(
        self,
        items: List[tuple[str, str, str]],
        *,
        depth: int = 0,
    ) -> Dict[str, DifficultyResult]:
        ids = [iid for iid, _, _ in items]
        prompt = self._build_batch_prompt(items)

        results: Dict[str, DifficultyResult] = {}
        for attempt in range(BATCH_RETRY_LIMIT):
            raw = self._run_agent(prompt)
            results = self._parse_batch_result(raw, ids, fill_defaults=False)
            missing_ids = [iid for iid in ids if iid not in results]
            if not missing_ids:
                return results
            if attempt + 1 < BATCH_RETRY_LIMIT:
                logger.warning(
                    "Incomplete batch response (%d/%d parsed). Retrying batch (%d/%d).",
                    len(results),
                    len(ids),
                    attempt + 2,
                    BATCH_RETRY_LIMIT,
                )

        missing_ids = [iid for iid in ids if iid not in results]
        if not missing_ids:
            return results

        if len(items) == 1 or depth >= MAX_FALLBACK_SPLIT_DEPTH:
            for iid in missing_ids:
                logger.warning(
                    "Missing classification for %s after retries, defaulting to medium",
                    iid,
                )
                results[iid] = DifficultyResult(difficulty_level="medium")
            return results

        logger.warning(
            "Falling back to smaller sub-batches for %d missing IDs (depth=%d).",
            len(missing_ids),
            depth + 1,
        )

        item_map = {
            iid: (iid, problem_statement, patch)
            for iid, problem_statement, patch in items
        }
        missing_items = [item_map[iid] for iid in missing_ids if iid in item_map]
        mid = max(1, len(missing_items) // 2)
        left_items = missing_items[:mid]
        right_items = missing_items[mid:]

        if left_items:
            results.update(
                self._classify_items_with_fallback(left_items, depth=depth + 1)
            )
        if right_items:
            results.update(
                self._classify_items_with_fallback(right_items, depth=depth + 1)
            )
        return results

    # ------------------------------------------------------------------
    # Agent interaction
    # ------------------------------------------------------------------

    def _build_batch_prompt(
        self,
        items: List[tuple[str, str, str]],
    ) -> str:
        """Build a prompt containing one or more tasks to classify."""
        parts: List[str] = []
        parts.append(
            f"Classify the difficulty of the following {len(items)} task(s).\n"
        )
        for i, (instance_id, problem_statement, patch) in enumerate(items, 1):
            truncated = patch[:MAX_PATCH_CHARS]
            if len(patch) > MAX_PATCH_CHARS:
                truncated += f"\n... (truncated, {len(patch)} chars total)"
            parts.append(
                f"## Task {i}: {instance_id}\n"
                f"### Problem Statement\n{problem_statement}\n\n"
                f"### Patch\n```diff\n{truncated}\n```\n"
            )
        return "\n".join(parts)

    def _run_agent(self, prompt: str) -> str:
        if self._closed:
            raise RuntimeError("Classifier has been closed.")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            last_result = ""
            for attempt in range(2):
                runner = self._get_runner()
                _aio_logger = logging.getLogger("asyncio")
                _prev = _aio_logger.level
                _aio_logger.setLevel(logging.CRITICAL)
                try:
                    result = runner.run(self._run_agent_async(prompt))
                    last_result = result
                finally:
                    _aio_logger.setLevel(_prev)

                if last_result.strip():
                    return last_result
                logger.warning(
                    "Agent returned empty payload on attempt %d/%d",
                    attempt + 1,
                    2,
                )
            return last_result
        raise RuntimeError("Cannot call _run_agent inside an active event loop.")

    def _get_runner(self) -> asyncio.Runner:
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner

    async def _run_agent_async(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            max_turns=3,
            system_prompt=DIFFICULTY_SYSTEM_PROMPT,
            allowed_tools=[],
            permission_mode="bypassPermissions",
            model=self.model,
            output_format={
                "type": "json_schema",
                "schema": DIFFICULTY_OUTPUT_SCHEMA,
            },
        )
        text_chunks: List[str] = []
        query_gen = query(prompt=prompt, options=options)
        try:
            async for message in query_gen:
                msg_type = message.__class__.__name__
                logger.debug("Agent message type: %s", msg_type)

                if msg_type == "ResultMessage":
                    structured = getattr(message, "structured_output", None)
                    if structured is not None:
                        try:
                            return json.dumps(structured, ensure_ascii=False).strip()
                        except Exception:
                            pass
                    result = getattr(message, "result", None)
                    if result and isinstance(result, str):
                        return result.strip()
                    # result may be a list of content blocks
                    if result and isinstance(result, list):
                        for block in result:
                            text = getattr(block, "text", None)
                            if isinstance(text, str) and text.strip():
                                text_chunks.append(text.strip())
                    return "\n".join(text_chunks).strip()

                if msg_type == "ErrorMessage":
                    error_msg = getattr(message, "error", "Unknown error")
                    logger.error("Agent error: %s", error_msg)
                    raise RuntimeError(f"Agent error: {error_msg}")

                if msg_type == "AssistantMessage":
                    content = getattr(message, "content", None)
                    if not content:
                        continue
                    for block in content:
                        text = getattr(block, "text", None)
                        if isinstance(text, str) and text.strip():
                            text_chunks.append(text.strip())
        finally:
            await query_gen.aclose()
        return "\n".join(text_chunks).strip()

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _extract_json_payload(self, raw: str) -> str:
        """Extract the first JSON object/array from a free-form agent response."""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        if not cleaned:
            return cleaned

        if (cleaned.startswith("[") and cleaned.endswith("]")) or (
            cleaned.startswith("{") and cleaned.endswith("}")
        ):
            return cleaned

        decoder = json.JSONDecoder()
        for match in re.finditer(r"[\[{]", cleaned):
            start = match.start()
            try:
                _, end = decoder.raw_decode(cleaned[start:])
                return cleaned[start : start + end]
            except json.JSONDecodeError:
                continue
        return cleaned

    def _parse_batch_result(
        self,
        raw: str,
        expected_ids: List[str],
        *,
        fill_defaults: bool = True,
    ) -> Dict[str, DifficultyResult]:
        """Parse a JSON array of classification results."""
        logger.debug("Raw agent response (%d chars): %s", len(raw), raw[:500])
        results = self._parse_json_results(raw)
        missing_ids = [iid for iid in expected_ids if iid not in results]
        if missing_ids:
            heuristic = self._parse_text_results(raw, expected_ids)
            for iid in missing_ids:
                if iid in heuristic:
                    results[iid] = heuristic[iid]

        if fill_defaults:
            default = DifficultyResult(
                difficulty_level="medium",
            )
            for iid in expected_ids:
                if iid not in results:
                    logger.warning(
                        "Missing classification for %s, defaulting to medium", iid
                    )
                    results[iid] = default

        return results

    def _parse_json_results(self, raw: str) -> Dict[str, DifficultyResult]:
        cleaned = self._extract_json_payload(raw)
        results: Dict[str, DifficultyResult] = {}
        try:
            data = json.loads(cleaned)
            entries: Any
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                entries = data["results"]
            elif isinstance(data, list):
                entries = data
            elif isinstance(data, dict):
                entries = [data]
            else:
                entries = []
            for entry in entries:
                iid = str(entry.get("instance_id", "")).strip()
                if not iid:
                    continue
                level = str(entry.get("difficulty_level", "medium")).lower()
                if level not in VALID_LEVELS:
                    level = "medium"
                results[iid] = DifficultyResult(
                    difficulty_level=level,
                )
        except (json.JSONDecodeError, Exception) as exc:
            if cleaned:
                logger.warning("Failed to parse batch difficulty result: %s", exc)
            else:
                logger.debug("Empty batch difficulty payload: %s", exc)
        return results

    def _parse_text_results(
        self, raw: str, expected_ids: List[str]
    ) -> Dict[str, DifficultyResult]:
        """Best-effort parser when the agent returned non-JSON prose."""
        text = raw.strip()
        results: Dict[str, DifficultyResult] = {}
        for iid in expected_ids:
            id_pos = text.find(iid)
            if id_pos < 0:
                continue
            window = text[id_pos : min(len(text), id_pos + 800)]
            level_match = re.search(
                r"(?i)\bdifficulty\b[^a-zA-Z0-9]{0,12}(low|medium|high)\b", window
            )
            if not level_match:
                continue
            level = level_match.group(1).lower()
            results[iid] = DifficultyResult(
                difficulty_level=level,
            )
        return results

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for iid, entry in data.items():
                self._cache[iid] = DifficultyResult(**entry)
            logger.info("Loaded %d cached difficulty results", len(self._cache))
        except Exception as exc:
            logger.warning("Failed to load difficulty cache: %s", exc)

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {iid: result.model_dump() for iid, result in self._cache.items()}
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        logger.info("Saved %d difficulty results to cache", len(data))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._runner is not None:
            gc.collect()
            self._runner.close()
            self._runner = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
