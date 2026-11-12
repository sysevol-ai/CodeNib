# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Repository question answering and official scoring for SWE-QA-Pro."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path

from codenib.dataset.swe_qa_pro import PRO_DATASET_REVISION, load_swe_qa_pro_cases
from codenib.eval.agent_runner.baseline import BaselineTask
from codenib.eval.agent_runner.batch import run_baseline_batch
from codenib.eval.benchmarks.policy_compat import (
    inspect_git_revision,
    prepare_batch_output,
    write_json_atomic,
)

PRO_UPSTREAM_REVISION = "93ac6a4f3af3fe3f86580f62142f47e97e2cc897"


def pro_entry(*, task, model, result, error=None, **kwargs):
    from codenib.eval.agent_runner.qa import repository_relative_paths

    result = result or {}
    answer = result.get("answer", "")
    success = not error and bool(answer.strip()) and answer.strip() != "No answer found"
    return {
        "instance_id": task.instance_id,
        "repo": task.repo_name,
        **task.metadata,
        "question": task.query,
        "model": model,
        "agent_result": result,
        "error": repository_relative_paths(
            error or (None if success else "missing agent answer"), task.repo_path
        ),
        "success": success,
        "metrics": None,
    }


def run_pro_cases(
    cases, *, repos_root, output, model, policy="graph", max_steps=10, resume=False
):
    from codenib.eval.agent_runner.qa import answer_repository_question

    tasks = [
        BaselineTask(
            instance_id=c.instance_id,
            query=c.question,
            repo_path=str(Path(repos_root) / c.repo_name),
            repo_name=c.repo,
            metadata={
                "commit_id": c.commit_id,
                "cluster": c.cluster,
                "qa_type": c.qa_type,
            },
        )
        for c in cases
    ]

    def answer(task):
        commit = subprocess.check_output(
            ["git", "-C", task.repo_path, "rev-parse", "HEAD"], text=True
        ).strip()
        if commit != task.metadata["commit_id"]:
            raise ValueError("repository HEAD differs from released commit_id")
        return answer_repository_question(
            task.repo_path, task.query, model=model, policy=policy, max_steps=max_steps
        )

    prepare_batch_output(
        output,
        identity={
            "benchmark": "SWE-QA-Pro",
            "revision": PRO_DATASET_REVISION,
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
            entry_builder=pro_entry,
            resume=resume,
        )
    )


def load_pro_judge(checkout):
    """Import the scorer package from the pinned evaluator checkout."""
    root = Path(checkout).resolve()
    revision = inspect_git_revision(root, include_untracked=False)
    if revision.commit != PRO_UPSTREAM_REVISION or revision.dirty is not False:
        raise ValueError(
            f"use SWE-QA-Pro at {PRO_UPSTREAM_REVISION} with clean tracked files"
        )
    package = root / "eval/sweqapro"
    for name, module in tuple(sys.modules.items()):
        if name == "sweqapro" or name.startswith("sweqapro."):
            if (
                not Path(getattr(module, "__file__", ""))
                .resolve()
                .is_relative_to(package)
            ):
                raise ValueError("another sweqapro package is already imported")
    sys.path.insert(0, str(root / "eval"))
    try:
        module = importlib.import_module("sweqapro.scoring.judge")
    finally:
        sys.path.remove(str(root / "eval"))
    if Path(module.__file__).resolve() != package / "scoring/judge.py":
        raise ValueError("unexpected upstream scorer import path")
    return module


