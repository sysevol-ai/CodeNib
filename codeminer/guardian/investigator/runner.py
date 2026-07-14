# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Investigator runner: inner agent loop + LLM investigation utilities.

Merges the content of the former codeminer.guardian.investigator (minus
SandboxHandle/WorktreeSandbox, which moved to sandbox.py) with the former
codeminer.guardian.llm_investigator.

Public entry points:
* :func:`run_investigator` — inner tool-use agent loop (probe → observe → decide).
* :func:`investigate_signal` — generic LLM agentic loop for any signal type.
* :func:`build_test_failure_context` — build the user-message for a failing test.
* :class:`LLMUsage` — token usage accumulator.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Tuple

from ...log_utils import get_logger
from .sandbox import SandboxHandle

if TYPE_CHECKING:
    from ...llm.litellm_chat import LiteLLMChat
    from ..orchestrator import Hypothesis

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Token usage accumulator  (from llm_investigator.py)
# ---------------------------------------------------------------------------


@dataclass
class LLMUsage:
    """Token counts accumulated across all LLM calls in one Guardian cycle."""

    prompt_tokens: int = field(default=0)
    completion_tokens: int = field(default=0)
    total_tokens: int = field(default=0)

    def add(self, response: object) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        self.total_tokens += getattr(usage, "total_tokens", 0) or 0


# ---------------------------------------------------------------------------
# Tool schema — search_code (from llm_investigator.py)
# ---------------------------------------------------------------------------

_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_code",
        "description": (
            "Search the codebase for code relevant to a query using BM25 "
            "keyword matching. Returns matching symbol names and file locations. "
            "Use this to find callers, related modules, or tests in other files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keyword or identifier query — e.g. a function name you "
                        "want to find callers of, or a concept like 'language registry'."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}

# ---------------------------------------------------------------------------
# System prompts  (from llm_investigator.py)
# ---------------------------------------------------------------------------

_TEST_FAILURE_SYSTEM_PROMPT = (
    "You are Repository Guardian, a proactive code-health monitor. "
    "You receive details about a failing test. "
    "Your job is to trace the root cause through the codebase before concluding. "
    "\n\n"
    "Investigate iteratively using search_code:\n"
    "- Search for the failing symbol or assertion to find the implementation.\n"
    "- Follow leads: search for related helpers, fixtures, or recently changed code.\n"
    "- Keep searching until you understand *why* the test fails, not just *where*.\n"
    "- Only stop and write your final answer when you have traced the root cause.\n"
    "\n"
    "Final answer format (plain text, no JSON or Markdown):\n"
    "(1) Root cause of the failure, traced through code.\n"
    "(2) The specific failing assertion or symbol, with evidence from your searches.\n"
    "(3) A concrete fix recommendation."
)

# ---------------------------------------------------------------------------
# File content utilities  (from llm_investigator.py)
# ---------------------------------------------------------------------------

FILE_CONTENT_MAX_LINES = 150
_CONTENT_MAX_LINES = 50


