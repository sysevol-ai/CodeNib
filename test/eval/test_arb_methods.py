# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""ARB dense, hybrid, and Zoekt rankers: inputs, caching, fusion, and calls."""

import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from codenib.eval.benchmarks import _arb_methods as methods
from codenib.eval.benchmarks import agent_retrieval_bench_runner as runner


def rows(*items):
    return [
        {
            "path": path,
            "kind": kind,
            "symbol": symbol,
            "start_line": 1,
            "end_line": 2,
            "text": text,
        }
        for path, kind, symbol, text in items
    ]


def test_zoekt_terms_use_query_values_not_field_names():
    query = json.dumps(
        {
            "failure_excerpt": "FAIL: TestRenderJSON in render/json_test.go\n"
            "expected `application/json` from c.ShouldBindJSON",
            "run_strategy": "go test ./...",
        },
        sort_keys=True,
    )
    terms = methods.zoekt_terms(query)
    assert "failure_excerpt" not in terms and "run_strategy" not in terms
    # File names, then backticked spans, then identifiers.
    assert terms[:3] == ["json_test.go", "application/json", "TestRenderJSON"]
    assert {"application/json", "ShouldBindJSON"} <= set(terms)
    # The file name already covers its stem.
    assert "json_test" not in terms


def test_zoekt_terms_skip_noise_and_cap():
    query = " ".join(["ERROR TODO ab"] + [f"name_{i}" for i in range(50)])
    terms = methods.zoekt_terms(query)
    assert len(terms) == methods.ZOEKT_MAX_TERMS
    assert not {"ERROR", "TODO", "ab"} & set(terms)


def test_zoekt_literal_quotes_operators_and_escapes_re2():
    assert methods._zoekt_literal("c.JSON(x)[0]") == r'"c\\.JSON\\(x\\)\\[0\\]"'
    assert methods._zoekt_literal("or") == '"or"'
    assert methods._zoekt_literal('a"b') == r'"a\"b"'


class FakeEmbedding:
    def __init__(self):
        self.batches = []

    def embed_documents(self, texts):
        self.batches.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


def test_cached_embedding_encodes_each_text_once():
    cache = methods._CachedEmbedding(FakeEmbedding())
    first = cache.embed_documents(["a", "bb", "a"])
    second = cache.embed_documents(["bb", "ccc"])
    assert cache._embedding.batches == [["a", "bb"], ["ccc"]]
    assert (cache.encoded_documents, cache.reused_documents) == (3, 2)
    assert np.allclose(first[0], first[2]) and np.allclose(first[1], second[0])


def test_hybrid_fuses_complete_file_rankings(monkeypatch):
    corpus = rows(
        ("a.py", "file", "", "x"),
        ("b.py", "file", "", "y"),
        ("c.py", "file", "", "z"),
    )

    class Ranked:
        tool_calls = 1

        def __init__(self, order):
            self.order = order
            self.build_seconds = 1.0
            self.size = {"documents": 3}

        def rank(self, query):
            return list(self.order)

    bm25 = ["a.py", "b.py", "c.py"]
    dense = ["c.py", "b.py", "a.py"]
    monkeypatch.setattr(methods, "BM25CorpusIndex", lambda corpus: Ranked(bm25))
    monkeypatch.setattr(
        methods, "DenseCorpusIndex", lambda corpus, encoder: Ranked(dense)
    )
    index = methods.HybridCorpusIndex(corpus, encoder=None)
    assert index.build_seconds == 2.0 and index.tool_calls == 2
    # a and c tie (1/61 + 1/63) above b (2/62); ties keep the BM25 order.
    assert index.rank("q") == ["a.py", "c.py", "b.py"]


def test_bm25_method_needs_no_extra_configuration():
    assert runner.method_config("bm25") == {}
    build, usage = runner.method_indexer("bm25")
    assert usage() == {}
    with pytest.raises(ValueError, match="unknown"):
        runner.method_config("graph")


def test_dense_method_identity_is_static():
    config = runner.method_config("hybrid")
    assert config["model"] == methods.DENSE_MODEL
    assert config["query_prompt"] == ""
    assert "RRF" in config["fusion"]


def test_missing_zoekt_binaries_rejected(tmp_path):
    with pytest.raises(ValueError, match="make zoekt-tool"):
        runner.zoekt_bin_dir(tmp_path)


def _zoekt_dir():
    found = shutil.which("zoekt-index")
    tools = os.environ.get("CODENIB_SCIP_TOOLS_DIR")
    for directory in [Path(found).parent if found else None] + (
        [Path(tools) / "go-tools" / "bin"] if tools else []
    ):
        if directory and (directory / "zoekt").is_file():
            return directory
    pytest.skip("zoekt binaries not installed; run `make zoekt-tool`")


@pytest.mark.integration
def test_zoekt_ranks_symbol_rows_by_their_file_and_counts_calls():
    corpus = rows(
        ("src/router.go", "file", "", "package src\nfunc Route() {}"),
        # Released file text can stop before a symbol; its chunk still matches.
        ("src/router.go", "function", "handlePanic", "func handlePanic() {}"),
        ("src/router_test.go", "file", "", "func TestRoutePanic(t) { Route() }"),
        ("src/ops.go", "file", "", "// or file:x -ctx.Done(c.JSON("),
        ("README", "file", "", "unrelated"),
    )
    index = methods.ZoektCorpusIndex(corpus, _zoekt_dir())
    try:
        assert index.size["documents"] == 5 and index.size["index_bytes"] > 0
        # Operators, flag-like terms, and RE2 metacharacters match literally.
        for term in ("or", "file:x", "-ctx.Done", "c.JSON("):
            assert index._files(term) == ["src/ops.go"]
        query = json.dumps({"failure_excerpt": "handlePanic in TestRoutePanic"})
        ranked = index.rank(query)
        assert index.tool_calls == 2
        assert ranked == ["src/router.go", "src/router_test.go"]
        assert index.rank(json.dumps({"text": "nothing code-like"})) == []
        assert index.tool_calls == 0
    finally:
        index.close()
