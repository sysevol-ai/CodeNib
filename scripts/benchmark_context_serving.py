#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Source-only, frozen multilingual ContextBench serving cohort.

Select without labels, prepare private snapshots, then explicitly opt into paid
planner/decision calls. No solver, reference patch, or gold reaches retrieval.
Failures stay in the selected population. See docs/experiments/graph_decisions.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.code_chunker import CodeChunker, RepoChunkingConfig
from codenib.dataset.contextbench import (
    CONTEXTBENCH_DATASET_REVISION,
    cases_digest,
    load_contextbench_cases,
)
from codenib.eval.benchmarks.contextbench_runner import (
    audit_contextbench_snapshot,
    contextbench_evaluator_command,
    evaluate_contextbench,
    prepare_contextbench_repo,
)
from codenib.graph.code_graph import CodeGraph
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.languages import normalize_graph_language
from codenib.types import NODE_TYPE_FILE, NodeInfo
from scripts import benchmark_model_grep as grep
from scripts.benchmark_graph_decisions import (
    DECIDER,
    expand_candidates,
    graph_bindings,
    node_key,
    pack_context,
    prediction,
    strict_rerank,
    visible_node,
)
from scripts.benchmark_jev_base import digest, snapshot_chunks, write_json
from scripts.benchmark_jev_retrieval import MeasuredDecisions

MODELS = {
    "sonnet": ("anthropic/claude-sonnet-4.6", None, 1000),
    "opus": ("anthropic/claude-opus-5.5", "medium", 4096),
}
PACKING = {"top5": 5, "top10": 10, "budget": 40}


def implementation():
    paths = [
        Path(__file__),
        ROOT / "scripts/benchmark_graph_decisions.py",
        Path(grep.__file__),
        ROOT / "scripts/benchmark_jev_base.py",
        ROOT / "scripts/benchmark_jev_retrieval.py",
    ]
    return {str(p.relative_to(ROOT)): digest(p) for p in paths}


def sample_cases(cases, size, seed, excluded=()):
    """Allocate >=5/language, then proportional slots; round-robin repositories.

    Hash ordering uses IDs, repository names and a fixed seed only. Annotation
    size, issue text, preparation success and observed scores are never inputs.
    """
    groups = defaultdict(list)
    for case in cases:
        if case.instance_id not in excluded:
            groups[case.language].append(case)
    if size > sum(map(len, groups.values())) or size < 5 * len(groups):
        raise ValueError("Sample must fit the corpus with >=5 cases per language")
    if any(len(group) < 5 for group in groups.values()):
        raise ValueError("A language has fewer than five eligible cases")
    counts = {language: 5 for language in groups}
    # Proportional deficits with capacity constraints, deterministic ties.
    weights = {
        lang: len(rows) / sum(map(len, groups.values()))
        for lang, rows in groups.items()
    }
    extra = size - sum(counts.values())
    for _ in range(extra):
        language = max(
            (lang for lang in groups if counts[lang] < len(groups[lang])),
            key=lambda lang: (extra * weights[lang] - (counts[lang] - 5), lang),
        )
        counts[language] += 1

    def order(value):
        return hashlib.sha256(f"{seed}:{value}".encode()).hexdigest()

    selected = []
    for language in sorted(groups):
        repos = defaultdict(list)
        for case in groups[language]:
            repos[case.repo].append(case)
        for rows in repos.values():
            rows.sort(key=lambda c: order(c.instance_id))
        ordered_repos = sorted(repos, key=order)
        chosen = []
        while len(chosen) < counts[language]:
            for repo in ordered_repos:
                if repos[repo] and len(chosen) < counts[language]:
                    chosen.append(repos[repo].pop(0))
        selected.extend(chosen)
    return tuple(sorted(selected, key=lambda c: order(c.instance_id)))


