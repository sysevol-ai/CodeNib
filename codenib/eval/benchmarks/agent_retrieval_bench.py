# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Static ARB retrieval and pinned upstream evaluator boundary (labs)."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from codenib.dataset.agent_retrieval_bench import sha256_file

ARB_UPSTREAM_REVISION = "07014c986f3deadb1548c62b32c0ffbe6a81465d"
METRICS = ("Recall@5", "Recall@10", "Recall@20", "MRR") + tuple(
    f"BCY@{budget}" for budget in (4000, 8000, 16000, 32000)
)


class BM25CorpusIndex:
    """One in-memory BM25 index over every chunk of a released corpus snapshot.

    Construction is the cold index build; ``rank`` is one query. Samples that
    share a snapshot reuse the index, so build and query costs stay separable.
    """

    def __init__(self, corpus: list[dict[str, Any]]):
        from codenib.code_chunking.base import CodeChunk
        from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
        from codenib.types import NODE_TYPE_FILE, NODE_TYPE_SYMBOL

        started = time.perf_counter()
        chunks = [
            CodeChunk(
                content=row["text"],
                start_line=row["start_line"] - 1,
                end_line=row["end_line"] - 1,
                chunk_type=(
                    NODE_TYPE_FILE if row["kind"] == "file" else NODE_TYPE_SYMBOL
                ),
                name=row.get("symbol") or row["path"],
                file=row["path"],
                # A colon keeps extensionless paths from being treated as directories.
                node_id=f"{row['path']}:{row.get('symbol') or ''}:{number}",
            )
            for number, row in enumerate(corpus)
        ]
        self._paths = {row["path"] for row in corpus}
        self._top_k = len(chunks)
        self._index = BM25CodeIndexer(chunks=chunks, max_k=self._top_k)
        self.build_seconds = time.perf_counter() - started
        retriever = self._index.retriever
        self.size = {
            **retriever.size(),
            "indexed_text_bytes": sum(
                len(document.page_content.encode("utf-8"))
                for document in retriever.documents
            ),
        }

    def rank(self, query_text: str) -> list[str]:
        """Full chunk ranking -> first-hit file ranking over the whole corpus.

        The only task input is query_text. In particular this cannot use gold,
        fix commits, hard negatives, or given-file annotations as filters.
        """
        nodes = self._index.search(
            query_text,
            top_k=self._top_k,
            filter_test=False,
            return_code_content=False,
        )
        ranked = list(dict.fromkeys(node.file for node in nodes))
        if set(ranked) != self._paths:
            raise ValueError("BM25 did not return the complete corpus file ranking")
        return ranked


def rank_bm25(query_text: str, corpus: list[dict[str, Any]]) -> list[str]:
    return BM25CorpusIndex(corpus).rank(query_text)


def sample_split(sample_id: str) -> str:
    """Deterministic half split, shared with ARB community results."""
    digest = hashlib.sha256(sample_id.encode("utf-8")).digest()
    return "dev" if digest[0] % 2 == 0 else "heldout"


def implementation_receipt() -> dict[str, Any]:
    import codenib.dataset.agent_retrieval_bench as dataset
    import codenib.index.sparse_idx.bm25_index as bm25

    files = [
        Path(__file__),
        Path(__file__).with_name("agent_retrieval_bench_runner.py"),
        Path(__file__).with_name("_arb_evaluate.py"),
        Path(dataset.__file__),
        Path(bm25.__file__),
    ]
    package_root = Path(__file__).resolve().parents[2]
    return {
        "files": {
            str(path.resolve().relative_to(package_root)): sha256_file(path)
            for path in files
        },
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name) for name in ("rank-bm25", "numpy")
        },
    }


