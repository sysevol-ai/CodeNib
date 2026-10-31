# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Test exclusion must precede the real BM25 candidate limit."""

import pytest

from codenib.index.sparse_idx.bm25_index import BM25_TOKENIZER, BM25CodeIndexer
from codenib.utils import is_test_file


def _index(documents, max_k):
    indexer = BM25CodeIndexer()
    indexer.load_index_values(documents, {"max_k": max_k, "tokenizer": BM25_TOKENIZER})
    return indexer


def _document(path, content):
    return {
        "page_content": content,
        "metadata": {"node_id": f"{path}:handle()", "file": path},
    }


def _test_saturated_index(max_k):
    tests = [
        _document(f"tests/test_guard_{index}.py", "snapshotguard " * 8)
        for index in range(max_k + 1)
    ]
    implementation = _document("src/runtime.py", "snapshotguard " + "ordinary " * 100)
    unrelated = [
        _document(f"src/filler_{index}.py", "ordinary value")
        for index in range(3 * max_k)
    ]
    return _index([*tests, implementation, *unrelated], max_k)


@pytest.mark.parametrize("max_k", [3, 128])
@pytest.mark.parametrize("reload", [False, True])
def test_test_filter_precedes_real_retriever_limit(tmp_path, max_k, reload):
    indexer = _test_saturated_index(max_k)
    if reload:
        indexer.save_index(str(tmp_path))
        indexer = BM25CodeIndexer()
        indexer.load_index(str(tmp_path))
    unfiltered = indexer.search("snapshotguard", top_k=max_k)
    assert len(unfiltered) == max_k
    assert all(is_test_file(node.file) for node in unfiltered)

    filtered = indexer.search("snapshotguard", top_k=1, filter_test=True)

    assert filtered and filtered[0].file == "src/runtime.py"
    assert indexer.retriever.k == max_k
    assert indexer.search("snapshotguard", top_k=max_k) == unfiltered


@pytest.mark.parametrize("top_k", [1, 10])
def test_implementation_executor_survives_real_retriever_limit(top_k):
    from codenib.agent.skills.context import ComposerContexts
    from codenib.agent.skills.repository_search.executor import create_executor
    from codenib.ops.retrieve import RetrieveContext

    execute = create_executor(
        ComposerContexts(retrieve=RetrieveContext(bm25=_test_saturated_index(128)))
    )

    results = execute("snapshotguard", top_k=top_k)

    assert results and results[0].file == "src/runtime.py"
    assert all(not is_test_file(node.file) for node in results)


@pytest.mark.parametrize(
    "paths", [[], ["tests/test_a.py", "tests/test_b.py"], ["src/a.py", "src/b.py"]]
)
def test_test_filter_keeps_empty_and_uniform_corpora(paths):
    indexer = _index([_document(path, "ordinary") for path in paths], 3)
    before = indexer.search("ordinary")

    filtered = indexer.search("ordinary", filter_test=True)

    assert filtered == [node for node in before if not is_test_file(node.file)]
    assert indexer.retriever.k == 3
    assert indexer.search("ordinary") == before
