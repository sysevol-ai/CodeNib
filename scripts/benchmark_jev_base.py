#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Freeze CodeNib Base candidates, then compare dedicated rerankers with Jev.

Source is read from immutable Git objects, never the possibly dirty checkout.
The preparation phase cannot call a model. Every scoring arm verifies and uses
the same candidate files; gold patches and hints are never sent to a model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import platform
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.agent.rerank_agent import RerankAgent
from codenib.code_chunker import CodeChunker
from codenib.dataset_ids import CODENIB_BASE_DATASET
from codenib.eval.retrieval_eval import (
    collect_target_blocks,
    compute_block_metrics,
    compute_metrics,
    dedup_spans,
    nodes_to_spans,
    spans_overlap,
)
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.languages import extension_to_language_map
from codenib.repository_filters import repository_path_is_visible
from codenib.types import NodeInfo
from scripts.benchmark_jev_retrieval import MeasuredDecisions

DATASET_REVISION = "4eb84e2e8918474969ce68c5b06facf14d6be604"
MODEL_REVISIONS = {
    "Qwen/Qwen3-Reranker-0.6B": "e61197ed45024b0ed8a2d74b80b4d909f1255473",
    "Qwen/Qwen3-Reranker-4B": "22e683669bc0f0bd69640a1354a6d0aebcfeede5",
    "Qwen/Qwen3-Reranker-8B": "77d193c791ed757ca307ee72715aa132723da912",
}
KS = (1, 5, 10, 20, 50)


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args, input_bytes=None) -> bytes:
    # Scope the trust decision to this explicit, local dataset repository.
    # Do not change the user's global Git configuration or any checkout.
    return subprocess.run(
        ["git", "--no-optional-locks", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


def snapshot_files(repo: Path, commit: str, chunker: CodeChunker) -> list[tuple]:
    """Select tracked regular source blobs using production path filters."""
    extensions = extension_to_language_map("chunker")
    selected = []
    for record in git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        info, raw_path = record.split(b"\t", 1)
        mode, kind, oid = info.decode().split()
        path = Path(raw_path.decode("utf-8"))
        if kind != "blob" or mode not in {"100644", "100755"}:
            continue
        if not repository_path_is_visible(path):
            continue
        if any(
            not chunker._should_include_directory(Path(part))
            for part in path.parts[:-1]
        ):
            continue
        if chunker._should_process_file_path(path, extensions):
            selected.append((path.as_posix(), oid, extensions[path.suffix]))
    return sorted(selected)


def snapshot_chunks(repo: Path, commit: str, chunker: CodeChunker, cache: dict):
    files = snapshot_files(repo, commit, chunker)
    missing = [entry for entry in files if entry not in cache]
    if missing:
        raw = git(
            repo,
            "cat-file",
            "--batch",
            input_bytes="".join(oid + "\n" for _, oid, _ in missing).encode(),
        )
        stream = io.BytesIO(raw)
        for name, oid, language in missing:
            actual_oid, kind, size = stream.readline().decode().strip().split()
            if actual_oid != oid or kind != "blob":
                raise ValueError("Git blob identity mismatch")
            payload = stream.read(int(size))
            if len(payload) != int(size) or stream.read(1) != b"\n":
                raise ValueError("Truncated Git blob stream")
            key = (name, oid, language)
            if len(payload) > chunker.repo_config.max_file_size_mb * 1024**2:
                cache[key] = []
                continue
            source = (
                payload.decode("utf-8", errors="replace")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            if chunker._is_minified_source(source, path=name):
                cache[key] = []
                continue
            cache[key] = chunker._chunk_source_with_language(source, name, language)
    chunks = {}
    for entry in files:
        for chunk in cache[entry]:
            if chunk.content and chunk.content.strip():
                key = (chunk.node_id, chunk.start_line, chunk.end_line)
                chunks.setdefault(key, chunk._replace(file=entry[0]))
    return list(chunks.values()), files


def metrics(case: dict, nodes: list[NodeInfo]) -> dict:
    """Use unique files and non-overlapping, 1-based spans at each cutoff."""
    files = list(dict.fromkeys(node.file for node in nodes))
    spans = dedup_spans(nodes_to_spans(nodes, sort_by_score=False))
    targets = case["target_files"]
    blocks = case["target_blocks"]
    result = {}
    for k in KS:
        file_scores = compute_metrics(files[:k], targets)
        block_scores = compute_block_metrics(spans[:k], blocks)
        result[f"file_recall@{k}"] = file_scores["recall"]
        result[f"span_recall@{k}"] = block_scores["recall"]
        result[f"span_all@{k}"] = block_scores["accuracy"]
    result["span_mrr"] = next(
        (
            1 / rank
            for rank, span in enumerate(spans, 1)
            if any(spans_overlap(span, gold) for gold in blocks)
        ),
        0.0,
    )
    # Also report coverage within the first k raw chunks, without deduplication.
    for k in (5, 10):
        result[f"raw_chunk_file_recall@{k}"] = compute_metrics(
            list(dict.fromkeys(node.file for node in nodes[:k])), targets
        )["recall"]
    return result


def visible_node(chunk, max_chars: int, score: float = 0.0) -> NodeInfo:
    """Use the same visible text and line range for scoring and coverage audits."""
    content = chunk.content[:max_chars]
    return NodeInfo(
        node_id=chunk.node_id,
        node_name=chunk.node_id,
        file=chunk.file,
        type=chunk.chunk_type,
        start_line=chunk.start_line,
        end_line=min(chunk.end_line, chunk.start_line + len(content.splitlines()) - 1),
        content=content,
        score=score,
    )


def pool_coverage(case: dict, nodes: list[NodeInfo]) -> dict:
    """Measure raw pool coverage, before order-dependent overlap deduplication."""
    files = list(dict.fromkeys(n.file for n in nodes))
    blocks = compute_block_metrics(
        nodes_to_spans(nodes, sort_by_score=False), case["target_blocks"]
    )
    return {
        "file_recall": compute_metrics(files, case["target_files"])["recall"],
        "span_recall": blocks["recall"],
        "span_all": blocks["accuracy"],
    }


def prepare(args) -> None:
    from datasets import load_dataset

    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases").mkdir()
    dataset = load_dataset(
        CODENIB_BASE_DATASET, split="test", revision=DATASET_REVISION
    )
    rows = sorted(dataset, key=lambda row: (row["repo"], row["instance_id"]))
    if args.limit:
        rows = rows[: args.limit]
    chunker = CodeChunker(chunk_depth=2, max_lines_per_chunk=100)
    previous_repo = None
    cache = {}
    records = []
    for index, row in enumerate(rows, 1):
        started = time.perf_counter()
        if previous_repo != row["repo"]:
            cache.clear()
            previous_repo = row["repo"]
        repo = args.prebuilt_root / row["instance_id"] / "repo"
        commit = (
            git(repo, "rev-parse", row["base_commit"] + "^{commit}").decode().strip()
        )
        if commit != row["base_commit"]:
            raise ValueError("Dataset base commit did not resolve exactly")
        chunks, files = snapshot_chunks(repo, commit, chunker, cache)
        lookup = {(c.node_id, c.start_line, c.end_line): c for c in chunks}
        bm25 = BM25CodeIndexer(chunks=chunks, max_k=args.candidates)
        retrieval_start = time.perf_counter()
        hits = bm25.search(
            row["problem_statement"], top_k=args.candidates, return_code_content=False
        )
        retrieval_ms = (time.perf_counter() - retrieval_start) * 1000
        nodes = []
        for rank, hit in enumerate(hits):
            chunk = lookup[(hit.node_id, hit.start_line, hit.end_line)]
            nodes.append(visible_node(chunk, args.max_chars, float(len(hits) - rank)))
        case = {
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "language_group": row["language_group"],
            "base_commit": commit,
            "git_tree": git(repo, "rev-parse", commit + "^{tree}").decode().strip(),
            "query": row["problem_statement"],
            "target_files": row["gt_target_files"],
            "target_blocks": collect_target_blocks(row),
            "candidates": [n.model_dump() for n in nodes],
            "source_files": len(files),
            "corpus_chunks": len(chunks),
            "selected_blobs_sha256": hashlib.sha256(
                json.dumps(files).encode()
            ).hexdigest(),
            "retrieval_ms": retrieval_ms,
            "preparation_ms": (time.perf_counter() - started) * 1000,
        }
        case["baseline_metrics"] = metrics(case, nodes)
        if args.coverage_ks:
            case["coverage_curve"] = {
                str(k): pool_coverage(case, nodes[:k]) for k in args.coverage_ks
            }
            # Gold only selects rows for this offline audit, never candidates
            # for retrieval or model input. Non-target files cannot add recall.
            target_files = set(case["target_files"]) | {
                b["file"] for b in case["target_blocks"]
            }
            case["corpus_coverage"] = pool_coverage(
                case,
                [
                    visible_node(c, args.max_chars)
                    for c in chunks
                    if c.file in target_files
                ],
            )
        path = args.output / "cases" / f"{row['instance_id']}.json"
        write_json(path, case)
        records.append(
            {
                "instance_id": row["instance_id"],
                "file": str(path.relative_to(args.output)),
                "sha256": digest(path),
            }
        )
        print(
            json.dumps(
                {
                    "phase": "prepare",
                    "done": index,
                    "total": len(rows),
                    "instance": row["instance_id"],
                    "chunks": len(chunks),
                    "candidate_span_recall": pool_coverage(case, nodes)["span_recall"],
                    "seconds": round(case["preparation_ms"] / 1000, 2),
                }
            ),
            flush=True,
        )
    write_json(
        args.output / "manifest.json",
        {
            "dataset": CODENIB_BASE_DATASET,
            "dataset_revision": DATASET_REVISION,
            "split": "test",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "candidate_count": args.candidates,
            "coverage_ks": args.coverage_ks,
            "max_content_chars": args.max_chars,
            "retrieval": (
                "Production BM25 on Git base_commit source; full issue text; "
                "no hints, patch, or gold injection"
            ),
            "chunk_policy": (
                "Registry languages, production path/minification filters, "
                "depth 2, max 100 lines; visible spans after content truncation"
            ),
            "script_sha256": digest(Path(__file__)),
            "cases": records,
        },
    )


def load_cases(root: Path) -> tuple[dict, list]:
    manifest = json.loads((root / "manifest.json").read_text())
    cases = []
    for record in manifest["cases"]:
        path = root / record["file"]
        if digest(path) != record["sha256"]:
            raise ValueError(f"Frozen case changed: {path}")
        cases.append(json.loads(path.read_text()))
    return manifest, cases


def prefix(args) -> None:
    """Freeze an exact smaller prefix without rebuilding or changing tie order."""
    manifest, cases = load_cases(args.prepared)
    if args.candidates > manifest["candidate_count"]:
        raise ValueError("Prefix exceeds the prepared candidate pool")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases").mkdir()
    records = []
    for case in cases:
        case["candidates"] = case["candidates"][: args.candidates]
        nodes = [NodeInfo.model_validate(n) for n in case["candidates"]]
        case["baseline_metrics"] = metrics(case, nodes)
        path = args.output / "cases" / f"{case['instance_id']}.json"
        write_json(path, case)
        records.append(
            {
                "instance_id": case["instance_id"],
                "file": str(path.relative_to(args.output)),
                "sha256": digest(path),
            }
        )
    write_json(
        args.output / "manifest.json",
        {
            **manifest,
            "candidate_count": args.candidates,
            "parent_candidate_count": manifest["candidate_count"],
            "parent_manifest_sha256": digest(args.prepared / "manifest.json"),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "script_sha256": digest(Path(__file__)),
            "cases": records,
        },
    )


def summarize(rows: list) -> dict:
    import numpy as np

    return {
        "observations": len(rows),
        "instances": len({r["instance_id"] for r in rows}),
        "metrics": {
            key: float(np.mean([row["metrics"][key] for row in rows]))
            for key in rows[0]["metrics"]
        },
        "rerank_p50_ms": float(np.median([r["rerank_ms"] for r in rows])),
        "rerank_p95_ms": float(np.percentile([r["rerank_ms"] for r in rows], 95)),
        "rerank_mean_ms": float(np.mean([r["rerank_ms"] for r in rows])),
        "rerank_max_ms": max(r["rerank_ms"] for r in rows),
        "failed_observations": sum(
            bool(r.get("error") or r["failed_calls"]) for r in rows
        ),
        "failed_calls": sum(r["failed_calls"] for r in rows),
        "recorded_cost_usd": sum(r["recorded_cost_usd"] for r in rows),
        "mean_tie_fraction": float(np.mean([r["tie_fraction"] for r in rows])),
    }


def score_qwen(scorer, query: str, docs: list[str]) -> list[float]:
    """Use the production pair template with an efficient last-position head.

    Computing logits for earlier positions is unnecessary for yes/no scoring.
    This is an explicit benchmark inference setting, not a production change.
    BF16 GEMM shape changes may slightly change rounding versus full logits.
    Errors are surfaced rather than silently assigning zero on an OOM.
    """
    import torch

    pairs = [scorer._format_pair(query, doc) for doc in docs]
    scores = []
    with torch.inference_mode():
        for start in range(0, len(pairs), scorer.batch_size):
            inputs = scorer._build_input_ids(pairs[start : start + scorer.batch_size])
            inputs = {name: value.to(scorer._device) for name, value in inputs.items()}
            last = scorer._model(**inputs, logits_to_keep=1, use_cache=False).logits[
                :, -1, :
            ]
            yes_no = torch.stack(
                [last[:, scorer._yes_id], last[:, scorer._no_id]], dim=-1
            )
            scores.extend(torch.softmax(yes_no.float(), dim=-1)[:, 0].tolist())
    return scores


def run(args) -> None:
    import torch

    manifest, cases = load_cases(args.prepared)
    if args.limit:
        cases = cases[: args.limit]
    if args.backend == "jev" and (not args.live or not os.getenv("OPENROUTER_API_KEY")):
        raise ValueError("Jev requires --live and OPENROUTER_API_KEY")
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    client = None
    if args.backend == "jev":
        model = args.model or "typesafe/jev-1.13"
        client = MeasuredDecisions(model=model, max_cost_usd=args.max_cost_usd)
        scorer = RerankAgent(decisions=client)
        revision = None
    else:
        from huggingface_hub import snapshot_download

        from codenib.index.rerank.cross_encoder import QwenRerankerWrapper

        model = args.model or "Qwen/Qwen3-Reranker-0.6B"
        revision = MODEL_REVISIONS[model]
        # Resolve the exact cached snapshot first. This also avoids a Transformers
        # tokenizer metadata probe that ignores offline mode for remote IDs.
        snapshot = snapshot_download(model, revision=revision, local_files_only=True)
        scorer = QwenRerankerWrapper(
            snapshot,
            device="cuda",
            torch_dtype="bfloat16",
            batch_size=args.batch_size,
            max_length=12288,
        )
        # No benchmark case is used to choose prompts or warm the local model.
        score_qwen(
            scorer,
            "Find the code handling a failed operation.",
            ["def retry(): pass"] * args.batch_size,
        )
        torch.cuda.synchronize()
    load_ms = (time.perf_counter() - started) * 1000
    config = {
        "backend": args.backend,
        "model": model,
        "revision": revision,
        "gpu": torch.cuda.get_device_name(0),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "repeats": args.repeats,
        "instances": len(cases),
        "candidate_count": manifest["candidate_count"],
        "batch_size": args.batch_size if args.backend == "qwen" else 10,
        "qwen_max_length": 12288 if args.backend == "qwen" else None,
        "qwen_inference": (
            "BF16; logits_to_keep=1; use_cache=False; OOM is an explicit failure"
            if args.backend == "qwen"
            else None
        ),
        "load_and_warm_ms": load_ms,
        "prepared_manifest_sha256": digest(args.prepared / "manifest.json"),
        "script_sha256": digest(Path(__file__)),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Same frozen candidate content and order for every arm. "
            "Jev includes network and serialization. Local models are warm. "
            "Failed observations retain production fallback; no API retries."
        ),
    }
    write_json(args.output / "config.json", config)
    jobs = [(rep, case) for rep in range(args.repeats) for case in cases]
    random.Random(20260923).shuffle(jobs)
    rows = []
    for index, (rep, case) in enumerate(jobs, 1):
        nodes = [NodeInfo.model_validate(n) for n in case["candidates"]]
        call_start = len(client.calls) if client else 0
        error = None
        if not client:
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        try:
            if client:
                ranked = scorer.rerank_nodes(
                    case["query"], nodes, include_content=False
                )
            else:
                docs = [
                    f"Name: {n.node_name}\nFile: {n.file}\nContent:\n{n.content}"
                    for n in nodes
                ]
                scores = score_qwen(scorer, case["query"], docs)
                if len(scores) != len(nodes) or not all(
                    math.isfinite(s) for s in scores
                ):
                    raise ValueError("Reranker returned invalid scores")
                ranked = sorted(
                    [
                        n.model_copy(update={"score": float(s)})
                        for n, s in zip(nodes, scores, strict=True)
                    ],
                    key=lambda n: n.score,
                    reverse=True,
                )
                torch.cuda.synchronize()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            ranked = nodes
        elapsed_ms = (time.perf_counter() - started) * 1000
        calls = client.calls[call_start:] if client else []
        score_values = [float(n.score) for n in ranked]
        row = {
            "instance_id": case["instance_id"],
            "repo": case["repo"],
            "language_group": case["language_group"],
            "rep": rep,
            "rerank_ms": elapsed_ms,
            "peak_gpu_mib": (
                torch.cuda.max_memory_allocated() / 1024**2 if not client else None
            ),
            "metrics": metrics(case, ranked),
            "baseline_metrics": case["baseline_metrics"],
            "tie_fraction": (
                1 - len(set(score_values)) / len(score_values) if score_values else 0.0
            ),
            "ranking": [
                {
                    "node_id": n.node_id,
                    "start_line": n.start_line,
                    "end_line": n.end_line,
                    "score": n.score,
                }
                for n in ranked
            ],
            "call_indices": (
                list(range(call_start, len(client.calls))) if client else []
            ),
            "failed_calls": sum("error" in c for c in calls),
            "recorded_cost_usd": sum(
                c.get("usage", {}).get("cost", 0.0) for c in calls
            ),
            "error": error,
        }
        rows.append(row)
        write_json(args.output / "rows.json", rows)
        write_json(args.output / "summary.json", summarize(rows))
        if client:
            write_json(args.output / "calls.json", client.calls)
        print(
            json.dumps(
                {
                    "phase": "score",
                    "done": index,
                    "total": len(jobs),
                    "instance": case["instance_id"],
                    "rep": rep,
                    "ms": round(elapsed_ms, 1),
                    "span_recall@5": row["metrics"]["span_recall@5"],
                    "failed_calls": row["failed_calls"],
                    "error": error,
                }
            ),
            flush=True,
        )
        if client and client.cost_usd >= args.max_cost_usd:
            raise RuntimeError("Recorded cost guard reached; partial results retained")
    print(json.dumps({"phase": "complete", "summary": summarize(rows)}), flush=True)
    if not client:
        scorer.close()


def paired_bootstrap(cases: list, left: dict, right: dict, key: str) -> dict:
    """Paired differences, resampling repositories rather than repeat rows."""
    import numpy as np

    groups = defaultdict(list)
    for case in cases:
        iid = case["instance_id"]
        groups[case["repo"]].append(left[iid][key] - right[iid][key])
    values = list(groups.values())
    rng = np.random.default_rng(20260923)
    draws = [
        np.mean(
            [v for j in rng.integers(0, len(values), len(values)) for v in values[j]]
        )
        for _ in range(5000)
    ]
    return {
        "difference": float(np.mean([v for group in values for v in group])),
        "ci95": np.percentile(draws, [2.5, 97.5]).tolist(),
        "repositories": len(groups),
    }


def candidate_coverage(case: dict) -> dict:
    """Measure the frozen pool before order-dependent overlap deduplication."""
    return pool_coverage(case, [NodeInfo.model_validate(n) for n in case["candidates"]])


def repeat_consistency(rows: list) -> dict:
    """Compare ranks and scores across repeats, retaining failed observations."""
    import numpy as np

    groups = defaultdict(list)
    for row in rows:
        groups[row["instance_id"]].append(row)
    result = {
        "repeated_instances": 0,
        "instances_with_rank_changes": 0,
        "instances_with_score_changes": 0,
        "instances_with_span_recall5_changes": 0,
    }
    overlaps, correlations = [], []

    def keys(row):
        return [(n["node_id"], n["start_line"], n["end_line"]) for n in row["ranking"]]

    for items in groups.values():
        if len(items) < 2:
            continue
        result["repeated_instances"] += 1
        items = sorted(items, key=lambda r: r["rep"])
        first = items[0]
        reference = keys(first)
        positions = {key: rank for rank, key in enumerate(reference)}
        scores = dict(
            zip(reference, [n["score"] for n in first["ranking"]], strict=True)
        )
        rank_change = score_change = metric_change = False
        for other in items[1:]:
            order = keys(other)
            if (
                len(positions) != len(reference)
                or len(set(order)) != len(order)
                or set(order) != set(reference)
            ):
                raise ValueError("Repeated rankings changed candidate identity")
            rank_change |= order != reference
            score_change |= scores != dict(
                zip(order, [n["score"] for n in other["ranking"]], strict=True)
            )
            metric_change |= (
                first["metrics"]["span_recall@5"] != other["metrics"]["span_recall@5"]
            )
            k = min(5, len(order))
            overlaps.append(len(set(reference[:k]) & set(order[:k])) / max(k, 1))
            n = len(order)
            squared = sum(
                (positions[key] - rank) ** 2 for rank, key in enumerate(order)
            )
            correlations.append(1 - 6 * squared / (n * (n**2 - 1)) if n > 1 else 1)
        result["instances_with_rank_changes"] += rank_change
        result["instances_with_score_changes"] += score_change
        result["instances_with_span_recall5_changes"] += metric_change
    result["mean_raw_top5_set_overlap"] = float(np.mean(overlaps)) if overlaps else None
    result["mean_rank_spearman"] = (
        float(np.mean(correlations)) if correlations else None
    )
    return result


def analyze(args) -> dict:
    import numpy as np

    manifest, cases = load_cases(args.prepared)
    result = {
        "dataset": manifest,
        "arms": {},
        "per_case": {},
        "paired_jev_minus": {},
    }
    baseline = {c["instance_id"]: c["baseline_metrics"] for c in cases}
    coverage = {c["instance_id"]: candidate_coverage(c) for c in cases}
    reachable = {iid for iid, values in coverage.items() if values["span_recall"] > 0}
    result["candidate_coverage"] = {
        "metrics": {
            key: float(np.mean([value[key] for value in coverage.values()]))
            for key in next(iter(coverage.values()))
        },
        "instances_without_target_block": len(cases) - len(reachable),
        "instances_with_all_target_blocks": sum(
            value["span_all"] == 1.0 for value in coverage.values()
        ),
    }
    result["baseline_per_language"] = {
        language: {
            "instances": sum(c["language_group"] == language for c in cases),
            "metrics": {
                key: float(
                    np.mean(
                        [
                            baseline[c["instance_id"]][key]
                            for c in cases
                            if c["language_group"] == language
                        ]
                    )
                )
                for key in next(iter(baseline.values()))
            },
        }
        for language in sorted({c["language_group"] for c in cases})
    }
    per_arm = {getattr(args, "baseline_name", "bm25"): baseline}
    run_rows = {}
    for path in args.runs:
        config = json.loads((path / "config.json").read_text())
        rows = json.loads((path / "rows.json").read_text())
        if config["prepared_manifest_sha256"] != digest(
            args.prepared / "manifest.json"
        ):
            raise ValueError("Runs use different frozen candidates")
        expected = {
            (c["instance_id"], rep) for c in cases for rep in range(config["repeats"])
        }
        if {(r["instance_id"], r["rep"]) for r in rows} != expected or len(rows) != len(
            expected
        ):
            raise ValueError(f"Incomplete or duplicated run: {path}")
        arm = config["model"]
        if arm in run_rows:
            raise ValueError(f"Duplicate model arm: {arm}")
        run_rows[arm] = rows
        result["arms"][arm] = {
            "config": config,
            "summary": summarize(rows),
            "repeat_consistency": repeat_consistency(rows),
            "max_peak_gpu_mib": max(
                (r["peak_gpu_mib"] for r in rows if r["peak_gpu_mib"] is not None),
                default=None,
            ),
            "conditional_on_candidate_hit": (
                summarize([r for r in rows if r["instance_id"] in reachable])
                if reachable
                else None
            ),
            "per_language": {
                language: summarize(
                    [r for r in rows if r["language_group"] == language]
                )
                for language in sorted({r["language_group"] for r in rows})
            },
        }
        if config["backend"] == "jev":
            calls = json.loads((path / "calls.json").read_text())
            indices = [i for r in rows for i in r["call_indices"]]
            if sorted(indices) != list(range(len(calls))):
                raise ValueError("API call trace does not match observations")
            result["arms"][arm]["api"] = {
                "requests": len(calls),
                "successful_calls": sum("error" not in c for c in calls),
                "errors_by_status": dict(
                    Counter(
                        str(c.get("http_status", "non_http"))
                        for c in calls
                        if "error" in c
                    )
                ),
                "upstream_cloudflare_blocks": sum(
                    c.get("upstream_cloudflare_block", False) for c in calls
                ),
                "resolved_models": dict(
                    Counter(c["model"] for c in calls if "model" in c)
                ),
                "reported_input_tokens": sum(
                    c.get("usage", {}).get("input_tokens", 0) for c in calls
                ),
                "reported_output_tokens": sum(
                    c.get("usage", {}).get("output_tokens", 0) for c in calls
                ),
                "call_p50_ms": float(np.median([c["latency_ms"] for c in calls])),
                "call_p95_ms": float(
                    np.percentile([c["latency_ms"] for c in calls], 95)
                ),
                "failed_call_cost": "unknown; missing usage is not imputed as zero",
            }
            successful = [r for r in rows if not r["error"] and not r["failed_calls"]]
            result["arms"][arm]["successful_observation_summary"] = (
                summarize(successful) if successful else None
            )
        per_arm[arm] = {
            c["instance_id"]: {
                key: float(
                    np.mean(
                        [
                            r["metrics"][key]
                            for r in rows
                            if r["instance_id"] == c["instance_id"]
                        ]
                    )
                )
                for key in baseline[c["instance_id"]]
            }
            for c in cases
        }
    result["baseline"] = {
        key: float(np.mean([v[key] for v in baseline.values()]))
        for key in next(iter(baseline.values()))
    }
    jev = next(
        (
            a
            for a, value in result["arms"].items()
            if value["config"]["backend"] == "jev"
        ),
        None,
    )
    if jev:
        for arm in per_arm:
            if arm == jev:
                continue
            result["paired_jev_minus"][arm] = {
                key: paired_bootstrap(cases, per_arm[jev], per_arm[arm], key)
                for key in (
                    "span_recall@1",
                    "span_recall@5",
                    "file_recall@5",
                    "span_mrr",
                )
            }
            differences = [
                per_arm[jev][c["instance_id"]]["span_recall@5"]
                - per_arm[arm][c["instance_id"]]["span_recall@5"]
                for c in cases
            ]
            result["paired_jev_minus"][arm]["win_tie_loss"] = {
                "win": sum(d > 1e-9 for d in differences),
                "tie": sum(abs(d) <= 1e-9 for d in differences),
                "loss": sum(d < -1e-9 for d in differences),
            }
        failed_ids = {
            r["instance_id"] for r in run_rows[jev] if r["error"] or r["failed_calls"]
        }
        clean_cases = [c for c in cases if c["instance_id"] not in failed_ids]
        result["jev_success_subset"] = {
            "instances": len(clean_cases),
            "excluded_instances": sorted(failed_ids),
            "note": (
                "Post-hoc availability diagnostic; same subset for every arm; "
                "excludes any instance with a failed Jev repeat."
            ),
            "metrics": (
                {
                    arm: {
                        key: float(
                            np.mean(
                                [values[c["instance_id"]][key] for c in clean_cases]
                            )
                        )
                        for key in ("span_recall@5", "file_recall@5", "span_mrr")
                    }
                    for arm, values in per_arm.items()
                }
                if clean_cases
                else {}
            ),
            "paired_jev_minus": (
                {
                    arm: paired_bootstrap(
                        clean_cases, per_arm[jev], values, "span_recall@5"
                    )
                    for arm, values in per_arm.items()
                    if arm != jev
                }
                if clean_cases
                else {}
            ),
        }
    result["paired_qwen_scales"] = {}
    models = list(MODEL_REVISIONS)
    for smaller, larger in zip(models, models[1:], strict=False):
        if smaller in per_arm and larger in per_arm:
            result["paired_qwen_scales"][f"{larger} minus {smaller}"] = {
                key: paired_bootstrap(cases, per_arm[larger], per_arm[smaller], key)
                for key in (
                    "span_recall@1",
                    "span_recall@5",
                    "file_recall@5",
                    "span_mrr",
                )
            }
    for case in cases:
        iid = case["instance_id"]
        result["per_case"][iid] = {
            "repo": case["repo"],
            "language_group": case["language_group"],
            "base_commit": case["base_commit"],
            "query_chars": len(case["query"]),
            "target_blocks": len(case["target_blocks"]),
            "candidate_coverage": coverage[iid],
            "arms": {arm: values[iid] for arm, values in per_arm.items()},
            "rerank_mean_ms": {
                arm: float(
                    np.mean([r["rerank_ms"] for r in rows if r["instance_id"] == iid])
                )
                for arm, rows in run_rows.items()
            },
            "failed_observations": {
                arm: sum(
                    bool(r["failed_calls"] or r["error"])
                    for r in rows
                    if r["instance_id"] == iid
                )
                for arm, rows in run_rows.items()
            },
        }
    if args.case_csv:
        args.case_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.case_csv.open("w", newline="") as stream:
            writer = None
            for iid, case in result["per_case"].items():
                flat = {"instance_id": iid}
                for key in (
                    "repo",
                    "language_group",
                    "base_commit",
                    "query_chars",
                    "target_blocks",
                ):
                    flat[key] = case[key]
                flat["candidate_span_coverage"] = case["candidate_coverage"][
                    "span_recall"
                ]
                for arm, values in case["arms"].items():
                    for key in (
                        "span_recall@1",
                        "span_recall@5",
                        "span_recall@10",
                        "file_recall@5",
                        "span_mrr",
                    ):
                        flat[f"{arm}/{key}"] = values[key]
                    if arm in case["rerank_mean_ms"]:
                        flat[f"{arm}/rerank_mean_ms"] = case["rerank_mean_ms"][arm]
                        flat[f"{arm}/failed_observations"] = case[
                            "failed_observations"
                        ][arm]
                if writer is None:
                    writer = csv.DictWriter(
                        stream, fieldnames=list(flat), lineterminator="\n"
                    )
                    writer.writeheader()
                writer.writerow(flat)
        result.pop("per_case")
        result["per_case_csv"] = os.path.relpath(args.case_csv, args.output.parent)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "baseline": result["baseline"],
                "arms": {a: v["summary"] for a, v in result["arms"].items()},
                "paired": result["paired_jev_minus"],
            },
            indent=2,
        )
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--prebuilt-root", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--candidates", type=int, default=50)
    prep.add_argument("--max-chars", type=int, default=3000)
    prep.add_argument("--coverage-ks", type=int, nargs="+", default=[])
    prep.add_argument("--limit", type=int)
    prep.set_defaults(function=prepare)
    subset = commands.add_parser("prefix")
    subset.add_argument("--prepared", type=Path, required=True)
    subset.add_argument("--output", type=Path, required=True)
    subset.add_argument("--candidates", type=int, required=True)
    subset.set_defaults(function=prefix)
    score = commands.add_parser("run")
    score.add_argument("--prepared", type=Path, required=True)
    score.add_argument("--output", type=Path, required=True)
    score.add_argument("--backend", choices=("qwen", "jev"), required=True)
    score.add_argument("--model")
    score.add_argument("--repeats", type=int, default=2)
    score.add_argument("--batch-size", type=int, default=8)
    score.add_argument("--limit", type=int)
    score.add_argument("--live", action="store_true")
    score.add_argument("--max-cost-usd", type=float, default=2.0)
    score.set_defaults(function=run)
    analysis = commands.add_parser("analyze")
    analysis.add_argument("--prepared", type=Path, required=True)
    analysis.add_argument("--runs", type=Path, nargs="+", required=True)
    analysis.add_argument("--output", type=Path, required=True)
    analysis.add_argument("--baseline-name", default="bm25")
    analysis.add_argument(
        "--case-csv",
        type=Path,
        help="Write per-instance metrics separately to keep JSON compact",
    )
    analysis.set_defaults(function=analyze)
    args = parser.parse_args()
    for field in ("limit", "repeats", "batch_size", "candidates", "max_chars"):
        value = getattr(args, field, None)
        if value is not None and value < 1:
            parser.error(f"{field} must be positive")
    if any(k < 1 or k > args.candidates for k in getattr(args, "coverage_ks", [])):
        parser.error("coverage-ks must be positive and within candidates")
    cost = getattr(args, "max_cost_usd", 1.0)
    if not math.isfinite(cost) or cost <= 0:
        parser.error("max-cost-usd must be positive and finite")
    args.function(args)


if __name__ == "__main__":
    main()
