# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark FAISS index families on the 100-instance CodeMiner base corpus.

The benchmark controls the embedding signal: every index searches the same
prebuilt Qwen3-Embedding-0.6B L2 vectors with the same precomputed query vector.
It measures warm, batch-size-one FAISS search latency separately from query
encoding and evaluates both task file recall and overlap with exact Flat top-k.

Example:
    python -m scripts.retrieval_ablation.index_family_benchmark \
        --output /mnt/data/codeminer/results/faiss_index_family_100.json
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import pickle
import platform
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import faiss
import numpy as np

DEFAULT_PREBUILT_DIR = Path("/mnt/data/codeminer")
DEFAULT_STATS_FILE = DEFAULT_PREBUILT_DIR / "dataset_stats.json"
DEFAULT_OUTPUT = DEFAULT_PREBUILT_DIR / "results" / "faiss_index_family_qwen06_100.json"
DEFAULT_QUERY_CACHE = (
    DEFAULT_PREBUILT_DIR / "results" / "faiss_index_family_qwen06_queries.npz"
)
DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_DIMENSION = 1024
DEFAULT_INDEX_SUFFIX = "Qwen__Qwen3-Embedding-0.6B"
DEFAULT_IVF_PROBE_FRACTIONS = (0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 1.0)
DEFAULT_HNSW_EF_SEARCH = (16, 32, 64, 128)


def _normalize_path(value: str | None) -> str:
    return (value or "").strip().strip("`'\"").replace("\\", "/").lstrip("./")


def _covers(candidate: str, target: str) -> bool:
    candidate = _normalize_path(candidate)
    target = _normalize_path(target)
    return bool(candidate and target) and (
        candidate == target
        or candidate.endswith("/" + target)
        or target.endswith("/" + candidate)
    )


def _ranked_unique_files(documents: Sequence[Any], indices: Iterable[int]) -> list[str]:
    ranked: list[str] = []
    for raw_index in indices:
        index = int(raw_index)
        if index < 0 or index >= len(documents):
            continue
        path = _normalize_path(documents[index].metadata.get("file"))
        if path and not any(_covers(existing, path) for existing in ranked):
            ranked.append(path)
    return ranked


def _task_metrics(
    ranked_files: Sequence[str], targets: Sequence[str], file_k: int
) -> dict[str, float | int]:
    normalized_targets = [_normalize_path(target) for target in targets if target]
    predictions = list(ranked_files[:file_k])
    hits = sum(
        any(_covers(candidate, target) for candidate in predictions)
        for target in normalized_targets
    )
    return {
        "hits": int(hits),
        "targets": len(normalized_targets),
        "recall": hits / len(normalized_targets) if normalized_targets else 0.0,
        "all_targets_hit": int(
            bool(normalized_targets) and hits == len(normalized_targets)
        ),
    }


def _ann_recall(
    indices: Sequence[int], flat_indices: Sequence[int], neighbor_k: int
) -> float:
    expected = {int(index) for index in flat_indices[:neighbor_k] if int(index) >= 0}
    actual = {int(index) for index in indices[:neighbor_k] if int(index) >= 0}
    return len(actual & expected) / len(expected) if expected else 0.0


