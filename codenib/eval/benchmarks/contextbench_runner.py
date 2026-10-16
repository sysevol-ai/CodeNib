# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prepare, run, resume, and officially score ContextBench with CodeNib."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from filelock import FileLock

from codenib.dataset.contextbench import (
    CONTEXTBENCH_CONFIGS,
    CONTEXTBENCH_DATASET,
    CONTEXTBENCH_DATASET_REVISION,
    ContextBenchCase,
    cases_digest,
    load_contextbench_cases,
)

from .contextbench import (
    CONTEXTBENCH_UPSTREAM_REVISION,
    contextbench_gold_record,
    contextbench_prediction,
)
from .policy_compat import write_json_atomic

_WORKER = Path(__file__).with_name("_contextbench_evaluate.py")
_SKILLS = ("bm25_search", "repository_search")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.PIPE, text=True, timeout=300
    ).rstrip("\r\n")


def audit_contextbench_snapshot(repo: Path, commit: str) -> dict[str, Any]:
    """Reject revision drift or modified/untracked input, including ignored files."""
    observed = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    ignored = _git(repo, "ls-files", "--others", "--ignored", "--exclude-standard")
    flags = _git(repo, "ls-files", "-v")
    hidden = [line for line in flags.splitlines() if line and line[0] != "H"]
    submodules = _git(repo, "submodule", "status")
    if observed != commit or status or ignored or hidden:
        raise ValueError("snapshot audit failed: wrong revision or dirty/hidden files")
    for line in submodules.splitlines():
        if not line.startswith(" "):
            raise ValueError("snapshot audit failed: unprepared or modified submodule")
        sha, _, suffix = line.strip().partition(" ")
        subpath = (repo / suffix.split(" (", 1)[0]).resolve()
        if not subpath.is_relative_to(repo.resolve()):
            raise ValueError("submodule path escapes checkout")
        audit_contextbench_snapshot(subpath, sha)
    return {"commit": observed, "clean": True}


def prepare_contextbench_repo(case: ContextBenchCase, repos_root: Path) -> Path:
    """Create a private clone per task; never reset or clean an existing checkout."""
    repo = repos_root.resolve() / case.instance_id
    if not repo.exists():
        repos_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".checkout-", dir=repos_root))
        try:
            _git(temporary, "init")
            _git(temporary, "remote", "add", "origin", case.repo_url)
            _git(temporary, "fetch", "--depth", "1", "origin", case.base_commit)
            _git(temporary, "checkout", "--detach", case.base_commit)
            _git(
                temporary,
                "submodule",
                "update",
                "--init",
                "--recursive",
                "--depth",
                "1",
            )
            temporary.rename(repo)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    audit_contextbench_snapshot(repo, case.base_commit)
    return repo


def _bind_source_tool(execute, *, repo: Path, path_argument: str):
    def bound(**kwargs):
        raw = str(kwargs.get(path_argument) or ".")
        target = (repo / raw).resolve()
        if not target.is_relative_to(repo):
            raise ValueError("tool path escapes benchmark checkout")
        kwargs[path_argument] = str(target)
        result = execute(**kwargs)
        if path_argument == "path" and target != repo:
            # grep/glob return paths relative to their search root. Give the
            # model and exporter stable paths relative to the task checkout.
            base = target if target.is_dir() else target.parent
            lines = []
            for line in result.splitlines():
                match = re.match(r"^(.*?)(:\d+:?.*)$", line)
                name, tail = (match[1], match[2]) if match else (line, "")
                found = base / name
                if found.exists() and found.resolve().is_relative_to(repo):
                    line = found.relative_to(repo).as_posix() + tail
                lines.append(line)
            return "\n".join(lines)
        return result

    return bound


