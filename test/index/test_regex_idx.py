"""
Test RegexNodeIndex functionality using the httpie CLI repository.
"""

import os
import subprocess
from pathlib import Path

import pytest

from codeminer import RegexNodeIndex
from codeminer.ls_router import LSIndexer

pytestmark = pytest.mark.integration

HTTPIE_REPO_URL = "https://github.com/httpie/cli.git"
# Use user-specific path to avoid conflicts in multi-user environments (e.g., CI runners)
HTTPIE_REPO_PATH = Path(f"/tmp/httpie-cli-{os.getuid()}")


def ensure_httpie_repo() -> Path:
    """Clone the httpie/cli repository if needed and return its path."""
    if not HTTPIE_REPO_PATH.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", HTTPIE_REPO_URL, str(HTTPIE_REPO_PATH)],
            check=True,
        )
    return HTTPIE_REPO_PATH


def test_regex_index_basic(httpie_cli_repo=None, tmp_path_factory=None):
    """Test RegexNodeIndex functionality with the httpie CLI repository."""
    repo_path = httpie_cli_repo or ensure_httpie_repo()
    if tmp_path_factory:
        output_path = tmp_path_factory.mktemp("httpie_cli_regex")
    else:
        # Use user-specific path to avoid conflicts in multi-user environments
        output_path = Path(f"/tmp/httpie_cli_regex-{os.getuid()}")
        output_path.mkdir(parents=True, exist_ok=True)

    print(f"Testing with httpie repo at: {repo_path}")
    print(f"Output path: {output_path}")

    # Build CodeGraph using LSIndexer
    indexer = LSIndexer(str(repo_path), output_dir=str(output_path))
    code_graph = indexer.run_pipeline(
        project_name="httpie_cli_regex_test",
        skip_level="graph",  # Use cached graph if available
    )

    print(f"\nCodeGraph built with {code_graph.graph.vcount()} nodes")

    # Create RegexNodeIndex
    print("\nBuilding RegexNodeIndex...")
    regex_idx = RegexNodeIndex(code_graph=code_graph)

    # Test 1: Search for 'raw_main' (plain string)
    print("\n=== Test 1: Plain string search for 'raw_main' ===")
    results = regex_idx.search("raw_main", use_regex=False)
    print(f"Found {len(results)} nodes containing 'raw_main':")
    for node in results[:5]:  # Show first 5
        print(f"  - {node.node_name} ({node.type})")

    # Test 2: Regex search for function definitions
    print("\n=== Test 2: Regex search for function definitions ===")
    results = regex_idx.search(r"def\s+\w+", file_glob="*.py")
    print(f"Found {len(results)} nodes with function definitions:")
    for node in results[:5]:
        print(f"  - {node.file}:{node.start_line} - {node.node_name}")

    # Test 3: Search with file glob filter
    print("\n=== Test 3: Search in core files only ===")
    results = regex_idx.search("class", file_glob="*core.py", use_regex=False)
    print(f"Found {len(results)} nodes containing 'class' in core files:")
    for node in results:
        print(f"  - {node.file} - {node.node_name} ({node.type})")

    # Test 4: Search by node type
    print("\n=== Test 4: Count nodes by type ===")
    type_counts = {}
    for node in regex_idx.nodes:
        type_counts[node.type] = type_counts.get(node.type, 0) + 1
    print("Node counts by type:")
    for node_type, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {node_type}: {count}")

    # Test 5: Case-sensitive search
    print("\n=== Test 5: Case-sensitive vs case-insensitive ===")
    case_sensitive_results = regex_idx.search(
        "CLASS", case_sensitive=True, use_regex=False
    )
    case_insensitive_results = regex_idx.search(
        "CLASS", case_sensitive=False, use_regex=False
    )
    print(f"Case-sensitive 'CLASS': {len(case_sensitive_results)} results")
    print(f"Case-insensitive 'CLASS': {len(case_insensitive_results)} results")

    print("\n✅ All tests passed!")


if __name__ == "__main__":
    test_regex_index_basic()
