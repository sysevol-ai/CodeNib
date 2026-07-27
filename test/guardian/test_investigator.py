# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Load-bearing tests for the Repository Guardian L3 loop."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from codeminer.guardian.investigator import InvestigationTask, run_investigation
from codeminer.guardian.investigator.environment import TestRecipe as Recipe
from codeminer.guardian.investigator.types import CommandResult, ProcessStatus
from codeminer.guardian.loop import Hypothesis

RECIPE = Recipe(("python", "-m", "pytest"), "test")


def _call(call_id, name, **arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(*calls, total=15, content=""):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=list(calls))
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=total - 10, total_tokens=total
        ),
    )


class ScriptedLLM:
    max_tokens = 1_024

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _call_raw(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.responses.pop(0)


class FakeSandbox:
    def __init__(self, root: Path, statuses=None):
        self.repo_path = str(root)
        self.statuses = list(statuses or ["passed"])
        self.files = {"pkg/mod.py": "def target():\n    return 1\n"}
        (root / "pkg").mkdir(parents=True, exist_ok=True)
        (root / "pkg" / "mod.py").write_text(self.files["pkg/mod.py"], encoding="utf-8")

    def read_file(self, path):
        return self.files.get(path, "")

    def write_file(self, path, content):
        self.files[path] = content

    def run_command(self, command, *, timeout=60, env=None):
        if command[0] == "patch":
            return CommandResult(
                ProcessStatus.EXITED,
                0,
                None,
                list(command),
                self.repo_path,
                "abc",
            )
        status = self.statuses.pop(0)
        report_arg = next(item for item in command if item.startswith("--junitxml="))
        report = Path(self.repo_path) / report_arg.split("=", 1)[1]
        report.parent.mkdir(parents=True, exist_ok=True)
        child = {
            "passed": "",
            "failed": "<failure>pkg/mod.py target contract</failure>",
            "error": "<error>ImportError</error>",
            "skipped": "<skipped />",
            "empty": None,
        }[status]
        body = (
            ""
            if child is None
            else (
                '<testcase classname="pkg.mod" name="test_target">'
                f"{child}</testcase>"
            )
        )
        report.write_text(
            f"<testsuites><testsuite>{body}</testsuite></testsuites>",
            encoding="utf-8",
        )
        exit_code = {"passed": 0, "failed": 1, "error": 2, "skipped": 0, "empty": 5}[
            status
        ]
        return CommandResult(
            ProcessStatus.EXITED,
            exit_code,
            None,
            list(command),
            self.repo_path,
            "abc",
            stdout="pkg/mod.py target contract",
        )


class UnavailableSandbox(FakeSandbox):
    def run_command(self, command, *, timeout=60, env=None):
        return CommandResult(
            ProcessStatus.SPAWN_FAILED,
            None,
            None,
            list(command),
            self.repo_path,
            "abc",
            stderr="interpreter unavailable",
        )


class ProbeSandbox(FakeSandbox):
    def run_command(self, command, *, timeout=60, env=None):
        if command[-1] == "_guardian_probe.py":
            return CommandResult(
                ProcessStatus.EXITED,
                0,
                None,
                list(command),
                self.repo_path,
                "abc",
                stdout="contract mismatch reproduced",
            )
        return super().run_command(command, timeout=timeout, env=env)


def _task(grant=20_000):
    hypothesis = Hypothesis.create(
        claim="pkg.mod.target violates its contract",
        consequence="callers receive invalid results",
        remedy="restore the contract",
        origin="exploration",
        locus=["pkg/mod.py"],
    )
    return InvestigationTask(
        hypothesis=hypothesis,
        obligation="Does pkg.mod.target return the invalid value at HEAD?",
        excerpts=[],
        commit_diff="diff --git a/pkg/mod.py b/pkg/mod.py",
        prior_attempts=[],
        grant_tokens=grant,
        deadline_s=30,
    )


def test_every_model_call_keeps_the_tool_protocol(tmp_path):
    llm = ScriptedLLM([_response(content="plain answer")])
    result = run_investigation(
        _task(), llm, MagicMock(), FakeSandbox(tmp_path), recipe=RECIPE
    )
    assert result.exit_status == "no_progress"
    assert llm.calls
    assert all(call[1]["tools"] for call in llm.calls)


def test_budget_reservation_prevents_unaffordable_call(tmp_path):
    llm = ScriptedLLM([])
    result = run_investigation(
        _task(grant=100), llm, MagicMock(), FakeSandbox(tmp_path), recipe=RECIPE
    )
    assert result.exit_status == "budget_exceeded"
    assert result.budget.actual == 0
    assert llm.calls == []


def test_model_backend_failure_is_explicitly_degraded(tmp_path):
    llm = MagicMock()
    llm.max_tokens = 1_024
    llm._call_raw.side_effect = RuntimeError("backend unavailable")

    result = run_investigation(
        _task(),
        llm,
        MagicMock(),
        FakeSandbox(tmp_path),
        recipe=RECIPE,
    )

    assert result.exit_status == "environment_unavailable"
    assert result.evidence_status == "environment"
    assert result.degraded is True


def test_unavailable_test_environment_does_not_block_investigator(tmp_path):
    llm = ScriptedLLM([])
    result = run_investigation(
        _task(),
        llm,
        MagicMock(),
        UnavailableSandbox(tmp_path),
        repo_identity=str(tmp_path),
        commit="abc",
    )
    assert result.exit_status == "environment_unavailable"
    assert result.evidence_status == "environment"
    assert result.degraded is True
    assert result.budget.actual == 0
    assert len(llm.calls) == 1
    offered = {tool["function"]["name"] for tool in llm.calls[0][1]["tools"]}
    assert "read_code" in offered
    assert "run_probe" in offered
    assert "run_existing_test" not in offered


def test_source_grounded_static_verdict_needs_no_pytest(tmp_path):
    llm = ScriptedLLM(
        [
            _response(
                _call(
                    "read",
                    "read_code",
                    path="pkg/mod.py",
                    start_line=1,
                    end_line=2,
                )
            ),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="confirmed",
                    reasoning=(
                        "The exact source returns the value forbidden by the "
                        "stated contract."
                    ),
                    cites=["read"],
                )
            ),
        ]
    )

    result = run_investigation(
        _task(),
        llm,
        MagicMock(),
        UnavailableSandbox(tmp_path),
        repo_identity=str(tmp_path),
        commit="abc",
    )

    assert result.exit_status == "submitted"
    assert result.evidence_status == "source"
    assert result.degraded is True
    assert result.capabilities["pytest"] is False
    assert result.environment_diagnostics["guardian_interpreter"]
    assert "task_interpreter" in result.environment_diagnostics
    assert result.verdict == "confirmed"
    assert result.cites == ["read"]