def run_contextbench_agent(
    case: ContextBenchCase,
    *,
    repo: Path,
    cache_dir: Path,
    model: str,
    max_turns: int,
    max_tokens: int,
    top_k: int,
    timeout: float,
    skills: Sequence[str],
) -> dict[str, Any]:
    """Use the shared native AgentRunner, retaining its actual delivered messages."""
    from codenib.agent.runner import AgentRunner
    from codenib.agent.skills.registry import SkillRegistry
    from codenib.cli import detect_languages
    from codenib.compiler.params import SessionContext
    from codenib.eval.agent_runner.contexts import (
        AgentSkillContextSpec,
        load_agent_skill_contexts,
    )
    from codenib.index.embedding.model_policy import (
        DEFAULT_EMBEDDING_DIMENSION,
        DEFAULT_EMBEDDING_MODEL,
    )
    from codenib.llm.litellm_chat import LiteLLMChat, RetryConfig

    if not case.problem_statement.strip():
        raise ValueError("dataset task is missing problem_statement")
    started = time.perf_counter()
    languages = detect_languages(repo)
    if not languages:
        raise ValueError("no supported source languages detected")
    load_agent_skill_contexts(
        repo_path=str(repo),
        cache_dir=str(cache_dir),
        spec=AgentSkillContextSpec(
            skill_ids=skills,
            languages=languages,
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_dimension=DEFAULT_EMBEDDING_DIMENSION,
            top_k=top_k,
        ),
    )
    registry = SkillRegistry()
    for skill in skills:
        meta = registry.get(skill)
        if meta is not None:
            meta.defaults = {**(meta.defaults or {}), "top_k": top_k}
    build_seconds = time.perf_counter() - started
    llm = LiteLLMChat(
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
        retry=RetryConfig(max_retries=1),
        extra_kwargs={"timeout": timeout},
    )
    runner = AgentRunner(
        llm=llm,
        registry=registry,
        allow_skills=set(skills),
        default_tool_ids={"read", "grep", "glob"},
        max_turns=max_turns,
        force_final_answer=True,
        first_turn_tool_choice="required",
        force_first_turn_only=True,
        session_ctx=SessionContext(repo_path=str(repo)),
    )
    for tool_id, argument in (
        ("read", "file_path"),
        ("grep", "path"),
        ("glob", "path"),
    ):
        tool = runner.tool_registry.get(tool_id)
        tool.executor_fn = _bind_source_tool(
            tool.executor_fn, repo=repo, path_argument=argument
        )
    result = runner.run(case.problem_statement)
    prediction = contextbench_prediction(
        instance_id=case.instance_id, messages=result.messages, repo_path=repo
    )
    return {
        "instance_id": case.instance_id,
        "success": True,
        "prediction": prediction,
        "answer": result.answer,
        "messages": result.messages,
        "usage": result.usage.to_dict() if result.usage else None,
        "usage_records": [record.to_dict() for record in result.usage_records],
        "total_turns": result.total_turns,
        "timing_seconds": {
            "build": build_seconds,
            "agent": result.total_duration_ms / 1000,
        },
        "trace": result.trace.to_dict() if result.trace else None,
    }


