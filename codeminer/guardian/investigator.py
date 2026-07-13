# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Investigator sub-agent — the inner agent loop for Repository Guardian.

The orchestrator spawns one :func:`run_investigator` call per committed
hypothesis.  It is a true tool-use agent loop (not a fixed pipeline): the
sub-agent chooses which probe to run next based on what prior probes returned,
and stops when it reaches a conclusive verdict or the per-cycle token budget
is exhausted.

Tool dispatch:
  - ``retrieve_evidence``    — query the retrieval pipeline for relevant spans
  - ``run_existing_test``    — run an existing pytest pattern in the sandbox
  - ``synthesize_test``      — write a new targeted test that exposes the risk
  - ``run_synthesized_test`` — execute the synthesized test in the sandbox
  - ``fix_probe``            — minimal reversal of the suspected cause; check
                               that the synthesized test flips to green

Corroboration policy (enforced by system prompt; verified by probes.py in
Hour 2): a single red synthesized test is NOT sufficient to confirm a
hypothesis — a differential run (PASS→FAIL across snapshots) or a fix-probe
(FAIL→PASS on a minimal revert) is required before marking "confirmed".

``SandboxHandle`` and ``WorktreeSandbox`` live here so the loop can dispatch
probes without importing the full probes module.  Hour 2 (``probes.py``)
implements the complex probe functions; Hour 3 wires them into the dispatch
table below.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Protocol, Tuple, runtime_checkable

from ..log_utils import get_logger
from .llm_investigator import LLMUsage, _run_search

if TYPE_CHECKING:
    from ..llm.litellm_chat import LiteLLMChat
    from .hypothesize import Hypothesis

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

VALID_VERDICTS = frozenset({"confirmed", "rejected", "inconclusive"})


@dataclass
class ProbeRecord:
    """One probe taken during the investigation loop."""

    tool: str
    input_summary: str
    output_summary: str
    passed: Optional[bool] = None  # None for non-test probes


@dataclass
class InvestigatorResult:
    """Full output of one investigator sub-agent run."""

    verdict: str                              # "confirmed" | "rejected" | "inconclusive"
    reasoning: str                            # one-paragraph summary from the model
    evidence_test: str = ""                   # synthesized test source, or ""
    evidence_diff: str = ""                   # fix-probe diff, or ""
    probe_trace: List[ProbeRecord] = field(default_factory=list)
    tokens_used: int = 0

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reasoning": self.reasoning,
            "evidence_test": self.evidence_test,
            "evidence_diff": self.evidence_diff,
            "probe_trace": [
                {
                    "tool": p.tool,
                    "input_summary": p.input_summary,
                    "output_summary": p.output_summary,
                    "passed": p.passed,
                }
                for p in self.probe_trace
            ],
            "tokens_used": self.tokens_used,
        }


# ---------------------------------------------------------------------------
# Sandbox protocol + WorktreeSandbox
# ---------------------------------------------------------------------------


@runtime_checkable
class SandboxHandle(Protocol):
    """Minimal interface the investigator needs to run things in the sandbox."""

    repo_path: str

    def run_command(
        self, cmd: List[str], *, timeout: int = 60
    ) -> Tuple[int, str]: ...

    def write_file(self, rel_path: str, content: str) -> None: ...

    def read_file(self, rel_path: str) -> str: ...


