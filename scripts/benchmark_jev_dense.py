#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Freeze dense and BM25+dense controls for the existing Jev Base experiment.

Reuse the source snapshots, queries, chunk policy and scoring harness from
benchmark_jev_base.py. No model sees labels. Reranking is a separate command in
that harness; this script makes no paid calls. Document embeddings are cached
by exact input text within each repository, across its historical snapshots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codenib.code_chunker import CodeChunker
from codenib.index.embedding.model_policy import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_REVISION,
)
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
from codenib.ops.retrieve import merge_hybrid, to_queried_nodes
from codenib.types import NodeInfo
from scripts.benchmark_jev_base import (
    digest,
    git,
    load_cases,
    metrics,
    pool_coverage,
    snapshot_chunks,
    visible_node,
    write_json,
)


def node_key(node):
    return node.node_id, node.start_line, node.end_line


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bm25-prepared", type=Path, required=True)
    parser.add_argument("--prebuilt-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("limit must be positive")
    manifest, cases = load_cases(args.bm25_prepared)
    if manifest["candidate_count"] != 100 or manifest["max_content_chars"] != 3000:
        parser.error("This control requires the frozen K=100, 3000-char experiment")
    cases = cases[: args.limit]
    args.output.mkdir(parents=True, exist_ok=False)
    records = {route: [] for route in ("dense", "hybrid")}
    for route in records:
        (args.output / route / "cases").mkdir(parents=True)

    import faiss
    import numpy as np
    import torch

    from codenib.index.embedding.vector_store import _HuggingFaceEmbeddingWrapper

    faiss.omp_set_num_threads(1)
    started = time.perf_counter()
    embedding = _HuggingFaceEmbeddingWrapper(
        DEFAULT_EMBEDDING_MODEL,
        revision=DEFAULT_EMBEDDING_REVISION,
        device="cuda",
        local_files_only=True,
        max_seq_length=8192,
        default_batch_size=32,
        encode_kwargs={"show_progress_bar": False, "normalize_embeddings": True},
    )
    for _ in range(3):
        embedding.embed_query("Find the code handling a failed operation.")
    torch.cuda.synchronize()
    config = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "model": DEFAULT_EMBEDDING_MODEL,
        "revision": DEFAULT_EMBEDDING_REVISION,
        "model_max_tokens": embedding._model.max_seq_length,
        "document_batch_size": (
            "at most 32; length buckets with batch * tokens^2 "
            "<= 32 * 1024^2 (minimum one)"
        ),
        "document_template": "{full_chunk_content}; matches CodeVectorStore.add_code_chunks",
        "query_prompt_name": embedding._query_prompt_name,
        "query_prompt": embedding._query_prompt,
        "dtype": str(next(embedding._model.parameters()).dtype),
        "gpu": torch.cuda.get_device_name(0),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "faiss_threads": 1,
        "index": "FAISS IndexFlatIP; normalized float32 vectors",
        "model_load_and_warm_ms": (time.perf_counter() - started) * 1000,
        "hybrid": (
            "BM25 top-100 + dense top-100; production RRF k=60, equal weights; "
            "cap 100; BM25 first for stable ties"
        ),
        "bm25_manifest_sha256": digest(args.bm25_prepared / "manifest.json"),
        "script_sha256": digest(Path(__file__)),
        "instances": len(cases),
        "note": (
            "Offline build excluded. No query, target or score controls indexing. "
            "Reuse only exact document text within one repository."
        ),
    }
    write_json(args.output / "config.json", config)
    print(json.dumps({"phase": "model_ready", **config}), flush=True)
    chunker = CodeChunker(chunk_depth=2, max_lines_per_chunk=100)
    previous_repo, chunk_cache, vector_cache = None, {}, {}
    prompt = embedding._query_prompt
    if prompt is None:
        prompt = embedding._model.prompts.get(embedding._query_prompt_name, "")
    tokenizer = embedding._model.tokenizer

    for ordinal, source in enumerate(cases, 1):
        if source["repo"] != previous_repo:
            chunk_cache.clear()
            vector_cache.clear()
            previous_repo = source["repo"]
        repo = args.prebuilt_root / source["instance_id"] / "repo"
        started = time.perf_counter()
        chunks, files = snapshot_chunks(
            repo, source["base_commit"], chunker, chunk_cache
        )
        tree = (
            git(repo, "rev-parse", source["base_commit"] + "^{tree}").decode().strip()
        )
        blobs_hash = hashlib.sha256(json.dumps(files).encode()).hexdigest()
        if (
            tree != source["git_tree"]
            or blobs_hash != source["selected_blobs_sha256"]
            or len(chunks) != source["corpus_chunks"]
        ):
            raise ValueError(f"Source corpus changed: {source['instance_id']}")
        nodes = [visible_node(c, 3000) for c in chunks]
        lookup = {node_key(n): n for n in nodes}
        for raw in source["candidates"]:
            old = NodeInfo.model_validate(raw)
            if lookup[node_key(old)].content != old.content:
                raise ValueError("Candidate content differs from the frozen corpus")
        snapshot_ms = (time.perf_counter() - started) * 1000
        # Retrieval indexes full chunks, just as BM25 and CodeVectorStore do.
        # The shared 3,000-character cap applies when candidates are exposed
        # to rerankers and evaluated, not when the source is embedded.
        texts = [c.content for c in chunks]
        document_tokens = [
            len(ids) for ids in tokenizer(texts, truncation=False)["input_ids"]
        ]
        lengths = dict(zip(texts, document_tokens, strict=True))
        missing = list(dict.fromkeys(t for t in texts if t not in vector_cache))
        missing.sort(key=lengths.__getitem__)
        started = time.perf_counter()
        offset, next_notice = 0, 0
        while offset < len(missing):
            length = min(8192, lengths[missing[min(offset + 31, len(missing) - 1)]])
            size = max(1, min(32, 32 * 1024**2 // max(length, 1) ** 2))
            batch = missing[offset : offset + size]
            vectors = embedding.embed_documents(batch)
            for text, vector in zip(batch, vectors, strict=True):
                vector_cache[text] = np.asarray(vector, dtype=np.float32)
            offset += len(batch)
            if offset >= next_notice or offset == len(missing):
                print(
                    json.dumps(
                        {
                            "phase": "embed",
                            "case": ordinal,
                            "total_cases": len(cases),
                            "done": offset,
                            "new_texts": len(missing),
                        }
                    ),
                    flush=True,
                )
                next_notice = offset + 2048
        torch.cuda.synchronize()
        document_encode_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        matrix = np.stack([vector_cache[t] for t in texts])
        if not np.isfinite(matrix).all():
            raise ValueError("Non-finite document vectors")
        faiss.normalize_L2(matrix)
        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)
        index_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        bm25 = BM25CodeIndexer(chunks=chunks, max_k=100)
        bm25_build_ms = (time.perf_counter() - started) * 1000
        bm25.search("Find the code handling a failed operation.", top_k=100)
        query_tokens = len(
            tokenizer(prompt + source["query"], truncation=False)["input_ids"]
        )
        started = time.perf_counter()
        query = np.asarray([embedding.embed_query(source["query"])], dtype=np.float32)
        torch.cuda.synchronize()
        query_encode_ms = (time.perf_counter() - started) * 1000
        faiss.normalize_L2(query)
        scores, positions = index.search(query, 100)
        dense = [
            nodes[int(i)].model_copy(update={"score": float(s)})
            for s, i in zip(scores[0], positions[0], strict=True)
            if i >= 0
        ]
        dense_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        bm25_hits = bm25.search(source["query"], top_k=100, return_code_content=False)
        bm25_ms = (time.perf_counter() - started) * 1000
        if [node_key(h)[:2] for h in bm25_hits] != [
            (n["node_id"], n["start_line"]) for n in source["candidates"]
        ]:
            raise ValueError("Warm BM25 order differs from the frozen control")
        bm25_nodes = [NodeInfo.model_validate(n) for n in source["candidates"]]
        started = time.perf_counter()
        hybrid = merge_hybrid(
            [to_queried_nodes(bm25_nodes), to_queried_nodes(dense)],
            top_k=100,
            fusion="rrf",
            rrf_k=60,
        )
        fusion_ms = (time.perf_counter() - started) * 1000
        common = {
            k: v
            for k, v in source.items()
            if k
            not in {
                "candidates",
                "baseline_metrics",
                "coverage_curve",
                "corpus_coverage",
                "retrieval_ms",
                "preparation_ms",
            }
        }
        common["dense_preparation"] = {
            "snapshot_ms": snapshot_ms,
            "new_document_texts": len(missing),
            "cached_document_texts": len(texts) - len(missing),
            "document_encode_ms": document_encode_ms,
            "faiss_build_ms": index_ms,
            "bm25_build_ms": bm25_build_ms,
            "query_tokens": query_tokens,
            "query_truncated": query_tokens > 8192,
            "document_max_tokens": max(document_tokens),
            "documents_truncated": sum(n > 8192 for n in document_tokens),
        }
        common["online_components_ms"] = {
            "query_encode": query_encode_ms,
            "dense": dense_ms,
            "bm25": bm25_ms,
            "fusion": fusion_ms,
        }
        for route, candidates, latency in [
            ("dense", dense, dense_ms),
            ("hybrid", hybrid, dense_ms + bm25_ms + fusion_ms),
        ]:
            case = {
                **common,
                "candidates": [n.model_dump() for n in candidates],
                "retrieval_ms": latency,
            }
            case["baseline_metrics"] = metrics(case, candidates)
            case["candidate_coverage"] = pool_coverage(case, candidates)
            path = args.output / route / "cases" / f"{source['instance_id']}.json"
            write_json(path, case)
            records[route].append(
                {
                    "instance_id": source["instance_id"],
                    "file": str(path.relative_to(args.output / route)),
                    "sha256": digest(path),
                }
            )
        print(
            json.dumps(
                {
                    "phase": "prepared",
                    "done": ordinal,
                    "total": len(cases),
                    "instance": source["instance_id"],
                    "chunks": len(nodes),
                    "new_texts": len(missing),
                    "dense_ms": dense_ms,
                    "query_tokens": query_tokens,
                }
            ),
            flush=True,
        )

    for route in records:
        write_json(
            args.output / route / "manifest.json",
            {
                **{
                    k: v
                    for k, v in manifest.items()
                    if k
                    not in {
                        "cases",
                        "coverage_ks",
                        "parent_candidate_count",
                        "parent_manifest_sha256",
                    }
                },
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "retrieval": route,
                "embedding_config": config,
                "script_sha256": digest(Path(__file__)),
                "cases": records[route],
            },
        )
    print(json.dumps({"phase": "complete", "instances": len(cases)}), flush=True)


if __name__ == "__main__":
    main()