def _node_content(n: object, repo_path: str) -> str:
    """Return the source snippet for a result node.

    Uses the pre-embedded ``content`` field when available; falls back to
    reading lines ``start_line``..``end_line`` (0-based) from disk.
    """
    content = getattr(n, "content", None)
    if content:
        return content.strip()

    file = getattr(n, "file", None)
    start = getattr(n, "start_line", None)
    end = getattr(n, "end_line", None)
    if not file or start is None:
        return ""

    full_path = (
        os.path.join(repo_path, file)
        if repo_path and not os.path.isabs(file)
        else file
    )
    try:
        with open(full_path, encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        sl = max(0, start)
        el = min(len(all_lines), (end if end is not None else start) + 1)
        snippet = all_lines[sl:el]
        if len(snippet) > _CONTENT_MAX_LINES:
            snippet = snippet[:_CONTENT_MAX_LINES] + [
                f"... ({len(all_lines[sl:el]) - _CONTENT_MAX_LINES} more lines)\n"
            ]
        return "".join(snippet).strip()
    except OSError:
        return ""


def _run_search(query: str, retriever: object, top_k: int, repo_path: str = "") -> str:
    try:
        nodes = retriever.query(query, top_k=top_k)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_investigator search failed: %s", exc)
        return f"(search error: {exc})"

    parts: List[str] = []
    for n in nodes or []:
        file = getattr(n, "file", "?") or "?"
        name = getattr(n, "node_name", "?") or "?"
        typ = getattr(n, "type", "?") or "?"
        line = getattr(n, "start_line", None)
        score = getattr(n, "score", None)
        loc = f"{file}:{line}" if line is not None else file
        score_str = f"{score:.3f}" if isinstance(score, float) else "—"
        header = f"{loc} | {name} ({typ}) | score={score_str}"
        snippet = _node_content(n, repo_path)
        if snippet:
            parts.append(f"{header}\n```\n{snippet}\n```")
        else:
            parts.append(header)
    return "\n\n".join(parts) if parts else "(no results)"


# ---------------------------------------------------------------------------
# LLM agentic loop utilities  (from llm_investigator.py)
# ---------------------------------------------------------------------------


def _format_messages(messages: List[dict]) -> str:
    """Render a messages list as readable plain text for debug logging."""
    parts: List[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")

        if role == "tool":
            parts.append(f"[tool_result: {tool_call_id}]\n{content}")
        elif tool_calls:
            tc_lines = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                tc_lines.append(f"  {fn.get('name')}  {fn.get('arguments')}")
            parts.append(f"[{role} → tool_calls]\n" + "\n".join(tc_lines))
        else:
            parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def _log_usage(response: object, label: str) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    logger.debug(
        "agentic_loop: %s usage: prompt=%d completion=%d total=%d",
        label,
        getattr(usage, "prompt_tokens", 0) or 0,
        getattr(usage, "completion_tokens", 0) or 0,
        getattr(usage, "total_tokens", 0) or 0,
    )


def _agentic_loop(
    messages: List[dict],
    retriever: object,
    *,
    llm: "LiteLLMChat",
    max_tool_rounds: int,
    top_k: int,
    usage_acc: Optional[LLMUsage] = None,
    repo_path: str = "",
) -> str:
    """Run the tool-use loop until the model produces a final answer."""
    for round_idx in range(max_tool_rounds + 1):
        use_tools = round_idx < max_tool_rounds
        kwargs: dict = {}
        if use_tools:
            kwargs["tools"] = [_SEARCH_TOOL]
            kwargs["tool_choice"] = "auto"

        logger.debug(
            "agentic_loop: round=%d calling LLM with %d message(s):\n%s",
            round_idx,
            len(messages),
            _format_messages(messages),
        )

        try:
            response = llm._call_raw(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "llm_investigator: LLM call failed (round %d): %s", round_idx, exc
            )
            return f"(LLM investigation unavailable: {exc})"

        _log_usage(response, f"round={round_idx}")
        if usage_acc is not None:
            usage_acc.add(response)

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            logger.debug(
                "agentic_loop: round=%d final_answer:\n%s",
                round_idx,
                msg.content or "",
            )
            return (msg.content or "").strip()

        logger.debug(
            "agentic_loop: round=%d tool_calls:\n%s",
            round_idx,
            json.dumps(
                [
                    {
                        "id": tc.id,
                        "function": tc.function.name,
                        "args": tc.function.arguments,
                    }
                    for tc in tool_calls
                ],
                indent=2,
            ),
        )
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
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            query = args.get("query", "")
            result = _run_search(query, retriever, top_k, repo_path)
            logger.debug(
                "agentic_loop: tool_result query=%r:\n%s", query, result
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    try:
        response = llm._call_raw(messages)
        _log_usage(response, "round=final(forced)")
        if usage_acc is not None:
            usage_acc.add(response)
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_investigator: final call failed: %s", exc)
        return f"(LLM investigation unavailable: {exc})"


def build_test_failure_context(
    nodeid: str,
    error: str,
    *,
    test_content: str = "",
    source_content: str = "",
    source_path: str = "",
) -> str:
    """Build the LLM user-message for a failing test.

    Args:
        nodeid: Pytest node id, e.g. ``"test/guardian/test_cycle.py::test_foo"``.
        error: Failure message / short traceback from pytest output.
        test_content: Full source of the test file (truncated by caller).
        source_content: Full source of the inferred source file (optional).
        source_path: Relative path of the source file (for display).

    Returns:
        Plain-text context string ready to be placed in the ``user`` role.
    """
    lines = [f"Failing test: {nodeid}", ""]
    if error:
        lines += ["Error:", "```", error[:1200].rstrip(), "```", ""]
    if test_content:
        test_file = nodeid.split("::")[0]
        lines += [f"Test source ({test_file}):", "```python", test_content.rstrip(), "```", ""]
    if source_content and source_path:
        lines += [
            f"Source under test ({source_path}):",
            "```python",
            source_content.rstrip(),
            "```",
            "",
        ]
    lines.append(
        "Investigate why this test is failing. Use search_code to find related "
        "implementation code if needed. Explain the root cause and recommend a fix."
    )
    return "\n".join(lines)


def investigate_signal(
    context: str,
    retriever: object,
    *,
    system_prompt: str = _TEST_FAILURE_SYSTEM_PROMPT,
    llm: "LiteLLMChat",
    max_tool_rounds: int = 3,
    top_k: int = 5,
    usage_acc: Optional[LLMUsage] = None,
    repo_path: str = "",
) -> str:
    """Run the LLM agentic loop with a caller-supplied context string.

    Use :func:`build_test_failure_context` (or a custom builder) to construct
    ``context``.  The same ``search_code`` tool is available to the model.

    Args:
        context: Full user-message describing the signal to investigate.
        retriever: Duck-typed retriever with ``.query(str, top_k=int) -> list``.
        system_prompt: Override the default test-failure system prompt.
        llm: A :class:`~codeminer.llm.litellm_chat.LiteLLMChat` instance.
        max_tool_rounds: Max search rounds before forcing a final answer.
        top_k: Results per search query.

    Returns:
        Plain-text narrative from the LLM.
    """
    messages: List[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]
    return _agentic_loop(
        messages,
        retriever,
        llm=llm,
        max_tool_rounds=max_tool_rounds,
        top_k=top_k,
        usage_acc=usage_acc,
        repo_path=repo_path,
    )


# ---------------------------------------------------------------------------
# Investigator result dataclasses  (from investigator.py)
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
# Tool schemas (OpenAI/litellm format) — for the investigator loop
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
# Investigator system prompt
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

When writing synthesized tests, derive the import path from the file shown in
retrieve_evidence results:
  file: codeminer/agent/runner.py  →  from codeminer.agent.runner import <symbol>
  file: codeminer/guardian/cycle.py  →  from codeminer.guardian.cycle import <symbol>
Always call synthesize_test with a valid target_symbol before calling
run_synthesized_test — synthesize_test validates the import for you.

Write your reasoning paragraph (2–5 sentences), then on its own line write
EXACTLY one of the following (plain text, no markdown formatting, no trailing
punctuation):
verdict: confirmed
verdict: rejected
verdict: inconclusive
"""

# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------


def _parse_verdict(text: str) -> Tuple[str, str]:
    """Extract (verdict, reasoning) from the model's final response.

    Robust to markdown formatting (``**verdict: confirmed**``) and trailing
    punctuation (``verdict: confirmed.``).

    Returns ``("inconclusive", text)`` if no verdict line is found.
    """
    verdict = "inconclusive"
    reasoning_lines: List[str] = []
    found = False
    for line in text.splitlines():
        # Strip markdown bold/italic/code markers before matching.
        clean = re.sub(r"[*_`]+", "", line).strip().lower()
        if clean.startswith("verdict:"):
            candidate = clean[len("verdict:"):].strip().rstrip(".,;:")
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
    """Placeholder for probes not yet implemented (filled in by probes.py)."""
    msg = (
        f"(tool '{tool_name}' is not yet implemented in this build; "
        "install codeminer/guardian/investigator/probes.py to enable it)"
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
        hypothesis: The :class:`~codeminer.guardian.orchestrator.Hypothesis` to
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

    # Derive a Python import hint from the target file path so the model can
    # construct valid synthesize_test calls without guessing module paths.
    target = hypothesis.target
    import_hint = ""
    if target.endswith(".py"):
        module_path = target[:-3].replace("/", ".").replace("\\", ".")
        import_hint = (
            f"\nPython import hint: `from {module_path} import <symbol>`\n"
            "(Replace <symbol> with the actual class/function name from retrieve_evidence results.)"
        )

    messages: List[dict] = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": (
                f"Hypothesis (rank {hypothesis.rank}, confidence {hypothesis.confidence:.2f}):\n"
                f"{hypothesis.statement}\n\n"
                f"Target: {hypothesis.target}\n"
                f"Kind: {hypothesis.kind}"
                f"{import_hint}\n"
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