@dataclass
class WorktreeSandbox:
    """Sandbox backed by a plain git worktree directory on the host.

    The investigator writes test files and runs commands directly in the
    worktree.  This is the ``--sandbox worktree`` debug mode; the container
    sandbox (Hour 5) wraps the same interface.
    """

    repo_path: str

    def run_command(self, cmd: List[str], *, timeout: int = 60) -> Tuple[int, str]:
        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout + "\n" + result.stderr).strip()
            return result.returncode, output
        except subprocess.TimeoutExpired:
            return 1, f"(command timed out after {timeout}s)"
        except Exception as exc:  # noqa: BLE001
            return 1, f"(command error: {exc})"

    def write_file(self, rel_path: str, content: str) -> None:
        full_path = os.path.join(self.repo_path, rel_path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def read_file(self, rel_path: str) -> str:
        full_path = os.path.join(self.repo_path, rel_path)
        try:
            with open(full_path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI/litellm format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "retrieve_evidence",
            "description": (
                "Search the codebase for code spans relevant to the hypothesis "
                "using BM25 + hybrid retrieval.  Use this first to find callers, "
                "dependents, related tests, and recently changed neighbours."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or identifier query, e.g. a function name.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default 5).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_existing_test",
            "description": (
                "Run an existing pytest test file or node id in the sandbox.  "
                "Use this as a cheap first-pass check before writing a new test."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_pattern": {
                        "type": "string",
                        "description": "pytest path or node id, e.g. 'test/guardian/test_cycle.py'.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds before the test run is killed (default 60).",
                    },
                },
                "required": ["test_pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synthesize_test",
            "description": (
                "Write a new targeted pytest test whose FAILURE reveals the hypothesised risk.  "
                "The test must import and call the real symbol under investigation (not mock it), "
                "assert the old expected behaviour, and be ≤40 lines.  "
                "Returns the synthesized test source.  "
                "Run it next with run_synthesized_test."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "One-sentence description of what the test should demonstrate.",
                    },
                    "target_symbol": {
                        "type": "string",
                        "description": "The import path of the symbol to exercise, e.g. 'codeminer.guardian.cycle.run_cycle'.",
                    },
                },
                "required": ["description", "target_symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_synthesized_test",
            "description": (
                "Write test_source to the sandbox overlay and execute it with pytest.  "
                "A FAIL corroborates the hypothesis; a PASS or collection-error is not evidence.  "
                "After a FAIL, follow up with fix_probe to corroborate before marking 'confirmed'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_source": {
                        "type": "string",
                        "description": "Complete pytest-runnable Python source for the synthesized test.",
                    },
                },
                "required": ["test_source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fix_probe",
            "description": (
                "Apply a minimal diff to the sandbox overlay that reverts the suspected cause, "
                "then re-run the synthesized test.  A FAIL→PASS flip is strong corroboration.  "
                "Required before marking a hypothesis 'confirmed' based on a red synthesized test."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "diff": {
                        "type": "string",
                        "description": "Unified diff (patch format) to apply to the overlay before re-running.",
                    },
                    "test_source": {
                        "type": "string",
                        "description": "The synthesized test source to run after applying the patch.",
                    },
                },
                "required": ["diff", "test_source"],
            },
        },
    },
]

_TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are an investigator sub-agent for Repository Guardian.  You have been
spawned by the orchestrator to investigate ONE specific hypothesis about a
potential risk in the codebase.

Your job: gather evidence using the available tools, then return a verdict.

Probe order guidance (not mandatory — follow the evidence):
1. Start with retrieve_evidence to understand what code is involved.
2. Run an existing test that covers the symbol (run_existing_test) as a cheap check.
3. If existing tests are inconclusive, synthesize a targeted test (synthesize_test)
   then run it (run_synthesized_test).
4. If the synthesized test fails, corroborate with fix_probe before confirming.

Corroboration policy — you MUST follow this:
- A single red synthesized test is NOT sufficient to confirm.  You need at least
  one of:
    (a) fix_probe: the minimal reversal makes the test PASS (FAIL→PASS flip).
    (b) An existing test that also fails (run_existing_test → FAIL).
- A collection error (rc=2, import failure) from a synthesized test is INVALID —
  the test itself is broken, not the code under investigation.
- "rejected" means the evidence actively contradicts the hypothesis.
- "inconclusive" means evidence is unclear or budget ran out.

End your final response with EXACTLY one of these lines (no extra punctuation):
verdict: confirmed
verdict: rejected
verdict: inconclusive

