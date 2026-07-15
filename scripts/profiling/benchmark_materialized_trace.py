#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Measure materialization, hydration, and mixed-operation serving separately."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import resource
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.compiler.index_builders import (  # noqa: E402
    IndexBuilderRegistry,
    register_default_builders,
)
from codeminer.compiler.index_compiler import IndexCompiler  # noqa: E402
from codeminer.compiler.index_compiler import IndexCompilerConfig
from codeminer.compiler.manifest import MANIFEST_FILENAME, RepoManifest  # noqa: E402
from codeminer.eval.materialization_trace import TraceRequest  # noqa: E402
from codeminer.eval.materialization_trace import (
    attach_materialization_cost,
    load_trace,
    replay_materialized_trace,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--repo-path", type=Path)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--materialization-checkpoint", type=Path)
    parser.add_argument(
        "--materialize-only",
        action="store_true",
        help="Build artifacts and write a checkpoint, then exit before hydration.",
    )
    parser.add_argument("--languages", default="python")
    parser.add_argument("--graph-route", default="active")
    parser.add_argument("--embedding-model", default="Qwen/Qwen3-Embedding-0.6B")
    parser.add_argument("--embedding-revision")
    parser.add_argument("--embedding-dimension", type=int, default=1024)
    parser.add_argument("--embedding-batch-size", type=int, default=8)
    parser.add_argument("--embedding-max-seq-length", type=int, default=8192)
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--warmup-repetitions", type=int, default=1)
    parser.add_argument("--measured-repetitions", type=int, default=5)
    parser.add_argument("--reuse-dense-query-embedding", action="store_true")
    args = parser.parse_args(argv)
    if args.materialize_only and args.repo_path is None:
        parser.error("--materialize-only requires --repo-path")
    if args.materialize_only and args.materialization_checkpoint is not None:
        parser.error("--materialize-only cannot use --materialization-checkpoint")

    requests = load_trace(args.trace)
    materialization_seconds = None
    materialization = None
    materialization_resources = None
    if args.repo_path:
        if args.cache_dir is None:
            parser.error("--cache-dir is required with --repo-path")
        manifest_path, materialization_seconds = _materialize(
            repo_path=args.repo_path,
            cache_dir=args.cache_dir,
            languages=_languages(args.languages),
            requests=requests,
            graph_route=args.graph_route,
            embedding_model=args.embedding_model,
            embedding_revision=args.embedding_revision,
            embedding_dimension=args.embedding_dimension,
            embedding_batch_size=args.embedding_batch_size,
            embedding_max_seq_length=args.embedding_max_seq_length,
            trust_remote_code=args.trust_remote_code,
        )
        manifest = RepoManifest.load(manifest_path)
        materialization = _materialization_record(
            manifest, materialization_seconds=materialization_seconds
        )
        materialization_resources = _resource_usage(
            scope="cold materialization checkpoint"
        )
        materialization["resource_peaks"] = materialization_resources
        _write_json(
            args.output,
            {
                "schema_version": 1,
                "status": "materialized",
                "manifest": str(manifest_path),
                "materialization": materialization,
            },
        )
        if args.materialize_only:
            print(
                json.dumps(
                    {
                        "status": "materialized",
                        "manifest": str(manifest_path),
                        "materialization": materialization,
                    },
                    indent=2,
                )
            )
            return 0
    else:
        manifest_path = args.manifest
        if args.materialization_checkpoint:
            checkpoint = json.loads(
                args.materialization_checkpoint.read_text(encoding="utf-8")
            )
            materialization = checkpoint.get("materialization")
            if not isinstance(materialization, dict):
                raise ValueError("checkpoint has no materialization record")
            materialization_seconds = float(materialization["wall_seconds"])
            materialization_resources = materialization.get("resource_peaks")
            if not isinstance(materialization_resources, dict):
                raise ValueError("checkpoint has no materialization resource peaks")

    report = asyncio.run(
        replay_materialized_trace(
            manifest_path,
            requests,
            warmup_repetitions=args.warmup_repetitions,
            measured_repetitions=args.measured_repetitions,
            reuse_dense_query_embedding=args.reuse_dense_query_embedding,
        )
    )
    report["trace"] = {
        "path": str(args.trace.resolve()),
        "sha256": _file_sha256(args.trace),
    }
    report["protocol"]["hydration_process_boundary"] = (
        "fresh process after materialization checkpoint"
        if args.manifest is not None
        else "same process as materialization"
    )
    if materialization_seconds is not None:
        report = attach_materialization_cost(
            report,
            materialization_seconds=materialization_seconds,
            per_index_seconds=materialization["per_index_seconds"],
            artifact_bytes=materialization["artifact_bytes"],
        )
    final_resources = _resource_usage(scope="current process through trace replay")
    report["resources"] = (
        _combine_resource_usage(materialization_resources, final_resources)
        if materialization_resources is not None
        else final_resources
    )

    _write_json(args.output, report)
    print(json.dumps(_headline(report), indent=2))
    return 0


