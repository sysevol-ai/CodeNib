#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rerank latency benchmark.

Measures wall-clock time and GPU memory for every rerank backend that is
currently available. Candidates are pulled from the local BM25 index so no
external services are needed. New backends slot in under BACKENDS below.

Usage (from repo root, with codenib-yash conda env active):

    python scripts/bench_rerank_latency.py
    python scripts/bench_rerank_latency.py --candidates 50 100
    python scripts/bench_rerank_latency.py --backends embedding st_cross qwen_cross
    python scripts/bench_rerank_latency.py --llm-base http://localhost:11434/v1 --include-llm

Output: a Markdown table written to stdout and saved to
    scripts/bench_rerank_results.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

# ── repo root on path ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codenib.paths import repo_index_dir

MANIFEST = repo_index_dir(ROOT) / "repo_manifest.json"

# Queries that cover different retrieval patterns in CodeNib itself
BENCH_QUERIES = [
    "how does the BM25 index build and search work",
    "where is the FAISS vector store initialized and queried",
    "how does the agent runner dispatch tool calls to skills",
    "explain the cross-encoder reranking interface",
    "how are SCIP symbol edges decoded into the code graph",
]

# Candidate pool sizes to sweep
DEFAULT_CANDIDATE_SIZES = [10, 25, 50, 100]

# Number of timed repetitions per (backend, candidates, query) combo
REPS = 3


# ── result container ───────────────────────────────────────────────────────────


@dataclass
class BenchResult:
    backend: str
    model: str
    n_candidates: int
    query: str
    latency_ms: float  # median across reps
    latency_ms_min: float
    latency_ms_max: float
    gpu_mem_delta_mb: float  # peak VRAM increase during score()
    scores: List[float] = field(default_factory=list, repr=False)
    error: Optional[str] = None


# ── GPU memory helper ──────────────────────────────────────────────────────────


def _gpu_mb() -> float:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.memory_allocated() / 1024 / 1024
    except ImportError:
        pass
    return 0.0


def _gpu_peak_mb() -> float:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.max_memory_allocated() / 1024 / 1024
    except ImportError:
        pass
    return 0.0


def _reset_gpu_peak() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except ImportError:
        pass


# ── candidate loader ───────────────────────────────────────────────────────────


def load_candidates(query: str, n: int) -> List[str]:
    """Pull top-n BM25 hits from the cached index as plain text strings."""
    from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer

    manifest = json.loads(MANIFEST.read_text())
    bm25_path = manifest["indexes"]["bm25"]["path"]

    index = BM25CodeIndexer(max_k=max(n, 128))
    index.load_index(bm25_path)
    results = index.search(
        query=query,
        top_k=n,
        return_code_content=True,
        wrap_with_ln=False,
    )
    docs = []
    for r in results:
        content = getattr(r, "content", None) or ""
        if not content:
            content = str(r)
        docs.append(content[:2000])  # cap at 2k chars — realistic chunk size
    return docs[:n]


# ── timing wrapper ─────────────────────────────────────────────────────────────


