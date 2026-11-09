# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Native CodeNib retrieval over released CORE-Bench chunks and official metrics."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path

from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
from codenib.dataset.core_bench import (
    CoreCorpus,
    core_repository_directories,
    load_core_corpus,
    load_core_qrels,
)
from codenib.eval.benchmarks.policy_compat import (
    inspect_git_revision,
    prepare_batch_output,
    write_json_atomic,
)

CORE_UPSTREAM_REVISION = "5d00cdcaade77796fa6e5ac4852404002029c00a"


class _CheckpointEmbedding:
    """Reuse completed encoding batches through the existing EmbeddingsCache.

    One writer owns the run. A numbered shard commits when cache.save publishes
    its JSON index after its NPZ vectors. Resume loads consecutive committed
    shards and overwrites an unfinished next shard; FAISS is rebuilt in memory.
    """

    def __init__(self, embedding, directory, batch_size):
        from codenib.index.incremental.embeddings_cache import EmbeddingsCache

        self.embedding, self.directory = embedding, Path(directory)
        self.batch_size, self.cache_type = batch_size, EmbeddingsCache
        self.shards = []
        for index, path in enumerate(sorted(self.directory.glob("batch-*.json"))):
            if path.stem != f"batch-{index:08d}":
                raise ValueError("nonconsecutive embedding checkpoint batches")
            self.shards.append(EmbeddingsCache.load(path.with_suffix(".pkl")))

    def _encode(self, texts, kind, encode):
        keys = [
            hashlib.sha256((kind + "\0" + text).encode()).hexdigest() for text in texts
        ]
        cached = {}
        for shard in self.shards:
            cached.update(shard.get_batch(keys))
        missing = list(
            {
                key: text
                for key, text in zip(keys, texts, strict=True)
                if key not in cached
            }.items()
        )
        for offset in range(0, len(missing), self.batch_size):
            batch = missing[offset : offset + self.batch_size]
            hashes, pending = zip(*batch, strict=True)
            vectors = encode(list(pending))
            shard = self.cache_type()
            shard.put_batch(list(hashes), vectors)
            shard.save(self.directory / f"batch-{len(self.shards):08d}.pkl")
            self.shards.append(shard)
            cached.update(shard.get_batch(list(hashes)))
        return [cached[key].tolist() for key in keys]

    def embed_documents(self, texts):
        return self._encode(texts, "document", self.embedding.embed_documents)

    def embed_query(self, text):
        return self._encode(
            [text], "query", lambda rows: [self.embedding.embed_query(rows[0])]
        )[0]


class CoreRetriever:
    """Index released chunk IDs within the selected candidate snapshots."""

    def __init__(self, corpus: CoreCorpus, *, method="bm25", vector_store=None):
        from codenib.code_chunking.base import CodeChunk
        from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer
        from codenib.types import NODE_TYPE_SYMBOL

        if method not in {"bm25", "dense", "hybrid"}:
            raise ValueError(f"unsupported method: {method}")
        if method != "bm25" and vector_store is None:
            raise ValueError("dense/hybrid requires a CodeVectorStore")
        self.method, self.vector = method, vector_store
        self.ids = {f"core:{key}": key for key in corpus.document_ids}
        chunks = [
            CodeChunk(
                content=text,
                node_id=f"core:{key}",
                name=key,
                file="",
                start_line=0,
                end_line=0,
                chunk_type=NODE_TYPE_SYMBOL,
            )
            for key, text in zip(corpus.document_ids, corpus.texts, strict=True)
        ]
        self.bm25 = None
        if method != "dense":
            self.bm25 = BM25CodeIndexer(chunks=chunks, max_k=len(chunks))
        self.vector_ids = {
            f"core:{key}" for q in corpus.queries for key in q.candidates
        }
        if self.vector is not None:
            # Dense retrieval uses the selected candidate union. BM25's IDF
            # depends on the full corpus.
            self.vector.add_code_chunks(
                [
                    chunk._asdict()
                    for chunk in chunks
                    if chunk.node_id in self.vector_ids
                ]
            )

    def rank(self, query, *, top_k=100):
        from codenib.ops.retrieve import merge_hybrid, to_queried_nodes

        if top_k <= 0:
            raise ValueError("top_k must be positive")
        allowed = {f"core:{key}" for key in query.candidates}
        if not allowed <= self.ids.keys():
            raise ValueError("unknown candidate document")
        if self.vector is not None and not allowed <= self.vector_ids:
            raise ValueError("query candidates were not selected for this dense index")
        if not allowed:
            return {}
        branches = []
        if self.bm25 is not None:
            # Request the complete native ranking before applying the snapshot
            # mask. Filtering a global top-k would lose eligible candidates.
            nodes = self.bm25.search(
                query.text,
                top_k=len(self.ids),
                return_code_content=False,
                filter_test=False,
            )
            branches.append(
                to_queried_nodes([n for n in nodes if n.node_id in allowed])
            )
        if self.vector is not None:
            branches.append(
                to_queried_nodes(
                    self.vector.search_within_ids(
                        query.text,
                        allowed,
                        top_k=len(allowed),
                    )
                )
            )
        nodes = (
            merge_hybrid(branches, fusion="rrf", top_k=top_k)
            if self.method == "hybrid"
            else branches[0][:top_k]
        )
        # Upstream consumes ranked scores. Rank scores also preserve BM25's
        # ordering: its public API does not expose raw similarities.
        return {self.ids[n.node_id]: 1.0 / (i + 1) for i, n in enumerate(nodes)}


