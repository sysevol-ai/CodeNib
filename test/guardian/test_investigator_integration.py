# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Real disposable-snapshot acceptance test for Guardian L3."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from codeminer.guardian.investigator import (
    CurrentSnapshotSandbox,
    InvestigationTask,
    run_investigation,
)
from codeminer.guardian.loop import Hypothesis


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(call):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=[call])
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
    )


class DeterministicInvestigatorModel:
    """Provider-shaped model used only to drive the real L3 runtime."""

    max_tokens = 1_024

    def __init__(self):
        self.responses = [
            _response(
                _tool_call(
                    "read",
                    "read_code",
                    {"path": "pkg/mod.py", "start_line": 1, "end_line": 2},
                )
            ),
            _response(
                _tool_call(
                    "test",
                    "run_existing_test",
                    {"node_id": "test_contract.py::test_target_contract"},
                )
            ),
            _response(
                _tool_call(
                    "submit",
                    "submit_verdict",
                    {
                        "verdict": "refuted",
                        "reasoning": "The repository's contract test passes at HEAD.",
                        "cites": ["test"],
                    },
                )
            ),
        ]
        self.calls = []

    def _call_raw(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


@pytest.mark.integration
def test_real_probe_in_disposable_snapshot_parses_report_and_stays_in_grant(
    tmp_path,
):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "mod.py").write_text(
        "def target():\n    return 1\n", encoding="utf-8"
    )
    (repo / "test_contract.py").write_text(
        "from pkg.mod import target\n\n"
        "def test_target_contract():\n"
        "    assert target() == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "guardian@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Guardian Test"], cwd=repo, check=True
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    hypothesis = Hypothesis.create(
        claim="pkg.mod.target no longer returns one",
        consequence="dependent callers break",
        remedy="restore the return contract",
        origin="exploration",
        locus=["pkg/mod.py"],
    )
    task = InvestigationTask(
        hypothesis=hypothesis,
        obligation="Does target() still return one at HEAD?",
        excerpts=[],
        commit_diff="",
        prior_attempts=[],
        grant_tokens=12_000,
        deadline_s=60,
    )
    model = DeterministicInvestigatorModel()
    with CurrentSnapshotSandbox(str(repo)) as sandbox:
        result = run_investigation(
            task,
            model,
            retriever=None,
            sandbox=sandbox,
            repo_identity=str(repo),
            commit=commit,
        )

    assert result.exit_status == "submitted"
    assert result.verdict == "refuted"
    assert result.evidence_status == "valid"
    assert result.budget.actual == 45
    assert result.budget.actual <= result.budget.granted
    assert result.tool_calls[1].result.structured_content.commit == commit
    assert result.tool_calls[1].result.structured_content.tests
    assert all(call["tools"] for call in model.calls)
