# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Repository question answering and official scoring for SWE-QA."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from codenib.dataset.swe_qa import SWE_QA_DATA_REVISION, load_swe_qa_cases
from codenib.eval.agent_runner.baseline import BaselineTask
from codenib.eval.agent_runner.batch import run_baseline_batch
from codenib.eval.benchmarks.policy_compat import (
    inspect_git_revision,
    prepare_batch_output,
    write_json_atomic,
)

SCORE_KEYS = ("correctness", "completeness", "relevance", "clarity", "reasoning")


def check_release(checkout):
    checkout = Path(checkout).resolve()
    revision = inspect_git_revision(checkout, include_untracked=False)
    if revision.commit != SWE_QA_DATA_REVISION or revision.dirty is not False:
        raise ValueError(
            f"use SWE-QA at {SWE_QA_DATA_REVISION} with clean tracked files"
        )


def qa_entry(*, task, model, result, error=None, **kwargs):
    from codenib.eval.agent_runner.qa import repository_relative_paths

    result = result or {}
    answer = result.get("answer", "")
    success = not error and bool(answer.strip()) and answer.strip() != "No answer found"
    return {
        "instance_id": task.instance_id,
        "repo": task.repo_name,
        "base_commit": task.metadata.get("base_commit"),
        "question": task.query,
        "model": model,
        "success": success,
        "answer": answer,
        "native": {key: value for key, value in result.items() if key != "answer"},
        "error": repository_relative_paths(
            error or (None if success else "missing native answer"), task.repo_path
        ),
        "metrics": None,
    }


def run_cases(
    cases,
    *,
    repos_root,
    output,
    model,
    policy="graph",
    max_steps=10,
    resume=False,
):
    from codenib.eval.agent_runner.qa import answer_repository_question

    tasks = [
        BaselineTask(
            instance_id=case.instance_id,
            query=case.question,
            repo_path=str(Path(repos_root) / case.repo_name),
            repo_name=case.repo,
            metadata={"base_commit": case.base_commit},
        )
        for case in cases
    ]

    def answer(task):
        root = Path(task.repo_path)
        # Upstream publishes short Git SHAs. Resolve them unambiguously in the
        # local repository before checking HEAD.
        expected = subprocess.check_output(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--verify",
                task.metadata["base_commit"] + "^{commit}",
            ],
            text=True,
        ).strip()
        actual = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        if actual != expected:
            raise ValueError("repository HEAD differs from the released base commit")
        return answer_repository_question(
            root, task.query, model=model, policy=policy, max_steps=max_steps
        )

    prepare_batch_output(
        output,
        identity={
            "benchmark": "SWE-QA",
            "revision": SWE_QA_DATA_REVISION,
            "model": model,
            "policy": policy,
            "max_steps": max_steps,
            "tasks": [vars(task) for task in tasks],
        },
        resume=resume,
        instance_ids=[task.instance_id for task in tasks],
    )
    return asyncio.run(
        run_baseline_batch(
            tasks,
            runner=answer,
            agent_name="CodeNib",
            model=model,
            metrics_k=[],
            result_path=output,
            entry_builder=qa_entry,
            resume=resume,
        )
    )


def load_judge(checkout):
    check_release(checkout)
    path = Path(checkout) / "Benchmark construction/score/llm-as-a-judge.py"
    spec = importlib.util.spec_from_file_location("_sweqa_official_judge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score_answer


def judge_client(chat):
    """Adapt upstream judge calls to CodeNib's LiteLLM transport.

    LiteLLMChat supplies provider settings; upstream's thinking extra_body is
    omitted from this transport.
    """

    def create(*, model, messages, **kwargs):
        text = chat.complete(messages)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
        )

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


