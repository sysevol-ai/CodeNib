# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Token usage and cost tracking for LLM calls.

Wraps the native ``response.usage`` field that litellm exposes on
every ``completion()`` response and (when supported by the provider)
the cost estimate from ``litellm.completion_cost``.

Usage::

    from codenib.llm.usage import UsageTracker

    tracker = UsageTracker()
    llm._call_raw(messages, usage_tracker=tracker, _model=llm.model)
    print(tracker.totals())  # TokenUsage(prompt=..., completion=..., total=..., cost_usd=...)
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..log_utils import get_logger

logger = get_logger(__name__)

current_wiki_page_context: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("current_wiki_page_context", default=None)
)


@dataclass
class TokenUsage:
    """Cumulative token counts (plus optional USD cost).

    ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` are the
    provider's prompt-cache classes. Anthropic reports them separately from
    uncached ``input_tokens``; total processed input is therefore their sum.
    Other providers may normalize ``prompt_tokens`` differently, so offline
    repricing must declare the token semantics in its pricing snapshot rather
    than infer them here. Cache fields are 0 when caching is off or unsupported.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

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
            cache_read_input_tokens=(
                self.cache_read_input_tokens + other.cache_read_input_tokens
            ),
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
        )

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@dataclass
class UsageRecord:
    """A single ``_call_raw`` invocation's usage."""

    model: str
    usage: TokenUsage
    duration_ms: float = 0.0
    turn: Optional[int] = None
    page: Optional[str] = None
    cost_provenance: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "usage": self.usage.to_dict(),
            "duration_ms": self.duration_ms,
            "turn": self.turn,
            "page": self.page,
            "cost_provenance": self.cost_provenance,
        }


@dataclass
class FailureRecord:
    """Record of an unhandled LLM call failure."""

    model: str
    duration_ms: float = 0.0
    turn: Optional[int] = None
    page: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "duration_ms": self.duration_ms,
            "turn": self.turn,
            "page": self.page,
            "error": self.error,
        }


