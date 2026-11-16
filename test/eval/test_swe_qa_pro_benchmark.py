# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codenib.dataset.swe_qa_pro import SWEQAProCase
from codenib.eval.benchmarks.swe_qa_pro import (
    evaluate_pro,
    load_pro_judge,
    main,
    pro_entry,
    run_pro_cases,
)


def case():
    return SWEQAProCase(
        "repo:1",
        "owner/repo",
        "a" * 40,
        {"id": "1", "name": "Fixture"},
        {"class_name": "how", "sub_class_name": "behavior"},
        "How does the fixture work?",
        "PRIVATE GOLD",
    )


def prediction():
    return {
        **case().task_record(),
        "success": True,
        "agent_result": {
            "answer": "Candidate",
            "integration": {
                "agent_class": "codenib.agent.runner.AgentRunner",
            },
        },
    }


def test_pro_answer_and_failure_records_use_relative_paths(tmp_path):
    from codenib.eval.agent_runner.baseline import BaselineTask

    root = tmp_path / "private-parent" / "repo"
    task = BaselineTask(instance_id="repo:1", query="Question?", repo_path=str(root))
    record = pro_entry(
        task=task,
        model="scripted",
        result={"answer": "Candidate"},
        error=f"file not found: {root / 'missing.py'}",
    )
    assert str(tmp_path) not in json.dumps(record)
    assert record["agent_result"]["answer"] == "Candidate"
    assert record["error"] == "file not found: missing.py"
    task = replace(task, query=f"Explain {root / 'src/a.py'}")
    assert (
        pro_entry(task=task, model="scripted", result={"answer": "Candidate"})[
            "question"
        ]
        == task.query
    )


def test_pro_batch_uses_shared_codenib_agent_without_reference_answers(
    tmp_path, monkeypatch
):
    from codenib.eval.agent_runner import qa
    from codenib.eval.benchmarks import swe_qa_pro

    seen = []
    monkeypatch.setattr(swe_qa_pro.subprocess, "check_output", lambda *a, **k: "a" * 40)

    def answer(repo, question, **kwargs):
        seen.append((question, kwargs))
        return {"answer": "Candidate"}

    monkeypatch.setattr(qa, "answer_repository_question", answer)
    output = tmp_path / "answers.jsonl"
    summary = run_pro_cases(
        [case()], repos_root=tmp_path, output=output, model="scripted"
    )
    assert summary.succeeded == 1 and summary.scored == 0
    assert "PRIVATE GOLD" not in repr(seen) + output.read_text()
    row = json.loads(output.read_text())
    assert row["commit_id"] == "a" * 40 and row["agent_result"]["answer"] == "Candidate"
    resumed = run_pro_cases(
        [case()], repos_root=tmp_path, output=output, model="scripted", resume=True
    )
    assert resumed.skipped == 1 and len(seen) == 1
    with pytest.raises(ValueError, match="configuration"):
        run_pro_cases(
            [case()], repos_root=tmp_path, output=output, model="different", resume=True
        )
    monkeypatch.setattr(swe_qa_pro.subprocess, "check_output", lambda *a, **k: "b" * 40)
    failed = tmp_path / "failed.jsonl"
    summary = run_pro_cases(
        [case()], repos_root=tmp_path, output=failed, model="scripted"
    )
    assert summary.failed == 1 and len(seen) == 1
    assert "HEAD differs" in json.loads(failed.read_text())["error"]


