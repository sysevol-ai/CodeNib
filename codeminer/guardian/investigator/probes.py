# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Evidence-gathering tools for one Guardian investigation."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import replace
from typing import Optional, Protocol, Sequence, Tuple

from ...log_utils import get_logger
from ..report import Evidence
from ..signals import Hotspot
from .environment import TestRecipe
from .report_parser import parse_pytest_junit
from .sandbox import SandboxHandle
from .types import CommandResult, ProcessStatus, TestStatus, ToolResult

logger = get_logger(__name__)
FILE_CONTENT_MAX_LINES = 150
_SYNTH_TEST_FILENAME = "_guardian_synth_test.py"
_PATCH_FILENAME = "_guardian_fix.patch"
_OUTPUT_LIMIT = 2_000


class _Retriever(Protocol):
    def query(self, query: str, top_k: Optional[int] = ...) -> Sequence: ...


def read_file(path: str, max_lines: int = FILE_CONTENT_MAX_LINES) -> str:
    """Read a file and return bounded content."""

    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        logger.warning("probes: cannot read %s: %s", path, exc)
        return ""
    if len(lines) <= max_lines:
        return "".join(lines)
    return "".join(lines[:max_lines]) + (
        f"\n... ({len(lines) - max_lines} more lines truncated)"
    )


def hotspot_query(hotspot: Hotspot) -> str:
    stem = os.path.splitext(os.path.basename(hotspot.path))[0]
    parent = os.path.basename(os.path.dirname(hotspot.path))
    keywords = " ".join(part for part in (stem, parent) if part)
    return f"code in {hotspot.path} ({keywords})"


def investigate_hotspot(
    hotspot: Hotspot, retriever: _Retriever, *, top_k: int = 5
) -> list[Evidence]:
    try:
        nodes = retriever.query(hotspot_query(hotspot), top_k=top_k)
    except Exception as exc:  # noqa: BLE001 - retrieval failure degrades one signal
        logger.warning("investigate_hotspot: retrieval failed: %s", exc)
        return []
    return [
        Evidence(
            file=getattr(node, "file", None) or "",
            node_name=getattr(node, "node_name", "") or "",
            type=getattr(node, "type", "") or "",
            start_line=getattr(node, "start_line", None),
            end_line=getattr(node, "end_line", None),
            score=getattr(node, "score", None),
        )
        for node in nodes or []
    ]


def retrieve_evidence(
    query: str, retriever: object, top_k: int = 5, repo_path: str = ""
) -> ToolResult:
    """Search and retain the full provenance of every returned span."""

    try:
        nodes = retriever.query(query, top_k=top_k)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 - tool errors are results
        return ToolResult(f"retrieval failed: {exc}", is_error=True)
    spans = []
    rendered = []
    for node in nodes or []:
        path = getattr(node, "file", "") or ""
        start = getattr(node, "start_line", None)
        end = getattr(node, "end_line", start)
        content = getattr(node, "content", "") or ""
        if not content and path and start is not None:
            full_path = path if os.path.isabs(path) else os.path.join(repo_path, path)
            content = _read_span(full_path, int(start), int(end or start))
        span = {
            "path": path,
            "start_line": int(start or 0),
            "end_line": int(end or start or 0),
            "content": content[:8_000],
            "score": getattr(node, "score", None),
            "symbol": getattr(node, "node_name", "") or "",
        }
        spans.append(span)
        rendered.append(
            f"{path}:{span['start_line']}-{span['end_line']} "
            f"{span['symbol']}\n{span['content']}"
        )
    content = "\n\n".join(rendered) if rendered else "(no results)"
    return ToolResult(
        content[:30_000],
        structured_content={"query": query, "top_k": top_k, "spans": spans},
    )


