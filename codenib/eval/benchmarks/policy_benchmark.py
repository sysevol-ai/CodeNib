#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run paired LocAgent/OrcaLoca data-plane compatibility experiments.

This is an experiment driver, not a production runtime.  It keeps each
upstream agent's prompt and policy fixed while swapping only its repository
provider between the pinned native implementation and CodeNib.

The OrcaLoca scorer follows its published golden-patch file/function subset
metric.  SWE-Explore trajectory read sets are a different target.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from codenib.clients.locagent_agent import load_locagent_protocol, run_locagent_policy
from codenib.eval.benchmarks.locagent import locagent_regions
from codenib.eval.benchmarks.orcaloca import (
    OrcaLocaGroundTruth,
    OrcaLocaScore,
    orcaloca_regions,
    parse_orcaloca_locations,
    resolve_orcaloca_locations,
    score_orcaloca_locations,
    summarize_orcaloca_scores,
)
from codenib.eval.benchmarks.policy_compat import (
    POLICY_PROVIDERS,
    PolicyBenchmarkCase,
    PolicyCaseSet,
    RevisionSource,
    assess_policy_coverage,
    audit_policy_provenance,
    build_policy_provenance,
    inspect_policy_run_preflight,
    load_policy_results,
    policy_result_path,
    source_files,
    write_json_atomic,
)
from codenib.integrations.locagent import LOCAGENT_REVISION, OPENHANDS_LOCAGENT_REVISION
from codenib.integrations.orcaloca import ORCALOCA_REVISION

_CODE_ROOT = Path(__file__).resolve().parents[3]


def _locagent_revision_sources(checkout: Path) -> tuple[RevisionSource, ...]:
    return (
        RevisionSource(
            name="locagent",
            repository="https://github.com/gersteinlab/LocAgent",
            expected_commit=LOCAGENT_REVISION,
            checkout=checkout,
        ),
        RevisionSource(
            name="openhands-locagent-contract",
            repository="https://github.com/OpenHands/OpenHands",
            expected_commit=OPENHANDS_LOCAGENT_REVISION,
        ),
    )


def _orcaloca_revision_sources(checkout: Path) -> tuple[RevisionSource, ...]:
    return (
        RevisionSource(
            name="orcaloca",
            repository="https://github.com/fishmingyu/OrcaLoca",
            expected_commit=ORCALOCA_REVISION,
            checkout=checkout,
        ),
    )


def _activate_orcaloca_checkout(checkout: Path) -> None:
    """Ensure the validated checkout supplies the imported OrcaLoca policy."""

    root = checkout.expanduser().resolve()
    marker = root / "Orcar" / "search_agent.py"
    if not marker.is_file():
        raise FileNotFoundError(f"Not an OrcaLoca checkout: {root}")
    loaded = sys.modules.get("Orcar")
    loaded_path = Path(str(getattr(loaded, "__file__", ""))).resolve()
    if loaded is not None and root not in loaded_path.parents:
        raise RuntimeError(
            "OrcaLoca was imported before the validated checkout was activated: "
            f"{loaded_path}"
        )
    root_text = str(root)
    sys.path[:] = [entry for entry in sys.path if entry != root_text]
    sys.path.insert(0, root_text)
    importlib.invalidate_caches()