def test_pro_paid_commands_require_live_before_loading_models(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--output", str(tmp_path / "answers.jsonl")])
    assert exc.value.code == 2


def test_pro_cli_exposes_generation_resume(tmp_path, monkeypatch):
    from codenib.eval.benchmarks import swe_qa_pro

    seen = []
    monkeypatch.setattr(swe_qa_pro, "load_swe_qa_pro_cases", lambda *a, **k: [case()])

    def run(*args, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(total=1, succeeded=1, failed=0)

    monkeypatch.setattr(swe_qa_pro, "run_pro_cases", run)
    assert (
        main(
            [
                "run",
                "--output",
                str(tmp_path / "answers.jsonl"),
                "--repos-root",
                str(tmp_path),
                "--model",
                "scripted",
                "--resume",
                "--live",
            ]
        )
        == 0
    )
    assert seen[0]["resume"] is True


@pytest.mark.integration
def test_official_pro_whitespace_answer_resumes_without_rejudging(
    tmp_path, monkeypatch
):
    checkout = os.getenv("SWE_QA_PRO_CHECKOUT")
    if not checkout:
        pytest.skip("set SWE_QA_PRO_CHECKOUT to the pinned upstream checkout")
    judge = load_pro_judge(checkout)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps({key: 7 for key in judge.SCORE_KEYS})
        )

    monkeypatch.setattr(
        judge,
        "_make_client",
        lambda provider: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    candidate = prediction()
    candidate["agent_result"]["answer"] = "\n Candidate \t"
    kwargs = dict(output=tmp_path / "scores", judge=judge, workers=1)
    report = evaluate_pro([case()], [candidate], **kwargs)
    assert report["scored"] == 1 and report["mean_successful_total"] == 35
    assert json.loads((kwargs["output"] / "summary.json").read_text())["scored"] == 1
    assert evaluate_pro([case()], [candidate], resume=True, **kwargs)["scored"] == 1
    assert len(calls) == 1


@pytest.mark.integration
def test_official_pro_resume_preserves_previous_scores_and_full_mean(
    tmp_path, monkeypatch
):
    checkout = os.getenv("SWE_QA_PRO_CHECKOUT")
    if not checkout:
        pytest.skip("set SWE_QA_PRO_CHECKOUT to the pinned upstream checkout")
    judge = load_pro_judge(checkout)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        value = 7 if len(calls) == 1 else 9
        return SimpleNamespace(
            output_text=json.dumps({key: value for key in judge.SCORE_KEYS})
        )

    monkeypatch.setattr(
        judge,
        "_make_client",
        lambda provider: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    original = judge.evaluate_jsonl

    def interrupted(input_path, score_path, **kwargs):
        first = tmp_path / "first.jsonl"
        first.write_text(input_path.read_text().splitlines()[0] + "\n")
        original(first, score_path, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(judge, "evaluate_jsonl", interrupted)
    other = replace(case(), instance_id="repo:2", question="Second question?")
    kwargs = dict(output=tmp_path / "scores", judge=judge, workers=1)
    predictions = [prediction(), {**prediction(), **other.task_record()}]
    with pytest.raises(KeyboardInterrupt):
        evaluate_pro([case(), other], predictions, **kwargs)
    assert json.loads((kwargs["output"] / "summary.json").read_text())["scored"] == 1
    monkeypatch.setattr(judge, "evaluate_jsonl", original)
    report = evaluate_pro([case(), other], predictions, resume=True, **kwargs)
    assert len(calls) == 2 and report["scored"] == 2
    assert report["mean_successful_total"] == 40
    assert report["judge"]["average_total_score"] == 45  # current invocation only
    with pytest.raises(ValueError, match="configuration"):
        evaluate_pro(
            [case(), other], predictions, resume=True, model="different", **kwargs
        )
    assert len(calls) == 2


@pytest.mark.integration
def test_actual_pro_judge_preserves_protocol_and_failure_coverage(
    tmp_path, monkeypatch
):
    checkout = os.getenv("SWE_QA_PRO_CHECKOUT")
    if not checkout:
        pytest.skip("set SWE_QA_PRO_CHECKOUT to the pinned upstream checkout")
    judge = load_pro_judge(checkout)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps({key: 7 for key in judge.SCORE_KEYS})
        )

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(judge, "_make_client", lambda provider: client)
    missing = replace(case(), instance_id="repo:2", question="Missing answer?")
    failed = replace(case(), instance_id="repo:3", question="Failed answer?")
    predictions = [
        prediction(),
        {**failed.task_record(), "success": False, "error": "provider unavailable"},
    ]
    result = evaluate_pro(
        [case(), missing, failed],
        predictions,
        output=tmp_path / "scores",
        judge=judge,
        workers=1,
    )
    assert result["selected"] == 3 and result["scored"] == 1 and result["failed"] == 2
    assert result["mean_successful_total"] == 35
    assert [row["scored"] for row in result["coverage"]] == [True, False, False]
    assert len(calls) == 1 and calls[0]["reasoning"] == {"effort": "low"}
    assert calls[0]["text"] == {"verbosity": "low"}
    assert "PRIVATE GOLD" in calls[0]["input"][1]["content"]
    assert (
        judge._parse_scores(json.dumps({key: 11 for key in judge.SCORE_KEYS})) is None
    )
