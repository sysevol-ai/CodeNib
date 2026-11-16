# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import os
from types import SimpleNamespace

import pytest

from codenib.dataset.core_bench import CoreCorpus, CoreQuery
from codenib.eval.benchmarks.core_bench import (
    CoreRetriever,
    _CheckpointEmbedding,
    aggregate_core,
    load_official_collector,
    main,
    official_prediction_filename,
    score_core,
)


def core_data(tmp_path, repositories=("repo",)):
    group = tmp_path / "LEVEL-2/SWE-bench_Verified"
    for name in repositories:
        path = group / name
        path.mkdir(parents=True)
        records = {
            "corpus": [
                {"_id": "a", "text": "tax helper"},
                {"_id": "b", "text": "invoice"},
            ],
            "queries": [
                {"_id": f"{name}/{i}", "text": "tax", "filtered_corpus_id": ["a", "b"]}
                for i in (1, 2)
            ],
            "qrels": [
                {"query_id": f"{name}/{i}", "corpus_id": "a", "score": 1}
                for i in (1, 2)
            ],
        }
        for kind, rows in records.items():
            (path / f"{kind}.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
    return group


def fixture_collector():
    return SimpleNamespace(
        BENCHMARKS={
            "CORE_BENCH_LEVEL2": {"task_to_dir": {"Task": "SWE-bench_Verified"}}
        },
        calculate_metrics=lambda scores, gold: {
            "hit": float(bool(scores.keys() & gold))
        },
    )


def test_resume_skips_completed_repositories_and_queries(tmp_path, monkeypatch):
    from codenib.eval.benchmarks import core_bench

    group = core_data(tmp_path, ("one", "two"))
    monkeypatch.setattr(
        core_bench, "load_official_collector", lambda _: fixture_collector()
    )
    rank = CoreRetriever.rank
    calls = []

    def interrupted(self, query, **kwargs):
        calls.append(query.query_id)
        if query.query_id == "two/2" and calls.count("two/2") == 1:
            raise RuntimeError("interrupted")
        return rank(self, query, **kwargs)

    monkeypatch.setattr(CoreRetriever, "rank", interrupted)
    output = tmp_path / "output"
    args = [
        "run",
        "--data",
        str(group),
        "--upstream",
        str(tmp_path),
        "--output",
        str(output),
    ]
    with pytest.raises(RuntimeError, match="interrupted"):
        main(args)
    assert main(args + ["--resume"]) == 0
    assert calls == ["one/1", "one/2", "two/1", "two/2", "two/2"]
    assert main(args + ["--resume"]) == 0
    assert len(calls) == 5
    report = json.loads((output / "scores.json").read_text())
    assert report["queries"] == 4 and report["mean"]["hit"] == 1
    assert "per_query" not in report
    assert not (output / "predictions.json").exists()
    with pytest.raises(ValueError, match="configuration"):
        main(args + ["--resume", "--top-k", "101"])
    corpus_path = group / "one/corpus.jsonl"
    corpus_path.write_text(corpus_path.read_text().replace("tax helper", "new code"))
    with pytest.raises(ValueError, match="configuration"):
        main(args + ["--resume"])
    assert len(calls) == 5


def test_embedding_batches_and_queries_resume_from_existing_cache(tmp_path):
    calls, query_calls = [], []

    class Encoder:
        def embed_documents(self, texts):
            calls.extend(texts)
            if texts == ["invoice"] and calls.count("invoice") == 1:
                raise RuntimeError("provider interrupted")
            return [[2, 0] if text == "tax" else [1, 0] for text in texts]

        def embed_query(self, text):
            query_calls.append(text)
            return [0, 1]

    cache = tmp_path / "encoding"
    with pytest.raises(RuntimeError, match="provider interrupted"):
        _CheckpointEmbedding(Encoder(), cache, 1).embed_documents(["tax", "invoice"])
    # An unfinished next shard belongs to this run and can be overwritten.
    (cache / "batch-00000001.npz").write_bytes(b"unfinished")
    resumed = _CheckpointEmbedding(Encoder(), cache, 1)
    assert resumed.embed_documents(["tax", "invoice"]) == [[2, 0], [1, 0]]
    assert resumed.embed_query("tax") == [0, 1]
    again = _CheckpointEmbedding(Encoder(), cache, 2)
    assert again.embed_documents(["tax", "invoice"]) == [[2, 0], [1, 0]]
    assert again.embed_query("tax") == [0, 1]
    assert calls == ["tax", "invoice", "invoice"] and query_calls == ["tax"]


def test_cli_dense_encoding_recovers_without_reencoding_completed_batch(
    tmp_path, monkeypatch
):
    from codenib.eval.benchmarks import core_bench
    from codenib.index.embedding import vector_store

    group = core_data(tmp_path)
    monkeypatch.setattr(
        core_bench, "load_official_collector", lambda _: fixture_collector()
    )
    native_store = vector_store.CodeVectorStore
    calls = []

    class Encoder(Embedding):
        def embed_documents(self, texts):
            calls.extend(texts)
            if texts == ["invoice"] and calls.count("invoice") == 1:
                raise RuntimeError("provider interrupted")
            return super().embed_documents(texts)

    monkeypatch.setattr(
        vector_store,
        "CodeVectorStore",
        lambda **kwargs: native_store(
            embedding_model="fixture", embedding=Encoder(), dimension=2
        ),
    )
    output = tmp_path / "output"
    args = [
        "run",
        "--data",
        str(group),
        "--upstream",
        str(tmp_path),
        "--output",
        str(output),
        "--method",
        "dense",
        "--model",
        "fixture",
        "--embedding-batch-size",
        "1",
    ]
    with pytest.raises(RuntimeError, match="provider interrupted"):
        main(args)
    assert main(args + ["--resume"]) == 0
    assert calls == ["tax helper", "invoice", "invoice"]
    assert json.loads((output / "scores.json").read_text())["queries"] == 2


@pytest.mark.integration
def test_evaluate_export_preserves_official_tied_score_order(tmp_path):
    checkout = os.getenv("CORE_BENCH_CHECKOUT")
    if not checkout:
        pytest.skip("set CORE_BENCH_CHECKOUT to the pinned upstream checkout")
    group = core_data(tmp_path)
    repo = group / "repo"
    (repo / "qrels.jsonl").write_text(
        json.dumps({"query_id": "repo/1", "corpus_id": "b", "score": 1}) + "\n"
    )
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps({"repo/1": {"b": 1, "a": 1}}))
    output = tmp_path / "output"
    assert (
        main(
            [
                "evaluate",
                "--data",
                str(repo),
                "--upstream",
                checkout,
                "--output",
                str(output),
                "--predictions",
                str(predictions),
                "--limit",
                "1",
            ]
        )
        == 0
    )
    exported = json.loads(next((output / "official").glob("*.json")).read_text())
    assert list(exported["repo/1"]) == ["b", "a"]
    assert (
        load_official_collector(checkout).calculate_metrics(exported["repo/1"], {"b"})[
            "mrr"
        ]
        == 1
    )


