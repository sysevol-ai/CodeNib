# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Static ARB retrieval and pinned upstream evaluator boundary (labs)."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from codenib.dataset.agent_retrieval_bench import sha256_file

ARB_UPSTREAM_REVISION = "07014c986f3deadb1548c62b32c0ffbe6a81465d"
METRICS = ("Recall@5", "Recall@10", "Recall@20", "MRR") + tuple(
    f"BCY@{budget}" for budget in (4000, 8000, 16000, 32000)
)


def rank_bm25(query_text: str, corpus: list[dict[str, Any]]) -> list[str]:
    """All released chunks -> full chunk ranking -> first-hit file ranking.

    The only task input is query_text. In particular this function cannot use
    gold, fix commits, hard negatives, or given-file annotations as filters.
    """
    from codenib.code_chunking.base import CodeChunk
    from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
    from codenib.types import NODE_TYPE_FILE, NODE_TYPE_SYMBOL

    chunks = [
        CodeChunk(
            content=row["text"],
            start_line=row["start_line"] - 1,
            end_line=row["end_line"] - 1,
            chunk_type=NODE_TYPE_FILE if row["kind"] == "file" else NODE_TYPE_SYMBOL,
            name=row.get("symbol") or row["path"],
            file=row["path"],
            # A colon keeps extensionless paths from being treated as directories.
            node_id=f"{row['path']}:{row.get('symbol') or ''}:{number}",
        )
        for number, row in enumerate(corpus)
    ]
    index = BM25CodeIndexer(chunks=chunks, max_k=len(chunks))
    nodes = index.search(
        query_text, top_k=len(chunks), filter_test=False, return_code_content=False
    )
    ranked = list(dict.fromkeys(node.file for node in nodes))
    if set(ranked) != {row["path"] for row in corpus}:
        raise ValueError("BM25 did not return the complete corpus file ranking")
    return ranked


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
    }


def json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