def _policy_provenance_by_provider(
    *,
    agent: str,
    providers: Sequence[str],
    model: str,
    model_revision: str | None,
    case_set: PolicyCaseSet,
    cases: Sequence[PolicyBenchmarkCase],
    revision_sources: Sequence[RevisionSource],
    run_options: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    return {
        provider: build_policy_provenance(
            agent=agent,
            provider=provider,
            model=model,
            model_revision=model_revision,
            code_root=_CODE_ROOT,
            case_set=case_set,
            selected_cases=cases,
            revision_sources=revision_sources,
            run_options=run_options,
        )
        for provider in providers
    }


class _NativeLocAgentClient:
    def __init__(
        self,
        *,
        python: Path,
        checkout: Path,
        repo_path: Path,
        instance_id: str,
        base_commit: str,
        graph_index_dir: Path,
        bm25_index_dir: Path,
    ) -> None:
        env = dict(os.environ)
        python_paths = [str(_CODE_ROOT), str(checkout)]
        if env.get("PYTHONPATH"):
            python_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        env["GRAPH_INDEX_DIR"] = str(graph_index_dir)
        env["BM25_INDEX_DIR"] = str(bm25_index_dir)
        command = [
            str(python),
            "-m",
            "codenib.eval.benchmarks.policy_benchmark",
            "locagent-worker",
            "--repo-path",
            str(repo_path),
            "--instance-id",
            instance_id,
            "--base-commit",
            base_commit,
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(graph_index_dir.parent),
        )
        assert self.process.stdout is not None
        ready_line = self.process.stdout.readline()
        if not ready_line:
            raise RuntimeError(
                f"LocAgent worker exited during setup with {self.process.returncode}"
            )
        ready = json.loads(ready_line)
        if not ready.get("ok"):
            raise RuntimeError(f"LocAgent worker setup failed: {ready}")
        self.load_seconds = float(ready["load_seconds"])

    def dispatch(self, name: str, arguments: Mapping[str, Any]) -> str:
        if self.process.poll() is not None:
            raise RuntimeError("LocAgent worker is no longer running")
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            json.dumps({"name": name, "arguments": dict(arguments)}) + "\n"
        )
        self.process.stdin.flush()
        response = json.loads(self.process.stdout.readline())
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "unknown LocAgent tool error"))
        return str(response.get("result", ""))

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"name": "__close__"}) + "\n")
        self.process.stdin.flush()
        self.process.wait(timeout=30)


def _run_locagent_loop(
    *,
    case: PolicyBenchmarkCase,
    checkout: Path,
    dispatch: Callable[[str, Mapping[str, Any]], str],
    model: str,
    base_url: str | None,
    max_iterations: int,
) -> dict[str, Any]:
    from openai import OpenAI

    protocol = load_locagent_protocol(
        checkout,
        problem_statement=case.problem_statement,
        package_name=case.instance_id.split("_")[0],
    )
    client_options = {"base_url": base_url} if base_url else {}
    execution = run_locagent_policy(
        protocol=protocol,
        model=model,
        dispatch=dispatch,
        client=OpenAI(**client_options),
        max_iterations=max_iterations,
        max_completion_tokens=4096,
        reasoning_effort="none",
    )
    usage = dict(execution.usage or {})
    elapsed = float(usage.get("elapsed_seconds", 0.0))
    tool_seconds = float(usage.get("tool_seconds", 0.0))
    return {
        "model": model,
        "elapsed_seconds": elapsed,
        "iterations": execution.iterations or 0,
        "tool_calls": execution.tool_calls or 0,
        "tool_seconds": tool_seconds,
        "usage": {
            key: value
            for key, value in usage.items()
            if key not in {"elapsed_seconds", "tool_seconds"}
        },
        "final_output": execution.final_output,
        "regions": list(
            locagent_regions(
                execution.final_output,
                valid_files=source_files(case.repo_path),
            )
        ),
        "tool_trace": list(execution.tool_trace),
        "trajectory": list(execution.trajectory),
    }


