#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Local paired BM25/dense retrieval experiment with opt-in billed Jev calls.

Uses one frozen Python chunk corpus, the production BM25 index, embedding
wrapper, and decision reranker. Dense document encoding is an offline build
cost; online dense timings include one query encoding and an in-memory FAISS
search. Run with OPENROUTER_API_KEY and --live to enable OpenRouter requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.agent.rerank_agent import RerankAgent
from codenib.code_chunker import CodeChunker
from codenib.index.embedding.model_policy import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
)
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.llm import OpenRouterDecisions
from codenib.types import NodeInfo


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _key(node) -> tuple:
    return node.node_id, node.start_line, node.end_line


def quality(case: dict, nodes: list, top_k: int) -> dict:
    """Path-group coverage, all-group success, and first relevant reciprocal rank.

    Multiple chunks from the same file cannot inflate coverage. These are
    file-level localization metrics, not function correctness or answer quality.
    """
    paths = [node.file for node in nodes[:top_k]]
    groups = case["required_path_groups"]
    hits = [any(path in group for path in paths) for group in groups]
    ranks = [
        rank
        for rank, path in enumerate(paths, 1)
        if any(path in group for group in groups)
    ]
    return {
        "coverage": sum(hits) / len(groups),
        "success": float(all(hits)),
        "mrr": 1 / ranks[0] if ranks else 0.0,
    }


class MeasuredDecisions(OpenRouterDecisions):
    """Record actual successful-call usage; never silently count a fallback as Jev."""

    def __init__(self, *, model: str, max_cost_usd: float):
        super().__init__(model=model, max_retries=0, timeout=30)
        self.calls = []
        self.cost_usd = 0.0
        self.max_cost_usd = max_cost_usd

    def decide(self, *, state, questions):
        start = time.perf_counter()
        record = {"question_count": len(questions)}
        try:
            if self.cost_usd >= self.max_cost_usd:
                raise RuntimeError("Experiment API cost limit reached")
            result = super().decide(state=state, questions=questions)
            record.update(
                model=result.model,
                usage=result.usage,
                answers={
                    name: answer.model_dump() for name, answer in result.answers.items()
                },
            )
            self.cost_usd += result.usage.get("cost", 0.0)
            return result
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            response = getattr(error, "response", None)
            if response is not None:
                record["http_status"] = response.status_code
                record["upstream_cloudflare_block"] = (
                    "Cloudflare" in response.text and "typesafe.ai" in response.text
                )
            raise
        finally:
            record["latency_ms"] = (time.perf_counter() - start) * 1000
            self.calls.append(record)


def build_corpus(repo: Path, max_chars: int):
    chunker = CodeChunker(language="python", chunk_depth=2, max_lines_per_chunk=100)
    chunks = {}
    files = sorted((repo / "codenib").rglob("*.py"))
    for path in files:
        relative = path.relative_to(repo).as_posix()
        for chunk in chunker.chunk_file(str(path), relative_path=relative):
            if chunk.content and chunk.content.strip():
                chunks.setdefault(
                    (chunk.node_id, chunk.start_line, chunk.end_line),
                    chunk._replace(file=relative, content=chunk.content[:max_chars]),
                )
    ordered = sorted(
        chunks.values(), key=lambda c: (c.node_id, c.start_line, c.end_line)
    )
    nodes = [
        NodeInfo(
            node_id=c.node_id,
            node_name=c.node_id,
            file=c.file,
            type=c.chunk_type,
            start_line=c.start_line,
            end_line=c.end_line,
            content=c.content,
        )
        for c in ordered
    ]
    return ordered, nodes, len(files)


