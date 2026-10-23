# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""BM25 code tokenization: whole identifiers plus their camelCase parts."""

import json
import re

import pytest

from codenib.code_chunking.base import CodeChunk
from codenib.index.sparse_idx.bm25_index import (
    BM25_TOKENIZER,
    BM25_TOKENIZER_LEGACY,
    BM25CodeIndexer,
)


def _chunk(path: str, name: str, content: str) -> CodeChunk:
    return CodeChunk(content, 0, 3, "function", name, path, f"{path}:{name}()")


def _indexer() -> BM25CodeIndexer:
    return BM25CodeIndexer(
        chunks=[
            _chunk(
                "src/users.py", "getUserName", "def getUserName(user): return user.name"
            ),
            _chunk(
                "src/orders.py", "orderTotal", "def orderTotal(order): return order.sum"
            ),
            _chunk("src/http.py", "HTTPServer", "class HTTPServer: handles requests"),
        ],
        max_k=3,
    )


def test_tokens_keep_whole_identifier_and_add_camel_case_parts():
    stem = BM25CodeIndexer()._apply_stemming
    assert stem("getUserName()") == "getusername get user name"
    assert stem("HTTPServer.handle_request") == "httpserver http server handle request"
    assert stem("plain words v2Router") == "plain words v2router"


def test_prose_query_finds_camel_case_identifier():
    indexer = _indexer()
    assert indexer.tokenizer == BM25_TOKENIZER
    top = indexer.search("user name", top_k=1, return_code_content=False)[0]
    assert top.file == "src/users.py"
    top = indexer.search("http server", top_k=1, return_code_content=False)[0]
    assert top.file == "src/http.py"


def _legacy_text(text: str) -> str:
    """The earlier tokenizer: whole lowercase alphanumeric runs only."""
    return " ".join(t.lower() for t in re.split(r"[^A-Za-z0-9]+", text) if t)


def test_legacy_index_tokenizes_queries_as_it_was_built():
    documents = [
        {
            "page_content": _legacy_text("src/users.py:getUserName() getUserName"),
            "metadata": {"node_id": "src/users.py:getUserName()"},
        },
        # Separate words would outscore the identifier if queries were split.
        {
            "page_content": _legacy_text("docs/guide.md get the user name get user"),
            "metadata": {"node_id": "docs/guide.md"},
        },
    ] + [
        # Unrelated files give BM25 a realistic document frequency spread.
        {
            "page_content": _legacy_text(f"src/m{i}.py:helper{i} compute value {i}"),
            "metadata": {"node_id": f"src/m{i}.py:helper{i}"},
        }
        for i in range(6)
    ]
    indexer = BM25CodeIndexer()
    indexer.load_index_values(documents, {"max_k": len(documents)})
    assert indexer.tokenizer == BM25_TOKENIZER_LEGACY
    assert indexer._apply_stemming("getUserName") == "getusername"
    top = indexer.search("getUserName", top_k=1, return_code_content=False)[0]
    assert top.node_id == "src/users.py:getUserName()"


def test_saved_index_records_and_restores_its_tokenizer(tmp_path):
    _indexer().save_index(str(tmp_path))
    metadata = json.loads((tmp_path / "bm25_metadata.json").read_text())
    assert metadata["tokenizer"] == BM25_TOKENIZER
    loaded = BM25CodeIndexer()
    loaded.load_index(str(tmp_path))
    assert loaded.tokenizer == BM25_TOKENIZER
    top = loaded.search("user name", top_k=1, return_code_content=False)[0]
    assert top.file == "src/users.py"
    for bad in ("code-v9", [], {}):
        with pytest.raises(ValueError, match="tokenizer is not supported"):
            BM25CodeIndexer().load_index_values([], {"tokenizer": bad})
