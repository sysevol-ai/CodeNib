# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from codenib.dataset.swe_qa import SWEQACase
from codenib.eval.agent_runner.baseline import BaselineTask
from codenib.eval.agent_runner.batch import run_baseline_batch
from codenib.eval.benchmarks.swe_qa import (
    SCORE_KEYS,
    evaluate_cases,
    judge_client,
    load_judge,
    main,
    qa_entry,
    run_cases,
)


def cases():
    return (SWEQACase("repo:1", "owner/repo", "abcdef1", "Question?", "PRIVATE GOLD"),)


def prediction():
    return {
        "instance_id": "repo:1",
        "repo": "owner/repo",
        "base_commit": "abcdef1",
        "question": "Question?",
        "answer": "Candidate",
        "success": True,
    }


def test_qa_uses_shared_batch_without_localization_metrics(tmp_path):
    task = BaselineTask(instance_id="repo:1", query="Question?", repo_path="/repo")
    path = tmp_path / "answers.jsonl"
    calls = []

    def answer(task):
        calls.append(task.query)
        return {"answer": "Candidate", "steps": 2}

    kwargs = dict(
        runner=answer,
        agent_name="QAAgent",
        model="scripted",
        metrics_k=[],
        result_path=path,
        entry_builder=qa_entry,
    )
    first = asyncio.run(run_baseline_batch([task], **kwargs))
    second = asyncio.run(run_baseline_batch([task], resume=True, **kwargs))
    assert first.succeeded == 1 and first.scored == 0
    assert second.skipped == 1 and calls == ["Question?"]
    row = json.loads(path.read_text())
    assert row["answer"] == "Candidate" and row["native"]["steps"] == 2
    assert row["metrics"] is None
    assert "answer" not in row["native"]


def test_answer_and_failure_records_use_relative_source_paths(tmp_path):
    root = tmp_path / "private-parent" / "repo"
    task = BaselineTask(instance_id="repo:1", query="Question?", repo_path=str(root))
    record = qa_entry(
        task=task,
        model="scripted",
        result={"answer": "Candidate"},
        error=f"file not found: {root / 'missing.py'}",
    )
    assert str(tmp_path) not in json.dumps(record)
    assert record["answer"] == "Candidate"
    assert record["error"] == "file not found: missing.py"
    task = BaselineTask(
        instance_id="repo:1", query=f"Explain {root / 'src/a.py'}", repo_path=str(root)
    )
    assert (
        qa_entry(task=task, model="scripted", result={"answer": "Candidate"})[
            "question"
        ]
        == task.query
    )


def test_batch_forwards_question_only_and_records_failures(tmp_path, monkeypatch):
    from codenib.eval.agent_runner import qa
    from codenib.eval.benchmarks import swe_qa

    seen = []
    monkeypatch.setattr(swe_qa.subprocess, "check_output", lambda *a, **k: "abcdef1\n")

    def answer(root, question, **kwargs):
        seen.append((question, kwargs))
        return {"answer": "Candidate"}

    monkeypatch.setattr(qa, "answer_repository_question", answer)
    output = tmp_path / "answers.jsonl"
    summary = run_cases(cases(), repos_root=tmp_path, output=output, model="scripted")
    assert summary.succeeded == 1
    assert "PRIVATE GOLD" not in repr(seen)
    assert json.loads(output.read_text())["base_commit"] == "abcdef1"

    def fail(*a, **k):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(qa, "answer_repository_question", fail)
    output = tmp_path / "failed.jsonl"
    summary = run_cases(cases(), repos_root=tmp_path, output=output, model="scripted")
    assert summary.failed == 1
    assert json.loads(output.read_text())["error"] == "provider unavailable"


def test_judge_keeps_missing_answers_in_coverage():
    another = SWEQACase("repo:2", "owner/repo", "abcdef1", "Other?", "Other gold")
    seen = []

    def scorer(question, reference, candidate, client, model):
        seen.append((reference, candidate))
        return {key: 10 for key in SCORE_KEYS}

    report = evaluate_cases(
        (*cases(), another),
        [prediction()],
        scorer=scorer,
        client=None,
        model="scripted",
    )
    assert report["selected"] == 2 and report["scored"] == report["failed"] == 1
    assert seen == [("PRIVATE GOLD", "Candidate")]
    assert report["mean_successful_total"] == 50