def evaluate_pro(
    cases,
    predictions,
    *,
    output,
    judge,
    provider="openai",
    model=None,
    workers=4,
    reasoning_effort="low",
    resume=False,
):
    """Score saved answers and retain coverage for every selected task."""
    indexed = {row["instance_id"]: row for row in predictions}
    if len(indexed) != len(predictions) or indexed.keys() - {
        c.instance_id for c in cases
    }:
        raise ValueError("duplicate or unselected prediction IDs")
    records, coverage = [], []
    for case in cases:
        prediction = indexed.get(case.instance_id)
        result, error = {}, "missing prediction"
        if prediction is not None:
            if any(
                prediction.get(k) != getattr(case, k)
                for k in ("repo", "commit_id", "question")
            ):
                raise ValueError(
                    f"prediction task identity mismatch: {case.instance_id}"
                )
            if prediction.get("success"):
                result, error = prediction.get("agent_result") or {}, None
            else:
                error = prediction.get("error") or "answer generation failed"
        records.append(
            {
                **case.task_record(),
                "answer": case.reference_answer,
                "agent_result": result,
            }
        )
        coverage.append({"instance_id": case.instance_id, "generation_error": error})
    output = Path(output)
    if output.exists() and not resume:
        raise ValueError("output exists; use --resume or a new output")
    if len({case.question for case in cases}) != len(cases):
        raise ValueError(
            "upstream resume identifies questions by text; select unique questions"
        )
    input_path, score_path = output / "judge-input.jsonl", output / "scores.jsonl"
    model = model or judge.DEFAULT_JUDGE_MODELS[provider]
    prepare_batch_output(
        input_path,
        identity={
            "records": records,
            "provider": provider,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "upstream": PRO_UPSTREAM_REVISION,
            "data": PRO_DATASET_REVISION,
        },
        resume=resume,
    )
    if not input_path.exists():
        input_path.write_text("".join(json.dumps(r) + "\n" for r in records))
    elif [
        json.loads(line) for line in input_path.read_text().splitlines() if line
    ] != records:
        raise ValueError("saved judge input differs from the selected predictions")
    expected = {(r["repo"], r["commit_id"], r["question"]): r for r in records}

    def score_rows():
        rows = (
            [json.loads(line) for line in score_path.read_text().splitlines() if line]
            if score_path.exists()
            else []
        )
        keys = [(r["repo"], r["commit_id"], r["question"]) for r in rows]
        if len(keys) != len(set(keys)) or set(keys) - expected.keys():
            raise ValueError("duplicate or unselected official score records")
        for row, key in zip(rows, keys, strict=True):
            source = expected[key]
            candidate = judge._get_candidate(source)
            if (
                row["reference_answer"] != source["answer"]
                or candidate is None
                or row["candidate_answer"] != candidate["answer"]
            ):
                raise ValueError(
                    "official score inputs differ from selected predictions"
                )
            if any(
                type(row.get(k)) is not int or not 1 <= row[k] <= 10
                for k in judge.SCORE_KEYS
            ) or row["total_score"] != sum(row[k] for k in judge.SCORE_KEYS):
                raise ValueError("invalid official score record")
        return rows

    score_rows()  # Reject damaged or foreign checkpoints before any model call.
    if score_path.exists() and score_path.stat().st_size:
        with score_path.open("rb+") as stream:
            stream.seek(-1, 2)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
    summary = None
    try:
        summary = judge.evaluate_jsonl(
            input_path,
            score_path,
            judge_provider=provider,
            judge_model=model,
            max_workers=workers,
            resume=resume,
            reasoning_effort=reasoning_effort,
        )
    finally:
        scored = score_rows()
        report = pro_report(
            cases, coverage, scored, summary, reasoning_effort, provider, model
        )
        write_json_atomic(output / "summary.json", report)
    return report


def pro_report(cases, coverage, scored, summary, reasoning_effort, provider, model):
    scored_keys = {(r["repo"], r["commit_id"], r["question"]) for r in scored}
    for case, row in zip(cases, coverage, strict=True):
        row["scored"] = (case.repo, case.commit_id, case.question) in scored_keys
    report = {
        "selected": len(cases),
        "scored": len(scored),
        "failed": len(cases) - len(scored),
        "mean_successful_total": (
            sum(row["total_score"] for row in scored) / len(scored) if scored else None
        ),
        "judge": summary,
        "coverage": coverage,
        "upstream_revision": PRO_UPSTREAM_REVISION,
        "dataset_revision": PRO_DATASET_REVISION,
        "judge_provider": provider,
        "judge_model": model,
        "reasoning_effort": reasoning_effort if provider == "openai" else None,
    }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run", "evaluate"])
    parser.add_argument("--data", type=Path)
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--question-id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repos-root", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument(
        "--policy", choices=["graph", "bm25", "dense", "hybrid"], default="graph"
    )
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--upstream", type=Path)
    parser.add_argument("--judge", choices=["openai", "deepseek"], default="openai")
    parser.add_argument("--judge-model")
    parser.add_argument("--judge-workers", type=int, default=4)
    parser.add_argument(
        "--reasoning-effort",
        choices=["minimal", "low", "medium", "high"],
        default="low",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.output.exists() and not args.resume:
        parser.error("output exists; use --resume or a new path")
    if args.command == "prepare" and args.resume:
        parser.error("--resume applies only to run/evaluate")
    if args.max_steps <= 0 or args.judge_workers <= 0:
        parser.error("step/worker budgets must be positive")
    if args.command != "prepare" and not args.live:
        parser.error("run/evaluate require --live to enable billed model calls")
    if args.command == "run" and (not args.model or not args.repos_root):
        parser.error("run requires --model and --repos-root")
    if args.command == "evaluate" and (not args.predictions or not args.upstream):
        parser.error("evaluate requires --predictions and --upstream")
    cases = load_swe_qa_pro_cases(
        args.data, repos=args.repo, question_ids=args.question_id, limit=args.limit
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "prepare":
        args.output.write_text(
            "".join(json.dumps(c.task_record()) + "\n" for c in cases)
        )
    elif args.command == "run":
        summary = run_pro_cases(
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
        predictions = [
            json.loads(line)
            for line in args.predictions.read_text().splitlines()
            if line
        ]
        evaluate_pro(
            cases,
            predictions,
            output=args.output,
            judge=load_pro_judge(args.upstream),
            provider=args.judge,
            model=args.judge_model,
            workers=args.judge_workers,
            reasoning_effort=args.reasoning_effort,
            resume=args.resume,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
