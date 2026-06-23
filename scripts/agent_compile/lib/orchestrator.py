# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Scatter-isolate-converge orchestration for pre-load candidates.

Why this exists (see ``.claude/design/preload-aware-harness.md``): injecting a
noisy candidate set into a SINGLE agent context pollutes a strong agent's
exploration — on synthesis ``behavioral`` queries Qwen3.5-27B searched a lot
(turns=16, GT even present in the candidates) yet still answered wrong, while a
blank-slate grep answered cleanly. The fix is context isolation, not prompt
wording.

This module spreads the candidates over isolated *verify-subagents* (each judges
ONE candidate in its own context), then the orchestrator converges only their
structured verdicts:

    scatter   one minimal verify-subagent per candidate (isolated context)
    isolate   each subagent's dead-end reading stays in its own context
    converge  >=1 yes -> synthesize from confirmed locations
              all  no -> blank-slate explorer (the clean grep_only path)

Subagents are executed via a caller-supplied ``run_subagent`` closure so their
token/turn usage accrues to the cell's aggregate (cost axis stays honest). v1 is
serial — prove the accuracy claim (behavioral REGRESS disappears) before paying
for concurrency.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from codeminer.agent.agent_types import AgentResult

# Adaptive routing gate (one cheap LLM call). The discriminator is NOT
# "has an identifier" — that sends traversal queries (no identifier, but
# candidates ON THE CALL CHAIN are useful) wrongly to grep. The right axis,
# from the measured per-category candidate-value (behavioral: grep>preload;
# traversal/symbol/file: preload>=grep), is: route to GREP only a SINGLE-FEATURE
# behavioral symptom (edit site is one non-obvious spot retrieval can't pinpoint);
# route to EAGER anything that names an identifier OR traces a cross-component
# flow. Verified on n=400 multilingual: behavioral 73% GREP, traversal 88% EAGER,
# symbol/file/reasoning ~100% EAGER.
GATE_SYSTEM = (
    "Route a code-localization query. Reply with EXACTLY one word: EAGER or "
    "GREP.\n"
    "GREP = the query only describes ONE feature's buggy behavior/symptom in "
    "plain English, with no code identifier AND no cross-component flow to follow "
    "(the edit site is a single non-obvious spot).\n"
    "EAGER = the query EITHER names a concrete code identifier "
    "(function/method/class/file), OR traces a flow across multiple "
    "components/steps (chains, merges, consolidates, 'X before Y', a call path) "
    "— cases where ranked retrieval candidates help.\n"
    "Answer EAGER or GREP."
)


def query_is_specific(call_llm, query: str) -> bool:
    """One cheap LLM call: should this query use eager pre-load (vs blank grep)?

    ``call_llm(messages) -> content``. True (EAGER) -> use eager pre-load;
    False (GREP) -> blank-slate grep (wins on single-feature behavioral queries).
    Defaults to True (eager, the cheaper path) on failure / ambiguity.
    """
    # NOTE: the caller's ``call_llm`` MUST disable Qwen3.5 chain-of-thought
    # (extra_body chat_template_kwargs enable_thinking=False); otherwise the
    # one-word verdict is buried under a truncated "Thinking Process…" and every
    # query reads as EAGER. Parse the LAST verdict token; EAGER wins ties/absence.
    msgs = [
        {"role": "system", "content": GATE_SYSTEM},
        {"role": "user", "content": (query or "")[:2000]},
    ]
    try:
        out = (call_llm(msgs) or "").upper()
    except Exception:  # noqa: BLE001 — gate is best-effort; default to eager
        return True
    return out.rfind("GREP") <= out.rfind("EAGER")


# A verify-subagent judges ONE candidate in an isolated context. It is
# deliberately skeptical so a wrong candidate is ruled out rather than
# rationalized (the failure mode that caused the behavioral REGRESS).
VERIFY_SYSTEM_PROMPT = """\
You verify ONE candidate code location for a localization task.

You are given the task and a single candidate location. Read that location and \
only the code directly related to it (its definition, callers, callees). Decide \
whether THIS location is part of the code that must change to address the task.

Be skeptical — a retriever guessed this candidate and it may be irrelevant. Do \
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


def _verdict_is_yes(answer: str) -> bool:
    """True iff the LAST ``VERDICT:`` line in the answer says yes."""
    verdicts = _VERDICT_RE.findall(answer or "")
    return bool(verdicts) and verdicts[-1].lower() == "yes"


def _cand_loc(cand: Dict[str, Any]) -> str:
    return f"{cand['file']}:{cand.get('start')}-{cand.get('end')}"


def _verify_query(query: str, cand: Dict[str, Any], snippet: str) -> str:
    snip = f"\n{snippet}" if snippet else ""
    return (
        f"Task:\n{query}\n\n"
        f"Candidate location to verify:\n{_cand_loc(cand)}{snip}\n\n"
        "Read it and decide whether this is the code to change. "
        "End with VERDICT: yes/no (+ Files/Symbols/Locations if yes)."
    )


def _converge_query(query: str, confirmed: List[Dict[str, Any]]) -> str:
    locs = "\n".join(f"- {_cand_loc(c)}" for c in confirmed)
    return (
        f"Task:\n{query}\n\n"
        f"These candidate locations were independently CONFIRMED relevant:\n{locs}\n\n"
        "Give the FINAL localization answer (Files:/Symbols:/Locations:) for the "
        "exact edit site(s). You may read them to pin precise line ranges, but do "
        "not re-explore the repository widely."
    )


def _merge(final: AgentResult, all_results: List[AgentResult]) -> AgentResult:
    """Final answer + merged tool-calls across every subagent (for scoring)."""
    tool_calls = []
    for r in all_results:
        tool_calls.extend(r.tool_calls or [])
    return AgentResult(
        answer=final.answer or "",
        tool_calls=tool_calls,
        messages=final.messages,
        total_turns=sum(r.total_turns or 0 for r in all_results),
        total_duration_ms=sum(r.total_duration_ms or 0.0 for r in all_results),
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
    """Scatter verify-subagents over candidates, converge to a final answer.

    ``run_subagent(system_prompt, query, max_turns) -> AgentResult`` is supplied
    by the caller (it owns chdir + usage/turn accrual). ``system_prompt=None``
    means the default localization prompt (used for converge / blank-slate
    explore). Returns one merged :class:`AgentResult` (final answer + all
    subagents' tool-calls) so the existing scorer works unchanged.
    """
    snippets = snippets or {}
    cands = list(candidates or [])[:max_candidates]

    # No candidates -> just explore (degenerates to grep_only).
    if not cands:
        return run_subagent(None, query, explore_turns)

    results: List[AgentResult] = []
    confirmed: List[Dict[str, Any]] = []
    for cand in cands:
        snip = snippets.get((cand["file"], cand.get("start"), cand.get("end")), "")
        r = run_subagent(
            VERIFY_SYSTEM_PROMPT, _verify_query(query, cand, snip), sub_turns
        )
        results.append(r)
        if _verdict_is_yes(r.answer or ""):
            confirmed.append(cand)

    if confirmed:
        final = run_subagent(None, _converge_query(query, confirmed), 2)
    else:
        # Every candidate was noise — fall back to the clean blank-slate path.
        final = run_subagent(None, query, explore_turns)
    results.append(final)
    return _merge(final, results)
