# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Probe toolset for the investigator sub-agent (Hour 2, W3.3b + W3.3c).

Each public function implements one tool the investigator loop can call.
They are all pure functions of their arguments — no global state, no direct
LLM calls (the LLM writes the test source itself; see ``synthesize_test``).

``dispatch_advanced_probe`` is the single entry point imported by
``runner.py``; it maps tool names to the appropriate probe function and
returns a ``(observation_text, ProbeRecord)`` pair matching the contract
expected by ``_dispatch_tool`` in the investigator loop.

Corroboration policy (§3.3c):
  A red synthesized test alone is NOT sufficient to confirm a hypothesis.
  ``corroboration_policy`` encodes the rule: the test must actually fail
  (rc != 0, rc != 2), AND at least one of:
    (a) fix_probe FAIL→PASS flip, or
    (b) differential_run PASS→FAIL across snapshots
  must hold before a verdict of "confirmed" is admissible.

Also contains the former codeminer.guardian.investigate functions
(hotspot_query, investigate_hotspot, Evidence) and the read_file
utility from codeminer.guardian.llm_investigator, all moved here for
co-location with the rest of the probe toolset.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence, Tuple

from ...log_utils import get_logger
from ..report import Evidence
from ..signals import Hotspot
from .runner import ProbeRecord, _run_search
from .sandbox import SandboxHandle

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# File reader utility  (from llm_investigator.py)
# ---------------------------------------------------------------------------

FILE_CONTENT_MAX_LINES = 150


def read_file(path: str, max_lines: int = FILE_CONTENT_MAX_LINES) -> str:
    """Read a file and return its content, truncated to ``max_lines``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        logger.warning("probes: cannot read %s: %s", path, exc)
        return ""
    if len(lines) <= max_lines:
        return "".join(lines)
    kept = "".join(lines[:max_lines])
    return kept + f"\n... ({len(lines) - max_lines} more lines truncated)"


# ---------------------------------------------------------------------------
# Hotspot investigate utilities  (from investigate.py)
# ---------------------------------------------------------------------------


class _Retriever(Protocol):
    """Minimal surface Guardian needs from a retrieval pipeline."""

    def query(self, query: str, top_k: Optional[int] = ...) -> Sequence: ...


def hotspot_query(hotspot: Hotspot) -> str:
    """Build a natural-language retrieval query from a hotspot path.

    Uses the file stem plus its parent directory so BM25 has identifier-like
    keywords to latch onto (e.g. ``"runner agent"`` for ``agent/runner.py``).
    """
    stem = os.path.splitext(os.path.basename(hotspot.path))[0]
    parent = os.path.basename(os.path.dirname(hotspot.path))
    keywords = " ".join(t for t in (stem, parent) if t)
    return f"code in {hotspot.path} ({keywords})"


def investigate_hotspot(
    hotspot: Hotspot,
    retriever: _Retriever,
    *,
    top_k: int = 5,
) -> List[Evidence]:
    """Return up to ``top_k`` evidence locations for a hotspot.

    Degrades gracefully: if retrieval raises (e.g. an index failed to build),
    we log and return an empty list rather than aborting the whole cycle.
    """
    query = hotspot_query(hotspot)
    try:
        nodes = retriever.query(query, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 — a bad query must not kill the cycle
        logger.warning("investigate_hotspot: retrieval failed for %s: %s", query, exc)
        return []

    evidence: List[Evidence] = []
    for n in nodes or []:
        evidence.append(
            Evidence(
                file=getattr(n, "file", None) or "",
                node_name=getattr(n, "node_name", "") or "",
                type=getattr(n, "type", "") or "",
                start_line=getattr(n, "start_line", None),
                end_line=getattr(n, "end_line", None),
                score=getattr(n, "score", None),
            )
        )
    return evidence


# ---------------------------------------------------------------------------
# retrieve_evidence
# ---------------------------------------------------------------------------

_TOP_K_DEFAULT = 5


def retrieve_evidence(
    query: str,
    retriever: object,
    top_k: int = _TOP_K_DEFAULT,
    repo_path: str = "",
) -> Tuple[str, dict]:
    """Search the codebase for spans relevant to a hypothesis.

    Returns ``(observation_text, result_dict)`` where ``observation_text``
    is what the LLM receives as the tool result.
    """
    text = _run_search(query, retriever, top_k, repo_path)
    return text, {"query": query, "top_k": top_k, "result": text}


# ---------------------------------------------------------------------------
# run_existing_test
# ---------------------------------------------------------------------------

_PYTEST_FLAGS = ["-x", "--tb=short", "-q"]
_COLLECTION_ERROR_RC = 2


def run_existing_test(
    test_pattern: str,
    sandbox: SandboxHandle,
    *,
    timeout: int = 60,
) -> Tuple[str, dict]:
    """Run an existing pytest test pattern in the sandbox.

    Returns ``(observation_text, result_dict)``.
    ``result_dict`` keys: ``passed`` (bool|None), ``rc``, ``output``.
    A collection error (rc=2) sets ``passed=None`` (invalid, not a FAIL).
    """
    rc, output = sandbox.run_command(
        ["python", "-m", "pytest", test_pattern] + _PYTEST_FLAGS,
        timeout=timeout,
    )
    passed: Optional[bool]
    if rc == _COLLECTION_ERROR_RC:
        passed = None
        label = "INVALID (collection error)"
    elif rc == 0:
        passed = True
        label = "PASS"
    else:
        passed = False
        label = "FAIL"

    obs = f"{label}\n{output[-2000:]}"
    return obs, {"passed": passed, "rc": rc, "output": output}


# ---------------------------------------------------------------------------
# synthesize_test
# ---------------------------------------------------------------------------

_SYNTH_MAX_LINES = 40
_SYNTH_SCAFFOLD = """\
import pytest
from {module} import {symbol}


