# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Declarative agent harness configuration.

The runner owns execution. This module owns the reusable contract that says
which tools, prompts, and loop controls a harness exposes. Dataset selection,
benchmark scoring, model-specific routing, and experiment policy stay outside
this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Collection, Dict, FrozenSet, Iterable, List, Mapping, Optional

from ..llm.usage import TokenUsage

USAGE_TOTAL_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost_usd",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _normalize_names(
    values: Optional[Iterable[str]],
    *,
    field_name: str,
    empty_is_none: bool,
) -> Optional[FrozenSet[str]]:
    if values is None:
        return None
    names = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} entries must be strings")
        name = value.strip()
        if not name:
            raise ValueError(f"{field_name} entries must be non-empty strings")
        names.append(name)
    if not names and empty_is_none:
        return None
    return frozenset(names)


@dataclass(frozen=True)
class AgentHarnessSpec:
    """Stable runner-facing harness contract.

    This deliberately mirrors generic :class:`AgentRunner` controls only. It
    does not encode benchmark arms, scorer fields, model names, or dataset
    identifiers, so experiments can compare policies without leaking those
    policies into the core runtime API.
    """

    max_turns: int = 10
    max_context_tokens: Optional[int] = None
    allow_skills: Optional[Collection[str]] = None
    exclude_skills: Optional[Collection[str]] = None
    include_default_tools: bool = True
    default_tool_ids: Optional[Collection[str]] = None
    system_prompt: Optional[str] = None
    first_turn_tool_choice: Optional[str] = None
    force_first_turn_only: bool = False
    force_localization_contract: bool = True
    compact_after_read: bool = False
    compact_keep_reads: int = 0

    def __post_init__(self) -> None:
        if self.max_turns <= 0:
            raise ValueError("max_turns must be positive")
        if self.max_context_tokens is not None and self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive when set")
        if self.compact_keep_reads < 0:
            raise ValueError("compact_keep_reads must be non-negative")

        object.__setattr__(
            self,
            "allow_skills",
            _normalize_names(
                self.allow_skills,
                field_name="allow_skills",
                empty_is_none=False,
            ),
        )
        object.__setattr__(
            self,
            "exclude_skills",
            _normalize_names(
                self.exclude_skills,
                field_name="exclude_skills",
                empty_is_none=True,
            ),
        )
        object.__setattr__(
            self,
            "default_tool_ids",
            _normalize_names(
                self.default_tool_ids,
                field_name="default_tool_ids",
                empty_is_none=True,
            ),
        )

    def with_overrides(self, **overrides: Any) -> "AgentHarnessSpec":
        """Return a copy with selected fields replaced and revalidated."""
        data = {field.name: getattr(self, field.name) for field in fields(self)}
        unknown = set(overrides) - set(data)
        if unknown:
            raise TypeError(f"Unknown harness option(s): {sorted(unknown)}")
        data.update(overrides)
        return type(self)(**data)

    def to_runner_kwargs(
        self,
        *,
        session_ctx: Optional[Any] = None,
        manifest: Optional[Any] = None,
        compile_table: Optional[Any] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return ``AgentRunner`` keyword arguments for this harness.

        Collection fields are copied into mutable sets because ``AgentRunner``
        treats them as constructor inputs and may derive internal sets from
        them. The spec remains immutable and reusable across cells/subagents.
        """
        kwargs: Dict[str, Any] = {
            "max_turns": self.max_turns,
            "max_context_tokens": self.max_context_tokens,
            "allow_skills": (
                set(self.allow_skills) if self.allow_skills is not None else None
            ),
            "exclude_skills": (
                set(self.exclude_skills) if self.exclude_skills is not None else None
            ),
            "include_default_tools": self.include_default_tools,
            "default_tool_ids": (
                set(self.default_tool_ids)
                if self.default_tool_ids is not None
                else None
            ),
            "system_prompt": self.system_prompt,
            "first_turn_tool_choice": self.first_turn_tool_choice,
            "force_first_turn_only": self.force_first_turn_only,
            "force_localization_contract": self.force_localization_contract,
            "compact_after_read": self.compact_after_read,
            "compact_keep_reads": self.compact_keep_reads,
            "session_ctx": session_ctx,
            "manifest": manifest,
            "compile_table": compile_table,
        }
        if extra:
            kwargs.update(extra)
        return kwargs

    def create_runner(
        self,
        *,
        llm: Optional[Any] = None,
        model: Optional[str] = None,
        registry: Optional[Any] = None,
        session_ctx: Optional[Any] = None,
        manifest: Optional[Any] = None,
        compile_table: Optional[Any] = None,
        **overrides: Any,
    ) -> Any:
        """Build an ``AgentRunner`` using this harness.

        ``overrides`` are one-off runner keyword overrides for cases like
        subagents with a shorter turn budget. They do not mutate the spec.
        """
        from .runner import AgentRunner

        kwargs = self.to_runner_kwargs(
            session_ctx=session_ctx,
            manifest=manifest,
            compile_table=compile_table,
            extra=overrides,
        )
        return AgentRunner(llm=llm, model=model, registry=registry, **kwargs)


@dataclass
class AgentRunAccumulator:
    """Accumulate usage and turn counts across related agent runs.

    A harness may charge one logical cell for several LLM interactions: routing
    gates, isolated subagents, verify retries, or a final convergence run. This
    helper keeps that accounting generic so experiment scripts do not each
    reimplement token/turn summation.
    """

    _usage_values: Dict[str, List[float]] = field(
        default_factory=lambda: {key: [] for key in USAGE_TOTAL_KEYS}
    )
    _turns: List[int] = field(default_factory=list)

    def add_usage(self, usage: Optional[Any]) -> None:
        """Add a ``TokenUsage`` or mapping with flat/nested token fields."""
        if usage is None:
            return
        if isinstance(usage, TokenUsage):
            payload: Mapping[str, Any] = usage.to_dict()
        elif isinstance(usage, Mapping):
            nested = usage.get("token_usage")
            payload = nested if isinstance(nested, Mapping) else usage
        else:
            payload = {
                key: getattr(usage, key, None)
                for key in USAGE_TOTAL_KEYS
                if getattr(usage, key, None) is not None
            }

        for key in USAGE_TOTAL_KEYS:
            value = payload.get(key)
            if value is None:
                continue
            try:
                self._usage_values[key].append(float(value))
            except (TypeError, ValueError):
                continue

    def add_turns(self, turns: Optional[int]) -> None:
        """Add one run's turn count when it is known."""
        if turns is None:
            return
        self._turns.append(int(turns))

    def add_result(self, result: Any) -> None:
        """Add accounting fields from an ``AgentResult``-like object."""
        self.add_usage(getattr(result, "usage", None))
        self.add_turns(getattr(result, "total_turns", None))

    def usage_sum(self, key: str) -> Optional[float]:
        """Return the sum for one usage field, or ``None`` if never recorded."""
        if key not in self._usage_values:
            raise KeyError(f"Unknown usage field: {key}")
        values = self._usage_values[key]
        if not values:
            return None
        total = sum(values)
        if key != "cost_usd" and total.is_integer():
            return int(total)
        return total

    def usage_totals(self) -> Dict[str, Optional[float]]:
        """Return flat totals with ``None`` for fields never observed."""
        return {key: self.usage_sum(key) for key in USAGE_TOTAL_KEYS}

    def total_turns(self, *, fallback: Optional[int] = None) -> Optional[int]:
        """Return summed turns, or ``fallback`` when no turn count was added."""
        if not self._turns:
            return fallback
        return sum(self._turns)