def test_failed_retrieval_cannot_be_cited_as_source_evidence(tmp_path):
    retriever = MagicMock()
    retriever.query.side_effect = RuntimeError("index unavailable")
    llm = ScriptedLLM(
        [
            _response(
                _call(
                    "retrieve",
                    "retrieve_evidence",
                    query="target contract",
                )
            ),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="confirmed",
                    reasoning="The retrieval proves the mismatch.",
                    cites=["retrieve"],
                )
            ),
        ]
    )

    result = run_investigation(
        _task(),
        llm,
        retriever,
        FakeSandbox(tmp_path),
        recipe=RECIPE,
        max_rounds=2,
    )

    assert result.verdict == "inconclusive"
    assert result.evidence_status == "invalid"
    assert result.tool_calls[-1].result.is_error is True
    assert "source span" in result.tool_calls[-1].result.content


def test_model_designed_probe_can_confirm_without_specialized_probe_type(tmp_path):
    llm = ScriptedLLM(
        [
            _response(
                _call(
                    "read",
                    "read_code",
                    path="pkg/mod.py",
                    start_line=1,
                    end_line=2,
                )
            ),
            _response(
                _call(
                    "probe",
                    "run_probe",
                    source=(
                        "from pathlib import Path\n"
                        "print(Path('pkg/mod.py').read_text())\n"
                    ),
                    snapshot="current",
                )
            ),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="confirmed",
                    reasoning="The source and executed probe establish the mismatch.",
                    cites=["read", "probe"],
                )
            ),
        ]
    )

    result = run_investigation(
        _task(),
        llm,
        MagicMock(),
        ProbeSandbox(tmp_path),
        recipe=RECIPE,
    )

    assert result.verdict == "confirmed"
    assert result.evidence_status == "valid"
    assert result.tool_calls[1].skill_id == "run_probe"