def contextbench_evaluator_command(upstream: Path, python: str) -> list[str]:
    """Check the upstream revision before executing its code in a subprocess."""
    upstream = upstream.resolve()
    if _git(upstream, "rev-parse", "HEAD") != CONTEXTBENCH_UPSTREAM_REVISION:
        raise ValueError(
            "ContextBench evaluator revision does not match the pinned revision"
        )
    if _git(
        upstream, "status", "--porcelain", "--untracked-files=all", "--", "contextbench"
    ):
        raise ValueError("ContextBench evaluator source has local modifications")
    flags = _git(upstream, "ls-files", "-v", "--", "contextbench")
    if any(line and line[0] != "H" for line in flags.splitlines()):
        raise ValueError("ContextBench evaluator source has hidden index flags")
    return [python, str(_WORKER), "--upstream", str(upstream)]


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    temporary = path.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def _implementation_digest() -> str:
    """Bind resume to installed Python/skill source, including uncommitted edits."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".yaml", ".md"}:
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def evaluate_contextbench(
    *,
    cases: Sequence[ContextBenchCase],
    predictions: Sequence[dict[str, Any]],
    repos_root: Path,
    output: Path,
    command: Sequence[str],
    timeout: float = 1800,
) -> dict[str, Any]:
    """Score every selected case; missing predictions and scoring failures stay visible."""
    aliases = {
        key: c for c in cases for key in (c.instance_id, c.original_inst_id) if key
    }
    by_id = {}
    for prediction in predictions:
        key = prediction.get("instance_id") or prediction.get("original_inst_id")
        if key not in aliases:
            raise ValueError(f"prediction is outside the selected case set: {key}")
        instance_id = aliases[key].instance_id
        if instance_id in by_id:
            raise ValueError(f"duplicate prediction: {instance_id}")
        by_id[instance_id] = {**prediction, "instance_id": instance_id}
    items, failures = [], []
    for case in cases:
        if case.instance_id not in by_id:
            failures.append(
                {"instance_id": case.instance_id, "error": "missing_prediction"}
            )
            continue
        try:
            repo = prepare_contextbench_repo(case, repos_root)
            items.append(
                {
                    "repo_path": str(repo),
                    "gold": contextbench_gold_record(case),
                    "prediction": by_id[case.instance_id],
                }
            )
        except Exception as exc:
            failures.append(
                {
                    "instance_id": case.instance_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
    input_path, result_path = output / "evaluation-input.json", output / "metrics.json"
    input_path.write_text(json.dumps(items), encoding="utf-8")
    # A stale result cannot make an interrupted or crashed evaluator look green.
    result_path.unlink(missing_ok=True)
    with (output / "evaluation.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            [*command, "--input", str(input_path), "--output", str(result_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    if completed.returncode not in (0, 1) or not result_path.is_file():
        raise RuntimeError("official evaluation failed; see evaluation.log")
    report = json.loads(result_path.read_text(encoding="utf-8"))
    expected = {item["prediction"]["instance_id"] for item in items}
    observed = [row["instance_id"] for row in report["cases"]]
    if len(observed) != len(set(observed)) or set(observed) != expected:
        raise ValueError("official evaluator returned an incomplete case set")
    report["cases"].extend(failures)
    by_case = {case.instance_id: case for case in cases}
    for row in report["cases"]:
        case = by_case[row["instance_id"]]
        row.update(
            original_inst_id=case.original_inst_id,
            source=case.source,
            language=case.language,
            repo=case.repo,
            base_commit=case.base_commit,
        )
    summary = report["summary"]
    summary["num_total"] = len(cases)
    summary["failed_cases"] = len(cases) - summary["num_valid"]
    summary["metric_population"] = "successfully_scored_cases"
    summary["failure_ids"] = [
        row["instance_id"] for row in report["cases"] if "error" in row
    ]
    summary["editloc_population"] = "cases_with_a_supplied_model_patch_only"
    write_json_atomic(result_path, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "evaluate"))
    parser.add_argument(
        "--data",
        type=Path,
        help="Local official JSON/JSONL/Parquet; otherwise load Hugging Face",
    )
    parser.add_argument("--config", choices=CONTEXTBENCH_CONFIGS, default="default")
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--language", action="append", default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--repos-root",
        type=Path,
        help="Reuse task checkouts from a prepare run; default: OUTPUT/repos",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Private run directory (also holds checkouts and indexes)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse successful cases only when the complete run configuration matches",
    )
    parser.add_argument(
        "--contextbench-root",
        type=Path,
        help="Official source checkout at the pinned revision",
    )
    parser.add_argument("--evaluator-python", default=sys.executable)
    parser.add_argument(
        "--pred", type=Path, help="Unified prediction JSON/JSONL for evaluate"
    )
    parser.add_argument("--model", default="openrouter/openai/gpt-4.1-mini")
    parser.add_argument(
        "--live", action="store_true", help="Enable billed model calls for run"
    )
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--request-timeout", type=float, default=120)
    parser.add_argument("--evaluation-timeout", type=float, default=1800)
    parser.add_argument("--skill", choices=_SKILLS, action="append")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if (
        min(
            args.max_turns,
            args.max_tokens,
            args.top_k,
            args.request_timeout,
            args.evaluation_timeout,
        )
        <= 0
    ):
        parser.error("budgets and timeouts must be positive")
    if args.command == "run":
        if not args.live:
            parser.error("run requires --live to enable model calls")
        if not args.model.startswith("openrouter/") or not os.environ.get(
            "OPENROUTER_API_KEY"
        ):
            parser.error(
                "run requires an openrouter/<provider>/<model> and OPENROUTER_API_KEY"
            )
    if args.command != "prepare" and args.contextbench_root is None:
        parser.error("run/evaluate requires --contextbench-root")
    if args.command == "evaluate" and args.pred is None:
        parser.error("evaluate requires --pred")
    try:
        return _run(args)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"contextbench: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    command = None
    evaluator_identity = None
    if args.command != "prepare":
        command = contextbench_evaluator_command(
            args.contextbench_root, args.evaluator_python
        )
        try:
            ready = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=60
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"official evaluator preflight failed:\n{exc.stderr.strip()}"
            ) from exc
        evaluator_identity = json.loads(ready.stdout)
    cases = load_contextbench_cases(
        args.data,
        config=args.config,
        instance_ids=args.instance_id,
        languages=args.language,
        sources=args.source,
        limit=args.limit,
    )
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    repos_root = (
        args.repos_root.expanduser().resolve() if args.repos_root else output / "repos"
    )
    skills = tuple(sorted(set(args.skill or ("bm25_search",))))
    config = {
        "schema_version": 1,
        "command": args.command,
        "dataset": CONTEXTBENCH_DATASET,
        "dataset_revision": (
            CONTEXTBENCH_DATASET_REVISION if args.data is None else None
        ),
        "dataset_config": args.config if args.data is None else None,
        "data_sha256": (
            hashlib.sha256(args.data.read_bytes()).hexdigest() if args.data else None
        ),
        "case_digest": cases_digest(cases),
        "upstream_revision": CONTEXTBENCH_UPSTREAM_REVISION,
        "implementation_sha256": _implementation_digest(),
        "python_version": sys.version,
        "evaluator": evaluator_identity,
        "repos_root": str(repos_root),
        "model": args.model if args.command == "run" else None,
        "max_turns": args.max_turns,
        "max_tokens": args.max_tokens,
        "top_k": args.top_k,
        "request_timeout": args.request_timeout,
        "skills": list(skills),
        "prediction_sha256": (
            hashlib.sha256(args.pred.read_bytes()).hexdigest() if args.pred else None
        ),
    }
    # The whole run owns this existing FileLock primitive. Acquisition is the
    # linearization point: only its owner may bill calls or replace artifacts.
    # There are no nested run locks; a crashed process releases the OS lock,
    # and --resume validates configuration before consuming successful cases.
    with FileLock(str(output / ".run.lock"), timeout=0):
        config_path = output / "run.json"
        if config_path.exists():
            if not args.resume:
                raise ValueError(
                    "output already contains a run; use --resume or a new directory"
                )
            if json.loads(config_path.read_text()) != config:
                raise ValueError("resume configuration does not match the recorded run")
        elif args.resume:
            raise ValueError("cannot resume without run.json")
        write_json_atomic(config_path, config)
        _write_jsonl(output / "cases.jsonl", [case.to_record() for case in cases])
        _write_jsonl(
            output / "gold.jsonl", [contextbench_gold_record(case) for case in cases]
        )
        results = []
        predictions = []
        if args.command == "evaluate":
            raw = args.pred.read_text(encoding="utf-8")
            predictions = (
                [json.loads(line) for line in raw.splitlines() if line]
                if args.pred.suffix == ".jsonl"
                else json.loads(raw)
            )
            if isinstance(predictions, dict):
                predictions = [predictions]
        else:
            for index, case in enumerate(cases, 1):
                print(f"[{index}/{len(cases)}] {case.instance_id}", flush=True)
                path = output / "cases" / f"{case.instance_id}.json"
                row = None
                try:
                    repo = prepare_contextbench_repo(case, repos_root)
                    if args.resume and path.exists():
                        previous = json.loads(path.read_text())
                        if previous.get("success"):
                            row = previous
                    if row is None:
                        if args.command == "prepare":
                            row = {"instance_id": case.instance_id, "success": True}
                        else:
                            row = run_contextbench_agent(
                                case,
                                repo=repo,
                                cache_dir=output / "indexes" / case.instance_id,
                                model=args.model,
                                max_turns=args.max_turns,
                                max_tokens=args.max_tokens,
                                top_k=args.top_k,
                                timeout=args.request_timeout,
                                skills=skills,
                            )
                        row["snapshot"] = audit_contextbench_snapshot(
                            repo, case.base_commit
                        )
                except Exception as exc:
                    row = {
                        "instance_id": case.instance_id,
                        "success": False,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                write_json_atomic(path, row)
                results.append(row)
                if row.get("prediction"):
                    predictions.append(row["prediction"])
                # Persist completed work even if a later request is interrupted.
                _write_jsonl(output / "predictions.jsonl", predictions)
        if args.command == "prepare":
            report = {
                "cases": results,
                "summary": {
                    "num_total": len(cases),
                    "num_valid": sum(r["success"] for r in results),
                },
            }
        else:
            report = evaluate_contextbench(
                cases=cases,
                predictions=predictions,
                repos_root=repos_root,
                output=output,
                command=command,
                timeout=args.evaluation_timeout,
            )
            report["runs"] = [
                {
                    k: v
                    for k, v in row.items()
                    if k not in ("messages", "trace", "prediction", "answer")
                }
                for row in results
            ]
        report["provenance"] = config
        write_json_atomic(output / "report.json", report)
        print(json.dumps(report["summary"], indent=2))
        return 0 if report["summary"]["num_valid"] == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
