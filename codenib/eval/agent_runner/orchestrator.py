# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Scatter-isolate-converge orchestration for candidate evaluation."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from codenib.agent.agent_types import AgentResult

# Adaptive routing gate (one cheap LLM call). Route to blank exploration only
# when the query is a single-feature behavioral symptom with no identifier or
# cross-component flow; otherwise ranked candidates are likely useful context.
GATE_SYSTEM = (
    "Route a code-localization query. Reply with EXACTLY one word: EAGER or "
    "GREP.\n"
    "GREP = the query only describes ONE feature's buggy behavior/symptom in "
    "plain English, with no code identifier AND no cross-component flow to follow "
    "(the edit site is a single non-obvious spot).\n"
    "EAGER = the query EITHER names a concrete code identifier "
    "(function/method/class/file), OR traces a flow across multiple "
    "components/steps (chains, merges, consolidates, 'X before Y', a call path) "
    "- cases where ranked retrieval candidates help.\n"
    "Answer EAGER or GREP."
)


def query_is_specific(call_llm, query: str) -> bool:
    """Return true when a query should use eager pre-load.

    ``call_llm(messages) -> content``. True (EAGER) means use eager pre-load;
    False (GREP) means blank exploration. Defaults to eager on failure or
    ambiguous output so the optional gate cannot disable retrieval accidentally.
    """
    msgs = [
        {"role": "system", "content": GATE_SYSTEM},
        {"role": "user", "content": (query or "")[:2000]},
    ]
    try:
        out = (call_llm(msgs) or "").upper()
    except Exception:  # noqa: BLE001 - gate is best-effort; default to eager
        return True
    return out.rfind("GREP") <= out.rfind("EAGER")


# A verify-subagent judges ONE candidate in an isolated context. It is
# deliberately skeptical so a wrong candidate is ruled out rather than
# rationalized.
VERIFY_SYSTEM_PROMPT = """\
You verify ONE candidate code location for a localization task.

You are given the task and a single candidate location. Read that location and \
only the code directly related to it (its definition, callers, callees). Decide \
whether THIS location is part of the code that must change to address the task.

Be skeptical - a retriever guessed this candidate and it may be irrelevant. Do \
NOT rationalize a weak match; if it is not clearly the edit site, say no.

End your reply with exactly one line:
VERDICT: yes
or
VERDICT: no

If yes, also include the exact edit site, repo-relative:
Files: <path>
Symbols: <file:symbol>
Locations: <file:start-end>
"""

_VERDICT_RE = re.compile(r"(?im)^\s*[*_`> ]*VERDICT[*_`: ]*\s*(yes|no)\b")


def verdict_is_yes(answer: str) -> bool:
    """Return true iff the last ``VERDICT:`` line says yes."""
    verdicts = _VERDICT_RE.findall(answer or "")
    return bool(verdicts) and verdicts[-1].lower() == "yes"


def candidate_location(candidate: Dict[str, Any]) -> str:
    """Return the compact ``file:start-end`` display for a candidate."""
    return f"{candidate['file']}:{candidate.get('start')}-{candidate.get('end')}"


def verify_query(query: str, candidate: Dict[str, Any], snippet: str) -> str:
    """Build the isolated verifier prompt for one candidate."""
    snip = f"\n{snippet}" if snippet else ""
    return (
        f"Task:\n{query}\n\n"
        f"Candidate location to verify:\n{candidate_location(candidate)}{snip}\n\n"
        "Read it and decide whether this is the code to change. "
        "End with VERDICT: yes/no (+ Files/Symbols/Locations if yes)."
    )


def converge_query(query: str, confirmed: List[Dict[str, Any]]) -> str:
    """Build the final convergence prompt from confirmed candidates."""
    locs = "\n".join(f"- {candidate_location(candidate)}" for candidate in confirmed)
    return (
        f"Task:\n{query}\n\n"
        f"These candidate locations were independently CONFIRMED relevant:\n{locs}\n\n"
        "Give the FINAL localization answer (Files:/Symbols:/Locations:) for the "
        "exact edit site(s). You may read them to pin precise line ranges, but do "
        "not re-explore the repository widely."
    )


def _merge(final: AgentResult, all_results: List[AgentResult]) -> AgentResult:
    """Return final answer plus merged tool calls across subagents."""
    tool_calls = []
    for result in all_results:
        tool_calls.extend(result.tool_calls or [])
    return AgentResult(
        answer=final.answer or "",
        tool_calls=tool_calls,
        messages=final.messages,
        total_turns=sum(result.total_turns or 0 for result in all_results),
        total_duration_ms=sum(
            result.total_duration_ms or 0.0 for result in all_results
        ),
        usage=final.usage,
        usage_records=final.usage_records,
    )


def scatter_gather_localize(
    query: str,
    candidates: List[Dict[str, Any]],
    run_subagent: Callable[[Optional[str], str, int], AgentResult],
    *,
    snippets: Optional[Dict[Tuple[str, Any, Any], str]] = None,
    max_candidates: int = 6,
    sub_turns: int = 5,
    explore_turns: int = 12,
) -> AgentResult:
    """Verify candidate locations in isolation, then converge or explore.

    ``run_subagent(system_prompt, query, max_turns) -> AgentResult`` is supplied
    by the caller, which owns cwd scoping and accounting. ``system_prompt=None``
    means the caller's default localization prompt. The returned
    :class:`AgentResult` keeps the final answer while preserving tool-call audit
    data from every subagent run.
    """
    snippets = snippets or {}
    selected = list(candidates or [])[:max_candidates]

    if not selected:
        return run_subagent(None, query, explore_turns)

    results: List[AgentResult] = []
    confirmed: List[Dict[str, Any]] = []
    for candidate in selected:
        snippet = snippets.get(
            (candidate["file"], candidate.get("start"), candidate.get("end")),
            "",
        )
        result = run_subagent(
            VERIFY_SYSTEM_PROMPT,
            verify_query(query, candidate, snippet),
            sub_turns,
        )
        results.append(result)
        if verdict_is_yes(result.answer or ""):
            confirmed.append(candidate)

    if confirmed:
        final = run_subagent(None, converge_query(query, confirmed), 2)
    else:
        final = run_subagent(None, query, explore_turns)
    results.append(final)
    return _merge(final, results)