def evaluate_cases(
    cases, predictions, *, scorer, client, model, output=None, resume=False
):
    ids = {case.instance_id for case in cases}
    indexed = {row["instance_id"]: row for row in predictions}
    if len(indexed) != len(predictions) or indexed.keys() - ids:
        raise ValueError("duplicate or unselected prediction IDs")
    if output is not None:
        prepare_batch_output(
            output,
            identity={
                "cases": [vars(case) for case in cases],
                "predictions": predictions,
                "model": model,
                "revision": SWE_QA_DATA_REVISION,
            },
            resume=resume,
        )
    previous = {}
    if resume and output is not None and Path(output).exists():
        previous = {
            row["instance_id"]: row
            for row in json.loads(Path(output).read_text())["per_question"]
        }
    rows = dict(previous)

    def report():
        ordered = [rows[case.instance_id] for case in cases if case.instance_id in rows]
        totals = [
            sum(row["score"][key] for key in SCORE_KEYS)
            for row in ordered
            if row["score"] is not None
        ]
        return {
            "selected": len(cases),
            "scored": len(totals),
            "failed": sum(row["score"] is None for row in ordered),
            "pending": len(cases) - len(ordered),
            "mean_successful_total": sum(totals) / len(totals) if totals else None,
            "per_question": ordered,
            "judge_model": model,
            "upstream_revision": SWE_QA_DATA_REVISION,
            "transport": "CodeNib LiteLLMChat; upstream thinking extra_body omitted",
        }

    for case in cases:
        saved = previous.get(case.instance_id)
        if saved and saved.get("score") is not None:
            continue
        prediction = indexed.get(case.instance_id, {})
        scores, error = None, None
        if (
            prediction.get("question") != case.question
            or prediction.get("repo") != case.repo
            or prediction.get("base_commit") != case.base_commit
        ):
            error = "missing prediction or task identity mismatch"
        elif not prediction.get("success"):
            error = prediction.get("error") or "answer generation failed"
        else:
            try:
                scores = scorer(
                    case.question,
                    case.reference_answer,
                    prediction["answer"],
                    client,
                    model,
                )
                if not isinstance(scores, dict) or any(
                    type(scores.get(key)) is not int or not 1 <= scores[key] <= 20
                    for key in SCORE_KEYS
                ):
                    raise ValueError(
                        "official judge must return five integer scores in 1..20"
                    )
                # Extra model fields are not scoring dimensions.
                scores = {key: scores[key] for key in SCORE_KEYS}
            except Exception as exc:
                scores, error = None, str(exc)
        rows[case.instance_id] = {
            "instance_id": case.instance_id,
            "score": scores,
            "error": error,
        }
        if output is not None:
            write_json_atomic(output, report())
    result = report()
    if output is not None:
        write_json_atomic(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run", "evaluate"])
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--question-id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repos-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--judge-model")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument(
        "--policy", choices=["graph", "bm25", "dense", "hybrid"], default="graph"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.output.exists() and not args.resume:
        parser.error("output exists; use --resume or a new path")
    if args.command == "prepare" and args.resume:
        parser.error("--resume applies only to run/evaluate")
    if args.max_steps <= 0:
        parser.error("--max-steps must be positive")
    if args.command != "prepare" and not args.live:
        parser.error("run/evaluate require --live to enable billed model calls")
    if args.command == "run" and (not args.repos_root or not args.model):
        parser.error("run requires --repos-root and --model")
    if args.command == "evaluate" and (not args.predictions or not args.judge_model):
        parser.error("evaluate requires --predictions and --judge-model")
    check_release(args.upstream)
    cases = load_swe_qa_cases(
        args.upstream, repos=args.repo, question_ids=args.question_id, limit=args.limit
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "prepare":
        rows = [
            {
                "instance_id": c.instance_id,
                "repo": c.repo,
                "base_commit": c.base_commit,
                "question": c.question,
            }
            for c in cases
        ]
        args.output.write_text("".join(json.dumps(r) + "\n" for r in rows))
    elif args.command == "run":
        summary = run_cases(
            cases,
            repos_root=args.repos_root,
            output=args.output,
            model=args.model,
            policy=args.policy,
            max_steps=args.max_steps,
            resume=args.resume,
        )
        print(
            json.dumps(
                {
                    "selected": summary.total,
                    "succeeded": summary.succeeded,
                    "failed": summary.failed,
                }
            )
        )
    else:
        from codenib.llm import LiteLLMChat

        predictions = [
            json.loads(line)
            for line in args.predictions.read_text().splitlines()
            if line
        ]
        chat = LiteLLMChat(model=args.judge_model)
        evaluate_cases(
            cases,
            predictions,
            scorer=load_judge(args.upstream),
            client=judge_client(chat),
            model=args.judge_model,
            output=args.output,
            resume=args.resume,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