def adaptive_nlist(
    vector_count: int,
    *,
    centroid_scale: float = 4.0,
    minimum_training_vectors: int = 39,
) -> int:
    """Choose an IVF cell count without undertraining per-repo centroids."""

    if vector_count <= 0:
        raise ValueError("vector_count must be positive")
    scale_target = max(1, int(round(centroid_scale * math.sqrt(vector_count))))
    training_limit = max(1, vector_count // minimum_training_vectors)
    return min(scale_target, training_limit)


def _probe_count(nlist: int, fraction: float) -> int:
    if not 0 < fraction <= 1:
        raise ValueError("IVF probe fractions must be in (0, 1]")
    return max(1, min(nlist, int(math.ceil(nlist * fraction))))


def _fraction_label(fraction: float) -> str:
    return f"ivf-{int(round(100 * fraction)):03d}pct"


def _distribution(values_ms: Sequence[float]) -> dict[str, float | int]:
    values = np.asarray(values_ms, dtype=np.float64)
    if values.size == 0:
        return {"count": 0, "mean_ms": 0.0, "median_ms": 0.0, "p95_ms": 0.0}
    return {
        "count": int(values.size),
        "mean_ms": float(values.mean()),
        "median_ms": float(np.median(values)),
        "p95_ms": float(np.percentile(values, 95)),
        "min_ms": float(values.min()),
        "max_ms": float(values.max()),
    }


def _measure_search(
    index: faiss.Index,
    query_vector: np.ndarray,
    search_k: int,
    *,
    warmups: int,
    repetitions: int,
) -> tuple[dict[str, float | int], list[int]]:
    for _ in range(warmups):
        index.search(query_vector, search_k)

    durations_ms = []
    last_indices: np.ndarray | None = None
    for _ in range(repetitions):
        start = time.perf_counter_ns()
        _, last_indices = index.search(query_vector, search_k)
        durations_ms.append((time.perf_counter_ns() - start) / 1_000_000)
    assert last_indices is not None
    return _distribution(durations_ms), [int(value) for value in last_indices[0]]


def _measure_flat_load(
    index_path: Path,
    *,
    warmups: int,
    repetitions: int,
) -> tuple[dict[str, float | int], faiss.Index]:
    loaded: faiss.Index | None = None
    durations_ms = []
    for repetition in range(warmups + repetitions):
        start = time.perf_counter_ns()
        candidate = faiss.read_index(str(index_path))
        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
        if repetition >= warmups:
            durations_ms.append(elapsed_ms)
        loaded = candidate
    assert loaded is not None
    return _distribution(durations_ms), loaded


def _timed_build(builder) -> tuple[faiss.Index, float]:
    start = time.perf_counter_ns()
    index = builder()
    elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
    return index, elapsed_ms


def _build_flat(vectors: np.ndarray) -> faiss.Index:
    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)
    return index


def _build_ivf(vectors: np.ndarray, nlist: int) -> faiss.IndexIVFFlat:
    dimension = vectors.shape[1]
    index = faiss.IndexIVFFlat(
        faiss.IndexFlatIP(dimension), dimension, nlist, faiss.METRIC_INNER_PRODUCT
    )
    index.train(vectors)
    index.add(vectors)
    return index