def test_refuted_verdict_requires_source_and_parsed_pass(tmp_path):
    llm = ScriptedLLM(
        [
            _response(
                _call("read", "read_code", path="pkg/mod.py", start_line=1, end_line=2)
            ),
            _response(
                _call("test", "run_existing_test", node_id="test_mod.py::test_target")
            ),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="refuted",
                    reasoning="The existing contract test passes.",
                    cites=["test"],
                )
            ),
        ]
    )
    result = run_investigation(
        _task(), llm, MagicMock(), FakeSandbox(tmp_path, ["passed"]), recipe=RECIPE
    )
    assert result.verdict == "refuted"
    assert result.exit_status == "submitted"
    assert result.evidence_status == "valid"
    assert result.cites == ["test"]
    assert result.source_spans[0].path == "pkg/mod.py"


def test_bound_fix_transition_can_confirm(tmp_path):
    source = "def test_target():\n    assert False"
    llm = ScriptedLLM(
        [
            _response(
                _call("read", "read_code", path="pkg/mod.py", start_line=1, end_line=2)
            ),
            _response(
                _call(
                    "synth",
                    "synthesize_test",
                    target_symbol="pkg.mod.target",
                    body=source,
                )
            ),
            _response(_call("run", "run_synthesized_test", tool_call_id="synth")),
            _response(
                _call(
                    "fix",
                    "corroborate",
                    tool_call_id="run",
                    method="fix",
                    diff="--- a/pkg/mod.py\n+++ b/pkg/mod.py\n",
                )
            ),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="confirmed",
                    reasoning="The exact failing test passes after the minimal reversal.",
                    cites=["run", "fix"],
                )
            ),
        ]
    )
    result = run_investigation(
        _task(),
        llm,
        MagicMock(),
        FakeSandbox(tmp_path, ["failed", "passed"]),
        recipe=RECIPE,
    )
    assert result.verdict == "confirmed"
    assert result.evidence_status == "valid"
    assert result.evidence_diff.startswith("--- a/pkg/mod.py")
    assert result.tool_calls[3].parent_tool_call_id == "run"
    assert "FAIL_TO_PASS" in result.tool_calls[3].result.content


def test_fix_for_uncited_test_cannot_confirm(tmp_path):
    source = "def test_target():\n    assert False"
    llm = ScriptedLLM(
        [
            _response(
                _call("read", "read_code", path="pkg/mod.py", start_line=1, end_line=2)
            ),
            _response(
                _call(
                    "synth",
                    "synthesize_test",
                    target_symbol="pkg.mod.target",
                    body=source,
                )
            ),
            _response(_call("run", "run_synthesized_test", tool_call_id="synth")),
            _response(
                _call(
                    "fix",
                    "corroborate",
                    tool_call_id="run",
                    method="fix",
                    diff="--- a/pkg/mod.py\n+++ b/pkg/mod.py\n",
                )
            ),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="confirmed",
                    reasoning="Unsupported citation set.",
                    cites=["fix"],
                )
            ),
        ]
    )
    result = run_investigation(
        _task(),
        llm,
        MagicMock(),
        FakeSandbox(tmp_path, ["failed", "passed"]),
        recipe=RECIPE,
        max_rounds=5,
    )
    assert result.verdict == "inconclusive"
    assert result.exit_status == "no_progress"
    assert result.evidence_status == "invalid"
    assert result.tool_calls[-1].result.is_error is True