Follow that line with a brief reasoning paragraph (2–5 sentences).
"""

# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


def _parse_verdict(text: str) -> Tuple[str, str]:
    """Extract (verdict, reasoning) from the model's final response.

    Returns ``("inconclusive", text)`` if no verdict line is found.
    """
    verdict = "inconclusive"
    reasoning_lines: List[str] = []
    found = False
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("verdict:"):
            candidate = stripped[len("verdict:"):].strip()
            if candidate in VALID_VERDICTS:
                verdict = candidate
                found = True
            continue
        reasoning_lines.append(line)

    if not found:
        logger.debug("investigator: no verdict line found; defaulting to inconclusive")

    reasoning = "\n".join(reasoning_lines).strip()
    return verdict, reasoning


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _dispatch_retrieve(args: dict, retriever: object, repo_path: str) -> Tuple[str, ProbeRecord]:
    query = args.get("query", "")
    top_k = int(args.get("top_k", 5))
    result = _run_search(query, retriever, top_k, repo_path)
    summary = f"retrieve_evidence({query!r}, top_k={top_k})"
    output_summary = result[:200] if result else "(no results)"
    return result, ProbeRecord(
        tool="retrieve_evidence",
        input_summary=summary,
        output_summary=output_summary,
    )


def _dispatch_run_existing(args: dict, sandbox: SandboxHandle) -> Tuple[str, ProbeRecord]:
    pattern = args.get("test_pattern", "")
    timeout = int(args.get("timeout", 60))
    rc, output = sandbox.run_command(
        ["python", "-m", "pytest", pattern, "-x", "--tb=short", "-q"],
        timeout=timeout,
    )
    passed = rc == 0
    label = "PASS" if passed else ("INVALID" if rc == 2 else "FAIL")
    result_text = f"{label}\n{output[-1500:]}"
    return result_text, ProbeRecord(
        tool="run_existing_test",
        input_summary=f"run_existing_test({pattern!r})",
        output_summary=f"{label}: {output[:120]}",
        passed=passed if rc != 2 else None,
    )


def _dispatch_stub(tool_name: str, args: dict) -> Tuple[str, ProbeRecord]:
    """Placeholder for probes not yet implemented (filled in by probes.py Hour 2)."""
    msg = (
        f"(tool '{tool_name}' is not yet implemented in this build; "
        "install codeminer/guardian/probes.py to enable it)"
    )
    return msg, ProbeRecord(
        tool=tool_name,
        input_summary=str(args)[:120],
        output_summary=msg,
    )


def _dispatch_tool(
    tool_name: str,
    args: dict,
    sandbox: SandboxHandle,
    retriever: object,
    repo_path: str,
    *,
    synth_test_store: List[str],
) -> Tuple[str, ProbeRecord]:
    """Execute one tool call; return (observation_text, ProbeRecord).

    ``synth_test_store`` is a mutable list used to carry the most recently
    synthesized test source across tool calls so ``run_synthesized_test`` and
    ``fix_probe`` can access it even if the LLM omits the source on repeat calls.
    """
    if tool_name == "retrieve_evidence":
        return _dispatch_retrieve(args, retriever, repo_path)

    if tool_name == "run_existing_test":
        return _dispatch_run_existing(args, sandbox)

    # Advanced probes: delegate to probes.py when available, else stub.
    try:
        from .probes import dispatch_advanced_probe  # type: ignore[import]
        return dispatch_advanced_probe(
            tool_name, args, sandbox, synth_test_store=synth_test_store
        )
    except ImportError:
        return _dispatch_stub(tool_name, args)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_investigator(
    hypothesis: "Hypothesis",
    llm: "LiteLLMChat",
    retriever: object,
    sandbox: SandboxHandle,
    *,
    budget_tokens: int = 20_000,
    max_rounds: int = 8,
    usage_acc: Optional[LLMUsage] = None,
) -> InvestigatorResult:
    """Inner agent loop: probe → observe → decide until verdict or budget.

    The orchestrator calls this once per committed hypothesis.  The sub-agent
    runs its own tool-use loop with *its own* message context — independent of
    the orchestrator's context — and returns an :class:`InvestigatorResult`.

    Args:
        hypothesis: The :class:`~codeminer.guardian.hypothesize.Hypothesis` to
            investigate.
        llm: A :class:`~codeminer.llm.litellm_chat.LiteLLMChat` instance.
        retriever: Duck-typed retrieval pipeline with ``.query(str, top_k=int)``.
        sandbox: A :class:`SandboxHandle` for running commands and writing files.
        budget_tokens: Stop issuing new probes when this token count is reached.
        max_rounds: Hard cap on tool-use rounds regardless of budget.
        usage_acc: Optional shared :class:`LLMUsage` accumulator for the cycle.

    Returns:
        :class:`InvestigatorResult` with verdict, reasoning, and probe trace.
    """
    repo_path = getattr(sandbox, "repo_path", "")

    # Check budget before spending any tokens.
    if usage_acc is not None and usage_acc.total_tokens >= budget_tokens:
        logger.info(
            "investigator: budget already exhausted (%d >= %d); skipping %s",
            usage_acc.total_tokens, budget_tokens, hypothesis.target,
        )
        return InvestigatorResult(
            verdict="inconclusive",
            reasoning="Budget exhausted before investigation could begin.",
        )

    messages: List[dict] = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": (
                f"Hypothesis (rank {hypothesis.rank}, confidence {hypothesis.confidence:.2f}):\n"
                f"{hypothesis.statement}\n\n"
                f"Target: {hypothesis.target}\n"
                f"Kind: {hypothesis.kind}\n"
            ),
        },
    ]

    probe_trace: List[ProbeRecord] = []
    synth_test_store: List[str] = []  # mutable carry for synthesized test source
    local_usage = LLMUsage()

    for round_idx in range(max_rounds + 1):
        # Budget check at the start of every round.
        combined_tokens = (usage_acc.total_tokens if usage_acc else 0) + local_usage.total_tokens
        if combined_tokens >= budget_tokens:
            logger.info(
                "investigator: budget reached at round %d (%d tokens); stopping",
                round_idx, combined_tokens,
            )
            break

        use_tools = round_idx < max_rounds
        kwargs: dict = {}
        if use_tools:
            kwargs["tools"] = TOOLS
            kwargs["tool_choice"] = "auto"

        try:
            response = llm._call_raw(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("investigator: LLM call failed (round %d): %s", round_idx, exc)
            return InvestigatorResult(
                verdict="inconclusive",
                reasoning=f"LLM unavailable: {exc}",
                probe_trace=probe_trace,
                tokens_used=local_usage.total_tokens,
            )

        local_usage.add(response)
        if usage_acc is not None:
            usage_acc.add(response)

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            # Model produced a final answer — parse the verdict.
            final_text = (msg.content or "").strip()
            logger.debug(
                "investigator: round=%d final answer: %s", round_idx, final_text[:200]
            )
            verdict, reasoning = _parse_verdict(final_text)
            return InvestigatorResult(
                verdict=verdict,
                reasoning=reasoning,
                evidence_test=synth_test_store[-1] if synth_test_store else "",
                probe_trace=probe_trace,
                tokens_used=local_usage.total_tokens,
            )

        # Append the assistant turn with tool calls.
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        # Execute each tool call and append results.
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            if name not in _TOOL_NAMES:
                obs = f"(unknown tool: {name!r})"
                record = ProbeRecord(
                    tool=name,
                    input_summary=str(args)[:120],
                    output_summary=obs,
                )
            else:
                obs, record = _dispatch_tool(
                    name, args, sandbox, retriever, repo_path,
                    synth_test_store=synth_test_store,
                )
                # Track synthesized test source for evidence_test field.
                if name == "synthesize_test" and obs and not obs.startswith("("):
                    synth_test_store.append(obs)

            probe_trace.append(record)
            logger.debug("investigator: tool=%s obs=%s", name, obs[:120])
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": obs})

    # Budget or max-rounds exhausted — force a final answer.
    logger.info("investigator: forcing final answer after %d rounds", max_rounds)
    try:
        response = llm._call_raw(messages)
        local_usage.add(response)
        if usage_acc is not None:
            usage_acc.add(response)
        final_text = (response.choices[0].message.content or "").strip()
        verdict, reasoning = _parse_verdict(final_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("investigator: forced final call failed: %s", exc)
        verdict, reasoning = "inconclusive", f"Budget exhausted; forced call failed: {exc}"

    return InvestigatorResult(
        verdict=verdict,
        reasoning=reasoning,
        evidence_test=synth_test_store[-1] if synth_test_store else "",
        probe_trace=probe_trace,
        tokens_used=local_usage.total_tokens,
    )