def _run_locagent(args: argparse.Namespace) -> int:
    case_set = PolicyCaseSet.load(args.cases)
    cases = case_set.select(args.instance_id)
    output_dir = args.output_dir
    order = POLICY_PROVIDERS if args.provider == "both" else (args.provider,)
    if "native" in order and args.locagent_python is None:
        raise ValueError("--locagent-python is required for the native provider")
    if "native" in order and args.native_index_dir is None:
        raise ValueError("--native-index-dir is required for the native provider")
    revision_sources = _locagent_revision_sources(args.locagent_checkout)
    preflight = inspect_policy_run_preflight(
        cases,
        agent="locagent",
        providers=order,
        revision_sources=revision_sources,
        required_capabilities=(
            {"codenib": ("sparse_search", "symbol_navigation")}
            if "codenib" in order
            else {}
        ),
    )
    preflight.require_eligible()
    provenance = _policy_provenance_by_provider(
        agent="locagent",
        providers=order,
        model=args.model,
        model_revision=args.model_revision,
        case_set=case_set,
        cases=cases,
        revision_sources=revision_sources,
        run_options={
            "max_iterations": args.max_iterations,
            "max_completion_tokens": 4096,
            "reasoning_effort": "none",
            "custom_base_url": bool(args.base_url),
        },
    )
    failed = False

    for case_index, case in enumerate(cases):
        case_order = order
        if args.provider == "both" and case_index % 2:
            case_order = tuple(reversed(order))
        for provider_name in case_order:
            output_path = policy_result_path(
                output_dir,
                instance_id=case.instance_id,
                agent="locagent",
                provider=provider_name,
            )
            if output_path.exists() and not args.overwrite:
                print(f"skip existing {output_path}", flush=True)
                continue
            print(
                f"run LocAgent {case.instance_id} provider={provider_name} "
                f"model={args.model}",
                flush=True,
            )
            provider: Any | None = None
            started = time.perf_counter()
            try:
                if provider_name == "native":
                    provider = _NativeLocAgentClient(
                        python=args.locagent_python,
                        checkout=args.locagent_checkout,
                        repo_path=case.checkout_for(
                            agent="locagent", provider=provider_name
                        ),
                        instance_id=case.instance_id,
                        base_commit=case.base_commit,
                        graph_index_dir=args.native_index_dir / "graph",
                        bm25_index_dir=args.native_index_dir / "bm25",
                    )
                    dispatch = provider.dispatch
                else:
                    from codenib.integrations.locagent import LocAgentToolProvider

                    load_started = time.perf_counter()
                    provider = LocAgentToolProvider.from_manifest(case.manifest_path)
                    provider.load_seconds = time.perf_counter() - load_started
                    dispatch = provider.dispatch
                result = _run_locagent_loop(
                    case=case,
                    checkout=args.locagent_checkout,
                    dispatch=dispatch,
                    model=args.model,
                    base_url=args.base_url,
                    max_iterations=args.max_iterations,
                )
                result.update(
                    {
                        "agent": "locagent",
                        "provider": provider_name,
                        "instance_id": case.instance_id,
                        "provider_load_seconds": provider.load_seconds,
                        "provenance": provenance[provider_name],
                    }
                )
            except Exception as exc:
                failed = True
                result = {
                    "agent": "locagent",
                    "provider": provider_name,
                    "instance_id": case.instance_id,
                    "model": args.model,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": time.perf_counter() - started,
                    "regions": [],
                    "provenance": provenance[provider_name],
                }
            finally:
                close = (
                    getattr(provider, "close", None) if provider is not None else None
                )
                if close is not None:
                    close()
            write_json_atomic(output_path, result)
            usage = result.get("usage") or {}
            print(
                f"done {output_path.name}: regions={len(result['regions'])} "
                f"tokens={usage.get('total_tokens', 'n/a')} "
                f"time={result['elapsed_seconds']:.1f}s "
                f"error={result.get('error', 'none')}",
                flush=True,
            )
    return 1 if failed else 0


def _run_locagent_worker(args: argparse.Namespace) -> int:
    from plugins.location_tools.repo_ops import repo_ops

    repo_path = args.repo_path.resolve()
    repo_ops.setup_repo = lambda **_: str(repo_path)
    instance = {
        "instance_id": args.instance_id,
        "repo": args.instance_id.rsplit("-", 1)[0].replace("__", "/"),
        "base_commit": args.base_commit,
        "problem_statement": "",
        "patch": "",
    }
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            repo_ops.set_current_issue(instance_data=instance)
        ready = {"ok": True, "load_seconds": time.perf_counter() - started}
    except Exception as exc:
        ready = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(ready), flush=True)
    if not ready["ok"]:
        return 1

    functions = {
        "search_code_snippets": repo_ops.search_code_snippets,
        "get_entity_contents": repo_ops.get_entity_contents,
        "explore_tree_structure": repo_ops.explore_tree_structure,
    }
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("name") == "__close__":
                break
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = functions[request["name"]](**request.get("arguments", {}))
                captured = output.getvalue()
                if captured:
                    result = captured + (str(result) if result is not None else "")
                response = {"ok": True, "result": result or ""}
            except Exception as exc:
                response = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            print(json.dumps(response), flush=True)
    finally:
        with contextlib.suppress(Exception):
            repo_ops.reset_current_issue()
    return 0