def test_error_only_test_cannot_be_submitted_as_behavioural_verdict(tmp_path):
    llm = ScriptedLLM(
        [
            _response(
                _call("read", "read_code", path="pkg/mod.py", start_line=1, end_line=2)
            ),
            _response(
                _call("test", "run_existing_test", node_id="test_mod.py::test_target")
            ),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="confirmed",
                    reasoning="Import failed.",
                    cites=["test"],
                )
            ),
        ]
    )
    result = run_investigation(
        _task(),
        llm,
        MagicMock(),
        FakeSandbox(tmp_path, ["error"]),
        recipe=RECIPE,
        max_rounds=3,
    )
    assert result.verdict == "inconclusive"
    assert result.evidence_status == "environment"


def test_each_tool_call_is_checkpointed(tmp_path):
    checkpoint = tmp_path / "out" / "investigation.json"
    llm = ScriptedLLM(
        [
            _response(
                _call("read", "read_code", path="pkg/mod.py", start_line=1, end_line=2)
            ),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="inconclusive",
                    reasoning="More evidence is needed.",
                    cites=[],
                )
            ),
        ]
    )
    result = run_investigation(
        _task(),
        llm,
        MagicMock(),
        FakeSandbox(tmp_path),
        recipe=RECIPE,
        checkpoint_path=checkpoint,
    )
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert result.exit_status == "submitted"
    assert [item["tool_call_id"] for item in saved["run"]["tool_calls"]] == [
        "read",
        "submit",
    ]
    assert saved["run"]["exit_status"] == "submitted"
    assert saved["run"]["verdict"] == result.verdict


def test_non_submitted_terminal_outcome_replaces_running_checkpoint(tmp_path):
    checkpoint = tmp_path / "investigation.json"
    result = run_investigation(
        _task(),
        ScriptedLLM([_response(content="plain answer")]),
        MagicMock(),
        FakeSandbox(tmp_path),
        recipe=RECIPE,
        checkpoint_path=checkpoint,
    )

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert result.exit_status == "no_progress"
    assert saved["run"]["exit_status"] == "no_progress"
    assert saved["run"]["reasoning"] == "Model returned no Guardian tool call."


def test_l3_compacts_its_own_transcript_and_resets_session(tmp_path):
    class Session:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
            self.reset_count = 0
            self.closed = False

        def _call_raw(self, messages, **kwargs):
            self.calls.append((list(messages), kwargs))
            return self.responses.pop(0)

        def reset(self):
            self.reset_count += 1

        def close(self):
            self.closed = True

    class SessionLLM:
        model = "test-model"
        max_tokens = 1_024

        def __init__(self, session):
            self.session = session

        def start_agent_loop(self):
            return self.session

    sandbox = FakeSandbox(tmp_path)
    large_source = "\n".join(
        " ".join(f"value_{index}_{column}" for column in range(12))
        for index in range(200)
    )
    sandbox.files["pkg/mod.py"] = large_source
    (tmp_path / "pkg" / "mod.py").write_text(large_source, encoding="utf-8")
    session = Session(
        [
            _response(
                _call(
                    "read",
                    "read_code",
                    path="pkg/mod.py",
                    start_line=1,
                    end_line=200,
                )
            ),
            _response(content="The source span is retained; submit inconclusive."),
            _response(
                _call(
                    "submit",
                    "submit_verdict",
                    verdict="inconclusive",
                    reasoning="The source alone does not establish behaviour.",
                    cites=[],
                )
            ),
        ]
    )
    checkpoint = tmp_path / "investigation.json"

    result = run_investigation(
        _task(grant=100_000),
        SessionLLM(session),
        MagicMock(),
        sandbox,
        recipe=RECIPE,
        checkpoint_path=checkpoint,
        max_context_tokens=6_000,
        context_reserve_tokens=1_000,
    )

    assert result.exit_status == "submitted"
    assert session.reset_count == 1
    assert session.closed
    assert session.calls[1][1]["_guardian_text_response"] is True
    archives = list((tmp_path / "context").glob("*.json"))
    assert len(archives) == 1
    assert "value_100" in archives[0].read_text(encoding="utf-8")