def load_official_collector(checkout: str | Path):
    checkout = Path(checkout).resolve()
    revision = inspect_git_revision(checkout, include_untracked=False)
    if revision.commit != CORE_UPSTREAM_REVISION or revision.dirty is not False:
        raise ValueError(
            f"use CORE-Bench-Eval at {CORE_UPSTREAM_REVISION} with clean tracked files"
        )
    path = checkout / "collect_results/collect_core_bench_results.py"
    spec = importlib.util.spec_from_file_location("_core_collector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def official_prediction_filename(directory, collector):
    """Use the pinned collector's task mapping and prediction-file contract."""
    directory = Path(directory)
    benchmark = "CORE_BENCH_" + directory.parent.parent.name.replace("-", "")
    config = collector.BENCHMARKS.get(benchmark, {})
    tasks = [
        task
        for task, name in config.get("task_to_dir", {}).items()
        if name == directory.parent.name
    ]
    if len(tasks) != 1:
        raise ValueError("retain the released LEVEL-2/LEVEL-3 task/repository layout")
    return f"{tasks[0]}_test_{directory.name}_predictions.json"


def aggregate_core(reports):
    """Average official metric rows across the selected queries."""
    rows = [
        {"repository": name, **row}
        for name, report in reports.items()
        for row in report["per_query"]
    ]
    return {
        "queries": len(rows),
        "repositories": reports,
        "mean": {
            key: sum(row[key] for row in rows) / len(rows)
            for key in rows[0]
            if key not in {"repository", "query_id"}
        },
    }


def score_core(corpus, gold, predictions, scorer):
    """Official collector's binary metrics; missing answers keep the denominator."""
    known = {q.query_id for q in corpus.queries}
    if set(predictions) - known:
        raise ValueError("predictions include unselected queries")
    rows = []
    for query in corpus.queries:
        expected = gold.get(query.query_id, set())
        if not expected or not expected <= set(query.candidates):
            raise ValueError(f"{query.query_id}: missing or ineligible positive qrels")
        scores = predictions.get(query.query_id, {})
        if not set(scores) <= set(query.candidates):
            raise ValueError("prediction outside query snapshot")
        if any(not math.isfinite(float(v)) for v in scores.values()):
            raise ValueError("non-finite prediction score")
        rows.append({"query_id": query.query_id, **scorer(scores, expected)})
    return {
        "queries": len(rows),
        "per_query": rows,
        "mean": {
            k: sum(r[k] for r in rows) / len(rows) for k in rows[0] if k != "query_id"
        },
        "scorer": "official collector, binary relevance",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "evaluate"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--method", choices=["bm25", "dense", "hybrid"], default="bm25")
    parser.add_argument("--query-id", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--model", default="nomic-ai/CodeRankEmbed")
    parser.add_argument(
        "--revision", default="3c4b60807d71f79b43f3c4363786d9493691f8b1"
    )
    parser.add_argument("--device", default="cpu")
    parser.add_argument(
        "--embedding-provider", choices=["huggingface", "openai"], default="huggingface"
    )
    parser.add_argument(
        "--embedding-base-url", help="OpenAI-compatible embedding API base"
    )
    parser.add_argument("--embedding-api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--embedding-dimension", type=int)
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.top_k < 100:
        parser.error("--top-k must be >=100 for the collector's Recall@100")
    if args.output.exists() and not args.resume:
        parser.error("output exists; use --resume or a new directory")
    if args.command == "evaluate" and args.predictions is None:
        parser.error("evaluate requires --predictions")
    if args.embedding_dimension is not None and args.embedding_dimension <= 0:
        parser.error("--embedding-dimension must be positive")
    if args.embedding_batch_size <= 0:
        parser.error("--embedding-batch-size must be positive")
    if (
        args.command == "run"
        and args.method != "bm25"
        and args.embedding_provider == "openai"
    ):
        if not args.embedding_dimension or args.model == "nomic-ai/CodeRankEmbed":
            parser.error(
                "API embeddings require an explicit --model and --embedding-dimension"
            )
    collector = load_official_collector(args.upstream)
    directories = core_repository_directories(args.data)
    if len(directories) > 1 and args.query_id:
        parser.error("--query-id requires a single repository")
    if len(directories) > 1 and args.predictions and not args.predictions.is_dir():
        parser.error(
            "group evaluation requires a directory of official prediction files"
        )
    selections = [
        (
            directory,
            official_prediction_filename(directory, collector),
        )
        for directory in directories
    ]
    data_identity = {
        str(directory.relative_to(args.data.resolve())): {
            name: regular_file_fingerprint((directory / name).resolve())
            for name in ("corpus.jsonl", "queries.jsonl", "qrels.jsonl")
        }
        for directory in directories
    }
    prediction_identity = None
    if args.predictions is not None:
        paths = (
            [args.predictions / filename for _, filename in selections]
            if args.predictions.is_dir()
            else [args.predictions]
        )
        prediction_identity = [
            regular_file_fingerprint(path.resolve()) for path in paths
        ]
    prepare_batch_output(
        args.output,
        identity={
            "data": data_identity,
            "prediction_files": [filename for _, filename in selections],
            "predictions": prediction_identity,
            "command": args.command,
            "query_ids": args.query_id,
            "limit": args.limit,
            "top_k": args.top_k,
            "method": args.method,
            "model": args.model,
            "revision": args.revision,
            "device": args.device,
            "provider": args.embedding_provider,
            "dimension": args.embedding_dimension,
            "endpoint": args.embedding_base_url
            or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "scorer": CORE_UPSTREAM_REVISION,
        },
        resume=args.resume,
    )
    args.output.mkdir(parents=True, exist_ok=args.resume)
    export = args.output / "official"
    export.mkdir(exist_ok=args.resume)
    reports = {}
    previous = args.output / "scores.json"
    embedding = None
    if args.command == "run" and args.method != "bm25":
        embedding = {
            "provider": args.embedding_provider,
            "model": args.model,
            "dimension": args.embedding_dimension,
            "base_url": args.embedding_base_url,
            "batch_size": (
                args.embedding_batch_size
                if args.embedding_provider == "openai"
                else None
            ),
            "revision": (
                args.revision if args.embedding_provider == "huggingface" else None
            ),
        }
        if args.resume and previous.exists():
            embedding.update(json.loads(previous.read_text()).get("embedding", {}))
    for directory, filename in selections:
        corpus = load_core_corpus(directory, query_ids=args.query_id, limit=args.limit)
        gold = load_core_qrels(directory)
        prediction_path = export / filename
        predictions = (
            json.loads(prediction_path.read_text())
            if args.resume and prediction_path.exists()
            else {}
        )
        score_core(corpus, gold, predictions, collector.calculate_metrics)
        vector = None
        try:
            if args.command == "evaluate":
                path = args.predictions
                if path.is_dir():
                    path = path / filename
                predictions = json.loads(path.read_text())
            elif pending := tuple(
                q for q in corpus.queries if q.query_id not in predictions
            ):
                if args.method != "bm25":
                    if args.embedding_provider == "openai" and not os.getenv(
                        args.embedding_api_key_env
                    ):
                        parser.error(
                            f"missing embedding credential: {args.embedding_api_key_env}"
                        )
                    from codenib.index.embedding.vector_store import CodeVectorStore

                    options = (
                        {"revision": args.revision, "device": args.device}
                        if args.embedding_provider == "huggingface"
                        else {
                            "api_key": os.environ[args.embedding_api_key_env],
                            "base_url": args.embedding_base_url,
                            "request_options": {"encoding_format": "float"},
                            "batch_size": args.embedding_batch_size,
                        }
                    )
                    vector = CodeVectorStore(
                        embedding_model=args.model,
                        embedding_provider=args.embedding_provider,
                        dimension=args.embedding_dimension,
                        **options,
                    )
                    vector.embedding = _CheckpointEmbedding(
                        vector.embedding,
                        args.output / "embeddings" / Path(filename).stem,
                        args.embedding_batch_size,
                    )
                    embedding["dimension"] = vector.dimension
                retriever = CoreRetriever(
                    CoreCorpus(corpus.document_ids, corpus.texts, pending),
                    method=args.method,
                    vector_store=vector,
                )
                for query in pending:
                    predictions[query.query_id] = retriever.rank(
                        query, top_k=args.top_k
                    )
                    write_json_atomic(prediction_path, predictions, sort_keys=False)
            # Include empty predictions so missing answers retain their query denominator
            # when the exported files are subsequently read by the official collector.
            if set(predictions) - {q.query_id for q in corpus.queries}:
                raise ValueError("predictions include unselected queries")
            predictions = {
                q.query_id: predictions.get(q.query_id, {}) for q in corpus.queries
            }
            result = score_core(
                corpus,
                gold,
                predictions,
                collector.calculate_metrics,
            )
            write_json_atomic(prediction_path, predictions, sort_keys=False)
            reports[
                str(directory.relative_to(args.data.resolve())) or directory.name
            ] = result
        finally:
            if vector is not None:
                vector.close()
    result = aggregate_core(reports)
    result["scorer"] = "official collector, binary relevance; query-weighted mean"
    result["selection"] = {
        "limit_per_repository": args.limit,
        "query_ids": args.query_id,
    }
    result["upstream_revision"] = CORE_UPSTREAM_REVISION
    if embedding is not None:
        result["embedding"] = embedding
    write_json_atomic(args.output / "scores.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
