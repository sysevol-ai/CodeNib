# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run the controlled embedding x GraphRAG x cross-encoder matrix."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class EmbeddingCell:
    slug: str
    model: str
    dimension: int


EMBEDDINGS = (
    EmbeddingCell("swerank-small", "Salesforce/SweRankEmbed-Small", 768),
    EmbeddingCell("qwen3-0p6b", "Qwen/Qwen3-Embedding-0.6B", 1024),
    EmbeddingCell("jina-code-1p5b", "jinaai/jina-code-embeddings-1.5b", 1536),
    EmbeddingCell("qwen3-4b", "Qwen/Qwen3-Embedding-4B", 2560),
    EmbeddingCell("swerank-large", "fishmingyu/SweRankEmbed-Large", 3584),
)
DEFAULT_CROSS_ENCODER = "Qwen/Qwen3-Reranker-4B"


def _safe_model_name(model: str) -> str:
    return model.rsplit("/", 1)[-1].lower().replace(".", "p")


def _output_path(
    output_dir: Path,
    cell: EmbeddingCell,
    cross_encoder_model: str,
    candidate_k: int,
    partition: str,
) -> Path:
    reranker = _safe_model_name(cross_encoder_model)
    return output_dir / (
        f"dense_graphrag_{cell.slug}_{reranker}_k{candidate_k}_{partition}.json"
    )


def _is_complete(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    results = payload.get("results") or []
    instance_ids = payload.get("instance_ids") or []
    summary = payload.get("summary") or {}
    return bool(instance_ids) and (
        len(results) == len(instance_ids)
        and summary.get("scored") == len(instance_ids)
        and summary.get("errors") == 0
    )


def _command(
    *,
    cell: EmbeddingCell,
    output: Path,
    partition: str,
    prebuilt_dir: Path,
    cross_encoder_model: str,
    cross_encoder_candidate_k: int,
    cross_encoder_batch_size: int,
    limit: Optional[int],
) -> List[str]:
    command = [
        sys.executable,
        "-m",
        "scripts.retrieval_ablation.dense_graphrag_benchmark",
        "--out",
        str(output),
        "--partition",
        partition,
        "--prebuilt-dir",
        str(prebuilt_dir),
        "--embedding-model",
        cell.model,
        "--embedding-dim",
        str(cell.dimension),
        "--dense-pool-k",
        "300",
        "--graph-seed-k",
        "10",
        "--neighbors-per-seed",
        "10",
        "--direction",
        "both",
        "--rerank-mode",
        "index",
        "--graph-weight",
        "0.5",
        "--rrf-k",
        "60",
        "--cross-encoder-model",
        cross_encoder_model,
        "--cross-encoder-candidate-k",
        str(cross_encoder_candidate_k),
        "--cross-encoder-batch-size",
        str(cross_encoder_batch_size),
        "--resume",
    ]
    if "Qwen3-Reranker" in cross_encoder_model:
        command.extend(("--cross-encoder-backend", "qwen"))
    if limit is not None:
        command.extend(("--limit", str(limit)))
    return command


def _write_manifest(path: Path, payload: Dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def _run_and_tee(command: Sequence[str], log_path: Path, env: Dict[str, str]) -> int:
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        return process.wait()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/mnt/data/codeminer/results/dense_graphrag_matrix_v1"),
    )
    parser.add_argument(
        "--embedding",
        action="append",
        choices=[cell.slug for cell in EMBEDDINGS],
        help="Run only selected embedding slugs; repeat for multiple cells.",
    )
    parser.add_argument("--partition", choices=("dev", "test", "all"), default="all")
    parser.add_argument(
        "--prebuilt-dir", type=Path, default=Path("/mnt/data/codeminer")
    )
    parser.add_argument("--cross-encoder-model", default=DEFAULT_CROSS_ENCODER)
    parser.add_argument("--cross-encoder-candidate-k", type=int, default=50)
    parser.add_argument("--cross-encoder-batch-size", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--cuda-visible-devices")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.cross_encoder_candidate_k < 10:
        parser.error("cross-encoder-candidate-k must be at least 10")
    if args.cross_encoder_batch_size <= 0:
        parser.error("cross-encoder-batch-size must be positive")

    selected_slugs = set(args.embedding or [])
    cells = [
        cell for cell in EMBEDDINGS if not selected_slugs or cell.slug in selected_slugs
    ]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "matrix_manifest.json"
    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "partition": args.partition,
        "cross_encoder_model": args.cross_encoder_model,
        "cross_encoder_candidate_k": args.cross_encoder_candidate_k,
        "cross_encoder_batch_size": args.cross_encoder_batch_size,
        "limit": args.limit,
        "cells": {},
    }
    env = dict(os.environ)
    if args.cuda_visible_devices is not None:
        env["CUDA_VISIBLE_DEVICES"] = args.cuda_visible_devices

    for index, cell in enumerate(cells, 1):
        output = _output_path(
            args.output_dir,
            cell,
            args.cross_encoder_model,
            args.cross_encoder_candidate_k,
            args.partition,
        )
        log_path = output.with_suffix(".log")
        command = _command(
            cell=cell,
            output=output,
            partition=args.partition,
            prebuilt_dir=args.prebuilt_dir,
            cross_encoder_model=args.cross_encoder_model,
            cross_encoder_candidate_k=args.cross_encoder_candidate_k,
            cross_encoder_batch_size=args.cross_encoder_batch_size,
            limit=args.limit,
        )
        status = "complete" if _is_complete(output) else "pending"
        manifest["cells"][cell.slug] = {
            "model": cell.model,
            "dimension": cell.dimension,
            "output": str(output),
            "log": str(log_path),
            "status": status,
        }
        _write_manifest(manifest_path, manifest)
        if status == "complete":
            print(f"[{index}/{len(cells)}] {cell.slug}: already complete", flush=True)
            continue
        print(f"[{index}/{len(cells)}] {cell.slug}: {' '.join(command)}", flush=True)
        if args.dry_run:
            continue
        manifest["cells"][cell.slug]["status"] = "running"
        _write_manifest(manifest_path, manifest)
        return_code = _run_and_tee(command, log_path, env)
        complete = return_code == 0 and _is_complete(output)
        manifest["cells"][cell.slug]["status"] = "complete" if complete else "failed"
        manifest["cells"][cell.slug]["return_code"] = return_code
        _write_manifest(manifest_path, manifest)
        if not complete:
            return return_code or 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