def select(args):
    cases = load_contextbench_cases(args.dataset)
    excluded = (
        [c.instance_id for c in load_contextbench_cases(args.exclude)]
        if args.exclude
        else []
    )
    selected = sample_cases(cases, args.size, args.seed, excluded)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "selected.jsonl").write_text(
        "".join(json.dumps(c.to_record()) + "\n" for c in selected)
    )
    write_json(
        args.output / "selection.json",
        {
            "dataset": str(args.dataset.resolve()),
            "dataset_sha256": digest(args.dataset),
            "dataset_revision": CONTEXTBENCH_DATASET_REVISION,
            "case_digest": cases_digest(selected),
            "seed": args.seed,
            "selection": (
                "minimum five per language, proportional remainder, repository "
                "round-robin, SHA256 order"
            ),
            "excluded_previous_development_ids": excluded,
            "languages": dict(Counter(c.language for c in selected)),
            "sources": dict(Counter(c.source for c in selected)),
            "repos": dict(Counter(c.repo for c in selected)),
            "primary_packing": "top10",
            "secondary_packing": ["top5", "budget"],
            "candidate_limit": 40,
            "graph_seeds": 5,
            "graph_hops": 2,
            "context_tokens": 4096,
            "corpus": (
                "tracked regular registered source; include tests; native AST chunks "
                "plus bounded uncovered spans; production "
                "vendor/generated/minified/size filters"
            ),
            "graph_policy": (
                "active primary-language backend, private build copy, bounded wall "
                "time; unavailable graph explicitly falls back to grep"
            ),
            "models": MODELS,
            "decider": DECIDER,
            "implementation": implementation(),
        },
    )
    print(
        json.dumps(
            {
                "cases": len(selected),
                "languages": dict(Counter(c.language for c in selected)),
                "repos": len({c.repo for c in selected}),
            }
        ),
        flush=True,
    )


def load_selection(root, *, check_implementation=True):
    config = json.loads((root / "selection.json").read_text())
    cases = load_contextbench_cases(root / "selected.jsonl")
    if config["case_digest"] != cases_digest(cases):
        raise ValueError("Selected task inputs changed")
    if check_implementation and config["implementation"] != implementation():
        raise ValueError("Experiment implementation changed; use a fresh cohort")
    return config, cases


def complete_nodes(chunks, sources, max_chars=3000, max_lines=100):
    """Keep visible AST chunks and fill uncovered source with bounded intervals.

    Filling uses source coverage only, never annotations. A line exceeding the
    character cap is explicitly omitted rather than silently clipped mid-line.
    """
    nodes, covered = [], defaultdict(set)
    for chunk in chunks:
        node = visible_node(chunk, sources[chunk.file], max_chars)
        if node.content:
            nodes.append(node)
            covered[node.file].update(range(node.start_line, node.end_line + 1))
    oversized = []
    for file, lines in sorted(sources.items()):
        start = 0
        while start < len(lines):
            if start in covered[file]:
                start += 1
                continue
            if len(lines[start]) > max_chars:
                oversized.append([file, start + 1])
                start += 1
                continue
            end, used = start, 0
            while (
                end < len(lines)
                and end not in covered[file]
                and end - start < max_lines
                and used + len(lines[end]) <= max_chars
            ):
                used += len(lines[end])
                end += 1
            content = "".join(lines[start:end])
            if content:
                ident = f"{file}::source-gap:{start}-{end - 1}"
                nodes.append(
                    NodeInfo(
                        node_id=ident,
                        node_name=ident,
                        type=NODE_TYPE_FILE,
                        file=file,
                        start_line=start,
                        end_line=end - 1,
                        content=content,
                    )
                )
            start = end
    unique = {node_key(n): n for n in nodes}
    return list(unique.values()), oversized


def graph_worker(args):
    from codenib.ls_router import build_graph_for_languages_with_report

    preparation = getattr(args, "allow_project_preparation", False)
    head_command = ["git", "rev-parse", "HEAD"]
    before = (
        subprocess.check_output(head_command, cwd=args.repo) if preparation else None
    )
    result = build_graph_for_languages_with_report(
        args.repo,
        args.output,
        languages=[args.language],
        project_name="contextbench",
        allow_partial=True,
        allow_project_preparation=preparation,
    )
    if preparation and (
        subprocess.check_output(head_command, cwd=args.repo) != before
        or subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--"], cwd=args.repo, check=False
        ).returncode
    ):
        raise ValueError("Graph preparation changed tracked source")
    record = {
        "available": result.graph is not None,
        "failed_languages": result.failed_languages,
        "index_generation_reports": result.index_generation_reports,
    }
    if result.graph is not None:
        result.graph.save_graph(args.output / "graph.pkl")
    write_json(args.output / "graph-status.json", record)