def _read_span(path: str, start: int, end: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    return "".join(lines[max(0, start) : max(start + 1, end + 1)])


def _test_result(
    command_result: CommandResult, report_path: str, *, label: str
) -> ToolResult:
    parsed = parse_pytest_junit(report_path)
    command_result = replace(
        command_result, tests=parsed.tests if parsed.parseable else {}
    )
    statuses = list(command_result.tests.values())
    if command_result.status is not ProcessStatus.EXITED:
        is_error = True
        summary = command_result.status.value
    elif not parsed.parseable:
        is_error = True
        summary = f"report unavailable: {parsed.error}"
    elif statuses:
        is_error = False
        counts = {
            status.value: statuses.count(status) for status in set(statuses)
        }
        summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    elif command_result.exit_code in {2, 3, 4}:
        is_error = True
        summary = f"pytest infrastructure exit {command_result.exit_code}"
    else:
        is_error = False
        summary = "no tests collected"
    output = (command_result.stdout + "\n" + command_result.stderr).strip()
    return ToolResult(
        f"{label}: {summary}\n{output[-_OUTPUT_LIMIT:]}".strip(),
        is_error=is_error,
        structured_content=command_result,
    )


def _run_pytest(
    target: str,
    sandbox: SandboxHandle,
    recipe: TestRecipe,
    *,
    timeout: int,
    label: str,
) -> ToolResult:
    report_rel = f".guardian/reports/run-{uuid.uuid4().hex}.xml"
    report_abs = os.path.join(sandbox.repo_path, report_rel)
    command = recipe.command(target, report_rel)
    command_result = sandbox.run_command(command, timeout=timeout)
    return _test_result(command_result, report_abs, label=label)


def run_existing_test(
    node_id: str,
    sandbox: SandboxHandle,
    recipe: TestRecipe,
    *,
    timeout: int = 60,
) -> ToolResult:
    """Run an existing test; parsed report statuses are the sole authority."""

    return _run_pytest(
        node_id, sandbox, recipe, timeout=timeout, label="existing test"
    )


def _check_self_mocking(test_src: str) -> bool:
    has_patch = bool(re.search(r"@patch\s*\(", test_src))
    has_mock = "MagicMock" in test_src or "mock.patch" in test_src
    return has_patch and has_mock


def synthesize_test(target_symbol: str, body: str) -> ToolResult:
    """Validate and retain model-authored pytest source without executing it."""

    if not target_symbol.strip() or not body.strip():
        return ToolResult(
            "target_symbol and body are required", is_error=True
        )
    if len(body.splitlines()) > 40:
        return ToolResult("synthesized test exceeds 40 lines", is_error=True)
    try:
        compile(body, _SYNTH_TEST_FILENAME, "exec")
    except SyntaxError as exc:
        return ToolResult(f"invalid Python test source: {exc}", is_error=True)
    warning = _check_self_mocking(body)
    return ToolResult(
        (
            "synthesized test accepted"
            + ("; warning: test may mock its own target" if warning else "")
        ),
        structured_content={
            "target_symbol": target_symbol,
            "source": body,
            "self_mocking_warning": warning,
        },
    )


def run_synthesized_test(
    test_src: str,
    sandbox: SandboxHandle,
    recipe: TestRecipe,
    *,
    timeout: int = 60,
    test_filename: str = _SYNTH_TEST_FILENAME,
) -> ToolResult:
    sandbox.write_file(test_filename, test_src)
    return _run_pytest(
        test_filename, sandbox, recipe, timeout=timeout, label="synthesized test"
    )


def corroborate_fix(
    diff: str,
    test_src: str,
    sandbox: SandboxHandle,
    recipe: TestRecipe,
    *,
    timeout: int = 60,
) -> tuple[ToolResult, str]:
    """Apply a minimal reversal, then return the parsed rerun result."""

    sandbox.write_file(_PATCH_FILENAME, diff)
    patch = sandbox.run_command(
        ["patch", "-p1", "--forward", "--batch", "-i", _PATCH_FILENAME],
        timeout=min(timeout, 30),
    )
    if patch.status is not ProcessStatus.EXITED or patch.exit_code != 0:
        detail = (patch.stdout + "\n" + patch.stderr)[-_OUTPUT_LIMIT:]
        return ToolResult(
            f"fix patch could not be applied\n{detail}",
            is_error=True,
            structured_content=patch,
        ), ""
    return (
        run_synthesized_test(test_src, sandbox, recipe, timeout=timeout),
        diff,
    )


def corroborate_differential(
    test_src: str,
    prior_sandbox: SandboxHandle,
    recipe: TestRecipe,
    *,
    timeout: int = 60,
) -> ToolResult:
    """Run the same synthesized test on the prior snapshot."""

    return run_synthesized_test(
        test_src, prior_sandbox, recipe, timeout=timeout
    )


def command_result(result: ToolResult) -> Optional[CommandResult]:
    value = result.structured_content
    return value if isinstance(value, CommandResult) else None


def behavioural_tests(result: ToolResult) -> dict[str, TestStatus]:
    command = command_result(result)
    if command is None:
        return {}
    return {
        node_id: status
        for node_id, status in command.tests.items()
        if status in {TestStatus.PASSED, TestStatus.FAILED}
    }


def transition(
    parent: ToolResult, child: ToolResult
) -> Optional[Tuple[str, str]]:
    """Return a named SWE-bench transition shared by parent and child."""

    parent_command = command_result(parent)
    child_command = command_result(child)
    if parent_command is None or child_command is None:
        return None
    if (
        parent_command.status is not ProcessStatus.EXITED
        or child_command.status is not ProcessStatus.EXITED
    ):
        return None
    for node_id, before in parent_command.tests.items():
        after = child_command.tests.get(node_id)
        if before is TestStatus.FAILED and after is TestStatus.PASSED:
            return node_id, "FAIL_TO_PASS"
        if before is TestStatus.PASSED and after is TestStatus.FAILED:
            return node_id, "PASS_TO_FAIL"
    return None
