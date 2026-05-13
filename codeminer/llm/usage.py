# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Token usage and cost tracking for LLM calls.

Wraps the native ``response.usage`` field that litellm exposes on
every ``completion()`` response and (when supported by the provider)
the cost estimate from ``litellm.completion_cost``.

Usage::

    from codeminer.llm.usage import UsageTracker

    tracker = UsageTracker()
    llm._call_raw(messages, usage_tracker=tracker, _model=llm.model)
    print(tracker.totals())  # TokenUsage(prompt=..., completion=..., total=..., cost_usd=...)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from ..log_utils import get_logger

logger = get_logger(__name__)


@dataclass
class TokenUsage:
    """Cumulative token counts (plus optional USD cost)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None

    def add(self, other: "TokenUsage") -> "TokenUsage":
        """Return a new TokenUsage that is the sum of ``self`` and ``other``."""
        merged_cost: Optional[float]
        if self.cost_usd is None and other.cost_usd is None:
            merged_cost = None
        else:
            merged_cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=merged_cost,
        )

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class UsageRecord:
    """A single ``_call_raw`` invocation's usage."""

    model: str
    usage: TokenUsage
    duration_ms: float = 0.0
    turn: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "usage": self.usage.to_dict(),
            "duration_ms": self.duration_ms,
            "turn": self.turn,
        }


class UsageTracker:
    """Collects per-call usage and returns a running total."""

    def __init__(self) -> None:
        self.records: List[UsageRecord] = []

    def record_response(
        self,
        response: Any,
        *,
        model: str,
        duration_ms: float = 0.0,
        turn: Optional[int] = None,
    ) -> UsageRecord:
        """Extract ``response.usage`` + ``completion_cost`` into a ``UsageRecord``.

        Safe to call on any litellm response; missing fields default to zero and
        cost falls back to ``None`` if the provider/model is unpriced.
        """
        usage = _extract_token_usage(response)
        usage.cost_usd = _safe_completion_cost(response, model)
        record = UsageRecord(
            model=model, usage=usage, duration_ms=duration_ms, turn=turn
        )
        self.records.append(record)
        return record

    def totals(self) -> TokenUsage:
        """Sum all records into a single ``TokenUsage``."""
        total = TokenUsage()
        for rec in self.records:
            total = total.add(rec.usage)
        return total


def _extract_token_usage(response: Any) -> TokenUsage:
    """Pull ``prompt_tokens``/``completion_tokens``/``total_tokens`` off a response.

    litellm normalizes provider responses to an OpenAI-compatible shape; the
    ``usage`` attribute is usually a pydantic model with ``prompt_tokens`` /
    ``completion_tokens`` / ``total_tokens`` but can also be a plain dict.
    """
    usage_obj = getattr(response, "usage", None)
    if usage_obj is None:
        return TokenUsage()

    def _read(name: str) -> int:
        if hasattr(usage_obj, name):
            val = getattr(usage_obj, name)
        elif isinstance(usage_obj, dict):
            val = usage_obj.get(name, 0)
        else:
            val = 0
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    return TokenUsage(
        prompt_tokens=_read("prompt_tokens"),
        completion_tokens=_read("completion_tokens"),
        total_tokens=_read("total_tokens"),
    )


def _safe_completion_cost(response: Any, model: str) -> Optional[float]:
    """Best-effort USD cost via ``litellm.completion_cost``; ``None`` on failure."""
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response, model=model))
    except Exception as exc:  # litellm raises for unpriced models/providers
        logger.debug("completion_cost unavailable for %s: %s", model, exc)
        return None