class Embedding:
    def embed_documents(self, texts):
        vectors = {
            "tax helper": [2.0, 0.0],
            "invoice": [1.0, 0.0],
        }
        return [vectors[text] for text in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


@pytest.mark.parametrize("method", ["dense", "hybrid"])
def test_dense_snapshot_subset_matches_full_embedding_ranking(method):
    from codenib.index.embedding.vector_store import CodeVectorStore

    class FullEmbedding(Embedding):
        def embed_documents(self, texts):
            return [
                (
                    [99.0, 0.0]
                    if t == "tax tax tax"
                    else Embedding.embed_documents(self, [t])[0]
                )
                for t in texts
            ]

    query = CoreQuery("q", "tax", ("a", "b"))
    selected = CoreCorpus(
        ("future", "a", "b"), ("tax tax tax", "tax helper", "invoice"), (query,)
    )
    full = CoreCorpus(
        selected.document_ids,
        selected.texts,
        (query, CoreQuery("other", "tax", selected.document_ids)),
    )
    stores = [
        CodeVectorStore(embedding_model="fixture", embedding=e, dimension=2)
        for e in (Embedding(), FullEmbedding())
    ]
    try:
        # The subset encoder cannot encode the future chunk; encoding it would
        # fail. The control really embeds it, then applies the same query mask.
        ranks = [
            CoreRetriever(c, method=method, vector_store=v).rank(query)
            for c, v in zip((selected, full), stores, strict=True)
        ]
        assert ranks[0] == ranks[1]
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("method", ["bm25", "dense", "hybrid"])
def test_native_indexes_preserve_ids_and_mask_before_top_k(method):
    from codenib.index.embedding.vector_store import CodeVectorStore

    corpus = CoreCorpus(
        ("future", "a", "b"),
        ("tax tax tax", "tax helper", "invoice"),
        (CoreQuery("q", "tax", ("a", "b")),),
    )
    vector = None
    try:
        if method != "bm25":
            vector = CodeVectorStore(
                embedding_model="fixture", embedding=Embedding(), dimension=2
            )
        retriever = CoreRetriever(corpus, method=method, vector_store=vector)
        ranking = retriever.rank(corpus.queries[0], top_k=1)
        assert len(ranking) == 1
        assert set(ranking) <= {"a", "b"}
        if method == "dense":
            assert list(ranking) == ["a"]
        assert retriever.rank(CoreQuery("empty", "tax", ())) == {}
    finally:
        if vector:
            vector.close()


def test_missing_prediction_remains_in_official_denominator():
    corpus = CoreCorpus(
        ("a",),
        ("tax",),
        (
            CoreQuery("one", "tax", ("a",)),
            CoreQuery("two", "tax", ("a",)),
        ),
    )

    def scorer(predicted, gold):
        return {"recall": len(set(predicted) & gold) / len(gold)}

    result = score_core(corpus, {"one": {"a"}, "two": {"a"}}, {"one": {"a": 1}}, scorer)
    assert result["queries"] == 2
    assert result["mean"]["recall"] == 0.5
    with pytest.raises(ValueError, match="snapshot"):
        score_core(corpus, {"one": {"a"}}, {"one": {"future": 1}}, scorer)


def test_group_averages_queries_instead_of_repository_means():
    reports = {
        "small": {"per_query": [{"query_id": "one", "recall": 1.0}]},
        "large": {"per_query": [{"query_id": str(i), "recall": 0.0} for i in range(3)]},
    }
    assert aggregate_core(reports)["mean"]["recall"] == 0.25


@pytest.mark.integration
def test_group_exports_round_trip_through_official_collector(tmp_path):
    checkout = os.getenv("CORE_BENCH_CHECKOUT")
    if not checkout:
        pytest.skip("set CORE_BENCH_CHECKOUT to the pinned upstream checkout")
    collector = load_official_collector(checkout)
    root = tmp_path / "LEVEL-2"
    group = root / "SWE-bench_Verified"
    predictions = tmp_path / "predictions"
    predictions.mkdir()
    for name, count in [("small", 1), ("large", 3)]:
        repo = group / name
        repo.mkdir(parents=True)
        (repo / "corpus.jsonl").write_text(
            json.dumps({"_id": "a", "text": "tax"}) + "\n"
        )
        (repo / "queries.jsonl").write_text(
            "".join(
                json.dumps({"_id": str(i), "text": "tax", "filtered_corpus_id": ["a"]})
                + "\n"
                for i in range(count)
            )
        )
        (repo / "qrels.jsonl").write_text(
            "".join(
                json.dumps({"query_id": str(i), "corpus_id": "a", "score": 1}) + "\n"
                for i in range(count)
            )
        )
        # Deliberately omit all answers for the larger repository.
        rows = {"0": {"a": 1.0}} if name == "small" else {}
        (predictions / official_prediction_filename(repo, collector)).write_text(
            json.dumps(rows)
        )
    output = tmp_path / "result"
    assert (
        main(
            [
                "evaluate",
                "--data",
                str(group),
                "--upstream",
                checkout,
                "--predictions",
                str(predictions),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads((output / "scores.json").read_text())
    task = "CORE_L2_SWEBenchVerifiedRetrieval"
    rows = collector.collect_jsonl_task(
        "CodeNib", output / "official", "CORE_BENCH_LEVEL2", task, root, group.name
    )
    assert len(rows) == report["queries"] == 4
    assert (
        sum(row["recall@10"] for row in rows) / len(rows)
        == report["mean"]["recall@10"]
        == 0.25
    )


@pytest.mark.integration
def test_pinned_official_collector_with_native_bm25():
    checkout = os.getenv("CORE_BENCH_CHECKOUT")
    if not checkout:
        pytest.skip("set CORE_BENCH_CHECKOUT to the pinned upstream checkout")
    corpus = CoreCorpus(
        ("a", "b"), ("tax helper", "invoice"), (CoreQuery("q", "tax", ("a",)),)
    )
    ranker = CoreRetriever(corpus)
    predictions = {"q": ranker.rank(corpus.queries[0])}
    scorer = load_official_collector(checkout).calculate_metrics
    result = score_core(corpus, {"q": {"a"}}, predictions, scorer)
    assert result["mean"] == scorer({"a": 1.0}, {"a"})