def _build_hnsw(
    vectors: np.ndarray, *, m: int, ef_construction: int
) -> faiss.IndexHNSWFlat:
    index = faiss.IndexHNSWFlat(vectors.shape[1], m, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = ef_construction
    index.add(vectors)
    return index


def _serialized_size(index: faiss.Index) -> int:
    return int(faiss.serialize_index(index).size)


def _method_result(
    *,
    family: str,
    parameters: Mapping[str, Any],
    build_ms: float,
    index_bytes: int,
    latency: Mapping[str, float | int],
    indices: Sequence[int],
    flat_indices: Sequence[int],
    documents: Sequence[Any],
    targets: Sequence[str],
    file_k: int,
    neighbor_k: int,
) -> dict[str, Any]:
    ranked_files = _ranked_unique_files(documents, indices)
    return {
        "family": family,
        "parameters": dict(parameters),
        "build_ms": float(build_ms),
        "index_bytes": int(index_bytes),
        "search": dict(latency),
        "ann_recall_at_k": _ann_recall(indices, flat_indices, neighbor_k),
        "task": _task_metrics(ranked_files, targets, file_k),
        "unique_files_returned": len(ranked_files),
    }


def benchmark_vectors(
    *,
    vectors: np.ndarray,
    documents: Sequence[Any],
    query_vector: np.ndarray,
    targets: Sequence[str],
    search_k: int,
    file_k: int,
    neighbor_k: int,
    warmups: int,
    repetitions: int,
    ivf_probe_fractions: Sequence[float],
    hnsw_m: int,
    hnsw_ef_construction: int,
    hnsw_ef_search: Sequence[int],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build and benchmark all index families over one repository snapshot."""

    vectors = np.ascontiguousarray(vectors, dtype=np.float32)
    query_vector = np.ascontiguousarray(query_vector, dtype=np.float32).reshape(1, -1)
    if len(documents) != len(vectors):
        raise ValueError(
            f"document/vector mismatch: {len(documents)} != {len(vectors)}"
        )
    if query_vector.shape[1] != vectors.shape[1]:
        raise ValueError(
            f"query/vector dimension mismatch: {query_vector.shape[1]} != "
            f"{vectors.shape[1]}"
        )

    flat, flat_build_ms = _timed_build(lambda: _build_flat(vectors))
    flat_size = _serialized_size(flat)
    flat_latency, flat_indices = _measure_search(
        flat, query_vector, search_k, warmups=warmups, repetitions=repetitions
    )
    methods = {
        "flat": _method_result(
            family="Flat",
            parameters={"exact": True},
            build_ms=flat_build_ms,
            index_bytes=flat_size,
            latency=flat_latency,
            indices=flat_indices,
            flat_indices=flat_indices,
            documents=documents,
            targets=targets,
            file_k=file_k,
            neighbor_k=neighbor_k,
        )
    }

    nlist = adaptive_nlist(len(vectors))
    ivf, ivf_build_ms = _timed_build(lambda: _build_ivf(vectors, nlist))
    ivf_size = _serialized_size(ivf)
    for fraction in ivf_probe_fractions:
        nprobe = _probe_count(nlist, fraction)
        ivf.nprobe = nprobe
        latency, indices = _measure_search(
            ivf, query_vector, search_k, warmups=warmups, repetitions=repetitions
        )
        methods[_fraction_label(fraction)] = _method_result(
            family="IVF-Flat",
            parameters={
                "nlist": nlist,
                "nprobe": nprobe,
                "probe_fraction": fraction,
            },
            build_ms=ivf_build_ms,
            index_bytes=ivf_size,
            latency=latency,
            indices=indices,
            flat_indices=flat_indices,
            documents=documents,
            targets=targets,
            file_k=file_k,
            neighbor_k=neighbor_k,
        )

    hnsw, hnsw_build_ms = _timed_build(
        lambda: _build_hnsw(vectors, m=hnsw_m, ef_construction=hnsw_ef_construction)
    )
    hnsw_size = _serialized_size(hnsw)
    for ef_search in hnsw_ef_search:
        hnsw.hnsw.efSearch = max(search_k, int(ef_search))
        latency, indices = _measure_search(
            hnsw, query_vector, search_k, warmups=warmups, repetitions=repetitions
        )
        methods[f"hnsw-ef{ef_search}"] = _method_result(
            family="HNSW-Flat",
            parameters={
                "m": hnsw_m,
                "ef_construction": hnsw_ef_construction,
                "ef_search": max(search_k, int(ef_search)),
            },
            build_ms=hnsw_build_ms,
            index_bytes=hnsw_size,
            latency=latency,
            indices=indices,
            flat_indices=flat_indices,
            documents=documents,
            targets=targets,
            file_k=file_k,
            neighbor_k=neighbor_k,
        )

    return methods, {"nlist": nlist}


def _bootstrap_mean_ci(
    values: Sequence[float], *, seed: str, samples: int = 2000
) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    array = np.asarray(values, dtype=np.float64)
    if len(array) == 1:
        return float(array[0]), float(array[0])
    seed_value = int.from_bytes(seed.encode("utf-8"), "little") % (2**32)
    rng = np.random.default_rng(seed_value)
    draws = rng.choice(array, size=(samples, len(array)), replace=True).mean(axis=1)
    low, high = np.percentile(draws, [2.5, 97.5])
    return float(low), float(high)


def _summarize_parameters(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keys = sorted({key for row in rows for key in row["parameters"]})
    summary = {}
    for key in keys:
        values = [row["parameters"].get(key) for row in rows]
        if all(value == values[0] for value in values):
            summary[key] = values[0]
        elif all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in values
        ):
            summary[key] = {
                "min": min(values),
                "median": float(np.median(values)),
                "max": max(values),
            }
        else:
            summary[key] = {"values": sorted({str(value) for value in values})}
    return summary


def summarize(instances: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    successful = [instance for instance in instances if "methods" in instance]
    method_names = sorted(
        {name for instance in successful for name in instance["methods"]}
    )
    methods = {}
    for method_name in method_names:
        rows = [
            instance["methods"][method_name]
            for instance in successful
            if method_name in instance["methods"]
        ]
        latency = [float(row["search"]["mean_ms"]) for row in rows]
        task_recall = [float(row["task"]["recall"]) for row in rows]
        ann_recall = [float(row["ann_recall_at_k"]) for row in rows]
        build_ms = [float(row["build_ms"]) for row in rows]
        index_bytes = [float(row["index_bytes"]) for row in rows]
        latency_ci = _bootstrap_mean_ci(latency, seed=method_name + ":latency")
        task_ci = _bootstrap_mean_ci(task_recall, seed=method_name + ":task")
        methods[method_name] = {
            "family": rows[0]["family"],
            "parameters": _summarize_parameters(rows),
            "count": len(rows),
            "mean_search_ms": float(np.mean(latency)),
            "mean_search_ms_ci95": list(latency_ci),
            "median_search_ms": float(np.median(latency)),
            "p95_search_ms": float(np.percentile(latency, 95)),
            "mean_task_recall": float(np.mean(task_recall)),
            "mean_task_recall_ci95": list(task_ci),
            "task_all_targets_rate": float(
                np.mean([row["task"]["all_targets_hit"] for row in rows])
            ),
            "mean_ann_recall_at_k": float(np.mean(ann_recall)),
            "mean_build_ms": float(np.mean(build_ms)),
            "mean_index_mb": float(np.mean(index_bytes) / (1024 * 1024)),
        }

    flat_load = [
        float(instance["flat_load"]["mean_ms"])
        for instance in successful
        if instance.get("flat_load")
    ]
    vector_counts = [int(instance["n_vectors"]) for instance in successful]
    return {
        "attempted": len(instances),
        "successful": len(successful),
        "failed": len(instances) - len(successful),
        "methods": methods,
        "flat_load": _distribution(flat_load),
        "vector_count": {
            "mean": float(np.mean(vector_counts)) if vector_counts else 0.0,
            "median": float(np.median(vector_counts)) if vector_counts else 0.0,
            "p95": float(np.percentile(vector_counts, 95)) if vector_counts else 0.0,
            "max": max(vector_counts, default=0),
        },
    }


def _write_checkpoint(
    output: Path, config: Mapping[str, Any], instances: Sequence[Mapping[str, Any]]
) -> None:
    payload = {
        "schema_version": 1,
        "benchmark": "faiss_index_family",
        "config": dict(config),
        "instances": list(instances),
        "summary": summarize(instances),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(output)


def _load_documents(path: Path) -> list[Any]:
    with path.open("rb") as handle:
        documents = pickle.load(handle)
    if not isinstance(documents, list):
        raise TypeError(f"Expected document list in {path}, got {type(documents)!r}")
    return documents


def _load_dataset_rows(instance_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    from codeminer.dataset.codeminer_base import CodeMinerBaseDataset

    wanted = set(instance_ids)
    dataset = CodeMinerBaseDataset(log=False).load()
    return {
        row["instance_id"]: dict(row) for row in dataset if row["instance_id"] in wanted
    }


def _load_or_encode_queries(
    *,
    rows: Mapping[str, Mapping[str, Any]],
    instance_ids: Sequence[str],
    cache_path: Path,
    model_name: str,
    device: str,
    batch_size: int,
    max_seq_length: int | None,
) -> dict[str, np.ndarray]:
    texts = [
        str(rows[instance_id]["problem_statement"]) for instance_id in instance_ids
    ]
    signature_payload = {
        "model_name": model_name,
        "max_seq_length": max_seq_length,
        "instances": list(zip(instance_ids, texts, strict=True)),
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if cache_path.exists():
        cached = np.load(cache_path, allow_pickle=False)
        cached_ids = [str(value) for value in cached["instance_ids"]]
        vectors = cached["vectors"].astype(np.float32, copy=False)
        by_id = dict(zip(cached_ids, vectors, strict=True))
        cached_signature = (
            str(cached["signature"].item()) if "signature" in cached.files else None
        )
        if cached_signature == signature and all(
            instance_id in by_id for instance_id in instance_ids
        ):
            return {instance_id: by_id[instance_id] for instance_id in instance_ids}

    from codeminer.index.embedding.vector_store import _HuggingFaceEmbeddingWrapper

    wrapper = _HuggingFaceEmbeddingWrapper(
        model_name,
        device=device,
        trust_remote_code=True,
        max_seq_length=max_seq_length,
    )
    encode_kwargs = wrapper._build_encode_kwargs(  # noqa: SLF001 - exact prod path
        wrapper._query_prompt, wrapper._query_prompt_name  # noqa: SLF001
    )
    encode_kwargs.update(
        {
            "batch_size": batch_size,
            "convert_to_numpy": True,
            "show_progress_bar": True,
        }
    )
    vectors = np.asarray(
        wrapper._model.encode(texts, **encode_kwargs), dtype=np.float32
    )  # noqa: SLF001
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        cache_path,
        instance_ids=np.asarray(instance_ids),
        vectors=vectors,
        signature=np.asarray(signature),
    )
    del wrapper
    gc.collect()
    try:
        import torch

        torch.cuda.empty_cache()
    except ImportError:
        pass
    return dict(zip(instance_ids, vectors, strict=True))


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prebuilt-dir", type=Path, default=DEFAULT_PREBUILT_DIR)
    parser.add_argument("--stats-file", type=Path, default=DEFAULT_STATS_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--query-cache", type=Path, default=DEFAULT_QUERY_CACHE)
    parser.add_argument("--embedding-model", default=DEFAULT_MODEL)
    parser.add_argument("--embedding-dimension", type=int, default=DEFAULT_DIMENSION)
    parser.add_argument("--index-suffix", default=DEFAULT_INDEX_SUFFIX)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-seq-length", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--search-k", type=int, default=10)
    parser.add_argument("--file-k", type=int, default=10)
    parser.add_argument("--neighbor-k", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=10)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--load-warmups", type=int, default=1)
    parser.add_argument("--load-repetitions", type=int, default=3)
    parser.add_argument(
        "--ivf-probe-fractions",
        type=float,
        nargs="+",
        default=DEFAULT_IVF_PROBE_FRACTIONS,
    )
    parser.add_argument("--hnsw-m", type=int, default=32)
    parser.add_argument("--hnsw-ef-construction", type=int, default=200)
    parser.add_argument(
        "--hnsw-ef-search",
        type=int,
        nargs="+",
        default=DEFAULT_HNSW_EF_SEARCH,
    )
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    faiss.omp_set_num_threads(args.threads)

    stats = json.loads(args.stats_file.read_text(encoding="utf-8"))
    instance_ids = [row["instance_id"] for row in stats]
    if args.limit is not None:
        instance_ids = instance_ids[: args.limit]
    rows = _load_dataset_rows(instance_ids)
    missing_rows = sorted(set(instance_ids) - set(rows))
    if missing_rows:
        raise RuntimeError(f"Dataset rows missing for {missing_rows}")
    query_vectors = _load_or_encode_queries(
        rows=rows,
        instance_ids=instance_ids,
        cache_path=args.query_cache,
        model_name=args.embedding_model,
        device=args.device,
        batch_size=args.batch_size,
        max_seq_length=args.max_seq_length,
    )

    config = {
        "dataset": "fishmingyu/codeminer-base-dataset:test",
        "embedding_model": args.embedding_model,
        "embedding_dimension": args.embedding_dimension,
        "index_suffix": args.index_suffix,
        "threads": args.threads,
        "search_k": args.search_k,
        "file_k": args.file_k,
        "neighbor_k": args.neighbor_k,
        "warmups": args.warmups,
        "repetitions": args.repetitions,
        "load_warmups": args.load_warmups,
        "load_repetitions": args.load_repetitions,
        "ivf_probe_fractions": list(args.ivf_probe_fractions),
        "hnsw_m": args.hnsw_m,
        "hnsw_ef_construction": args.hnsw_ef_construction,
        "hnsw_ef_search": list(args.hnsw_ef_search),
        "faiss_version": faiss.__version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "instance_count": len(instance_ids),
        "instance_ids_sha256": hashlib.sha256(
            "\n".join(instance_ids).encode("utf-8")
        ).hexdigest(),
    }

    results: list[dict[str, Any]] = []
    completed = set()
    if args.resume and args.output.exists():
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        if existing.get("config") != config:
            raise RuntimeError(
                "Cannot resume: existing benchmark configuration does not match"
            )
        results = [
            result
            for result in existing.get("instances") or []
            if "methods" in result and result.get("instance_id") in instance_ids
        ]
        completed = {result["instance_id"] for result in results}

    for position, instance_id in enumerate(instance_ids, 1):
        if instance_id in completed:
            continue
        started = time.perf_counter()
        index_dir = args.prebuilt_dir / instance_id / "l2"
        index_path = index_dir / f"index_{args.index_suffix}.faiss"
        documents_path = index_dir / f"documents_{args.index_suffix}.pkl"
        try:
            flat_load, source_index = _measure_flat_load(
                index_path,
                warmups=args.load_warmups,
                repetitions=args.load_repetitions,
            )
            documents = _load_documents(documents_path)
            vectors = np.ascontiguousarray(
                source_index.reconstruct_n(0, source_index.ntotal), dtype=np.float32
            )
            if source_index.d != args.embedding_dimension:
                raise ValueError(
                    f"{instance_id}: expected dim {args.embedding_dimension}, "
                    f"found {source_index.d}"
                )
            del source_index
            methods, derived = benchmark_vectors(
                vectors=vectors,
                documents=documents,
                query_vector=query_vectors[instance_id],
                targets=rows[instance_id].get("gt_target_files") or [],
                search_k=args.search_k,
                file_k=args.file_k,
                neighbor_k=args.neighbor_k,
                warmups=args.warmups,
                repetitions=args.repetitions,
                ivf_probe_fractions=args.ivf_probe_fractions,
                hnsw_m=args.hnsw_m,
                hnsw_ef_construction=args.hnsw_ef_construction,
                hnsw_ef_search=args.hnsw_ef_search,
            )
            result = {
                "instance_id": instance_id,
                "repo": rows[instance_id].get("repo"),
                "language_group": rows[instance_id].get("language_group"),
                "n_vectors": int(len(vectors)),
                "dimension": int(vectors.shape[1]),
                "source_index_bytes": index_path.stat().st_size,
                "target_files": list(rows[instance_id].get("gt_target_files") or []),
                "flat_load": flat_load,
                "derived": derived,
                "methods": methods,
                "elapsed_s": time.perf_counter() - started,
            }
        except Exception as error:  # noqa: BLE001 - checkpoint the full corpus
            result = {
                "instance_id": instance_id,
                "error": repr(error),
                "elapsed_s": time.perf_counter() - started,
            }
        results.append(result)
        _write_checkpoint(args.output, config, results)
        status = "ok" if "methods" in result else result["error"]
        print(
            f"[{position}/{len(instance_ids)}] {instance_id}: {status} "
            f"({result['elapsed_s']:.1f}s)",
            flush=True,
        )
        gc.collect()

    _write_checkpoint(args.output, config, results)
    summary = summarize(results)
    print(json.dumps(summary, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