def cancel_graph_worker(_signum, _frame):
    # Python SCIP creates its own session. Unwind its existing BaseException
    # cleanup so that killing the outer worker cannot orphan that indexer.
    raise SystemExit(124)


def stop_graph_worker(process):
    try:
        process.terminate()
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def bounded_graph(repo, language, output, timeout, *, allow_project_preparation=False):
    """Only the owner launches/cleans its private build copy and process group.

    No checkout is shared with indexing. Timeout cancellation first unwinds the
    worker's native indexer cleanup (including SCIP's nested process session),
    then kills/reaps the outer group before removing the private workspace.
    Reaping is the completion point that permits workspace deletion; the creating
    call owns recovery and there is no shared lock or retained owner.
    Optional project preparation may write generated build files in that copy.
    The worker rejects changes to HEAD or tracked source before publishing a graph.
    """
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    record = {
        "available": False,
        "language": language,
        "allow_project_preparation": allow_project_preparation,
    }
    # Keep ancestor CodeNib pyproject/package configuration outside this build.
    with tempfile.TemporaryDirectory(prefix="codenib-contextbench-build-") as directory:
        scratch = Path(directory) / "repo"
        # Keep the authenticated source clean even if an indexer writes build files.
        subprocess.run(
            ["git", "clone", "--shared", "--quiet", str(repo), str(scratch)],
            check=True,
            timeout=60,
        )
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "graph-worker",
            "--repo",
            str(scratch),
            "--language",
            language,
            "--output",
            str(output),
        ]
        if allow_project_preparation:
            command.append("--allow-project-preparation")
        with (output / "build.log").open("w") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                stop_graph_worker(process)
                record["error"] = f"graph build exceeded {timeout}s"
            except BaseException:
                stop_graph_worker(process)
                raise
            record["returncode"] = process.returncode
        status = output / "graph-status.json"
        if process.returncode == 0 and status.exists():
            record.update(json.loads(status.read_text()))
        elif "error" not in record:
            record["error"] = "graph worker failed; see build.log"
    record["build_wall_s"] = time.perf_counter() - started
    if not record["available"]:
        CodeGraph(str(repo)).save_graph(output / "graph.pkl")
    record["sha256"] = digest(output / "graph.pkl")
    write_json(output / "receipt.json", record)
    return record


def prepare_case(case, root, timeout):
    started = time.perf_counter()
    row = {"instance_id": case.instance_id, "status": "error"}
    try:
        if shutil.disk_usage(root).free < 8 * 1024**3:
            raise RuntimeError("Less than 8 GiB free; preparation stopped")
        repo = prepare_contextbench_repo(case, root / "repos")
        clone_s = time.perf_counter() - started
        chunker = CodeChunker(
            max_lines_per_chunk=100, repo_config=RepoChunkingConfig(filter_tests=False)
        )
        chunks, files = snapshot_chunks(repo, case.base_commit, chunker, {})
        sources = {}
        eligible = []
        for file, oid, language in files:
            path = (repo / file).resolve()
            if not path.is_relative_to(repo.resolve()):
                raise ValueError("Source escaped checkout")
            if path.stat().st_size > chunker.repo_config.max_file_size_mb * 1024**2:
                continue
            text = path.read_text(errors="replace")
            if chunker._is_minified_source(text, path=file):
                continue
            sources[file] = text.splitlines(keepends=True)
            eligible.append((file, oid, language))
        nodes, oversized = complete_nodes(chunks, sources)
        if not nodes:
            raise ValueError("No eligible source nodes")
        corpus_s = time.perf_counter() - started - clone_s
        graph = bounded_graph(
            repo,
            normalize_graph_language(case.language),
            root / "graphs" / case.instance_id,
            timeout,
        )
        audit_contextbench_snapshot(repo, case.base_commit)
        row.update(
            status="ok",
            repo=str(repo),
            query=case.problem_statement,
            nodes=[n.model_dump() for n in nodes],
            files=eligible,
            oversized_lines=oversized,
            clone_s=clone_s,
            corpus_s=corpus_s,
            graph=graph,
        )
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["prepare_wall_s"] = time.perf_counter() - started
    path = root / "cases" / f"{case.instance_id}.json"
    write_json(path, row)
    print(
        json.dumps(
            {
                "case": case.original_inst_id,
                "status": row["status"],
                "graph": row.get("graph", {}).get("available"),
                "nodes": len(row.get("nodes", [])),
                "seconds": round(row["prepare_wall_s"], 2),
            }
        ),
        flush=True,
    )
    return {
        "path": str(path.relative_to(root)),
        "sha256": digest(path),
        "status": row["status"],
    }