def summarize(rows: list[dict]) -> dict:
    import numpy as np

    by_arm = defaultdict(list)
    for row in rows:
        by_arm[row["arm"]].append(row)
    return {
        arm: {
            "observations": len(items),
            "candidate_coverage": float(
                np.mean([r["candidate_quality"]["coverage"] for r in items])
            ),
            "coverage_at_5": float(
                np.mean([r["quality_at_5"]["coverage"] for r in items])
            ),
            "success_at_5": float(
                np.mean([r["quality_at_5"]["success"] for r in items])
            ),
            "mrr_at_5": float(np.mean([r["quality_at_5"]["mrr"] for r in items])),
            "total_p50_ms": float(np.median([r["total_ms"] for r in items])),
            "total_p95_ms": float(np.percentile([r["total_ms"] for r in items], 95)),
            "retrieval_p50_ms": float(np.median([r["retrieval_ms"] for r in items])),
            "rerank_p50_ms": float(np.median([r["rerank_ms"] for r in items])),
            "recorded_cost_usd": sum(r["recorded_cost_usd"] for r in items),
            "failed_calls": sum(r["failed_calls"] for r in items),
        }
        for arm, items in by_arm.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument(
        "--cases", type=Path, default=ROOT / "docs/experiments/jev_retrieval_cases.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--live", action="store_true", help="Authorize billed OpenRouter requests"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--candidates", type=int, default=20)
    parser.add_argument("--model", default="typesafe/jev-1.13")
    parser.add_argument("--max-cost-usd", type=float, default=0.25)
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Retain failed requests and measure production fallback behavior",
    )
    parser.add_argument(
        "--scan-size",
        type=int,
        default=0,
        help="Optional full Jev scan of a fixed small subcorpus on the first case",
    )
    args = parser.parse_args()
    if not args.live or not os.getenv("OPENROUTER_API_KEY"):
        parser.error("Use --live and set OPENROUTER_API_KEY for this experiment")
    if args.repeats < 1 or args.candidates < 5 or args.scan_size < 0:
        parser.error("repeats must be positive, candidates >= 5, scan-size >= 0")
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    if not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
        parser.error("max-cost-usd must be positive and finite")
    args.output.mkdir(parents=True, exist_ok=False)
    cases = json.loads(args.cases.read_text())[: args.limit]
    if not cases:
        parser.error("At least one query case is required")
    if args.scan_size and args.scan_size < len(cases[0]["required_path_groups"]):
        parser.error("scan-size must fit all declared gold groups")
    _write(args.output / "cases.json", cases)
    started = time.perf_counter()
    chunks, nodes, file_count = build_corpus(args.repo, 3000)
    payload = [n.model_dump() for n in nodes]
    _write(args.output / "corpus.json", payload)
    corpus_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    for case in cases:
        for group in case["required_path_groups"]:
            if not any(n.file in group for n in nodes):
                raise ValueError(
                    f"Gold group is absent from corpus: {case['id']}: {group}"
                )
    lookup = {_key(n): n for n in nodes}
    bm25 = BM25CodeIndexer(
        chunks=chunks, max_k=args.candidates, project_root=str(args.repo)
    )
    build_ms = (time.perf_counter() - started) * 1000
    print(
        json.dumps(
            {
                "phase": "corpus",
                "chunks": len(nodes),
                "files": file_count,
                "build_ms": build_ms,
            }
        ),
        flush=True,
    )

    import faiss
    import numpy as np
    import torch

    from codenib.index.embedding.vector_store import _HuggingFaceEmbeddingWrapper

    faiss.omp_set_num_threads(1)
    started = time.perf_counter()
    embedding = _HuggingFaceEmbeddingWrapper(
        DEFAULT_EMBEDDING_MODEL,
        device="cuda",
        local_files_only=True,
        max_seq_length=1024,
        default_batch_size=32,
        encode_kwargs={"show_progress_bar": False, "normalize_embeddings": True},
    )
    model_load_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    vectors = []
    for offset in range(0, len(nodes), 256):
        vectors.extend(
            embedding.embed_documents(
                [f"{n.node_id}\n{n.content}" for n in nodes[offset : offset + 256]]
            )
        )
        if offset % 1024 == 0:
            print(
                json.dumps(
                    {
                        "phase": "embed",
                        "done": min(offset + 256, len(nodes)),
                        "total": len(nodes),
                    }
                ),
                flush=True,
            )
    matrix = np.asarray(vectors, dtype=np.float32)
    faiss.normalize_L2(matrix)
    dense = faiss.IndexFlatIP(matrix.shape[1])
    dense.add(matrix)
    document_build_ms = (time.perf_counter() - started) * 1000
    for _ in range(3):
        embedding.embed_query("find the implementation of repository search")
        bm25.search(
            "find the implementation of repository search", top_k=args.candidates
        )

    def retrieve(arm: str, query: str):
        start = time.perf_counter()
        encode_ms = 0.0
        if arm == "bm25":
            found = bm25.search(query, top_k=args.candidates, return_code_content=False)
            candidates = [
                lookup[_key(node)].model_copy(update={"score": float(len(found) - i)})
                for i, node in enumerate(found)
            ]
        else:
            vec = np.asarray([embedding.embed_query(query)], dtype=np.float32)
            torch.cuda.synchronize()
            encode_ms = (time.perf_counter() - start) * 1000
            faiss.normalize_L2(vec)
            scores, indices = dense.search(vec, args.candidates)
            candidates = [
                nodes[int(i)].model_copy(update={"score": float(score)})
                for i, score in zip(indices[0], scores[0], strict=True)
                if i >= 0
            ]
        return candidates, (time.perf_counter() - start) * 1000, encode_ms

    client = MeasuredDecisions(model=args.model, max_cost_usd=args.max_cost_usd)
    reranker = RerankAgent(decisions=client)
    rows = []
    config = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "implementation_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__),
                ROOT / "codenib/llm/decisions.py",
                ROOT / "codenib/agent/rerank_agent.py",
            )
        },
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=args.repo, text=True
        ).strip(),
        "tracked_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff"], cwd=args.repo)
        ).hexdigest(),
        "corpus_sha256": corpus_hash,
        "corpus_chunks": len(nodes),
        "source_files": file_count,
        "max_content_chars": 3000,
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "embedding_revision": DEFAULT_EMBEDDING_REVISION,
        "embedding_max_seq_length": 1024,
        "gpu": torch.cuda.get_device_name(0),
        "python": platform.python_version(),
        "candidates": args.candidates,
        "repeats": args.repeats,
        "jev_model_requested": args.model,
        "continue_on_error": args.continue_on_error,
        "bm25_and_chunk_build_ms": build_ms,
        "embedding_model_load_ms": model_load_ms,
        "embedding_document_build_ms": document_build_ms,
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "note": (
            "Exploratory single-repository file-group labels; not held-out "
            "evaluation. Build and model load excluded from warmed online "
            "latency. No retries; failed API calls retained and their unknown "
            "cost not imputed as zero."
        ),
    }
    _write(args.output / "config.json", config)

    def save():
        _write(args.output / "rows.json", rows)
        _write(args.output / "calls.json", client.calls)
        _write(args.output / "summary.json", summarize(rows))

    jobs = [(rep, case) for rep in range(args.repeats) for case in cases]
    random.Random(20260922).shuffle(jobs)
    for rep, case in jobs:
        arms = ["bm25", "dense"]
        random.Random(f"{rep}:{case['id']}").shuffle(arms)
        for arm in arms:
            candidates, retrieval_ms, encode_ms = retrieve(arm, case["query"])
            base = {
                "case": case["id"],
                "rep": rep,
                "candidate_quality": quality(case, candidates, args.candidates),
                "candidate_ids": [list(_key(n)) for n in candidates],
                "retrieval_ms": retrieval_ms,
                "query_encode_ms": encode_ms,
            }
            rows.append(
                {
                    **base,
                    "arm": arm,
                    "rerank_ms": 0.0,
                    "total_ms": retrieval_ms,
                    "quality_at_5": quality(case, candidates, 5),
                    "top_5": [list(_key(n)) for n in candidates[:5]],
                    "recorded_cost_usd": 0.0,
                    "failed_calls": 0,
                }
            )
            first_call = len(client.calls)
            start = time.perf_counter()
            ranked = reranker.rerank_nodes(case["query"], candidates, top_k=5)
            rerank_ms = (time.perf_counter() - start) * 1000
            calls = client.calls[first_call:]
            rows.append(
                {
                    **base,
                    "arm": arm + "+jev",
                    "rerank_ms": rerank_ms,
                    "total_ms": retrieval_ms + rerank_ms,
                    "quality_at_5": quality(case, ranked, 5),
                    "top_5": [list(_key(n)) for n in ranked],
                    "call_indices": list(range(first_call, len(client.calls))),
                    "recorded_cost_usd": sum(
                        c.get("usage", {}).get("cost", 0.0) for c in calls
                    ),
                    "failed_calls": sum("error" in c for c in calls),
                }
            )
            save()
            print(
                json.dumps(
                    {
                        "phase": "query",
                        "case": case["id"],
                        "rep": rep,
                        "arm": arm,
                        "retrieval_ms": round(retrieval_ms, 2),
                        "rerank_ms": round(rerank_ms, 2),
                        "coverage_at_5": rows[-1]["quality_at_5"]["coverage"],
                        "failed_calls": rows[-1]["failed_calls"],
                        "total_cost_usd": client.cost_usd,
                    }
                ),
                flush=True,
            )
            if client.cost_usd >= args.max_cost_usd:
                raise RuntimeError("Experiment API cost limit reached; results saved")
            if rows[-1]["failed_calls"] and not args.continue_on_error:
                raise RuntimeError(
                    "Jev request failed; partial observations saved for diagnosis"
                )

    if args.scan_size:
        # A declared small-corpus scaling probe, not a full-repository quality arm.
        case = cases[0]
        positives = [
            next(n for n in nodes if n.file in group)
            for group in case["required_path_groups"]
        ]
        positive_keys = {_key(n) for n in positives}
        distractors = [n for n in nodes if _key(n) not in positive_keys]
        selected = positives + random.Random(20260922).sample(
            distractors, min(args.scan_size - len(positives), len(distractors))
        )
        random.Random(1).shuffle(selected)
        first_call = len(client.calls)
        start = time.perf_counter()
        ranked = reranker.rerank_nodes(case["query"], selected, top_k=5)
        scan_ms = (time.perf_counter() - start) * 1000
        calls = client.calls[first_call:]
        _write(
            args.output / "scan.json",
            {
                "case": case["id"],
                "chunks": len(selected),
                "latency_ms": scan_ms,
                "call_count": len(calls),
                "quality_at_5": quality(case, ranked, 5),
                "failed_calls": sum("error" in c for c in calls),
                "recorded_cost_usd": sum(
                    c.get("usage", {}).get("cost", 0.0) for c in calls
                ),
                "top_5": [list(_key(n)) for n in ranked],
                "note": (
                    "Gold file chunks deliberately included; this is a latency "
                    "scaling probe, not unbiased recall measurement."
                ),
            },
        )
        save()
        if any("error" in call for call in calls) and not args.continue_on_error:
            raise RuntimeError(
                "Jev scan failed; partial observations saved for diagnosis"
            )
    print(
        json.dumps(
            {
                "phase": "complete",
                "output": str(args.output),
                "summary": summarize(rows),
                "recorded_cost_usd": client.cost_usd,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