def timed_score(
    score_fn: Callable[[str, List[str]], List[float]],
    query: str,
    docs: List[str],
    reps: int = REPS,
    pre_fn: Optional[Callable[[str, List[str]], None]] = None,
) -> tuple[float, float, float, float, List[float]]:
    """Run score_fn `reps` times, return (median_ms, min_ms, max_ms, gpu_delta_mb, last_scores).

    If ``pre_fn`` is provided it runs ONCE before the timed loop — use this for
    backends that pre-compute document embeddings to simulate a pre-indexed store
    (e.g. ``embedding_cached``).
    """
    if pre_fn is not None:
        pre_fn(query, docs)

    timings = []
    last_scores: List[float] = []
    _reset_gpu_peak()
    mem_before = _gpu_mb()

    for _ in range(reps):
        t0 = time.perf_counter()
        last_scores = score_fn(query, docs)
        t1 = time.perf_counter()
        timings.append((t1 - t0) * 1000)

    timings.sort()
    median_ms = timings[len(timings) // 2]
    gpu_delta = _gpu_peak_mb() - mem_before

    return median_ms, timings[0], timings[-1], gpu_delta, last_scores


# ── backends ───────────────────────────────────────────────────────────────────


def _backend_embedding(query: str, docs: List[str]) -> List[float]:
    """Current fallback: dot-product with the in-process embedding model."""
    model = _backend_embedding._model  # type: ignore[attr-defined]
    q_vec = model.embed_query(query)
    d_vecs = model.embed_documents(docs)
    import numpy as np

    q = np.array(q_vec, dtype=np.float32)
    D = np.array(d_vecs, dtype=np.float32)
    return np.dot(D, q).tolist()


def _init_embedding(model_name: str) -> None:
    from codenib.index.embedding.vector_store import _HuggingFaceEmbeddingWrapper

    _backend_embedding._model = _HuggingFaceEmbeddingWrapper(model_name)  # type: ignore[attr-defined]
    _backend_embedding_cached._model = _backend_embedding._model  # type: ignore[attr-defined]
    _backend_embedding_cached._cached_vecs = None  # type: ignore[attr-defined]


# ── cached embedding: pre-index docs once, time only query-encode + dot ───────
#
# This simulates the production FAISS path: document vectors are stored in the
# index; at query time only embed_query() runs (one forward pass) followed by a
# cheap matrix multiply.  The doc embedding cost is NOT timed — it is amortized
# across all queries in a real deployment.


def _pre_embedding_cached(query: str, docs: List[str]) -> None:
    """Pre-embed docs ONCE before the timed loop — populates the module cache."""
    import numpy as np

    model = _backend_embedding_cached._model  # type: ignore[attr-defined]
    _backend_embedding_cached._cached_vecs = (  # type: ignore[attr-defined]
        np.array(model.embed_documents(docs), dtype=np.float32)
    )


def _backend_embedding_cached(query: str, docs: List[str]) -> List[float]:
    """Query-only path: embed_query + np.dot against pre-cached doc vectors."""
    import numpy as np

    model = _backend_embedding_cached._model  # type: ignore[attr-defined]
    d_vecs = _backend_embedding_cached._cached_vecs  # type: ignore[attr-defined]
    if d_vecs is None:
        # fallback if called without pre_fn (e.g. first warmup)
        d_vecs = np.array(model.embed_documents(docs), dtype=np.float32)
        _backend_embedding_cached._cached_vecs = d_vecs  # type: ignore[attr-defined]
    q_vec = np.array(model.embed_query(query), dtype=np.float32)
    return np.dot(d_vecs, q_vec).tolist()


def _make_st_cross(model_name: str) -> Callable[[str, List[str]], List[float]]:
    from codenib.index.rerank.cross_encoder import STCrossEncoderWrapper

    wrapper = STCrossEncoderWrapper(model_name, batch_size=16)
    return wrapper.score


def _make_qwen_cross(model_name: str) -> Callable[[str, List[str]], List[float]]:
    from codenib.index.rerank.cross_encoder import QwenRerankerWrapper

    wrapper = QwenRerankerWrapper(model_name, batch_size=8)
    return wrapper.score


def _make_llm_listwise(base_url: str) -> Callable[[str, List[str]], List[float]]:
    """LLM listwise via RerankAgent (slow — included for baseline comparison)."""
    from codenib.agent.rerank_agent import RerankAgent
    from codenib.llm.litellm_chat import LiteLLMChat
    from codenib.types import NodeInfo

    llm = LiteLLMChat(
        model="openai/qwen3:8b",
        api_base=base_url,
        max_tokens=512,
        temperature=0.0,
    )
    agent = RerankAgent(llm=llm, listwise_format="structured")

    def score(query: str, docs: List[str]) -> List[float]:
        nodes = [
            NodeInfo(
                node_name=f"chunk_{i}",
                type="function",
                file="bench.py",
                node_id=f"bench_{i}",
                start_line=i * 20,
                end_line=i * 20 + 19,
                score=1.0,
                content=doc,
            )
            for i, doc in enumerate(docs)
        ]
        ranked = agent.rerank_nodes(query=query, nodes=nodes, top_k=len(nodes))
        score_map = {r.node_id: r.score for r in ranked}
        return [float(score_map.get(f"bench_{i}") or 0.0) for i in range(len(docs))]

    return score


# ── registry of backends to try ───────────────────────────────────────────────

BACKENDS = {
    "embedding": {
        "label": "embedding rerank — re-encode docs (bge-base-en-v1.5)",
        "model": "BAAI/bge-base-en-v1.5",
        "init": _init_embedding,
        "score_fn": lambda _model: _backend_embedding,
        "pre_fn": None,
        "requires_llm": False,
    },
    "embedding_cached": {
        # ── FAIR PRODUCTION BASELINE ──
        # Docs are pre-embedded once (amortised across all queries in a real
        # deployment via FAISS).  Only query encoding + np.dot is timed.
        # This is what `rerank_strategy="embedding"` actually costs at serve time
        # when the vector store is already loaded.
        "label": "embedding retrieval — query-only + dot-product [PRODUCTION PATH]",
        "model": "BAAI/bge-base-en-v1.5",
        "init": _init_embedding,  # shares model with "embedding"
        "score_fn": lambda _model: _backend_embedding_cached,
        "pre_fn": _pre_embedding_cached,
        "requires_llm": False,
    },
    "st_cross": {
        # STCrossEncoderWrapper — sentence-transformers CrossEncoder
        "label": "STCrossEncoderWrapper (mxbai-rerank-large-v2, 570M)",
        "model": "mixedbread-ai/mxbai-rerank-large-v2",
        "init": None,
        "score_fn": _make_st_cross,
        "pre_fn": None,
        "requires_llm": False,
    },
    "qwen_cross": {
        # QwenRerankerWrapper — Qwen3-Reranker yes/no logit trick (0.6B ≈ bge-base size)
        "label": "QwenRerankerWrapper (Qwen3-Reranker-0.6B)",
        "model": "Qwen/Qwen3-Reranker-0.6B",
        "init": None,
        "score_fn": _make_qwen_cross,
        "pre_fn": None,
        "requires_llm": False,
    },
    "qwen_cross_4b": {
        "label": "QwenRerankerWrapper (Qwen3-Reranker-4B)",
        "model": "Qwen/Qwen3-Reranker-4B",
        "init": None,
        "score_fn": _make_qwen_cross,
        "pre_fn": None,
        "requires_llm": False,
    },
    "llm_listwise": {
        # Slow LLM baseline — only included with --include-llm
        "label": "RerankAgent listwise (qwen3:8b via Ollama)",
        "model": "qwen3:8b",
        "init": None,
        "score_fn": None,  # built at runtime from --llm-base
        "pre_fn": None,
        "requires_llm": True,
    },
}


# ── runner ─────────────────────────────────────────────────────────────────────


def run_benchmark(
    backend_keys: List[str],
    candidate_sizes: List[int],
    llm_base: str,
    include_llm: bool,
) -> List[BenchResult]:
    results: List[BenchResult] = []

    for key in backend_keys:
        cfg = BACKENDS.get(key)
        if cfg is None:
            print(f"[WARN] unknown backend {key!r}, skipping")
            continue
        if cfg["requires_llm"] and not include_llm:
            print(f"[SKIP] {cfg['label']} — pass --include-llm to enable")
            continue

        print(f"\n{'─'*60}")
        print(f"Backend: {cfg['label']}")
        print(f"Model:   {cfg['model']}")

        try:
            if cfg["requires_llm"]:
                score_fn = _make_llm_listwise(llm_base)
            else:
                if cfg["init"]:
                    cfg["init"](cfg["model"])
                score_fn = cfg["score_fn"](cfg["model"])
        except Exception as exc:
            print(f"  [LOAD FAIL] {exc}")
            for n in candidate_sizes:
                for q in BENCH_QUERIES:
                    results.append(
                        BenchResult(
                            backend=key,
                            model=cfg["model"],
                            n_candidates=n,
                            query=q[:40],
                            latency_ms=0,
                            latency_ms_min=0,
                            latency_ms_max=0,
                            gpu_mem_delta_mb=0,
                            error=str(exc),
                        )
                    )
            continue

        for n in candidate_sizes:
            print(f"  candidates={n}")
            for q in BENCH_QUERIES:
                docs = load_candidates(q, n)
                if len(docs) < n:
                    print(f"    [WARN] only {len(docs)} docs for {q[:30]!r}")

                try:
                    pre_fn = cfg.get("pre_fn")
                    med, lo, hi, gpu_delta, scores = timed_score(
                        score_fn, q, docs, pre_fn=pre_fn
                    )
                    results.append(
                        BenchResult(
                            backend=key,
                            model=cfg["model"],
                            n_candidates=n,
                            query=q[:40],
                            latency_ms=med,
                            latency_ms_min=lo,
                            latency_ms_max=hi,
                            gpu_mem_delta_mb=gpu_delta,
                            scores=scores,
                        )
                    )
                    print(
                        f"    q={q[:30]!r}  median={med:.0f}ms  gpu_delta={gpu_delta:.0f}MB"
                    )
                except Exception as exc:
                    print(f"    [SCORE FAIL] {exc}")
                    results.append(
                        BenchResult(
                            backend=key,
                            model=cfg["model"],
                            n_candidates=n,
                            query=q[:40],
                            latency_ms=0,
                            latency_ms_min=0,
                            latency_ms_max=0,
                            gpu_mem_delta_mb=0,
                            error=str(exc),
                        )
                    )

    return results


# ── report ─────────────────────────────────────────────────────────────────────


def _avg(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def build_report(results: List[BenchResult]) -> str:
    lines = ["# Rerank Latency Benchmark", ""]

    # Group by (backend, n_candidates) and average across queries
    from collections import defaultdict

    grouped: dict = defaultdict(list)
    for r in results:
        if r.error:
            continue
        grouped[(r.backend, r.n_candidates)].append(r)

    # Summary table
    lines.append("## Summary — median latency averaged across queries")
    lines.append("")
    lines.append(
        "| Backend | Model | N candidates | Avg latency (ms) | "
        "Min (ms) | Max (ms) | GPU delta (MB) |"
    )
    lines.append(
        "|---------|-------|:------------:|:----------------:|:--------:|:--------:|:--------------:|"
    )

    for (backend, n), rs in sorted(
        grouped.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        cfg = BACKENDS.get(backend, {})
        label = cfg.get("label", backend)
        model = rs[0].model
        avg_lat = _avg([r.latency_ms for r in rs])
        min_lat = min(r.latency_ms_min for r in rs)
        max_lat = max(r.latency_ms_max for r in rs)
        gpu = _avg([r.gpu_mem_delta_mb for r in rs])
        lines.append(
            f"| {label} | `{model}` | {n} | **{avg_lat:.0f}** | "
            f"{min_lat:.0f} | {max_lat:.0f} | {gpu:.0f} |"
        )

    lines.append("")

    # Per-query breakdown for each backend
    lines.append("## Per-query breakdown")
    lines.append("")
    for backend in dict.fromkeys(r.backend for r in results):
        cfg = BACKENDS.get(backend, {})
        lines.append(f"### {cfg.get('label', backend)}")
        lines.append("")
        lines.append(
            "| N | Query | Median (ms) | Min (ms) | Max (ms) | GPU delta (MB) | Error |"
        )
        lines.append(
            "|:-:|-------|:-----------:|:--------:|:--------:|:--------------:|-------|"
        )
        for r in results:
            if r.backend != backend:
                continue
            err = r.error or ""
            lines.append(
                f"| {r.n_candidates} | {r.query} | {r.latency_ms:.0f} | "
                f"{r.latency_ms_min:.0f} | {r.latency_ms_max:.0f} | "
                f"{r.gpu_mem_delta_mb:.0f} | {err} |"
            )
        lines.append("")

    # Speedup comparison vs embedding baseline
    baseline_key = "embedding"
    baseline_rows = {
        (r.n_candidates, r.query): r.latency_ms
        for r in results
        if r.backend == baseline_key and not r.error
    }
    if baseline_rows:
        lines.append("## Speedup vs embedding dot-product baseline")
        lines.append("")
        lines.append("| Backend | N | Avg speedup |")
        lines.append("|---------|:-:|:-----------:|")
        for backend in dict.fromkeys(r.backend for r in results):
            if backend == baseline_key:
                continue
            speedups = []
            for r in results:
                if r.backend != backend or r.error:
                    continue
                base = baseline_rows.get((r.n_candidates, r.query))
                if base and r.latency_ms > 0:
                    speedups.append(base / r.latency_ms)
            if speedups:
                cfg = BACKENDS.get(backend, {})
                avg_speedup = _avg(speedups)
                sign = "×faster" if avg_speedup >= 1 else "×slower"
                lines.append(
                    f"| {cfg.get('label', backend)} | all | {avg_speedup:.2f}{sign} |"
                )
        lines.append("")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────


def main() -> None:
    global REPS
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--backends",
        nargs="+",
        default=["embedding", "embedding_cached", "st_cross", "qwen_cross"],
        choices=list(BACKENDS),
        help=(
            "Which backends to benchmark "
            "(default: embedding embedding_cached st_cross qwen_cross)"
        ),
    )
    parser.add_argument(
        "--candidates",
        nargs="+",
        type=int,
        default=DEFAULT_CANDIDATE_SIZES,
        metavar="N",
        help="Candidate pool sizes to sweep (default: 10 25 50 100)",
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=REPS,
        help=f"Timed repetitions per combination (default: {REPS})",
    )
    parser.add_argument(
        "--llm-base",
        default="http://localhost:11434/v1",
        help="Base URL for Ollama/OpenAI-compat LLM (used with --include-llm)",
    )
    parser.add_argument(
        "--include-llm",
        action="store_true",
        help="Also benchmark the slow LLM listwise reranker (needs Ollama running)",
    )
    parser.add_argument(
        "--out",
        default="scripts/bench_rerank_results.md",
        help="Output Markdown path (default: scripts/bench_rerank_results.md)",
    )
    args = parser.parse_args()

    REPS = args.reps

    if not MANIFEST.exists():
        print(f"ERROR: manifest not found at {MANIFEST}")
        print("Run: python scripts/index_repo.py  (or start_local_yash.sh)")
        sys.exit(1)

    print("=" * 60)
    print("CodeNib rerank latency benchmark")
    print(f"Backends : {args.backends}")
    print(f"Candidates: {args.candidates}")
    print(f"Reps/combo: {REPS}")
    print(f"Queries  : {len(BENCH_QUERIES)}")
    print("=" * 60)

    results = run_benchmark(
        backend_keys=args.backends,
        candidate_sizes=args.candidates,
        llm_base=args.llm_base,
        include_llm=args.include_llm,
    )

    report = build_report(results)
    print("\n\n" + "=" * 60)
    print(report)

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"\nReport saved → {out_path}")


if __name__ == "__main__":
    main()
