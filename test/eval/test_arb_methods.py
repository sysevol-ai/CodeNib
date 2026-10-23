# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""ARB dense, hybrid, and Zoekt rankers: inputs, caching, fusion, and calls."""

import json
import os
import shutil
import subprocess
import sys
import time
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
    assert callable(runner.method_indexer("bm25", {}))
    with pytest.raises(ValueError, match="unknown"):
        runner.method_config("graph")


def test_dense_identity_pins_weights_device_and_precision(monkeypatch):
    monkeypatch.setattr(methods, "dense_precision", lambda device: ("cpu", "float32"))
    config = runner.method_config("hybrid")
    assert config["model"] == methods.DENSE_MODEL
    assert len(config["revision"]) == 40
    assert (config["device"], config["dtype"]) == ("cpu", "float32")
    assert config["query_prompt"] == ""
    assert "RRF" in config["fusion"]


def test_dense_precision_needs_bfloat16_support(monkeypatch):
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "is_bf16_supported", lambda: False)
    assert methods.dense_precision(None) == ("cuda", "float32")
    monkeypatch.setattr(torch.cuda, "is_bf16_supported", lambda: True)
    assert methods.dense_precision("cuda:1") == ("cuda:1", "bfloat16")
    assert methods.dense_precision("cpu") == ("cpu", "float32")


def test_missing_optional_extra_fails_before_any_case(monkeypatch):
    def missing(module):
        raise ImportError(f"No module named {module!r}")

    monkeypatch.setattr(runner.importlib, "import_module", missing)
    with pytest.raises(ValueError, match=r"codenib\[graph\]"):
        runner.method_config("graph_expansion")
    with pytest.raises(ValueError, match=r"codenib\[semantic\]"):
        runner.method_config("dense")


def test_embedding_usage_sums_checkpointed_builds():
    config = {"model": methods.DENSE_MODEL, "revision": methods.DENSE_REVISION}
    rows = [
        {
            "index_built": True,
            "model_load_seconds": 8.0,
            "index_size": {"encoded_documents": 10, "reused_documents": 0},
        },
        {"index_built": False},
        # A resumed process loads the model again; the hybrid nests dense sizes.
        {
            "index_built": True,
            "model_load_seconds": 7.0,
            "index_size": {"dense": {"encoded_documents": 3, "reused_documents": 7}},
        },
    ]
    usage = runner.embedding_usage(rows, config)
    assert usage["model_loads"] == 2 and usage["model_load_seconds"]["total"] == 15
    assert (usage["encoded_documents"], usage["reused_documents"]) == (13, 7)


def test_timeout_kills_the_whole_process_group(tmp_path):
    marker = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, time\n"
        "child = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        methods.run_process_group([sys.executable, "-c", script], timeout=2)
    stat = Path(f"/proc/{int(marker.read_text())}/stat")
    deadline = time.time() + 5
    while stat.exists() and stat.read_text().split()[2] != "Z":
        assert time.time() < deadline, "grandchild survived the timeout"
        time.sleep(0.1)


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


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_snapshot_checkout_has_no_later_history(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "t@example.com")
    _git(source, "config", "user.name", "t")
    _git(source, "config", "uploadpack.allowAnySHA1InWant", "true")
    (source / "a.py").write_text("base\n")
    _git(source, "add", "a.py")
    _git(source, "commit", "-qm", "base")
    base = _git(source, "rev-parse", "HEAD")
    (source / "a.py").write_text("fix\n")
    _git(source, "commit", "-qam", "resolving change")
    fix = _git(source, "rev-parse", "HEAD")

    root = tmp_path / "workspace"
    root.mkdir()
    remote = source.resolve().as_uri()
    checkout = methods.fetch_snapshot("owner/repo", base, root, remote=remote)
    assert (checkout / "a.py").read_text() == "base\n"
    assert _git(checkout, "rev-list", "--count", "--all") == "1"
    with pytest.raises(subprocess.CalledProcessError):
        _git(checkout, "cat-file", "-e", fix)
    # A reused checkout is revalidated and loses untracked or ignored output.
    (checkout / "stray.py").write_text("not at base\n")
    (checkout / "build").mkdir()
    (checkout / ".git" / "info" / "exclude").write_text("build/\n")
    (checkout / "build" / "generated.py").write_text("indexer output\n")
    assert methods.fetch_snapshot("owner/repo", base, root) == checkout
    assert sorted(path.name for path in checkout.iterdir()) == [".git", "a.py"]
    (checkout / "a.py").write_text("edited\n")
    with pytest.raises(ValueError, match="local changes"):
        methods.fetch_snapshot("owner/repo", base, root)
    with pytest.raises(ValueError, match="invalid"):
        methods.fetch_snapshot("owner/repo;rm", base, root)


