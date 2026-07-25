#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Test script for the CodeVectorStore functionality.
Demonstrates how to use the vector store for semantic search over code chunks.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from codenib.code_chunking import create_chunker
from codenib.index import create_code_vector_store

pytestmark = pytest.mark.slow

HTTPIE_REPO_URL = "https://github.com/httpie/cli.git"
HTTPIE_REPO_PATH = Path("/tmp/httpie-cli")


def ensure_httpie_repo() -> Path:
    """Clone the httpie/cli repository if needed and return its path."""
    if not HTTPIE_REPO_PATH.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", HTTPIE_REPO_URL, str(HTTPIE_REPO_PATH)],
            check=True,
        )
    return HTTPIE_REPO_PATH


# Add the parent directory to the path to import codenib modules
sys.path.insert(0, str(Path(__file__).parent.parent))


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


def test_vector_store_with_huggingface():
    """Test vector store with HuggingFace embeddings."""
    print("\n=== Testing CodeVectorStore with UniXcoder Embeddings ===")

    try:
        # Create vector store with UniXcoder embeddings
        vector_store = create_code_vector_store(
            embedding_model="microsoft/unixcoder-base",
            embedding_provider="huggingface",
            dimension=768,  # UniXcoder dimension
            store_path="./test_vector_store_unixcoder",
        )

        # Add sample code chunks
        chunks = create_sample_code_chunks()
        vector_store.add_code_chunks(chunks)

        print(f"Added {len(chunks)} code chunks to UniXcoder vector store")
        print(f"Vector store stats: {vector_store.get_stats()}")

        # Test search
        test_queries = [
            "fibonacci recursive function",
            "binary tree class",
            "sorting quicksort",
        ]

        for query in test_queries:
            print(f"\nSearching for: {query!r}")
            results = vector_store.search(query, top_k=5)  # Show all results

            for i, result in enumerate(results, 1):
                print(f"  {i}. {result.node_name} (score: {result.score:.4f})")
                print(f"     Type: {result.type}")

        print("✅ UniXcoder vector store test completed successfully!")

    except Exception as e:
        print(f"❌ HuggingFace test failed: {e}")
        print(
            "Note: You may need to install additional dependencies for HuggingFace embeddings"
        )


def test_with_real_code_chunks(httpie_cli_repo=None):
    """Test with real code chunks from the httpie CLI repository."""
    print("\n=== Testing with Real Code Chunks ===")

    try:
        repo_path = httpie_cli_repo or ensure_httpie_repo()
        sample_file = repo_path / "httpie" / "core.py"
        if not sample_file.exists():
            print(f"Target file not found: {sample_file}")
            return

        # Create chunker and chunk the file
        chunker = create_chunker("python")
        chunks = chunker.chunk_file(str(sample_file))

        if not chunks:
            print("No chunks generated from httpie/core.py")
            return

        print(f"Generated {len(chunks)} chunks from {sample_file}")

        # Convert chunks to dict format
        chunk_dicts = []
        for chunk in chunks:
            chunk_dict = {
                "content": chunk.content,
                "chunk_type": chunk.chunk_type,
                "name": chunk.name,
                "file": chunk.file,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "node_id": chunk.node_id,
            }
            chunk_dicts.append(chunk_dict)

        # Create vector store with UniXcoder embeddings
        vector_store = create_code_vector_store(
            embedding_model="microsoft/unixcoder-base",
            embedding_provider="huggingface",
            dimension=768,
            store_path="./test_vector_store_real_unixcoder",
        )

        # Add chunks
        vector_store.add_code_chunks(chunk_dicts)

        # Test search
        search_queries = [
            "raw_main",
            "Environment",
        ]

        for query in search_queries:
            print(f"\nSearching for: {query!r}")
            results = vector_store.search(query, top_k=3)

            for i, result in enumerate(results, 1):
                print(f"  {i}. {result.node_name} (score: {result.score:.4f})")
                print(
                    f"     Type: {result.type}, Lines: {result.start_line}-{result.end_line}"
                )

        print("✅ Real code chunks test completed successfully!")

    except Exception as e:
        print(f"❌ Real code chunks test failed: {e}")


def main():
    """Main test function."""
    print("Testing CodeVectorStore functionality...\n")

    # Test with sample chunks
    test_vector_store_with_huggingface()

    # Test with real code chunks
    test_with_real_code_chunks()

    print("\n🎉 All vector store tests completed!")


if __name__ == "__main__":
    main()