def _score_orcaloca_result(
    case: PolicyBenchmarkCase, result: Mapping[str, Any]
) -> tuple[dict[str, Any], OrcaLocaScore]:
    ground_truth = OrcaLocaGroundTruth.from_mapping(case.ground_truth)
    raw_locations = result.get("locations")
    locations = (
        parse_orcaloca_locations(raw_locations)
        if isinstance(raw_locations, list)
        else parse_orcaloca_locations(str(result.get("raw_output", "")))
    )
    score = score_orcaloca_locations(ground_truth, locations)
    usage = result.get("usage") or {}
    row = {
        "instance_id": case.instance_id,
        "provider": result.get("provider"),
        "error": result.get("error"),
        **score.to_record(),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "provider_load_seconds": result.get("provider_load_seconds"),
        "tool_calls": result.get("tool_calls"),
        "total_tokens_estimated": usage.get("total_tokens_estimated"),
    }
    return row, score


def _mean(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return sum(values) / len(values) if values else None


def _run_score_orcaloca(args: argparse.Namespace) -> int:
    case_set = PolicyCaseSet.load(args.cases)
    cases = case_set.select(args.instance_id)
    case_by_id = {case.instance_id: case for case in cases}
    providers = POLICY_PROVIDERS if args.provider == "both" else (args.provider,)
    rows: list[dict[str, Any]] = []
    scored_rows: list[tuple[dict[str, Any], OrcaLocaScore]] = []
    results = load_policy_results(
        args.results_dir,
        agent="orcaloca",
        cases=cases,
        providers=providers,
    )
    coverage = assess_policy_coverage(cases, results, providers=providers)
    provenance_audit = audit_policy_provenance(results)
    for result in results:
        case = case_by_id[str(result["instance_id"])]
        scored = _score_orcaloca_result(case, result)
        scored_rows.append(scored)
        rows.append(scored[0])

    models = {str(result.get("model") or "") for result in results}
    models.discard("")
    if args.model is not None and models - {args.model}:
        raise ValueError(
            f"result model mismatch: found {sorted(models)}, expected {args.model}"
        )
    if len(models) > 1:
        raise ValueError(f"cannot aggregate mixed models: {sorted(models)}")
    model = args.model or (next(iter(models)) if models else None)

    summary = []
    for provider in providers:
        provider_rows = [row for row in rows if row["provider"] == provider]
        if not provider_rows:
            continue
        provider_scores = [
            score for row, score in scored_rows if row.get("provider") == provider
        ]
        benchmark_summary = summarize_orcaloca_scores(provider_scores).to_record()
        summary.append(
            {
                "provider": provider,
                **benchmark_summary,
                "elapsed_seconds_mean": _mean(provider_rows, "elapsed_seconds"),
                "total_tokens_estimated_mean": _mean(
                    provider_rows, "total_tokens_estimated"
                ),
            }
        )

    paired = []
    rows_by_key = {(str(row["instance_id"]), str(row["provider"])): row for row in rows}
    for instance_id in case_by_id:
        native = rows_by_key.get((instance_id, "native"))
        codenib = rows_by_key.get((instance_id, "codenib"))
        if native is None or codenib is None:
            continue
        if native.get("error") or codenib.get("error"):
            continue
        native_elapsed = native.get("elapsed_seconds")
        codenib_elapsed = codenib.get("elapsed_seconds")
        native_tokens = native.get("total_tokens_estimated")
        codenib_tokens = codenib.get("total_tokens_estimated")
        paired.append(
            {
                "instance_id": instance_id,
                "same_file_match": native["file_match"] == codenib["file_match"],
                "same_function_match": (
                    native["function_match"] == codenib["function_match"]
                ),
                "same_predicted_files": (
                    native["predicted_files"] == codenib["predicted_files"]
                ),
                "same_predicted_functions": (
                    native["predicted_functions"] == codenib["predicted_functions"]
                ),
                "native_to_codenib_elapsed_ratio": (
                    float(native_elapsed) / float(codenib_elapsed)
                    if native_elapsed is not None
                    and codenib_elapsed is not None
                    and float(codenib_elapsed) > 0
                    else None
                ),
                "native_to_codenib_total_tokens_ratio": (
                    float(native_tokens) / float(codenib_tokens)
                    if native_tokens is not None
                    and codenib_tokens is not None
                    and float(codenib_tokens) > 0
                    else None
                ),
                "codenib_total_token_reduction_fraction": (
                    1.0 - (float(codenib_tokens) / float(native_tokens))
                    if native_tokens is not None
                    and codenib_tokens is not None
                    and float(native_tokens) > 0
                    else None
                ),
            }
        )

    ratios = [
        float(row["native_to_codenib_elapsed_ratio"])
        for row in paired
        if row["native_to_codenib_elapsed_ratio"] is not None
    ]
    token_ratios = [
        float(row["native_to_codenib_total_tokens_ratio"])
        for row in paired
        if row["native_to_codenib_total_tokens_ratio"] is not None
    ]
    token_reductions = [
        float(row["codenib_total_token_reduction_fraction"])
        for row in paired
        if row["codenib_total_token_reduction_fraction"] is not None
    ]
    payload = {
        "metric": "OrcaLoca golden-patch subset match",
        "model": model,
        "case_set": {
            "source_sha256": case_set.source_sha256,
            "selection_sha256": case_set.selection_sha256(cases),
        },
        "coverage": coverage.to_record(),
        "provenance": provenance_audit.to_record(),
        "rows": rows,
        "summary": summary,
        "paired": paired,
        "paired_summary": {
            "n": len(paired),
            "same_file_match_count": sum(row["same_file_match"] for row in paired),
            "same_function_match_count": sum(
                row["same_function_match"] for row in paired
            ),
            "same_predicted_files_count": sum(
                row["same_predicted_files"] for row in paired
            ),
            "same_predicted_functions_count": sum(
                row["same_predicted_functions"] for row in paired
            ),
            "native_to_codenib_elapsed_ratio_median": (
                statistics.median(ratios) if ratios else None
            ),
            "native_to_codenib_total_tokens_ratio_median": (
                statistics.median(token_ratios) if token_ratios else None
            ),
            "codenib_total_token_reduction_fraction_median": (
                statistics.median(token_reductions) if token_reductions else None
            ),
        },
        "missing": [
            str(
                policy_result_path(
                    args.results_dir,
                    instance_id=key.instance_id,
                    agent="orcaloca",
                    provider=key.provider,
                )
            )
            for key in coverage.missing
        ],
    }
    if args.output is not None:
        write_json_atomic(args.output, payload)
        print(args.output)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    incomplete = bool(coverage.missing or coverage.failed or provenance_audit.invalid)
    return 1 if incomplete and args.require_complete else 0


def _run_orcaloca(args: argparse.Namespace) -> int:
    case_set = PolicyCaseSet.load(args.cases)
    cases = case_set.select(args.instance_id)
    providers = POLICY_PROVIDERS if args.provider == "both" else (args.provider,)
    revision_sources = _orcaloca_revision_sources(args.orcaloca_checkout)
    preflight = inspect_policy_run_preflight(
        cases,
        agent="orcaloca",
        providers=providers,
        revision_sources=revision_sources,
        required_capabilities=(
            {"codenib": ("symbol_navigation",)} if "codenib" in providers else {}
        ),
    )
    preflight.require_eligible()
    provenance = _policy_provenance_by_provider(
        agent="orcaloca",
        providers=providers,
        model=args.model,
        model_revision=args.model_revision,
        case_set=case_set,
        cases=cases,
        revision_sources=revision_sources,
        run_options={
            "max_iterations": args.max_iterations,
            "temperature": args.temperature,
            "context_window": args.context_window,
            "tokenizer_encoding": args.tokenizer_encoding,
            "custom_base_url": bool(args.base_url),
            "trace_analysis": False,
        },
    )
    _activate_orcaloca_checkout(args.orcaloca_checkout)

    if args.tokenizer_encoding:
        import tiktoken.model

        tiktoken.model.MODEL_TO_ENCODING[args.model] = args.tokenizer_encoding
    if args.context_window is not None:
        import llama_index.llms.openai.utils as openai_utils

        openai_utils.CHAT_MODELS[args.model] = args.context_window
        openai_utils.ALL_AVAILABLE_MODELS[args.model] = args.context_window

    from llama_index.llms.openai import OpenAI
    from Orcar.search_agent import SearchAgent, SearchWorker
    from Orcar.types import SearchInput, TraceAnalysisOutput

    original_finalize = SearchWorker.finalize_task

    def capture_finalize(worker: Any, task: Any, **kwargs: Any) -> None:
        worker._benchmark_token_counts = list(task.extra_state["token_cnts"])
        finalize = getattr(original_finalize, "__wrapped__", original_finalize)
        finalize(worker, task, **kwargs)

    SearchWorker.finalize_task = capture_finalize
    config_path = args.orcaloca_checkout / "Orcar" / "search.cfg"
    failed = False

    for case_index, case in enumerate(cases):
        case_providers = providers
        if args.provider == "both" and case_index % 2:
            case_providers = tuple(reversed(providers))
        for provider_name in case_providers:
            output_path = policy_result_path(
                args.output_dir,
                instance_id=case.instance_id,
                agent="orcaloca",
                provider=provider_name,
            )
            if output_path.exists() and not args.overwrite:
                print(f"skip existing {output_path}", flush=True)
                continue

            search_input = SearchInput(
                problem_statement=case.problem_statement,
                trace_analysis_output=TraceAnalysisOutput(),
            )
            llm_options = {"api_base": args.base_url} if args.base_url else {}
            llm = OpenAI(
                model=args.model,
                max_tokens=None,
                temperature=args.temperature,
                **llm_options,
            )
            kwargs: dict[str, Any] = {}
            repo_path = case.checkout_for(
                agent="orcaloca",
                provider=provider_name,
            )
            if provider_name == "codenib":
                from codenib.integrations.orcaloca import (
                    make_orcaloca_search_manager_factory,
                )

                kwargs["search_manager_factory"] = make_orcaloca_search_manager_factory(
                    case.manifest_path
                )
            print(
                f"run OrcaLoca {case.instance_id} provider={provider_name} "
                f"model={args.model}",
                flush=True,
            )
            started = time.perf_counter()
            try:
                agent = SearchAgent(
                    llm=llm,
                    search_input=search_input,
                    repo_path=str(repo_path),
                    max_iterations=args.max_iterations,
                    config_path=str(config_path),
                    **kwargs,
                )
                provider_load_seconds = time.perf_counter() - started
                response = agent.chat(search_input.get_content())
                elapsed = time.perf_counter() - started
                raw_output = str(response.response)
                worker = agent.agent_worker
                token_counts = getattr(worker, "_benchmark_token_counts", [])
                prompt_tokens = sum(
                    int(count.in_token_cnt) for _, count in token_counts
                )
                completion_tokens = sum(
                    int(count.out_token_cnt) for _, count in token_counts
                )
                manager = agent._search_manager
                raw_history = getattr(manager, "history", [])
                if hasattr(raw_history, "to_dict"):
                    history = raw_history.to_dict(orient="records")
                else:
                    history = [dict(row) for row in raw_history]
                result = {
                    "agent": "orcaloca",
                    "provider": provider_name,
                    "instance_id": case.instance_id,
                    "model": args.model,
                    "provider_load_seconds": provider_load_seconds,
                    "elapsed_seconds": elapsed,
                    "usage": {
                        "prompt_tokens_estimated": prompt_tokens,
                        "completion_tokens_estimated": completion_tokens,
                        "total_tokens_estimated": prompt_tokens + completion_tokens,
                    },
                    "tool_calls": len(history),
                    "raw_output": raw_output,
                    "locations": list(
                        resolve_orcaloca_locations(raw_output, repo_path=repo_path)
                    ),
                    "regions": list(orcaloca_regions(raw_output, repo_path=repo_path)),
                    "search_history": history,
                    "provenance": provenance[provider_name],
                }
            except Exception as exc:
                failed = True
                result = {
                    "agent": "orcaloca",
                    "provider": provider_name,
                    "instance_id": case.instance_id,
                    "model": args.model,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": time.perf_counter() - started,
                    "regions": [],
                    "provenance": provenance[provider_name],
                }
            write_json_atomic(output_path, result)
            print(
                f"done {output_path.name}: regions={len(result['regions'])} "
                f"time={result['elapsed_seconds']:.1f}s "
                f"error={result.get('error', 'none')}",
                flush=True,
            )
    return 1 if failed else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    locagent = subparsers.add_parser("locagent")
    locagent.add_argument("--cases", type=Path, required=True)
    locagent.add_argument("--instance-id", action="append")
    locagent.add_argument("--output-dir", type=Path, required=True)
    locagent.add_argument(
        "--provider", choices=("native", "codenib", "both"), default="both"
    )
    locagent.add_argument("--locagent-checkout", type=Path, required=True)
    locagent.add_argument("--locagent-python", type=Path)
    locagent.add_argument("--native-index-dir", type=Path)
    locagent.add_argument("--model", required=True)
    locagent.add_argument("--model-revision")
    locagent.add_argument("--base-url")
    locagent.add_argument("--max-iterations", type=int, default=10)
    locagent.add_argument("--overwrite", action="store_true")
    locagent.set_defaults(handler=_run_locagent)

    worker = subparsers.add_parser("locagent-worker")
    worker.add_argument("--repo-path", type=Path, required=True)
    worker.add_argument("--instance-id", required=True)
    worker.add_argument("--base-commit", required=True)
    worker.set_defaults(handler=_run_locagent_worker)

    orcaloca = subparsers.add_parser("orcaloca")
    orcaloca.add_argument("--cases", type=Path, required=True)
    orcaloca.add_argument("--instance-id", action="append")
    orcaloca.add_argument("--output-dir", type=Path, required=True)
    orcaloca.add_argument(
        "--provider", choices=("native", "codenib", "both"), default="both"
    )
    orcaloca.add_argument("--orcaloca-checkout", type=Path, required=True)
    orcaloca.add_argument("--model", required=True)
    orcaloca.add_argument("--model-revision")
    orcaloca.add_argument("--base-url")
    orcaloca.add_argument("--temperature", type=float, default=1.0)
    orcaloca.add_argument("--context-window", type=int)
    orcaloca.add_argument("--tokenizer-encoding")
    orcaloca.add_argument("--max-iterations", type=int, default=10)
    orcaloca.add_argument("--overwrite", action="store_true")
    orcaloca.set_defaults(handler=_run_orcaloca)

    score_orcaloca = subparsers.add_parser("score-orcaloca")
    score_orcaloca.add_argument("--cases", type=Path, required=True)
    score_orcaloca.add_argument("--instance-id", action="append")
    score_orcaloca.add_argument("--results-dir", type=Path, required=True)
    score_orcaloca.add_argument(
        "--provider", choices=("native", "codenib", "both"), default="both"
    )
    score_orcaloca.add_argument("--model")
    score_orcaloca.add_argument("--output", type=Path)
    completeness = score_orcaloca.add_mutually_exclusive_group()
    completeness.add_argument(
        "--require-complete",
        action="store_true",
        dest="require_complete",
        help="Fail when any requested provider cell is missing or failed (default).",
    )
    completeness.add_argument(
        "--allow-incomplete",
        action="store_false",
        dest="require_complete",
        help="Allow missing or failed cells after writing the coverage audit.",
    )
    score_orcaloca.set_defaults(
        handler=_run_score_orcaloca,
        require_complete=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