class UsageTracker:
    """Collects per-call usage and returns a running total (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: List[UsageRecord] = []
        self.failures: List[FailureRecord] = []

    def record_response(
        self,
        response: Any,
        *,
        model: str,
        duration_ms: float = 0.0,
        turn: Optional[int] = None,
        page: Optional[str] = None,
    ) -> UsageRecord:
        """Extract ``response.usage`` + cost into a ``UsageRecord``.

        Safe to call on any litellm response; missing fields default to zero and
        cost falls back to ``None`` if the provider/model is unpriced.
        """
        with self._lock:
            if page is None:
                page = current_wiki_page_context.get()
            usage = _extract_token_usage(response)
            cost = _safe_completion_cost(response, model, usage=usage)
            usage.cost_usd = cost
            provenance = _resolve_provenance(response, model, cost, usage)
            record = UsageRecord(
                model=model,
                usage=usage,
                duration_ms=duration_ms,
                turn=turn,
                page=page,
                cost_provenance=provenance,
            )
            self.records.append(record)
            return record

    def record_failure(
        self,
        *,
        model: str,
        duration_ms: float = 0.0,
        turn: Optional[int] = None,
        page: Optional[str] = None,
        error: Optional[str] = None,
    ) -> FailureRecord:
        """Record an unhandled exception from an LLM call attempt."""
        with self._lock:
            if page is None:
                page = current_wiki_page_context.get()
            record = FailureRecord(
                model=model,
                duration_ms=duration_ms,
                turn=turn,
                page=page,
                error=error,
            )
            self.failures.append(record)
            return record

    def totals(self) -> TokenUsage:
        """Sum all records into a single ``TokenUsage``."""
        with self._lock:
            total = TokenUsage()
            for rec in self.records:
                total = total.add(rec.usage)
            return total

    def summary(
        self,
        *,
        wall_clock_seconds: Optional[float] = None,
        rewritten_pages: Optional[int] = None,
        total_pages: Optional[int] = None,
        left_out_pages: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Aggregate records, failures, page breakdowns, and provenance into a summary dict."""
        with self._lock:
            total = TokenUsage()
            by_page: Dict[str, Dict[str, Any]] = {}
            models_seen = set()
            cost_provenances = []

            for rec in self.records:
                total = total.add(rec.usage)
                models_seen.add(rec.model)
                if rec.cost_provenance:
                    cost_provenances.append(rec.cost_provenance)

                p = rec.page or "unknown"
                if p not in by_page:
                    by_page[p] = {
                        "calls": 0,
                        "failed_calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cost_usd": 0.0 if rec.usage.cost_usd is not None else None,
                    }
                entry = by_page[p]
                entry["calls"] += 1
                entry["prompt_tokens"] += rec.usage.prompt_tokens
                entry["completion_tokens"] += rec.usage.completion_tokens
                entry["total_tokens"] += rec.usage.total_tokens
                if rec.usage.cost_usd is not None:
                    entry["cost_usd"] = (entry["cost_usd"] or 0.0) + rec.usage.cost_usd

            for fail in self.failures:
                models_seen.add(fail.model)
                p = fail.page or "unknown"
                if p not in by_page:
                    by_page[p] = {
                        "calls": 0,
                        "failed_calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "cost_usd": None,
                    }
                by_page[p]["failed_calls"] += 1

            if len(models_seen) == 1:
                model_name = next(iter(models_seen))
            elif models_seen:
                model_name = ", ".join(sorted(models_seen))
            else:
                model_name = "unknown"

            if not cost_provenances:
                primary_prov = runtime_cost_provenance(model_name, total.cost_usd)
            else:
                primary_prov = cost_provenances[0]

            left_out_summary = None
            if left_out_pages is not None:
                left_out_set = set(left_out_pages)
                lo_calls = 0
                lo_failed = 0
                lo_cost = 0.0 if total.cost_usd is not None else None
                for rec in self.records:
                    if rec.page in left_out_set:
                        lo_calls += 1
                        if rec.usage.cost_usd is not None and lo_cost is not None:
                            lo_cost += rec.usage.cost_usd
                for fail in self.failures:
                    if fail.page in left_out_set:
                        lo_failed += 1
                left_out_summary = {
                    "pages": list(left_out_pages),
                    "count": len(left_out_pages),
                    "calls": lo_calls,
                    "failed_calls": lo_failed,
                    "cost_usd": round(lo_cost, 6) if lo_cost is not None else None,
                }

            return {
                "model": model_name,
                "calls": len(self.records),
                "failed_calls": len(self.failures),
                "prompt_tokens": total.prompt_tokens,
                "completion_tokens": total.completion_tokens,
                "total_tokens": total.total_tokens,
                "cost_usd": (
                    round(total.cost_usd, 6) if total.cost_usd is not None else None
                ),
                "cost_provenance": primary_prov,
                "wall_clock_seconds": wall_clock_seconds,
                "rewritten_pages": rewritten_pages,
                "total_pages": total_pages,
                "left_out": left_out_summary,
                "by_page": by_page,
            }


def runtime_cost_provenance(
    model: str,
    cost_usd: Optional[float],
    *,
    source: Optional[str] = None,
    calculator: Optional[str] = None,
) -> Dict[str, Any]:
    """Describe a best-effort LiteLLM runtime estimate without pinning a price.

    The calculator version makes historical records auditable, but this is not
    an immutable pricing snapshot. Reports that need reproducible repricing
    must use raw token classes and a separate catalog.
    """
    calc = calculator or source or "litellm.completion_cost"
    if cost_usd is None:
        return {
            "kind": "unavailable",
            "calculator": calc,
            "calculator_version": _installed_litellm_version() if "litellm" in calc else None,
            "model": str(model),
        }

    version_str = _installed_litellm_version() if "litellm" in calc else None
    return {
        "kind": "runtime_estimate",
        "calculator": calc,
        "calculator_version": version_str,
        "model": str(model),
    }


@lru_cache(maxsize=1)
def _installed_litellm_version() -> Optional[str]:
    try:
        return version("litellm")
    except PackageNotFoundError:
        return None


def _extract_token_usage(response: Any) -> TokenUsage:
    """Pull ``prompt_tokens``/``completion_tokens``/``total_tokens`` off a response."""
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

    cached_via_details = 0
    details = getattr(usage_obj, "prompt_tokens_details", None)
    if details is None and isinstance(usage_obj, dict):
        details = usage_obj.get("prompt_tokens_details")
    if details is not None:
        if hasattr(details, "cached_tokens"):
            cached_via_details = int(details.cached_tokens or 0)
        elif isinstance(details, dict):
            cached_via_details = int(details.get("cached_tokens") or 0)

    return TokenUsage(
        prompt_tokens=_read("prompt_tokens"),
        completion_tokens=_read("completion_tokens"),
        total_tokens=_read("total_tokens"),
        cache_read_input_tokens=_read("cache_read_input_tokens") or cached_via_details,
        cache_creation_input_tokens=_read("cache_creation_input_tokens"),
    )