def test_{slug}():
    # Call {symbol} with the arguments the old API expected and assert
    # the old contract.  The test should FAIL on the current commit if
    # the risk hypothesis is correct.
    #
    # Do NOT mock {symbol} — call it directly so the test exercises
    # the real contract.
    raise NotImplementedError("Fill in the test body")
"""


def _parse_target_symbol(target_symbol: str) -> Tuple[str, str]:
    """Split 'a.b.c.Symbol' → ('a.b.c', 'Symbol')."""
    parts = target_symbol.rsplit(".", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return target_symbol, target_symbol


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()


def synthesize_test(
    description: str,
    target_symbol: str,
    sandbox: SandboxHandle,
) -> Tuple[str, dict]:
    """Return a scaffold for a targeted test that should expose the risk.

    The LLM fills in the test body and submits it via ``run_synthesized_test``.
    Returns a Python source scaffold with the import pre-validated so the LLM
    knows the import path is correct before writing the test body.

    If the import cannot be resolved, returns an error observation so the LLM
    can try a different symbol path.
    """
    module, symbol = _parse_target_symbol(target_symbol)

    # Validate the import in the sandbox before building the scaffold.
    rc, out = sandbox.run_command(
        ["python", "-c", f"from {module} import {symbol}; print('ok')"],
        timeout=10,
    )
    if rc != 0:
        msg = (
            f"Cannot import '{symbol}' from '{module}': {out[:300]}\n"
            "Check the target_symbol path and try again."
        )
        return msg, {"valid": False, "target_symbol": target_symbol, "error": out}

    scaffold = _SYNTH_SCAFFOLD.format(
        module=module,
        symbol=symbol,
        slug=_slug(symbol),
    )
    obs = (
        f"Scaffold for '{target_symbol}' (import validated):\n\n"
        f"```python\n{scaffold}```\n\n"
        f"Task: {description}\n\n"
        f"Fill in the test body (keep it <= {_SYNTH_MAX_LINES} lines) and call "
        f"run_synthesized_test with the complete source."
    )
    return obs, {"valid": True, "target_symbol": target_symbol, "scaffold": scaffold}


# ---------------------------------------------------------------------------
# run_synthesized_test
# ---------------------------------------------------------------------------

_SYNTH_TEST_FILENAME = "_guardian_synth_test.py"


def _check_self_mocking(test_src: str) -> bool:
    """True if the test appears to mock the target symbol under investigation.

    Heuristic: any ``@patch`` decorator combined with ``MagicMock`` usage
    suggests the test may be mocking the very thing it should exercise.
    """
    has_patch = bool(re.search(r"@patch\s*\(", test_src))
    has_mock = "MagicMock" in test_src or "mock.patch" in test_src
    return has_patch and has_mock


def run_synthesized_test(
    test_src: str,
    sandbox: SandboxHandle,
    *,
    test_filename: str = _SYNTH_TEST_FILENAME,
    timeout: int = 60,
) -> Tuple[str, dict]:
    """Write ``test_src`` to the sandbox overlay and execute it with pytest.

    Guards:
    - Collection error (rc=2): test is invalid (likely import error).  Sets
      ``valid=False`` — the corroboration policy will not accept this.
    - Self-mocking warning: if the test appears to mock the target symbol,
      ``self_mocking_warning`` is set in the result dict.

    Returns ``(observation_text, result_dict)``.
    ``result_dict`` keys: ``passed``, ``valid``, ``rc``, ``output``,
    ``self_mocking_warning``.
    """
    mocking_warning = _check_self_mocking(test_src)
    if mocking_warning:
        logger.warning(
            "probes: synthesized test may mock its own target "
            "(found @patch + MagicMock); result may not be valid evidence"
        )

    sandbox.write_file(test_filename, test_src)
    rc, output = sandbox.run_command(
        ["python", "-m", "pytest", test_filename] + _PYTEST_FLAGS,
        timeout=timeout,
    )

    valid = rc != _COLLECTION_ERROR_RC
    if not valid:
        passed = None
        label = "INVALID (collection error — check imports)"
    elif rc == 0:
        passed = True
        label = "PASS (test did not fail — no evidence of risk)"
    else:
        passed = False
        label = "FAIL (test confirms the risk)"

    if mocking_warning:
        label += " [WARNING: test may mock its own target]"

    obs = f"{label}\n{output[-2000:]}"
    return obs, {
        "passed": passed,
        "valid": valid,
        "rc": rc,
        "output": output,
        "self_mocking_warning": mocking_warning,
    }


# ---------------------------------------------------------------------------
# fix_probe
# ---------------------------------------------------------------------------

_PATCH_FILENAME = "_guardian_fix.patch"


def fix_probe(
    diff: str,
    test_src: str,
    sandbox: SandboxHandle,
    *,
    timeout: int = 60,
) -> Tuple[str, dict]:
    """Apply ``diff`` to the sandbox overlay and re-run ``test_src``.

    A FAIL→PASS flip (test was red before, green after the minimal revert)
    is strong corroboration that the hypothesis is confirmed.

    Returns ``(observation_text, result_dict)``.
    ``result_dict`` keys: ``patch_applied``, ``passed``, ``rc``, ``output``.
    """
    sandbox.write_file(_PATCH_FILENAME, diff)
    rc_patch, patch_out = sandbox.run_command(
        ["patch", "-p1", "--forward", "--batch", "-i", _PATCH_FILENAME],
        timeout=30,
    )
    if rc_patch != 0:
        msg = f"PATCH_FAILED: patch command exited {rc_patch}\n{patch_out[:500]}"
        return msg, {"patch_applied": False, "passed": None, "rc": rc_patch, "output": patch_out}

    _, result = run_synthesized_test(test_src, sandbox, timeout=timeout)
    flip = "FAIL→PASS" if result["passed"] is True else (
        "FAIL→FAIL (still red after fix)" if result["passed"] is False else "INVALID"
    )
    obs = f"fix_probe: patch applied  |  test result: {flip}\n{result['output'][-1500:]}"
    return obs, {"patch_applied": True, **result}


# ---------------------------------------------------------------------------
# differential_run
# ---------------------------------------------------------------------------


def differential_run(
    test_src: str,
    sandbox_current: SandboxHandle,
    sandbox_prior: SandboxHandle,
    *,
    timeout: int = 60,
) -> Tuple[str, dict]:
    """Run ``test_src`` on the prior snapshot then on the current snapshot.

    A PASS-on-prior / FAIL-on-current pair is the strongest corroboration:
    the regression is pinned to the interval between the two snapshots.

    Returns ``(observation_text, result_dict)``.
    ``result_dict`` keys: ``prior_passed``, ``current_passed``,
    ``prior_output``, ``current_output``, ``is_regression``.
    """
    _, prior_result = run_synthesized_test(
        test_src, sandbox_prior,
        test_filename=_SYNTH_TEST_FILENAME, timeout=timeout,
    )
    _, current_result = run_synthesized_test(
        test_src, sandbox_current,
        test_filename=_SYNTH_TEST_FILENAME, timeout=timeout,
    )

    prior_passed = prior_result["passed"]
    current_passed = current_result["passed"]
    is_regression = prior_passed is True and current_passed is False

    if is_regression:
        direction = "PASS→FAIL regression confirmed in this interval"
    elif prior_passed is False and current_passed is False:
        direction = "FAIL→FAIL (pre-existing failure, not a new regression)"
    elif prior_passed is True and current_passed is True:
        direction = "PASS→PASS (no regression detected)"
    else:
        direction = f"prior={prior_passed} / current={current_passed} (inconclusive)"

    obs = f"differential_run: {direction}"
    return obs, {
        "prior_passed": prior_passed,
        "current_passed": current_passed,
        "prior_output": prior_result["output"],
        "current_output": current_result["output"],
        "is_regression": is_regression,
    }


# ---------------------------------------------------------------------------
# corroboration_policy  (W3.3c)
# ---------------------------------------------------------------------------


def corroboration_policy(
    synth_result: dict,
    differential_result: Optional[dict] = None,
    fix_result: Optional[dict] = None,
) -> Tuple[bool, str]:
    """Return ``(may_confirm, reason)``.

    Rules:
    1. An invalid synthesized test (collection error) can never confirm.
    2. A test that PASSED (did not fail) provides no evidence of risk.
    3. A red synthesized test alone is insufficient — corroboration required.
    4. Corroboration is one of:
       (a) fix_probe FAIL→PASS: ``fix_result["passed"] is True``
       (b) differential PASS→FAIL: ``differential_result["is_regression"] is True``
    """
    if not synth_result.get("valid", True):
        return False, "synthesized test is invalid (collection error — check imports)"

    if synth_result.get("passed") is not False:
        return False, "synthesized test did not fail — no evidence of risk"

    # Test failed. Need corroboration.
    if fix_result is not None:
        if not fix_result.get("patch_applied", False):
            return False, "fix-probe patch failed to apply — cannot corroborate"
        if fix_result.get("passed") is True:
            return True, "fix-probe FAIL→PASS: minimal revert restores the contract"

    if differential_result is not None:
        if differential_result.get("is_regression") is True:
            return True, "differential run PASS→FAIL: regression pinned to this interval"

    if fix_result is not None or differential_result is not None:
        return False, "corroboration attempted but did not flip — evidence is weak"

    return False, "red synthesized test alone is insufficient — run fix_probe or differential_run"


# ---------------------------------------------------------------------------
# dispatch_advanced_probe  (entry point for runner.py)
# ---------------------------------------------------------------------------


def dispatch_advanced_probe(
    tool_name: str,
    args: dict,
    sandbox: SandboxHandle,
    *,
    synth_test_store: List[str],
) -> Tuple[str, ProbeRecord]:
    """Dispatch synthesize_test / run_synthesized_test / fix_probe tool calls.

    Returns ``(observation_text, ProbeRecord)`` matching the contract expected
    by ``_dispatch_tool`` in ``runner.py``.

    ``synth_test_store`` is the mutable list the investigator loop uses to
    carry the most recently synthesized test source across calls.
    """
    if tool_name == "synthesize_test":
        description = args.get("description", "")
        target_symbol = args.get("target_symbol", "")
        obs, result = synthesize_test(description, target_symbol, sandbox)
        passed = None
        return obs, ProbeRecord(
            tool="synthesize_test",
            input_summary=f"synthesize_test({target_symbol!r})",
            output_summary=("scaffold returned" if result.get("valid") else f"import error: {result.get('error', '')[:80]}"),
            passed=passed,
        )

    if tool_name == "run_synthesized_test":
        test_src = args.get("test_source", "")
        if test_src:
            synth_test_store.append(test_src)
        obs, result = run_synthesized_test(test_src, sandbox)
        return obs, ProbeRecord(
            tool="run_synthesized_test",
            input_summary=f"run_synthesized_test({len(test_src)} chars)",
            output_summary=obs[:200],
            passed=result.get("passed"),
        )

    if tool_name == "fix_probe":
        diff = args.get("diff", "")
        test_src = args.get("test_source", "") or (synth_test_store[-1] if synth_test_store else "")
        obs, result = fix_probe(diff, test_src, sandbox)
        return obs, ProbeRecord(
            tool="fix_probe",
            input_summary=f"fix_probe(diff={len(diff)} chars)",
            output_summary=obs[:200],
            passed=result.get("passed"),
        )

    # Unknown advanced probe — should not happen since runner guards _TOOL_NAMES.
    msg = f"(dispatch_advanced_probe: unknown tool '{tool_name}')"
    return msg, ProbeRecord(
        tool=tool_name,
        input_summary=str(args)[:120],
        output_summary=msg,
    )
