# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Typed L3 tool-use loop for investigating one Guardian hypothesis."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...agent.agent_types import ToolCallRecord
from ...llm.usage import TokenUsage, _extract_token_usage
from ...log_utils import get_logger
from .environment import TestRecipe, run_prelude
from .probes import (
    behavioural_tests,
    command_result,
    corroborate_differential,
    corroborate_fix,
    retrieve_evidence,
    run_existing_test,
    run_synthesized_test,
    synthesize_test,
    transition,
)
from .sandbox import ReadOnlySourceHandle, SandboxHandle
from .types import (
    BudgetLedger,
    InvestigationRunResult,
    InvestigationTask,
    ProcessStatus,
    SourceExcerpt,
    TestStatus,
    ToolResult,
)

logger = get_logger(__name__)
VALID_VERDICTS = frozenset({"confirmed", "refuted", "inconclusive"})
_MAX_TOOL_CONTENT = 30_000


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_code",
            "description": "Read a bounded source span from the disposable snapshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_evidence",
            "description": "Search for relevant code and retain result provenance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_existing_test",
            "description": "Run one existing pytest node with the validated recipe.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synthesize_test",
            "description": (
                "Register complete targeted pytest source without executing it. "
                "The test must exercise the real target and be at most 40 lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_symbol": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["target_symbol", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_synthesized_test",
            "description": "Run source registered by a synthesize_test call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_call_id": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["tool_call_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "corroborate",
            "description": (
                "Corroborate a synthesized-test run by a minimal fix or by "
                "running the same test on the prior snapshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_call_id": {"type": "string"},
                    "method": {"type": "string", "enum": ["fix", "differential"]},
                    "diff": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["tool_call_id", "method"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_verdict",
            "description": "Submit the investigation verdict; this is the only normal exit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["confirmed", "refuted", "inconclusive"],
                    },
                    "reasoning": {"type": "string"},
                    "cites": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["verdict", "reasoning", "cites"],
            },
        },
    },
]
_TOOL_NAMES = {item["function"]["name"] for item in TOOLS}

SYSTEM_PROMPT = """\
You are Repository Guardian's investigator. Answer exactly one falsifiable
obligation using only Guardian tools. Guardian owns all file and command
execution; never attempt execution yourself.

Read source before making a behavioural judgment. Test outcomes come only from
the structured report returned by a test tool. A process error, collection
error, timeout, or empty test set is not a test failure. A synthesized failing
test needs corroboration bound to that exact run. Finish only by calling
submit_verdict as the sole tool call in its turn. Use refuted, not rejected.
"""


