# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Agent compile: query-time skill selection.

Implements the lookup mechanism from issue #133 (RFC: Agent compile).
At agent entry, ``classify(query, session_ctx)`` produces a ``Scenario``;
``agent_compile`` then looks the scenario up in a ``compile_table`` and
returns the skill subset the agent is allowed to call this turn.

The classifier is deterministic by design. LLM-driven scenario
classification was explicitly de-scoped in the v2 RFC (open question 3):
start with rules, revisit once Phase 2 data is in. Two dimensions are
runtime-computable without GT access:

* ``language`` — taken from ``SessionContext.primary_language`` (which
  upstream loaders fill from the dataset row or repo metadata).
* ``has_stacktrace`` — regex sweep over the query text.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Union

from ..compiler.params import SessionContext
from ..languages import (
    agent_language_aliases,
    normalize_agent_language,
    supported_agent_languages,
)

logger = logging.getLogger(__name__)


SUPPORTED_LANGUAGES: FrozenSet[str] = supported_agent_languages()


# Stack-trace regexes. Each pattern matches a *structural* marker that only
# appears in a real captured trace — not in user prose mentioning the word
# "traceback" or "panic". Patterns are anchored where the runtime emits them
# (line start) and use ``re.MULTILINE`` because problem_statement bodies
# typically embed traces inline.
_STACKTRACE_PATTERNS = (
    # Python — exact runtime banner
    re.compile(r"Traceback \(most recent call last\):"),
    # Rust — panic header
    re.compile(r"thread '[^']+' panicked at", re.IGNORECASE),
    re.compile(r"\bpanicked at\b 'try '", re.IGNORECASE),  # older rust style
    # Go — runtime panic line + a goroutine frame
    re.compile(r"^panic: ", re.MULTILINE),
    re.compile(r"^goroutine \d+ \[", re.MULTILINE),
    # JS / TS / Node — stack frames "    at name (file.js:42[:col])"
    re.compile(
        r"^\s+at\s+\S+\s*\([^()\n]+:\d+(?::\d+)?\)\s*$",
        re.MULTILINE,
    ),
    # JVM — "\tat com.example.Class.method(File.java:42)"
    re.compile(r"^\s+at\s+\S+\.\S+\([^()\n]+:\d+\)\s*$", re.MULTILINE),
    # C/C++ — addr2line / backtrace lines look like "#0 0x... in fn at file:42"
    re.compile(r"^#\d+\s+0x[0-9a-fA-F]+\s+in\s+\S+", re.MULTILINE),
)


def detect_stacktrace(query: str) -> bool:
    """Return True if the query embeds a recognisable runtime stack trace."""
    if not query:
        return False
    return any(p.search(query) for p in _STACKTRACE_PATTERNS)


_LANGUAGE_ALIASES: Dict[str, str] = agent_language_aliases()


def normalize_language(raw: Optional[str]) -> Optional[str]:
    """Map a dataset / repo-meta language string to a canonical key.

    Returns ``None`` for unknown or empty inputs so callers can branch on
    "no language signal" without sentinel strings.
    """
    if not raw:
        return None
    return normalize_agent_language(raw)


@dataclass(frozen=True, slots=True)
class Scenario:
    """A (query, repo) bucket used as a compile-table lookup key.

    Two-dimensional per RFC v2 — ``query_length`` and ``query_concreteness``
    from v1 were dropped to keep the eval cells statistically meaningful.
    """

    language: Optional[str]
    has_stacktrace: bool

    def key(self) -> str:
        """Stable string key for ``compile_table.json`` lookup."""
        lang = self.language or "unknown"
        flag = "stacktrace" if self.has_stacktrace else "no_stacktrace"
        return f"{lang}:{flag}"

    @classmethod
    def from_key(cls, key: str) -> "Scenario":
        lang, flag = key.split(":", 1)
        return cls(
            language=None if lang == "unknown" else lang,
            has_stacktrace=(flag == "stacktrace"),
        )


def classify(query: str, session_ctx: Optional[SessionContext]) -> Scenario:
    """Compute the scenario for a single (query, repo) pair."""
    lang: Optional[str] = None
    if session_ctx is not None:
        lang = normalize_language(session_ctx.primary_language)
    return Scenario(language=lang, has_stacktrace=detect_stacktrace(query))


CompileTable = Mapping[str, Iterable[str]]


def load_compile_table(path: Union[str, Path]) -> Dict[str, FrozenSet[str]]:
    """Load a ``compile_table`` file as a ``{scenario_key: frozenset}`` map.

    Supports JSON (``.json``) and YAML (``.yaml`` / ``.yml``). The on-disk
    format is a mapping whose keys are the strings returned by
    :meth:`Scenario.key` and whose values are lists of skill IDs.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    with p.open("r", encoding="utf-8") as f:
        if suffix in {".yaml", ".yml"}:
            import yaml  # local import — PyYAML is already a project dep

            raw = yaml.safe_load(f)
        elif suffix == ".json":
            raw = json.load(f)
        else:
            raise ValueError(
                f"compile_table at {p}: unsupported extension {suffix!r}; "
                "use .json, .yaml, or .yml"
            )
    if not isinstance(raw, dict):
        raise ValueError(
            f"compile_table at {p} must be a mapping, got {type(raw).__name__}"
        )
    out: Dict[str, FrozenSet[str]] = {}
    for k, v in raw.items():
        if not isinstance(v, (list, tuple)):
            raise ValueError(
                f"compile_table[{k!r}] must be a list of skill IDs, "
                f"got {type(v).__name__}"
            )
        out[k] = frozenset(str(s) for s in v)
    return out


def agent_compile(
    query: str,
    session_ctx: Optional[SessionContext],
    table: Optional[CompileTable] = None,
) -> Optional[FrozenSet[str]]:
    """Resolve which skills the agent should be allowed to invoke.

    Args:
        query: The incoming user query / problem statement.
        session_ctx: Repo-side context (language, repo size, ...). May be ``None``.
        table: Mapping from scenario key to skill-ID iterable. ``None`` or empty
            means "no restriction" — caller should expose the full registry.

    Returns:
        A ``frozenset`` of skill IDs to allow, or ``None`` to signal "no
        restriction" (caller falls back to its default, typically A6 = full
        registry). This matches ``AgentRunner.allow_skills`` semantics where
        ``None`` means "no filter".
    """
    if not table:
        return None
    scenario = classify(query, session_ctx)
    key = scenario.key()
    if key in table:
        return frozenset(table[key])
    logger.warning(
        "agent_compile: scenario %r not in compile_table (size=%d); "
        "falling back to full registry",
        key,
        len(table),
    )
    return None


__all__ = [
    "Scenario",
    "CompileTable",
    "SUPPORTED_LANGUAGES",
    "classify",
    "detect_stacktrace",
    "normalize_language",
    "load_compile_table",
    "agent_compile",
]