def validate_upstream(checkout: Path) -> Path:
    checkout = checkout.resolve()

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(checkout), *args], text=True
        ).strip()

    if git("rev-parse", "HEAD") != ARB_UPSTREAM_REVISION:
        raise ValueError(f"ARB evaluator must be pinned to {ARB_UPSTREAM_REVISION}")
    if git("status", "--porcelain", "--untracked-files=all", "--", "src"):
        raise ValueError("ARB evaluator source must be clean")
    if any(
        not line.startswith("H ")
        for line in git("ls-files", "-v", "--", "src").splitlines()
    ):
        raise ValueError("ARB evaluator source has hidden Git index flags")
    if not (checkout / "src/agent_retrieval_bench/bcy_curve.py").is_file():
        raise ValueError("ARB evaluator package is missing")
    return checkout


def evaluate_official(
    jobs: list[dict[str, Any]], upstream: Path, python: str = sys.executable
) -> list[dict[str, Any]]:
    """Run official functions in an isolated interpreter, without installation."""
    upstream = validate_upstream(upstream)
    process = subprocess.run(
        [
            python,
            "-I",
            str(Path(__file__).with_name("_arb_evaluate.py")),
            str(upstream),
        ],
        input=json.dumps(jobs),
        text=True,
        capture_output=True,
        check=False,
        timeout=3600,
    )
    if process.returncode:
        raise RuntimeError(f"official ARB evaluator failed: {process.stderr[-4000:]}")
    rows = json.loads(process.stdout)
    if [row["sample_id"] for row in rows] != [job["sample"]["id"] for job in jobs]:
        raise ValueError("ARB evaluator returned a different case set")
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every selected case counts; failures contribute zero, not survivor bias."""

    def group(items: list[dict[str, Any]]) -> dict[str, Any]:
        valid = [row for row in items if row["status"] == "ok"]
        return {
            "selected": len(items),
            "valid": len(valid),
            "failed": len(items) - len(valid),
            "failure_ids": [r["sample_id"] for r in items if r["status"] != "ok"],
            "metrics_all_selected": {
                key: sum(row["metrics"][key] for row in valid) / len(items)
                for key in METRICS
            },
            "metrics_valid_only": (
                {
                    key: sum(row["metrics"][key] for row in valid) / len(valid)
                    for key in METRICS
                }
                if valid
                else None
            ),
        }

    return {
        "overall": group(rows),
        "by_task": {
            task: group([row for row in rows if row["task_type"] == task])
            for task in sorted({row["task_type"] for row in rows})
        },
        "by_split": {
            split: group([row for row in rows if row["split"] == split])
            for split in sorted({row["split"] for row in rows})
        },
    }


def _distribution(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "total": sum(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max": ordered[-1],
    }


def retrieval_usage(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Cold index builds, per-sample queries, and calls, never folded together.

    A resumed run keeps the timings recorded before the interruption, so a
    snapshot whose samples straddle it can count more than one build.
    """
    built = [row for row in predictions if row.get("index_built")]
    queried = [row for row in predictions if "query_seconds" in row]
    return {
        "index_builds": len(built),
        "corpus_load_seconds": _distribution(
            [row["corpus_load_seconds"] for row in built]
        ),
        "index_build_seconds": _distribution(
            [row["index_build_seconds"] for row in built]
        ),
        "index_size": {
            field: _distribution([row["index_size"][field] for row in built])
            for field in ("documents", "tokens", "vocabulary", "indexed_text_bytes")
        },
        "query_seconds": _distribution([row["query_seconds"] for row in queried]),
        "tool_calls": _distribution([row.get("tool_calls", 0) for row in predictions]),
        "model_calls": 0,
        "api_cost_usd": 0.0,
    }


def run_environment() -> dict[str, Any]:
    cpu = platform.processor() or platform.machine()
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            cpu = next(
                line.split(":", 1)[1].strip()
                for line in handle
                if line.startswith("model name")
            )
    except (OSError, StopIteration):
        pass
    environment: dict[str, Any] = {
        "platform": platform.platform(),
        "cpu": cpu,
        "cpu_count": os.cpu_count(),
        "index_location": "in memory; nothing persisted",
    }
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return environment
    # ru_maxrss is kilobytes on Linux and bytes on macOS. It covers only the
    # final process of a resumed run.
    scale = 1 if sys.platform == "darwin" else 1024
    environment["peak_rss_bytes"] = (
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
    )
    return environment


def json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
