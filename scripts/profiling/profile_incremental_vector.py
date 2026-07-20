#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Compare fresh and incremental vector-index maintenance on frozen commits.

The runner consumes the same versioned transition manifest as the incremental
graph experiment. For every target commit it executes two independent arms:

1. ``fully-rebuild`` chunks, embeds, and persists a fresh Flat FAISS artifact.
2. ``incremental`` reuses content-addressed vectors, embeds cache misses, and
   either mutates or rebuilds FAISS according to the configured delta threshold.

Latency is admitted only when document identities and reconstructed vectors
match the fresh artifact and deterministic top-k replay is exact.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from codeminer.compiler.index_builders import VectorIndexBuilder  # noqa: E402
from codeminer.eval.maintenance_manifest import load_manifest  # noqa: E402
from codeminer.index.embedding.vector_store import CodeVectorStore  # noqa: E402
from codeminer.languages import chunker_languages_for_graph_language  # noqa: E402
from codeminer.log_utils import logging_manager  # noqa: E402

PROTOCOL_VERSION = 4
DEFAULT_CACHE_ROOT = Path("/mnt/data/codeminer")
DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_DIMENSION = 1024
DEFAULT_TOP_K = 10
DEFAULT_REPLAY_QUERIES = 32
DEFAULT_EMBEDDING_KWARGS = {
    "default_batch_size": 32,
    "encode_kwargs": {"show_progress_bar": False},
    "max_seq_length": 2048,
}
DEFAULT_LOG_LEVEL = "WARNING"