@pytest.mark.parametrize("extra", [50, {"explanation": "ignored"}])
def test_extra_judge_fields_neither_inflate_totals_nor_lose_results(extra):
    report = evaluate_cases(
        cases(),
        [prediction()],
        scorer=lambda *a: {
            **{key: 10 for key in SCORE_KEYS},
            "extra": extra,
        },
        client=None,
        model="scripted",
    )
    assert report["mean_successful_total"] == 50
    assert set(report["per_question"][0]["score"]) == set(SCORE_KEYS)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {"correctness": 10},
        {key: True for key in SCORE_KEYS},
        {key: 21 for key in SCORE_KEYS},
    ],
)
def test_invalid_judge_scores_are_retained_as_failures(bad):
    report = evaluate_cases(
        cases(), [prediction()], scorer=lambda *a: bad, client=None, model="scripted"
    )
    assert report["scored"] == 0 and report["failed"] == 1


def test_judge_checkpoints_survive_interruption_and_resume_without_rebilling(tmp_path):
    second = SWEQACase("repo:2", "owner/repo", "abcdef1", "Other?", "Other gold")
    selected = (*cases(), second)
    predictions = [
        prediction(),
        {
            **prediction(),
            "instance_id": second.instance_id,
            "question": second.question,
        },
    ]
    output = tmp_path / "scores.json"
    calls = []

    def scorer(question, *args):
        calls.append(question)
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return {key: 10 for key in SCORE_KEYS}

    with pytest.raises(KeyboardInterrupt):
        evaluate_cases(
            selected,
            predictions,
            scorer=scorer,
            client=None,
            model="scripted",
            output=output,
        )
    assert json.loads(output.read_text())["scored"] == 1
    report = evaluate_cases(
        selected,
        predictions,
        scorer=scorer,
        client=None,
        model="scripted",
        output=output,
        resume=True,
    )
    assert calls == ["Question?", "Other?", "Other?"]
    assert report["scored"] == 2 and report["pending"] == 0
    with pytest.raises(ValueError, match="configuration"):
        evaluate_cases(
            selected,
            predictions,
            scorer=scorer,
            client=None,
            model="different",
            output=output,
            resume=True,
        )
    assert len(calls) == 3


def test_generation_resume_is_exposed_and_bound_to_model(tmp_path, monkeypatch):
    from codenib.eval.agent_runner import qa
    from codenib.eval.benchmarks import swe_qa

    calls = []
    monkeypatch.setattr(swe_qa.subprocess, "check_output", lambda *a, **k: "abcdef1\n")

    def answer(*args, **kwargs):
        calls.append(1)
        return {"answer": "Candidate"}

    monkeypatch.setattr(qa, "answer_repository_question", answer)
    kwargs = dict(
        repos_root=tmp_path,
        output=tmp_path / "answers.jsonl",
        model="scripted",
    )
    run_cases(cases(), **kwargs)
    assert run_cases(cases(), resume=True, **kwargs).skipped == 1
    assert len(calls) == 1
    with pytest.raises(ValueError, match="configuration"):
        run_cases(cases(), resume=True, **{**kwargs, "model": "other"})
    assert len(calls) == 1


def test_paid_commands_require_explicit_live(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--upstream", str(tmp_path), "--output", str(tmp_path / "out")])
    assert exc.value.code == 2


def test_cli_forwards_resume_to_generation(tmp_path, monkeypatch):
    from codenib.eval.benchmarks import swe_qa

    seen = []
    monkeypatch.setattr(swe_qa, "check_release", lambda *args: None)
    monkeypatch.setattr(swe_qa, "load_swe_qa_cases", lambda *a, **k: cases())

    def run(*args, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(total=1, succeeded=1, failed=0)

    monkeypatch.setattr(swe_qa, "run_cases", run)
    assert (
        main(
            [
                "run",
                "--upstream",
                str(tmp_path),
                "--repos-root",
                str(tmp_path),
                "--output",
                str(tmp_path / "out"),
                "--model",
                "scripted",
                "--live",
                "--resume",
            ]
        )
        == 0
    )
    assert seen[0]["resume"] is True


@pytest.mark.integration
def test_actual_judge_prompt_and_parser_with_scripted_transport():
    checkout = os.getenv("SWE_QA_CHECKOUT")
    if not checkout:
        pytest.skip("set SWE_QA_CHECKOUT to the pinned upstream checkout")
    seen = []

    class Chat:
        def complete(self, messages):
            seen.append(messages)
            return json.dumps(
                {
                    key: 10
                    for key in (
                        "correctness",
                        "completeness",
                        "relevance",
                        "clarity",
                        "reasoning",
                    )
                }
            )

    result = evaluate_cases(
        cases(),
        [prediction()],
        scorer=load_judge(checkout),
        client=judge_client(Chat()),
        model="scripted",
    )
    assert result["mean_successful_total"] == 50
    assert len(seen) == 1
    assert "PRIVATE GOLD" in seen[0][0]["content"][0]["text"]
    assert "Candidate" in seen[0][0]["content"][0]["text"]