def _materialize(
    *,
    repo_path: Path,
    cache_dir: Path,
    languages: list[str],
    requests: Sequence[TraceRequest],
    graph_route: str,
    embedding_model: str,
    embedding_revision: str | None,
    embedding_dimension: int,
    embedding_batch_size: int,
    embedding_max_seq_length: int,
    trust_remote_code: bool,
) -> tuple[Path, float]:
    repo_path = repo_path.resolve()
    cache_dir = cache_dir.resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repository does not exist: {repo_path}")
    if cache_dir.exists() and any(cache_dir.iterdir()):
        raise ValueError(f"cold materialization requires an empty cache: {cache_dir}")

    index_types = _required_index_types(requests)
    registry = IndexBuilderRegistry()
    register_default_builders(
        registry,
        languages=languages,
        graph_route=graph_route,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
        embedding_dimension=embedding_dimension,
        trust_remote_code=trust_remote_code,
        embedding_batch_size=embedding_batch_size,
        embedding_max_seq_length=embedding_max_seq_length,
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(index_types=index_types, languages=languages),
    )
    start = time.perf_counter()
    manifest = compiler.compile_repo(
        str(repo_path),
        index_types=index_types,
        cache_dir=str(cache_dir),
    )
    elapsed = time.perf_counter() - start
    failed = sorted(
        name
        for name in index_types
        if manifest.indexes.get(name) is None
        or manifest.indexes[name].status != "fresh"
    )
    if failed:
        details = {
            name: (
                manifest.indexes[name].metadata.get("error")
                if name in manifest.indexes
                else "missing manifest entry"
            )
            for name in failed
        }
        raise RuntimeError(f"materialization failed: {details}")
    return cache_dir / MANIFEST_FILENAME, elapsed


def _required_index_types(requests: Sequence[TraceRequest]) -> list[str]:
    operation_index = {
        "dense": "vector",
        "bm25": "bm25",
        "regex": "symbol_graph",
        "definition": "symbol_graph",
        "references": "symbol_graph",
        "route": "symbol_graph",
    }
    return sorted({operation_index[request.operation] for request in requests})


def _languages(value: str) -> list[str]:
    languages = []
    for language in value.replace("/", ",").split(","):
        normalized = language.strip().lower()
        if normalized and normalized not in languages:
            languages.append(normalized)
    if not languages:
        raise ValueError("at least one language is required")
    return languages


def _artifact_bytes(manifest: RepoManifest) -> int:
    total = 0
    seen = set()
    for entry in manifest.indexes.values():
        root = Path(entry.path)
        paths = root.rglob("*") if root.is_dir() else [root]
        for path in paths:
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            total += path.stat().st_size
    return total


def _materialization_record(
    manifest: RepoManifest, *, materialization_seconds: float
) -> dict:
    return {
        "wall_seconds": materialization_seconds,
        "per_index_seconds": {
            name: float(entry.metadata.get("build_duration_seconds", 0.0))
            for name, entry in sorted(manifest.indexes.items())
        },
        "artifact_bytes": _artifact_bytes(manifest),
    }


def _resource_usage(*, scope: str) -> dict:
    """Return process-wide resource peaks at one explicit checkpoint."""

    # Linux reports ru_maxrss in KiB; this experiment is explicitly Linux-only.
    process = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    usage = {
        "scope": scope,
        "process_max_rss_bytes": int(process.ru_maxrss) * 1024,
        "max_child_rss_bytes": int(children.ru_maxrss) * 1024,
        "child_rss_semantics": "maximum resident set of any terminated child",
    }
    try:
        import torch

        if torch.cuda.is_available():
            usage["cuda_device"] = str(torch.cuda.get_device_name())
            usage["cuda_peak_allocated_bytes"] = int(torch.cuda.max_memory_allocated())
            usage["cuda_peak_reserved_bytes"] = int(torch.cuda.max_memory_reserved())
    except (ImportError, RuntimeError):
        pass
    return usage


def _combine_resource_usage(materialization: dict, final: dict) -> dict:
    """Combine peaks when checkpoint resume spans one or two processes."""

    peak_keys = (
        "process_max_rss_bytes",
        "max_child_rss_bytes",
        "cuda_peak_allocated_bytes",
        "cuda_peak_reserved_bytes",
    )
    combined = {
        "scope": "cold materialization + manifest hydration + trace replay",
        "combination": "maximum of materialization checkpoint and final process",
        "materialization_checkpoint": dict(materialization),
        "final_process": dict(final),
        "child_rss_semantics": "maximum resident set of any terminated child",
    }
    for key in peak_keys:
        values = [
            int(payload[key])
            for payload in (materialization, final)
            if payload.get(key) is not None
        ]
        if values:
            combined[key] = max(values)
    devices = {
        str(payload["cuda_device"])
        for payload in (materialization, final)
        if payload.get("cuda_device")
    }
    if len(devices) > 1:
        raise ValueError(f"resource checkpoints used different CUDA devices: {devices}")
    if devices:
        combined["cuda_device"] = devices.pop()
    return combined


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _headline(report: dict) -> dict:
    headline = {
        "load_seconds": report["phases"]["load_seconds"],
        "measured_request_seconds": report["phases"]["measured_request_seconds"],
        "operations": report["operations"],
    }
    if "materialization" in report:
        headline["materialization"] = report["materialization"]
    return headline


if __name__ == "__main__":
    raise SystemExit(main())