def _opening_context(task: InvestigationTask, recipe: TestRecipe) -> str:
    hypothesis = task.hypothesis
    payload = {
        "hypothesis": {
            key: value
            for key, value in vars(hypothesis).items()
            if not key.startswith("_")
        },
        "obligation": task.obligation,
        "excerpts": [asdict(item) for item in task.excerpts],
        "commit_diff": task.commit_diff,
        "prior_attempts": task.prior_attempts,
        "grant_tokens": task.grant_tokens,
        "deadline_s": task.deadline_s,
        "validated_test_recipe": list(recipe.command_prefix),
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _estimate_call(messages: List[dict], llm: object, grant: int) -> Tuple[int, int]:
    prompt_chars = len(json.dumps(messages, sort_keys=True, default=str))
    prompt_tokens = max(1, (prompt_chars + 2) // 3)
    configured = int(getattr(llm, "max_tokens", 8_192) or 8_192)
    max_completion = min(configured, 8_192, max(1_024, grant // 2))
    return prompt_tokens + max_completion, max_completion


def _bounded(result: ToolResult) -> ToolResult:
    if len(result.content) <= _MAX_TOOL_CONTENT:
        return result
    return ToolResult(
        result.content[:_MAX_TOOL_CONTENT] + "\n[truncated]",
        is_error=result.is_error,
        structured_content=result.structured_content,
    )


def _read_code(
    args: dict, source_handle: ReadOnlySourceHandle
) -> Tuple[ToolResult, Optional[SourceExcerpt]]:
    path = str(args.get("path", ""))
    start = max(1, int(args.get("start_line", 1)))
    end = max(start, int(args.get("end_line", start)))
    if end - start > 200:
        return ToolResult("source span exceeds 200 lines", is_error=True), None
    try:
        content = source_handle.read_file(path)
    except (OSError, ValueError) as exc:
        return ToolResult(f"cannot read source: {exc}", is_error=True), None
    if not content:
        return ToolResult(f"source path is empty or unavailable: {path}", True), None
    lines = content.splitlines()
    selected = "\n".join(lines[start - 1 : end])
    excerpt = SourceExcerpt(path, start, min(end, len(lines)), selected[:16_000])
    return ToolResult(excerpt.content, structured_content=asdict(excerpt)), excerpt


def _source_from_synthesis(record: ToolCallRecord) -> str:
    result = record.result
    if not isinstance(result, ToolResult) or not isinstance(
        result.structured_content, dict
    ):
        return ""
    return str(result.structured_content.get("source", ""))


def _dispatch(
    name: str,
    args: dict,
    call_id: str,
    records: Dict[str, ToolCallRecord],
    sandbox: SandboxHandle,
    prior_sandbox: Optional[SandboxHandle],
    retriever: object,
    recipe: TestRecipe,
    source_handle: ReadOnlySourceHandle,
) -> Tuple[ToolResult, Optional[str], Optional[SourceExcerpt], str]:
    """Return result, parent id, source span, and evidence diff."""

    if name == "read_code":
        result, excerpt = _read_code(args, source_handle)
        return result, None, excerpt, ""
    if name == "retrieve_evidence":
        return (
            retrieve_evidence(
                str(args.get("query", "")),
                retriever,
                int(args.get("top_k", 5)),
                sandbox.repo_path,
            ),
            None,
            None,
            "",
        )
    if name == "run_existing_test":
        result = run_existing_test(
            str(args.get("node_id", "")),
            sandbox,
            recipe,
            timeout=int(args.get("timeout", 60)),
        )
        return result, None, None, ""
    if name == "synthesize_test":
        return (
            synthesize_test(
                str(args.get("target_symbol", "")), str(args.get("body", ""))
            ),
            None,
            None,
            "",
        )
    if name == "run_synthesized_test":
        parent_id = str(args.get("tool_call_id", ""))
        parent = records.get(parent_id)
        if parent is None or parent.skill_id != "synthesize_test":
            return (
                ToolResult("tool_call_id must name a synthesize_test call", True),
                parent_id or None,
                None,
                "",
            )
        source = _source_from_synthesis(parent)
        if not source:
            return ToolResult("referenced synthesis has no source", True), parent_id, None, ""
        return (
            run_synthesized_test(
                source,
                sandbox,
                recipe,
                timeout=int(args.get("timeout", 60)),
            ),
            parent_id,
            None,
            "",
        )
    if name == "corroborate":
        parent_id = str(args.get("tool_call_id", ""))
        parent = records.get(parent_id)
        if parent is None or parent.skill_id != "run_synthesized_test":
            return (
                ToolResult(
                    "tool_call_id must name a run_synthesized_test call", True
                ),
                parent_id or None,
                None,
                "",
            )
        synthesis = records.get(parent.parent_tool_call_id or "")
        source = _source_from_synthesis(synthesis) if synthesis else ""
        method = str(args.get("method", ""))
        if not source:
            return ToolResult("parent test source is unavailable", True), parent_id, None, ""
        if method == "fix":
            diff = str(args.get("diff", ""))
            if not diff:
                return ToolResult("fix corroboration requires diff", True), parent_id, None, ""
            result, applied_diff = corroborate_fix(
                diff,
                source,
                sandbox,
                recipe,
                timeout=int(args.get("timeout", 60)),
            )
            parent_result = _record_result(parent)
            named = transition(parent_result, result) if parent_result else None
            if named and named[1] == "FAIL_TO_PASS":
                result = ToolResult(
                    f"FAIL_TO_PASS {named[0]}\n{result.content}",
                    is_error=result.is_error,
                    structured_content=result.structured_content,
                )
            return result, parent_id, None, applied_diff
        if method == "differential":
            if prior_sandbox is None:
                return (
                    ToolResult("prior snapshot is unavailable", True),
                    parent_id,
                    None,
                    "",
                )
            result = corroborate_differential(
                source,
                prior_sandbox,
                recipe,
                timeout=int(args.get("timeout", 60)),
            )
            parent_result = _record_result(parent)
            named = transition(parent_result, result) if parent_result else None
            if named and named[1] == "FAIL_TO_PASS":
                result = ToolResult(
                    f"PASS_TO_FAIL {named[0]}\n{result.content}",
                    is_error=result.is_error,
                    structured_content=result.structured_content,
                )
            return result, parent_id, None, ""
        return ToolResult("method must be fix or differential", True), parent_id, None, ""
    return ToolResult(f"unknown tool: {name}", is_error=True), None, None, ""


def _record_result(record: ToolCallRecord) -> Optional[ToolResult]:
    return record.result if isinstance(record.result, ToolResult) else None


def _has_source(source_spans: List[SourceExcerpt]) -> bool:
    return any(item.content.strip() for item in source_spans)


def _shared_transition(
    parent: ToolCallRecord, child: ToolCallRecord
) -> Optional[Tuple[str, TestStatus, TestStatus]]:
    parent_result = _record_result(parent)
    child_result = _record_result(child)
    if parent_result is None or child_result is None:
        return None
    before = command_result(parent_result)
    after = command_result(child_result)
    if before is None or after is None:
        return None
    if (
        before.status is not ProcessStatus.EXITED
        or after.status is not ProcessStatus.EXITED
    ):
        return None
    for node_id, status in before.tests.items():
        child_status = after.tests.get(node_id)
        if child_status is not None:
            return node_id, status, child_status
    return None


def _existing_failure_matches_locus(
    record: ToolCallRecord, task: InvestigationTask
) -> bool:
    result = _record_result(record)
    command = command_result(result) if result else None
    if command is None or TestStatus.FAILED not in command.tests.values():
        return False
    haystack = (
        " ".join(command.tests)
        + "\n"
        + command.stdout
        + "\n"
        + command.stderr
    ).lower()
    for locus in getattr(task.hypothesis, "locus", []) or []:
        normalized = str(locus).split(":", 1)[0]
        fragments = {
            normalized.lower(),
            os.path.basename(normalized).lower(),
            Path(normalized).stem.lower(),
        }
        if any(fragment and fragment in haystack for fragment in fragments):
            return True
    return False


def _validate_submission(
    args: dict,
    records: Dict[str, ToolCallRecord],
    task: InvestigationTask,
    source_spans: List[SourceExcerpt],
) -> Tuple[bool, str, str, List[str]]:
    verdict = str(args.get("verdict", ""))
    reasoning = str(args.get("reasoning", "")).strip()
    cites = list(dict.fromkeys(str(item) for item in args.get("cites", [])))
    if verdict not in VALID_VERDICTS:
        return False, "invalid verdict", "inconclusive", []
    if not reasoning:
        return False, "reasoning is required", verdict, cites
    cited = [records[item] for item in cites if item in records]
    if len(cited) != len(cites):
        return False, "every cite must name a prior tool call", verdict, cites
    if verdict == "inconclusive":
        return True, "", verdict, cites
    if not cites:
        return False, "behavioural verdicts require cites", verdict, cites
    if not _has_source(source_spans):
        return False, "behavioural verdicts require a source span", verdict, cites
    behavioural = []
    for record in cited:
        result = _record_result(record)
        if result is not None and behavioural_tests(result):
            behavioural.append(record)
    if not behavioural:
        return False, "cites contain no parsed PASSED/FAILED test", verdict, cites
    if verdict == "refuted":
        for record in behavioural:
            result = _record_result(record)
            if (
                result is not None
                and TestStatus.PASSED in behavioural_tests(result).values()
            ):
                return True, "", verdict, cites
        return False, "refutation requires a cited passing test", verdict, cites

    if any(
        record.skill_id == "run_existing_test"
        and _existing_failure_matches_locus(record, task)
        for record in behavioural
    ):
        return True, "", verdict, cites
    for child in cited:
        if child.skill_id != "corroborate" or not child.parent_tool_call_id:
            continue
        parent = records.get(child.parent_tool_call_id)
        if parent is None or parent not in cited:
            continue
        shared = _shared_transition(parent, child)
        if shared is None:
            continue
        _, current, corroborated = shared
        method = child.arguments.get("method")
        if (
            current is TestStatus.FAILED
            and corroborated is TestStatus.PASSED
            and method in {"fix", "differential"}
        ):
            return True, "", verdict, cites
    return False, "confirmation lacks bound corroboration", verdict, cites


def _derive_non_submitted_evidence(records: List[ToolCallRecord]) -> str:
    test_records = [
        record
        for record in records
        if record.skill_id in {
            "run_existing_test",
            "run_synthesized_test",
            "corroborate",
        }
    ]
    if not test_records:
        return "invalid"
    if all(
        (result := _record_result(record)) is None
        or not behavioural_tests(result)
        for record in test_records
    ):
        return "environment"
    return "invalid"


def _checkpoint(
    path: Optional[Path],
    task: InvestigationTask,
    records: List[ToolCallRecord],
    messages: List[dict],
    ledger: BudgetLedger,
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    projection = InvestigationRunResult(
        verdict="inconclusive",
        exit_status="running",
        evidence_status=_derive_non_submitted_evidence(records),
        reasoning="",
        tool_calls=records,
        budget=ledger,
    ).to_dict()
    payload = {
        "hypothesis_id": getattr(task.hypothesis, "id", ""),
        "run": projection,
        "messages": messages,
    }
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _result(
    *,
    verdict: str,
    exit_status: str,
    evidence_status: str,
    reasoning: str,
    records: List[ToolCallRecord],
    cites: Optional[List[str]],
    source_spans: List[SourceExcerpt],
    usage: TokenUsage,
    ledger: BudgetLedger,
    evidence_test: str = "",
    evidence_diff: str = "",
) -> InvestigationRunResult:
    return InvestigationRunResult(
        verdict=verdict,
        exit_status=exit_status,
        evidence_status=evidence_status,
        reasoning=reasoning,
        tool_calls=records,
        cites=cites or [],
        evidence_test=evidence_test,
        evidence_diff=evidence_diff,
        source_spans=source_spans,
        usage=usage,
        budget=ledger,
    )


def run_investigation(
    task: InvestigationTask,
    llm: object,
    retriever: object,
    sandbox: SandboxHandle,
    *,
    prior_sandbox: Optional[SandboxHandle] = None,
    usage_acc: object = None,
    memory_store: object = None,
    max_rounds: int = 8,
    checkpoint_path: Optional[Path] = None,
    recipe: Optional[TestRecipe] = None,
    repo_identity: str = "",
    commit: str = "",
) -> InvestigationRunResult:
    """Run the deterministic prelude and typed probe/observe/submit loop."""

    ledger = BudgetLedger(task.grant_tokens)
    usage = TokenUsage()
    if recipe is None:
        prelude = run_prelude(
            sandbox,
            memory_store=memory_store,
            commit=commit,
            repo_identity=repo_identity,
            timeout=min(60, max(1, int(task.deadline_s))),
        )
        if prelude.blocked or prelude.recipe is None:
            return _result(
                verdict="inconclusive",
                exit_status="environment_unavailable",
                evidence_status="environment",
                reasoning=prelude.reason,
                records=[],
                cites=[],
                source_spans=list(task.excerpts),
                usage=usage,
                ledger=ledger,
            )
        recipe = prelude.recipe

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _opening_context(task, recipe)},
    ]
    records: List[ToolCallRecord] = []
    by_id: Dict[str, ToolCallRecord] = {}
    source_spans = list(task.excerpts)
    source_handle = ReadOnlySourceHandle(sandbox.repo_path)
    evidence_test = ""
    evidence_diff = ""
    started = time.monotonic()

    for _round in range(max_rounds):
        if time.monotonic() - started >= task.deadline_s:
            exit_status = "wall_clock_exceeded"
            reasoning = "Investigation deadline elapsed."
            break
        estimate, max_completion = _estimate_call(
            messages, llm, task.grant_tokens
        )
        if not ledger.reserve(estimate):
            exit_status = "budget_exceeded"
            reasoning = "Token reservation would exceed the investigation grant."
            break
        try:
            response = llm._call_raw(
                messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=max_completion,
            )
        except Exception as exc:  # noqa: BLE001 - provider failure is typed exit
            ledger.reconcile(estimate, 0)
            exit_status = "environment_unavailable"
            reasoning = f"Model backend unavailable: {exc}"
            break
        call_usage = _extract_token_usage(response)
        usage = usage.add(call_usage)
        ledger.reconcile(estimate, call_usage.total_tokens)
        if ledger.overshoot:
            logger.error(
                "investigator budget overshoot: actual=%d granted=%d overshoot=%d",
                ledger.actual,
                ledger.granted,
                ledger.overshoot,
            )
        if usage_acc is not None:
            usage_acc.add(response)
        message = response.choices[0].message
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        messages.append(
            {
                "role": "assistant",
                "content": getattr(message, "content", "") or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ],
            }
        )
        if not tool_calls:
            exit_status = "no_progress"
            reasoning = "Model returned no Guardian tool call."
            break

        submit_calls = [
            call for call in tool_calls if call.function.name == "submit_verdict"
        ]
        submit_is_sole = len(tool_calls) == 1 and bool(submit_calls)
        for call in tool_calls:
            call_started = time.monotonic()
            name = str(call.function.name)
            try:
                arguments = json.loads(call.function.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                arguments = {}
                tool_result = ToolResult(f"malformed tool arguments: {exc}", True)
                parent_id = None
                excerpt = None
                call_diff = ""
            else:
                if name == "submit_verdict":
                    if not submit_is_sole:
                        tool_result = ToolResult(
                            "submit_verdict must be the turn's only tool call", True
                        )
                        accepted = False
                        verdict = "inconclusive"
                        cites = []
                    else:
                        accepted, error, verdict, cites = _validate_submission(
                            arguments, by_id, task, source_spans
                        )
                        tool_result = ToolResult(
                            "verdict accepted" if accepted else error,
                            is_error=not accepted,
                            structured_content={
                                "accepted": accepted,
                                "verdict": verdict,
                                "cites": cites,
                            },
                        )
                    parent_id = None
                    excerpt = None
                    call_diff = ""
                elif name not in _TOOL_NAMES:
                    tool_result = ToolResult(f"unknown tool: {name}", True)
                    parent_id = None
                    excerpt = None
                    call_diff = ""
                else:
                    try:
                        tool_result, parent_id, excerpt, call_diff = _dispatch(
                            name,
                            arguments,
                            call.id,
                            by_id,
                            sandbox,
                            prior_sandbox,
                            retriever,
                            recipe,
                            source_handle,
                        )
                    except Exception as exc:  # noqa: BLE001 - dispatch never escapes
                        tool_result = ToolResult(
                            f"tool execution failed: {type(exc).__name__}: {exc}",
                            True,
                        )
                        parent_id = None
                        excerpt = None
                        call_diff = ""
            tool_result = _bounded(tool_result)
            record = ToolCallRecord(
                tool_call_id=call.id,
                skill_id=name,
                arguments=arguments,
                result=tool_result,
                duration_ms=(time.monotonic() - call_started) * 1000,
                error=tool_result.content if tool_result.is_error else None,
                parent_tool_call_id=parent_id,
            )
            records.append(record)
            by_id[record.tool_call_id] = record
            if excerpt is not None:
                source_spans.append(excerpt)
            if (
                name == "retrieve_evidence"
                and isinstance(tool_result.structured_content, dict)
            ):
                for span in tool_result.structured_content.get("spans", []):
                    if not isinstance(span, dict) or not span.get("content"):
                        continue
                    source_spans.append(
                        SourceExcerpt(
                            path=str(span.get("path", "")),
                            start_line=int(span.get("start_line", 0)),
                            end_line=int(span.get("end_line", 0)),
                            content=str(span.get("content", "")),
                        )
                    )
            if name == "synthesize_test" and not tool_result.is_error:
                evidence_test = _source_from_synthesis(record)
            if call_diff:
                evidence_diff = call_diff
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": tool_result.content,
                    "is_error": tool_result.is_error,
                }
            )
            _checkpoint(checkpoint_path, task, records, messages, ledger)
            if (
                name == "submit_verdict"
                and submit_is_sole
                and isinstance(tool_result.structured_content, dict)
                and tool_result.structured_content.get("accepted")
            ):
                return _result(
                    verdict=str(tool_result.structured_content["verdict"]),
                    exit_status="submitted",
                    evidence_status=(
                        "invalid"
                        if tool_result.structured_content["verdict"] == "inconclusive"
                        else "valid"
                    ),
                    reasoning=str(arguments.get("reasoning", "")).strip(),
                    records=records,
                    cites=list(tool_result.structured_content["cites"]),
                    source_spans=source_spans,
                    usage=usage,
                    ledger=ledger,
                    evidence_test=evidence_test,
                    evidence_diff=evidence_diff,
                )
    else:
        exit_status = "no_progress"
        reasoning = "Investigation round limit reached without submit_verdict."

    evidence_status = _derive_non_submitted_evidence(records)
    result = _result(
        verdict="inconclusive",
        exit_status=exit_status,
        evidence_status=evidence_status,
        reasoning=reasoning,
        records=records,
        cites=[],
        source_spans=source_spans,
        usage=usage,
        ledger=ledger,
        evidence_test=evidence_test,
        evidence_diff=evidence_diff,
    )
    _checkpoint(checkpoint_path, task, records, messages, ledger)
    return result


def run_investigator(
    hypothesis: object,
    llm: object,
    retriever: object,
    sandbox: SandboxHandle,
    *,
    budget_tokens: int = 20_000,
    max_rounds: int = 8,
    usage_acc: object = None,
    prior_sandbox: Optional[SandboxHandle] = None,
    memory_store: object = None,
    obligation: str = "",
    excerpts: Optional[List[SourceExcerpt]] = None,
    commit_diff: str = "",
    prior_attempts: Optional[List[dict]] = None,
    deadline_s: float = 300,
    checkpoint_path: Optional[Path] = None,
    recipe: Optional[TestRecipe] = None,
    repo_identity: str = "",
    commit: str = "",
) -> InvestigationRunResult:
    """Compatibility wrapper that constructs the full ``InvestigationTask``."""

    task = InvestigationTask(
        hypothesis=hypothesis,
        obligation=obligation
        or f"Determine whether this claim holds at HEAD: {getattr(hypothesis, 'claim', '')}",
        excerpts=list(excerpts or []),
        commit_diff=commit_diff,
        prior_attempts=list(prior_attempts or []),
        grant_tokens=max(0, int(budget_tokens)),
        deadline_s=max(0.1, float(deadline_s)),
    )
    return run_investigation(
        task,
        llm,
        retriever,
        sandbox,
        prior_sandbox=prior_sandbox,
        usage_acc=usage_acc,
        memory_store=memory_store,
        max_rounds=max_rounds,
        checkpoint_path=checkpoint_path,
        recipe=recipe,
        repo_identity=repo_identity,
        commit=commit,
    )
