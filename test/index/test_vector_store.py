#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Slow end-to-end tests for HuggingFace-backed semantic search."""

import pytest

from codenib.code_chunking import create_chunker
from codenib.index import create_code_vector_store

pytestmark = pytest.mark.slow


def create_sample_code_chunks():
    """Create sample code chunks for testing."""
    chunks = [
        {
            "content": '''def calculate_fibonacci(n):
    """Calculate the nth Fibonacci number recursively."""
    if n <= 1:
        return n
    return calculate_fibonacci(n-1) + calculate_fibonacci(n-2)''',
            "chunk_type": "function",
            "name": "calculate_fibonacci",
            "file": "math_utils.py",
            "start_line": 0,
            "end_line": 4,
        },
        {
            "content": '''class BinaryTree:
    """A simple binary tree implementation."""

    def __init__(self, value):
        self.value = value
        self.left = None
        self.right = None

    def insert(self, value):
        """Insert a value into the binary tree."""
        if value < self.value:
            if self.left is None:
                self.left = BinaryTree(value)
            else:
                self.left.insert(value)
        else:
            if self.right is None:
                self.right = BinaryTree(value)
            else:
                self.right.insert(value)''',
            "chunk_type": "class",
            "name": "BinaryTree",
            "file": "data_structures.py",
            "start_line": 0,
            "end_line": 16,
        },
        {
            "content": '''def quicksort(arr):
    """Sort an array using the quicksort algorithm."""
    if len(arr) <= 1:
        return arr

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quicksort(left) + middle + quicksort(right)''',
            "chunk_type": "function",
            "name": "quicksort",
            "file": "algorithms.py",
            "start_line": 0,
            "end_line": 9,
        },
        {
            "content": '''async def fetch_user_data(user_id):
    """Fetch user data from API asynchronously."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f'/api/users/{user_id}') as response:
            if response.status == 200:
                return await response.json()
            else:
                raise ValueError(f"Failed to fetch user {user_id}")''',
            "chunk_type": "function",
            "name": "fetch_user_data",
            "file": "api_client.py",
            "start_line": 10,
            "end_line": 16,
        },
        {
            "content": """import os
import sys
from pathlib import Path

# Configuration constants
API_BASE_URL = "https://api.example.com"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3""",
            "chunk_type": "header",
            "name": "header",
            "file": "config.py",
            "start_line": 0,
            "end_line": 7,
        },
    ]
    return chunks


def test_vector_store_with_huggingface(tmp_path):
    """Test vector store with HuggingFace embeddings."""
    vector_store = create_code_vector_store(
        embedding_model="microsoft/unixcoder-base",
        embedding_provider="huggingface",
        dimension=768,
        store_path=str(tmp_path / "unixcoder"),
    )
    try:
        chunks = create_sample_code_chunks()
        vector_store.add_code_chunks(chunks)
        assert vector_store.get_stats()["total_documents"] == len(chunks)

        for query in [
            "fibonacci recursive function",
            "binary tree class",
            "sorting quicksort",
        ]:
            results = vector_store.search(query, top_k=len(chunks))
            assert results, f"Semantic search returned no results for {query!r}"
            assert len(results) == len(chunks)
    finally:
        vector_store.close()


def test_with_real_code_chunks(httpie_cli_repo, tmp_path):
    """Test with real code chunks from the httpie CLI repository."""
    sample_file = httpie_cli_repo / "httpie" / "core.py"
    assert sample_file.is_file()

    chunks = create_chunker("python").chunk_file(str(sample_file))
    assert chunks, "Chunking httpie/core.py produced no chunks"

    chunk_dicts = [
        {
            "content": chunk.content,
            "chunk_type": chunk.chunk_type,
            "name": chunk.name,
            "file": chunk.file,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "node_id": chunk.node_id,
        }
        for chunk in chunks
    ]

    vector_store = create_code_vector_store(
        embedding_model="microsoft/unixcoder-base",
        embedding_provider="huggingface",
        dimension=768,
        store_path=str(tmp_path / "real-unixcoder"),
    )
    try:
        vector_store.add_code_chunks(chunk_dicts)
        assert vector_store.get_stats()["total_documents"] == len(chunk_dicts)

        for query in ["raw_main", "Environment"]:
            results = vector_store.search(query, top_k=3)
            assert results, f"Semantic search returned no results for {query!r}"
            assert len(results) <= 3
    finally:
        vector_store.close()
