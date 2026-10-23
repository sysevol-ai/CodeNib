# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""BM25 code tokenization: whole identifiers plus their camelCase parts."""

import re

from codenib.code_chunking.base import CodeChunk
from codenib.index.sparse_idx.bm25_index import BM25CodeIndexer


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
    stem = BM25CodeIndexer.__new__(BM25CodeIndexer)._apply_stemming
    assert stem("getUserName()") == "getusername get user name"
    assert stem("HTTPServer.handle_request") == "httpserver http server handle request"
    assert stem("plain words v2Router") == "plain words v2router"


def test_prose_query_finds_camel_case_identifier():
    top = _indexer().search("user name", top_k=1, return_code_content=False)[0]
    assert top.file == "src/users.py"
    top = _indexer().search("http server", top_k=1, return_code_content=False)[0]
    assert top.file == "src/http.py"


def test_index_built_before_camel_case_parts_still_matches_identifiers():
    built = _indexer()
    legacy = [
        {
            # The earlier tokenizer kept only whole lowercase tokens.
            "page_content": " ".join(
                token.lower()
                for token in re.split(r"[^A-Za-z0-9]+", document.metadata["node_id"])
                if token
            ),
            "metadata": dict(document.metadata),
        }
        for document in built.documents
    ]
    indexer = BM25CodeIndexer()
    indexer.load_index_values(legacy, {"max_k": 3})
    top = indexer.search("getUserName", top_k=1, return_code_content=False)[0]
    assert top.file == "src/users.py"