def run_git(args: Sequence[str], cwd: Path, timeout: int = 120):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def checkout(repo: Path, commit: str) -> None:
    result = run_git(["checkout", "-f", commit], cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(
            f"checkout {commit[:12]} failed: {result.stderr.strip()[-400:]}"
        )


def directory_size(path: Path) -> int:
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


def _content_hash(document) -> str:
    value = document.metadata.get("content_hash")
    if value:
        return str(value)
    return hashlib.md5(
        document.page_content.encode("utf-8", errors="replace")
    ).hexdigest()


def document_identity(document) -> Tuple[Any, ...]:
    """Canonical identity shared by fresh and incremental artifacts."""
    metadata = document.metadata
    return (
        _content_hash(document),
        str(metadata.get("file") or ""),
        int(metadata.get("start_line") or 0),
        int(metadata.get("end_line") or 0),
        str(metadata.get("chunk_type") or ""),
        str(metadata.get("name") or ""),
        str(metadata.get("node_id") or ""),
    )


def result_identity(result) -> Tuple[Any, ...]:
    return (
        str(result.file or ""),
        int(result.start_line or 0),
        int(result.end_line or 0),
        str(result.type or ""),
        str(result.node_name or ""),
        str(result.node_id or ""),
    )


def _vectors_by_identity(store: CodeVectorStore, level: str):
    index, documents = store._get_index_and_docs(level)
    vectors: Dict[Tuple[Any, ...], List[np.ndarray]] = defaultdict(list)
    for row, document in enumerate(documents):
        vectors[document_identity(document)].append(
            np.asarray(index.reconstruct(row), dtype=np.float32)
        )
    return documents, vectors


def _compare_vectors(
    predicted: Mapping[Tuple[Any, ...], List[np.ndarray]],
    reference: Mapping[Tuple[Any, ...], List[np.ndarray]],
) -> Dict[str, Any]:
    predicted_counts = Counter({key: len(values) for key, values in predicted.items()})
    reference_counts = Counter({key: len(values) for key, values in reference.items()})
    identities_exact = predicted_counts == reference_counts
    max_abs_error = 0.0
    vectors_close = identities_exact
    if identities_exact:
        for identity in reference:
            predicted_vectors = predicted[identity]
            reference_vectors = reference[identity]
            for left, right in zip(predicted_vectors, reference_vectors, strict=True):
                if left.shape != right.shape:
                    vectors_close = False
                    continue
                if left.size:
                    max_abs_error = max(
                        max_abs_error,
                        float(np.max(np.abs(left - right))),
                    )
                if not np.allclose(left, right, rtol=1e-5, atol=1e-6):
                    vectors_close = False
    return {
        "predicted_documents": sum(predicted_counts.values()),
        "reference_documents": sum(reference_counts.values()),
        "document_identities_exact": identities_exact,
        "vectors_close": vectors_close,
        "max_vector_abs_error": max_abs_error,
    }


def _sample_queries(documents: Iterable[Any], limit: int) -> List[str]:
    candidates = []
    for document in documents:
        text = document.page_content.strip()
        if not text:
            continue
        identity = repr(document_identity(document)).encode("utf-8")
        candidates.append((hashlib.sha256(identity).hexdigest(), text[:1000]))
    candidates.sort()
    return [text for _digest, text in candidates[:limit]]


def compare_top_k_replay(
    predicted: CodeVectorStore,
    reference: CodeVectorStore,
    *,
    level: str,
    top_k: int,
    max_queries: int,
) -> Dict[str, Any]:
    _index, reference_documents = reference._get_index_and_docs(level)
    queries = _sample_queries(reference_documents, max_queries)
    exact = 0
    set_exact = 0
    overlaps = []
    max_score_error = 0.0
    for query in queries:
        predicted_results = predicted.search(query, top_k=top_k, level=level)
        reference_results = reference.search(query, top_k=top_k, level=level)
        predicted_ids = [result_identity(result) for result in predicted_results]
        reference_ids = [result_identity(result) for result in reference_results]
        if predicted_ids == reference_ids:
            exact += 1
        predicted_set = set(predicted_ids)
        reference_set = set(reference_ids)
        if predicted_set == reference_set:
            set_exact += 1
        denominator = max(len(reference_set), 1)
        overlaps.append(len(predicted_set & reference_set) / denominator)
        predicted_scores = {
            result_identity(result): float(result.score) for result in predicted_results
        }
        for result in reference_results:
            identity = result_identity(result)
            if identity in predicted_scores:
                max_score_error = max(
                    max_score_error,
                    abs(predicted_scores[identity] - float(result.score)),
                )
    count = len(queries)
    return {
        "query_count": count,
        "top_k": top_k,
        "ordered_exact_fraction": exact / count if count else None,
        "set_exact_fraction": set_exact / count if count else None,
        "mean_overlap": float(np.mean(overlaps)) if overlaps else None,
        "min_overlap": min(overlaps) if overlaps else None,
        "max_score_abs_error": max_score_error,
    }


def compare_artifacts(
    incremental: CodeVectorStore,
    fresh: CodeVectorStore,
    *,
    levels: Sequence[str],
    top_k: int,
    max_queries: int,
) -> Dict[str, Any]:
    level_results = {}
    for level in levels:
        fresh_documents, fresh_vectors = _vectors_by_identity(fresh, level)
        _incremental_documents, incremental_vectors = _vectors_by_identity(
            incremental, level
        )
        vector_result = _compare_vectors(incremental_vectors, fresh_vectors)
        replay = compare_top_k_replay(
            incremental,
            fresh,
            level=level,
            top_k=top_k,
            max_queries=max_queries,
        )
        level_results[level] = {
            **vector_result,
            "replay": replay,
            "exact": (
                vector_result["document_identities_exact"]
                and vector_result["vectors_close"]
                and replay["ordered_exact_fraction"] == 1.0
                and replay["set_exact_fraction"] == 1.0
            ),
            "query_source_documents": len(fresh_documents),
        }
    return {
        "levels": level_results,
        "equivalence_guardrail_pass": all(
            result["exact"] for result in level_results.values()
        ),
    }


def load_store(
    path: Path,
    *,
    embedding: Any,
    model: str,
    provider: str,
    dimension: int,
    metric: str,
    embedding_kwargs: Mapping[str, Any],
) -> CodeVectorStore:
    store = CodeVectorStore(
        embedding_model=model,
        embedding_provider=provider,
        dimension=dimension,
        index_metric=metric,
        store_path=str(path),
        embedding=embedding,
        **embedding_kwargs,
    )
    store.load(str(path))
    return store


def compare_persisted_artifacts(
    incremental_path: Path,
    fresh_path: Path,
    *,
    embedding: Any,
    model: str,
    provider: str,
    dimension: int,
    metric: str,
    embedding_kwargs: Mapping[str, Any],
    levels: Sequence[str],
    top_k: int,
    max_queries: int,
) -> Dict[str, Any]:
    """Load fidelity stores only for the duration of one transition."""

    fresh = load_store(
        fresh_path,
        embedding=embedding,
        model=model,
        provider=provider,
        dimension=dimension,
        metric=metric,
        embedding_kwargs=embedding_kwargs,
    )
    incremental = load_store(
        incremental_path,
        embedding=embedding,
        model=model,
        provider=provider,
        dimension=dimension,
        metric=metric,
        embedding_kwargs=embedding_kwargs,
    )
    try:
        return compare_artifacts(
            incremental,
            fresh,
            levels=levels,
            top_k=top_k,
            max_queries=max_queries,
        )
    finally:
        del incremental
        del fresh
        gc.collect()


def build_shared_embedding(
    *,
    model: str,
    provider: str,
    dimension: int,
    metric: str,
    embedding_kwargs: Mapping[str, Any],
) -> Tuple[Any, float]:
    started = time.monotonic()
    bootstrap = CodeVectorStore(
        embedding_model=model,
        embedding_provider=provider,
        dimension=dimension,
        index_metric=metric,
        **embedding_kwargs,
    )
    return bootstrap.embedding, time.monotonic() - started


def experiment_config_id(
    *,
    model: str,
    provider: str,
    dimension: int,
    embedding_kwargs: Mapping[str, Any],
    levels: Sequence[str],
    metric: str,
    delta_threshold: float,
    top_k: int,
    replay_queries: int,
    runtime_log_level: str = DEFAULT_LOG_LEVEL,
) -> str:
    """Fingerprint every setting that changes an artifact or fidelity gate."""

    payload = {
        "model": model,
        "provider": provider,
        "dimension": dimension,
        "embedding_kwargs": embedding_kwargs,
        "levels": list(levels),
        "metric": metric,
        "delta_threshold": delta_threshold,
        "top_k": top_k,
        "replay_queries": replay_queries,
        "runtime_log_level": runtime_log_level,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _completed_steps(path: Path, config_id: str) -> Dict[str, set[int]]:
    completed: Dict[str, set[int]] = defaultdict(set)
    if not path.exists():
        return completed
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _record_is_resumable(row, config_id):
            continue
        instance_id = row.get("instance_id")
        step = row.get("step")
        if instance_id and isinstance(step, int):
            completed[instance_id].add(step)
    return completed


def _record_is_resumable(row: Mapping[str, Any], config_id: str) -> bool:
    """Only exact, two-arm rows can reconstruct later incremental state."""

    return bool(
        row.get("protocol_version") == PROTOCOL_VERSION
        and row.get("experiment_config_id") == config_id
        and isinstance(row.get("step"), int)
        and (row.get("fully-rebuild") or {}).get("status") == "ok"
        and (row.get("incremental") or {}).get("status") == "ok"
        and (row.get("fidelity") or {}).get("equivalence_guardrail_pass") is True
    )


def _completed_instances(path: Path, config_id: str) -> set[str]:
    completed = _completed_steps(path, config_id)
    expected: Dict[str, int] = {}
    if not path.exists():
        return set()
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("protocol_version") != PROTOCOL_VERSION:
            continue
        if row.get("experiment_config_id") != config_id:
            continue
        instance_id = row.get("instance_id")
        expected_steps = row.get("instance_transition_count")
        if instance_id and isinstance(expected_steps, int):
            expected[instance_id] = expected_steps
    return {
        instance_id
        for instance_id, steps in completed.items()
        if len(steps) == expected.get(instance_id, -1)
    }


def maintenance_arm_order(instance_id: str, step: int) -> Tuple[str, str]:
    """Deterministically counterbalance model/cache warm-up across arms."""
    digest = hashlib.sha256(f"{instance_id}:{step}".encode("utf-8")).digest()
    if digest[0] & 1:
        return ("incremental", "fully-rebuild")
    return ("fully-rebuild", "incremental")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transition-manifest", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--embedding-model", default=DEFAULT_MODEL)
    parser.add_argument("--embedding-provider", default="huggingface")
    parser.add_argument("--embedding-dimension", type=int, default=DEFAULT_DIMENSION)
    parser.add_argument(
        "--embedding-kwargs",
        default=json.dumps(DEFAULT_EMBEDDING_KWARGS),
        help=(
            "JSON forwarded to the embedding provider; the Hugging Face default "
            "caps sequence length and batch size to keep memory bounded"
        ),
    )
    parser.add_argument("--levels", default="l2")
    parser.add_argument("--index-metric", choices=("ip", "l2"), default="l2")
    parser.add_argument("--delta-threshold", type=float, default=0.1)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--replay-queries", type=int, default=DEFAULT_REPLAY_QUERIES)
    parser.add_argument("--max-instances", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument(
        "--instance",
        action="append",
        default=[],
        help="run only this instance_id; repeat to select multiple instances",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retain-full-artifacts", action="store_true")
    parser.add_argument(
        "--log-level",
        choices=("WARNING", "ERROR", "CRITICAL"),
        default=DEFAULT_LOG_LEVEL,
        help="fixed benchmark logging level (default: WARNING)",
    )
    args = parser.parse_args()

    numeric_log_level = getattr(logging, args.log_level)
    logging.getLogger().setLevel(numeric_log_level)
    logging_manager.rich_handler.setLevel(numeric_log_level)
    for logger_name in ("sentence_transformers", "transformers"):
        logging.getLogger(logger_name).setLevel(numeric_log_level)

    levels = [level.strip().lower() for level in args.levels.split(",") if level]
    if not levels or any(level not in ("l0", "l2") for level in levels):
        parser.error("--levels must contain l0 and/or l2")
    if not 0.0 <= args.delta_threshold <= 1.0:
        parser.error("--delta-threshold must be between 0 and 1")
    embedding_kwargs = json.loads(args.embedding_kwargs)
    config_id = experiment_config_id(
        model=args.embedding_model,
        provider=args.embedding_provider,
        dimension=args.embedding_dimension,
        embedding_kwargs=embedding_kwargs,
        levels=levels,
        metric=args.index_metric,
        delta_threshold=args.delta_threshold,
        top_k=args.top_k,
        replay_queries=args.replay_queries,
        runtime_log_level=args.log_level,
    )
    instances, chains, transition_manifest_id = load_manifest(args.transition_manifest)
    if args.instance:
        selected = set(args.instance)
        instances = [entry for entry in instances if entry[0] in selected]
        missing = selected - {entry[0] for entry in instances}
        if missing:
            parser.error(f"instances not present in manifest: {sorted(missing)}")
    if args.max_instances is not None:
        instances = instances[: args.max_instances]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.work_root.mkdir(parents=True, exist_ok=True)
    completed_instances = (
        _completed_instances(args.output, config_id) if args.resume else set()
    )
    completed_steps = _completed_steps(args.output, config_id) if args.resume else {}
    mode = "a" if args.resume else "w"

    print(f"loading shared embedding model {args.embedding_model}", flush=True)
    embedding, model_load_s = build_shared_embedding(
        model=args.embedding_model,
        provider=args.embedding_provider,
        dimension=args.embedding_dimension,
        metric=args.index_metric,
        embedding_kwargs=embedding_kwargs,
    )
    print(f"model ready in {model_load_s:.2f}s", flush=True)

    with args.output.open(mode) as output:
        for instance_id, language, base_commit in instances:
            instance_chain = chains[instance_id]
            if args.max_steps is not None:
                instance_chain = instance_chain[: args.max_steps]
            if instance_id in completed_instances:
                print(f"[{instance_id}] resume-skip", flush=True)
                continue
            repo = args.cache_root / instance_id / "repo"
            if not repo.is_dir():
                raise FileNotFoundError(repo)
            instance_work = args.work_root / instance_id
            incremental_dir = instance_work / "incremental"
            if incremental_dir.exists():
                shutil.rmtree(incremental_dir)
            instance_work.mkdir(parents=True, exist_ok=True)

            chunker_languages = list(chunker_languages_for_graph_language(language))
            if not chunker_languages:
                raise ValueError(
                    f"no chunker languages registered for graph language {language!r}"
                )

            builder = VectorIndexBuilder(
                languages=chunker_languages,
                embedding_model=args.embedding_model,
                embedding_provider=args.embedding_provider,
                embedding_dimension=args.embedding_dimension,
                embedding_kwargs=embedding_kwargs,
                build_levels=levels,
                index_metric=args.index_metric,
                delta_threshold=args.delta_threshold,
                embedding=embedding,
            )

            checkout(repo, base_commit)
            base_started = time.monotonic()
            base_status = builder.build(
                "repository", repo_path=str(repo), output_dir=str(incremental_dir)
            )
            base_build_s = time.monotonic() - base_started
            print(
                f"[{instance_id}] base built in {base_build_s:.2f}s; "
                f"{len(instance_chain)} transitions",
                flush=True,
            )

            for transition in instance_chain:
                step = transition["step"]
                already_recorded = step in completed_steps.get(instance_id, set())
                full_dir = instance_work / f"full-step{step}"
                if full_dir.exists():
                    shutil.rmtree(full_dir)
                checkout(repo, transition["to_commit"])
                row: Dict[str, Any] = {
                    **transition,
                    "protocol_version": PROTOCOL_VERSION,
                    "experiment_config_id": config_id,
                    "transition_manifest_id": transition_manifest_id,
                    "instance_transition_count": len(instance_chain),
                    "embedding_model": args.embedding_model,
                    "embedding_provider": args.embedding_provider,
                    "embedding_dimension": args.embedding_dimension,
                    "embedding_kwargs": embedding_kwargs,
                    "chunker_languages": chunker_languages,
                    "levels": levels,
                    "index_type": "flat",
                    "index_metric": args.index_metric,
                    "delta_threshold": args.delta_threshold,
                    "runtime_log_level": args.log_level,
                    "arm_order": maintenance_arm_order(instance_id, step),
                    "model_load_s": model_load_s,
                    "base_build_s": base_build_s,
                    "base_build_metadata": base_status.metadata,
                    "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                try:
                    for arm in row["arm_order"]:
                        print(
                            f"[{instance_id}] step{step} {arm} "
                            f"{transition['to_commit'][:8]}",
                            flush=True,
                        )
                        started = time.monotonic()
                        if arm == "fully-rebuild":
                            status = builder.build(
                                "repository",
                                repo_path=str(repo),
                                output_dir=str(full_dir),
                            )
                            artifact_dir = full_dir
                        else:
                            status = builder.incremental_update(
                                "repository",
                                repo_path=str(repo),
                                output_dir=str(incremental_dir),
                                last_commit=transition["from_commit"],
                            )
                            artifact_dir = incremental_dir
                        row[arm] = {
                            "status": "ok",
                            "t_s": time.monotonic() - started,
                            "artifact_bytes": directory_size(artifact_dir),
                            "metadata": status.metadata,
                        }

                    full_s = row["fully-rebuild"]["t_s"]
                    incremental_s = row["incremental"]["t_s"]

                    fidelity = compare_persisted_artifacts(
                        incremental_dir,
                        full_dir,
                        embedding=embedding,
                        model=args.embedding_model,
                        provider=args.embedding_provider,
                        dimension=args.embedding_dimension,
                        metric=args.index_metric,
                        embedding_kwargs=embedding_kwargs,
                        levels=levels,
                        top_k=args.top_k,
                        max_queries=args.replay_queries,
                    )
                    row["fidelity"] = fidelity
                    source_changing = transition.get("delta_files_test_excluded", 0) > 0
                    row["analysis_admitted"] = (
                        source_changing and fidelity["equivalence_guardrail_pass"]
                    )
                    row["equivalence_guarded_speedup_vs_fully_rebuild"] = (
                        full_s / incremental_s
                        if row["analysis_admitted"] and incremental_s > 0
                        else None
                    )
                except Exception as exc:
                    row.setdefault("error", f"{type(exc).__name__}: {exc}")
                    row.setdefault("analysis_admitted", False)
                    row.setdefault("equivalence_guarded_speedup_vs_fully_rebuild", None)
                row["finished"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                if not already_recorded:
                    output.write(json.dumps(row) + "\n")
                    output.flush()
                if not args.retain_full_artifacts and full_dir.exists():
                    shutil.rmtree(full_dir)
                disposition = "state-replay" if already_recorded else "recorded"
                print(
                    f"[{instance_id}] step{step} {disposition} "
                    f"admitted={row['analysis_admitted']} "
                    f"speedup={row['equivalence_guarded_speedup_vs_fully_rebuild']}",
                    flush=True,
                )
                if "error" in row:
                    break

    print(f"done. wrote {args.output}", flush=True)


if __name__ == "__main__":
    main()