def prepare(args):
    _, cases = load_selection(args.cohort)
    (args.cohort / "cases").mkdir(exist_ok=False)
    with ProcessPoolExecutor(
        max_workers=args.workers, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = [
            pool.submit(prepare_case, c, args.cohort, args.graph_timeout) for c in cases
        ]
        records = [f.result() for f in as_completed(futures)]
    write_json(
        args.cohort / "prepared.json",
        {
            "selection_sha256": digest(args.cohort / "selection.json"),
            "workers": args.workers,
            "graph_timeout": args.graph_timeout,
            "cases": sorted(records, key=lambda r: r["path"]),
        },
    )


def source_chunks(nodes):
    from codenib.code_chunking.base import CodeChunk

    return [
        CodeChunk(
            n.content, n.start_line, n.end_line, n.type, n.node_name, n.file, n.node_id
        )
        for n in nodes
    ]


def serve_case(case, source, cohort, output, model, planner, decision_budget):
    import tiktoken

    path = output / model / f"{case.instance_id}.json"
    row = {
        "instance_id": case.instance_id,
        "model": model,
        "status": "error",
        "routes": {},
        "decision_calls": [],
    }
    client = MeasuredDecisions(model=DECIDER, max_cost_usd=decision_budget)
    client.timeout = 45
    started = time.perf_counter()
    try:
        if source["status"] != "ok":
            raise RuntimeError("Preparation failed: " + source.get("error", "unknown"))
        if not case.problem_statement.strip():
            raise ValueError("Task has no problem statement")
        repo = Path(source["repo"])
        audit_contextbench_snapshot(repo, case.base_commit)
        nodes = [NodeInfo.model_validate(n) for n in source["nodes"]]
        lookup = {node_key(n): i for i, n in enumerate(nodes)}
        index_start = time.perf_counter()
        bm25 = BM25CodeIndexer(max_k=len(nodes), language="english")
        bm25.build_index_from_chunks(source_chunks(nodes), project_root=str(repo))
        row["bm25_build_s"] = time.perf_counter() - index_start
        graph_path = cohort / "graphs" / case.instance_id / "graph.pkl"
        if digest(graph_path) != source["graph"]["sha256"]:
            raise ValueError("Graph bytes changed")
        graph = CodeGraph.load_graph(graph_path)
        row["graph_available"] = source["graph"]["available"]
        bound, _ = graph_bindings(graph, nodes)
        row["graph_bound_nodes"] = len(bound)
        payload = {
            "repo": case.repo,
            "issue": case.problem_statement,
            **grep.overview(source["files"]),
        }
        service_start = time.perf_counter()
        row["plan"] = planner.plan(payload)
        write_json(path, row)
        if row["plan"].get("error") or not isinstance(
            row["plan"].get("usage", {}).get("cost"), (int, float)
        ):
            raise RuntimeError("Planner failed or its billed cost is missing")
        with tempfile.TemporaryDirectory(prefix="codenib-cohort-grep-") as directory:
            materialize_start = time.perf_counter()
            grep.materialize(repo, source["files"], Path(directory))
            row["materialize_s"] = time.perf_counter() - materialize_start
            search_start = time.perf_counter()
            found, row["grep_audit"] = grep.execute(
                Path(directory), row["plan"]["actions"], nodes, 100
            )
            row["grep_s"] = time.perf_counter() - search_start
        if any("error" in a for a in row["grep_audit"]):
            raise RuntimeError("Invalid or failed grep action; no prompt repair")
        grep_ids = [lookup[node_key(n)] for n in found]
        graph_start = time.perf_counter()
        lexical = [
            lookup[node_key(n)] for n in bm25.search(source["query"], top_k=len(nodes))
        ]
        seen = set(lexical)
        lexical.extend(i for i in range(len(nodes)) if i not in seen)
        neighbors, row["traversal"] = expand_candidates(
            graph,
            nodes,
            lexical,
            seed_indices=grep_ids,
            seeds=5,
            hops=2,
            candidate_limit=40,
        )
        pools = {
            "grep": grep_ids[:40],
            "graph": list(dict.fromkeys(grep_ids[:20] + neighbors + grep_ids))[:40],
        }
        row["graph_expand_s"] = time.perf_counter() - graph_start
        row["discovery_wall_s"] = time.perf_counter() - service_start
        encoder = tiktoken.get_encoding("cl100k_base")
        # Alternate order by ID to avoid giving one arm the same request position.
        routes = sorted(
            pools,
            reverse=int(hashlib.sha256(case.instance_id.encode()).hexdigest(), 16) % 2
            == 0,
        )
        for name in routes:
            route = {"status": "error", "pool": pools[name]}
            call_start = len(client.calls)
            rank_start = time.perf_counter()
            try:
                ranked = strict_rerank(
                    client, source["query"], [nodes[i] for i in pools[name]]
                )
                route.update(
                    status="ok",
                    ranked=[n.model_dump() for n in ranked],
                    rerank_s=time.perf_counter() - rank_start,
                    contexts={},
                )
                for policy, count in PACKING.items():
                    pack_start = time.perf_counter()
                    context, tokens = pack_context(
                        ranked, top_k=count, token_budget=4096, encoder=encoder
                    )
                    route["contexts"][policy] = {
                        "context": context,
                        "tokens": tokens,
                        "pack_s": time.perf_counter() - pack_start,
                        "prediction": prediction(case.instance_id, context, repo),
                    }
            except Exception as exc:
                route["error"] = f"{type(exc).__name__}: {exc}"
            route["decision_calls"] = client.calls[call_start:]
            row["routes"][name] = route
            write_json(path, row)
        row["status"] = (
            "ok"
            if all(r["status"] == "ok" for r in row["routes"].values())
            else "error"
        )
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    row.update(
        wall_s=time.perf_counter() - started,
        decision_calls=client.calls,
        decision_cost_usd=client.cost_usd,
    )
    write_json(path, row)
    print(
        json.dumps(
            {
                "case": case.original_inst_id,
                "model": model,
                "status": row["status"],
                "decision_cost": client.cost_usd,
            }
        ),
        flush=True,
    )
    return row


def run(args):
    if not args.live or not os.getenv("OPENROUTER_API_KEY"):
        raise ValueError("Billed calls require --live and OPENROUTER_API_KEY")
    _, cases = load_selection(args.cohort)
    manifest = json.loads((args.cohort / "prepared.json").read_text())
    if manifest["selection_sha256"] != digest(args.cohort / "selection.json"):
        raise ValueError("Preparation selection changed")
    sources = {}
    for entry in manifest["cases"]:
        path = args.cohort / entry["path"]
        if digest(path) != entry["sha256"]:
            raise ValueError("Prepared source changed")
        source = json.loads(path.read_text())
        sources[source["instance_id"]] = source
    if set(sources) != {c.instance_id for c in cases}:
        raise ValueError("Incomplete preparation")
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(
        args.output / "config.json",
        {
            "prepared_sha256": digest(args.cohort / "prepared.json"),
            "selection_sha256": digest(args.cohort / "selection.json"),
            "implementation": implementation(),
            "workers": args.workers,
            "max_planner_cost_per_model_usd": args.planner_budget,
            "max_decision_cost_per_case_model_usd": args.decision_budget,
            "cost_guard": (
                "observed spend checked before calls; in-flight calls may exceed "
                "threshold"
            ),
            "solver": None,
            "packing": PACKING,
        },
    )
    planners = {}
    for name, (model, reasoning, tokens) in MODELS.items():
        (args.output / name).mkdir()
        planners[name] = grep.Planner(
            model, args.planner_budget, reasoning_effort=reasoning, max_tokens=tokens
        )
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                serve_case,
                c,
                sources[c.instance_id],
                args.cohort,
                args.output,
                name,
                planners[name],
                args.decision_budget,
            )
            for c in cases
            for name in MODELS
        ]
        rows = [f.result() for f in as_completed(futures)]
    write_json(
        args.output / "usage.json",
        {
            "planner_cost_usd": {k: p.cost_usd for k, p in planners.items()},
            "decision_cost_usd": sum(r["decision_cost_usd"] for r in rows),
            "attempts": len(rows),
            "failed_case_models": sum(r["status"] != "ok" for r in rows),
        },
    )