def _extract_response_cost(response: Any) -> Optional[float]:
    """Extract billed cost from OpenRouter or other provider response payload if present."""
    if response is None:
        return None
    candidates = [getattr(response, "usage", None), response]
    for target in candidates:
        if target is None:
            continue
        for key in ("cost", "total_cost", "_response_cost"):
            val = getattr(target, key, None)
            if val is None and isinstance(target, dict):
                val = target.get(key)
            if (
                val is None
                and hasattr(target, "model_extra")
                and isinstance(target.model_extra, dict)
            ):
                val = target.model_extra.get(key)
            if val is not None:
                try:
                    num = float(val)
                    if num > 0:
                        return num
                except (TypeError, ValueError):
                    pass
    return None


def _match_wiki_model(model: str) -> Optional[Any]:
    """Look up recorded pricing from codenib.wiki.visitor_models for fallback calculation."""
    try:
        from ..wiki.visitor_models import wiki_model
    except ImportError:
        return None
    candidates = [model]
    if model.startswith("openrouter/"):
        candidates.append(model[len("openrouter/") :])
    for c in candidates:
        wm = wiki_model(c)
        if wm is not None:
            return wm
    return None


def _resolve_cost_and_provenance(
    response: Any, model: str, usage: Optional[TokenUsage] = None
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Determine cost with hierarchical fallback: response billed cost -> LiteLLM -> visitor_models."""
    billed = _extract_response_cost(response)
    if billed is not None:
        return billed, {
            "kind": "billed_cost",
            "calculator": "response.usage.cost",
            "model": str(model),
        }

    try:
        import litellm

        cost = litellm.completion_cost(completion_response=response, model=model)
        if cost is not None:
            cost_flt = float(cost)
            if cost_flt >= 0:
                return cost_flt, {
                    "kind": "runtime_estimate",
                    "calculator": "litellm.completion_cost",
                    "calculator_version": _installed_litellm_version(),
                    "model": str(model),
                }
    except Exception as exc:
        logger.debug("completion_cost unavailable for %s: %s", model, exc)

    wm = _match_wiki_model(model)
    if wm is not None:
        tok_usage = usage or _extract_token_usage(response)
        prompt_cost = tok_usage.prompt_tokens * (wm.input_usd / 1_000_000.0)
        completion_cost = tok_usage.completion_tokens * (wm.output_usd / 1_000_000.0)
        total_cost = prompt_cost + completion_cost
        return round(total_cost, 6), {
            "kind": "catalog_fallback",
            "calculator": "codenib.wiki.visitor_models.WIKI_MODEL_CHOICES",
            "model": str(model),
        }

    return None, {
        "kind": "unavailable",
        "calculator": "litellm.completion_cost",
        "calculator_version": _installed_litellm_version(),
        "model": str(model),
    }


def _resolve_provenance(
    response: Any, model: str, cost: Optional[float], usage: Optional[TokenUsage]
) -> Dict[str, Any]:
    """Helper to ensure cost provenance reflects the actual resolved cost."""
    _, prov = _resolve_cost_and_provenance(response, model, usage)
    if cost is not None and prov.get("kind") == "unavailable":
        return runtime_cost_provenance(model, cost)
    return prov


def _safe_completion_cost(
    response: Any, model: str, usage: Optional[TokenUsage] = None
) -> Optional[float]:
    """Best-effort USD cost via response metadata, ``litellm.completion_cost``, or catalog fallback."""
    cost, _ = _resolve_cost_and_provenance(response, model, usage)
    return cost


def _format_duration(seconds: float) -> str:
    whole = max(0, round(seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, rest = divmod(whole, 60)
    return f"{minutes}m {rest}s" if rest else f"{minutes}m"


def format_usage_summary(summary: Dict[str, Any]) -> str:
    """Format a multi-line terminal string from a usage summary dict."""
    model = summary.get("model") or "unknown"
    calls = int(summary.get("calls") or 0)
    failed = int(summary.get("failed_calls") or 0)
    calls_str = str(calls)
    if failed > 0:
        calls_str += f" ({failed} failed)"
    prompt_tok = int(summary.get("prompt_tokens") or 0)
    comp_tok = int(summary.get("completion_tokens") or 0)
    cost_usd = summary.get("cost_usd")
    prov = summary.get("cost_provenance") or {}
    calc = prov.get("calculator") or ""
    if "WIKI_MODEL_CHOICES" in calc:
        cost_label = "(catalog estimate)"
    elif "response.usage.cost" in calc:
        cost_label = "(billed)"
    else:
        cost_label = "(estimate)"
    cost_str = f"${cost_usd:.3f} {cost_label}" if cost_usd is not None else "unavailable"

    wall_sec = summary.get("wall_clock_seconds")
    time_str = _format_duration(float(wall_sec)) if wall_sec is not None else None

    lines = [
        f"Model usage: {model}",
        f"  Calls:     {calls_str}",
        f"  Tokens:    {prompt_tok:,} in, {comp_tok:,} out",
        f"  Cost:      {cost_str}",
    ]
    if time_str:
        lines.append(f"  Time:      {time_str} writing pages")

    rewritten = summary.get("rewritten_pages")
    total_p = summary.get("total_pages")
    if rewritten is not None and total_p is not None and rewritten < total_p:
        lines.append(f"  Pages:     rewrote {rewritten} of {total_p}")

    left_out = summary.get("left_out")
    if isinstance(left_out, dict) and left_out.get("count", 0) > 0:
        lo_calls = int(left_out.get("calls") or 0)
        lo_cost = left_out.get("cost_usd")
        lo_count = int(left_out.get("count") or 0)
        page_word = "pages" if lo_count != 1 else "page"
        if lo_cost is not None:
            lines.append(
                f"  Left out:  {lo_calls} calls, ${lo_cost:.3f} on {lo_count} unpublished {page_word}"
            )
        else:
            lines.append(
                f"  Left out:  {lo_calls} calls on {lo_count} unpublished {page_word}"
            )

    return "\n".join(lines)


def format_usage_markdown(summary: Dict[str, Any]) -> str:
    """Format GitHub Step Summary markdown from a usage summary dict."""
    model = summary.get("model") or "unknown"
    calls = int(summary.get("calls") or 0)
    failed = int(summary.get("failed_calls") or 0)
    prompt_tok = int(summary.get("prompt_tokens") or 0)
    comp_tok = int(summary.get("completion_tokens") or 0)
    total_tok = int(summary.get("total_tokens") or (prompt_tok + comp_tok))
    cost_usd = summary.get("cost_usd")
    prov = summary.get("cost_provenance") or {}
    calc = prov.get("calculator") or "litellm.completion_cost"
    cost_val = f"${cost_usd:.3f} (`{calc}`)" if cost_usd is not None else "unavailable"

    wall_sec = summary.get("wall_clock_seconds")
    time_str = _format_duration(float(wall_sec)) if wall_sec is not None else "N/A"

    rows = [
        "### CodeNib Wiki Build Usage\n",
        "| Metric | Value |",
        "|---|---|",
        f"| **Model** | `{model}` |",
        f"| **Calls** | {calls:,} ({failed} failed) |",
        f"| **Tokens** | {prompt_tok:,} in, {comp_tok:,} out ({total_tok:,} total) |",
        f"| **Cost** | {cost_val} |",
        f"| **Time** | {time_str} writing pages |",
    ]
    rewritten = summary.get("rewritten_pages")
    total_p = summary.get("total_pages")
    if rewritten is not None and total_p is not None and rewritten < total_p:
        rows.append(f"| **Pages** | Rewrote {rewritten} of {total_p} pages |")

    left_out = summary.get("left_out")
    if isinstance(left_out, dict) and left_out.get("count", 0) > 0:
        lo_calls = int(left_out.get("calls") or 0)
        lo_cost = left_out.get("cost_usd")
        lo_count = int(left_out.get("count") or 0)
        lo_cost_str = f", ${lo_cost:.3f}" if lo_cost is not None else ""
        rows.append(
            f"| **Left Out** | {lo_calls} calls{lo_cost_str} across {lo_count} unpublished pages |"
        )

    return "\n".join(rows) + "\n"