def test_snapshot_languages_keep_major_source_languages():
    corpus = rows(
        *[(f"src/m{i}.py", "file", "", "") for i in range(8)],
        ("web/app.ts", "file", "", ""),
        ("web/app.js", "file", "", ""),
        ("README.md", "file", "", ""),
        ("src/m0.py", "function", "f", ""),
    )
    assert methods.snapshot_languages(corpus) == ["python", "typescript"]
    assert methods.snapshot_languages(rows(("README", "file", "", ""))) == []


def _graph_index(monkeypatch, tmp_path, graph, bm25_order):
    class Ranked:
        build_seconds = 0.1
        size = {"documents": len(bm25_order)}

        def __init__(self, corpus):
            pass

        def rank(self, query):
            return list(bm25_order)

    monkeypatch.setattr(methods, "BM25CorpusIndex", Ranked)
    monkeypatch.setattr(methods, "fetch_snapshot", lambda *args: tmp_path)
    saved = tmp_path / "graph.pkl"
    if graph is not None:
        graph.save_graph(str(saved))

    def build(checkout, output, languages):
        report = {"available_languages": languages, "graph": None}
        if graph is not None:
            report["graph"] = str(saved)
        return report

    monkeypatch.setattr(methods, "build_graph", build)
    return methods.GraphExpansionIndex(
        rows(*[(path, "file", "", "") for path in bm25_order]),
        repo="owner/repo",
        base_commit="a" * 40,
        workspace=tmp_path,
    )


def _reference_graph():
    from codenib.graph.code_graph import CodeGraph

    graph = CodeGraph("/repo")
    graph.add_root_node("/repo")
    for path, symbol in [
        ("seed.py", "seed.py:run"),
        ("callee.py", "callee.py:helper"),
        ("caller.py", "caller.py:main"),
        ("vendor.py", "vendor.py:lib"),
    ]:
        graph.add_file_node(path)
        graph.add_symbol_node(symbol, 1, 1, 5, "function")
        graph.add_containment_edge(symbol)
    graph.current_file, graph.current_scope = "seed.py", "seed.py:run"
    graph.add_symbol_reference("callee.py:helper")
    graph.add_symbol_reference("vendor.py:lib")
    graph.current_file, graph.current_scope = "caller.py", "caller.py:main"
    graph.add_symbol_reference("seed.py:run")
    graph.add_symbol_reference("seed.py:run")
    graph.current_file, graph.current_scope = "callee.py", "callee.py:helper"
    graph.add_symbol_reference("seed.py:run")
    return graph


def test_graph_expansion_promotes_referenced_files(monkeypatch, tmp_path):
    order = (
        ["seed.py"] + [f"other{i}.py" for i in range(30)] + ["caller.py", "callee.py"]
    )
    monkeypatch.setattr(methods, "GRAPH_SEED_FILES", 1)
    index = _graph_index(monkeypatch, tmp_path, _reference_graph(), order)
    ranked = index.rank("q")
    # caller.py (2 references) and callee.py (2) jump ahead of BM25's tail;
    # vendor.py is not a released candidate, so it is dropped and counted.
    assert ranked[:3] == ["seed.py", "callee.py", "caller.py"]
    assert sorted(ranked) == sorted(order)
    assert index.graph_used and index.tool_calls == 2
    assert index.removed_non_candidate_paths == 1
    assert index.size["graph"]["reference_edges"] > 0
    index.close()


def test_graph_expansion_without_graph_is_bm25(monkeypatch, tmp_path):
    order = ["a.py", "b.py", "c.py"]
    index = _graph_index(monkeypatch, tmp_path, None, order)
    assert index.rank("q") == order
    assert not index.graph_used and index.tool_calls == 1