def evaluate(args):
    # Scoring saved predictions does not execute the frozen serving pipeline.
    # Preparation and paid inference retain the implementation-drift guard.
    selection, cases = load_selection(args.cohort, check_implementation=False)
    config = json.loads((args.run / "config.json").read_text())
    if config["selection_sha256"] != digest(args.cohort / "selection.json"):
        raise ValueError("Run selection changed")
    if config["implementation"] != selection["implementation"]:
        raise ValueError("Run implementation differs from the frozen selection")
    if config["prepared_sha256"] != digest(args.cohort / "prepared.json"):
        raise ValueError("Run preparation changed")
    command = contextbench_evaluator_command(args.upstream, args.evaluator_python)
    reports = {}
    for model in MODELS:
        rows = [
            json.loads((args.run / model / f"{c.instance_id}.json").read_text())
            for c in cases
        ]
        for route in ("grep", "graph"):
            for policy in PACKING:
                name = f"{model}_{route}_{policy}"
                output = args.run / "evaluation" / name
                output.mkdir(parents=True, exist_ok=True)
                preds = []
                for case, row in zip(cases, rows, strict=True):
                    result = row["routes"].get(route, {})
                    if result.get("status") == "ok":
                        preds.append(result["contexts"][policy]["prediction"])
                    elif (args.cohort / "repos" / case.instance_id).is_dir():
                        # Failed retrieval contributes an explicit empty context.
                        preds.append(
                            prediction(
                                case.instance_id,
                                "[]",
                                args.cohort / "repos" / case.instance_id,
                            )
                        )
                report = evaluate_contextbench(
                    cases=cases,
                    predictions=preds,
                    repos_root=args.cohort / "repos",
                    output=output,
                    command=command,
                    timeout=1800,
                )
                reports[name] = report["summary"]
                print(
                    json.dumps({"arm": name, "valid": report["summary"]["num_valid"]}),
                    flush=True,
                )
    write_json(args.run / "summary.json", reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("select")
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--exclude", type=Path)
    p.add_argument("--size", type=int, default=100)
    p.add_argument("--seed", default="context-serving-v1-2026-10-11")
    p = sub.add_parser("prepare")
    p.add_argument("--cohort", type=Path, required=True)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--graph-timeout", type=float, default=120)
    p = sub.add_parser("graph-worker")
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--language", required=True)
    p.add_argument("--allow-project-preparation", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--cohort", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--planner-budget", type=float, default=3)
    p.add_argument("--decision-budget", type=float, default=0.02)
    p.add_argument("--live", action="store_true")
    p = sub.add_parser("evaluate")
    p.add_argument("--cohort", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--upstream", type=Path, required=True)
    p.add_argument("--evaluator-python", required=True)
    args = parser.parse_args()
    if args.command == "graph-worker":
        signal.signal(signal.SIGTERM, cancel_graph_worker)
    for key in (
        "size",
        "workers",
        "graph_timeout",
        "planner_budget",
        "decision_budget",
    ):
        value = getattr(args, key, None)
        if value is not None and (not math.isfinite(value) or value <= 0):
            parser.error(f"{key} must be positive and finite")
    for key in ("dataset", "output", "cohort", "exclude", "repo", "run", "upstream"):
        if getattr(args, key, None) is not None:
            setattr(args, key, getattr(args, key).resolve())
    {
        "select": select,
        "prepare": prepare,
        "graph-worker": graph_worker,
        "run": run,
        "evaluate": evaluate,
    }[args.command](args)


if __name__ == "__main__":
    main()
