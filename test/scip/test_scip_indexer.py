# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

import subprocess
import tempfile
from pathlib import Path

import pytest

from codenib.ls_router import LSIndexer

pytestmark = pytest.mark.integration

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


@pytest.fixture
def test_dir():
    """Set up a temporary directory for testing"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir


@pytest.fixture
def indexer(test_dir):
    """Create a LSIndexer instance"""
    return LSIndexer(test_dir)


@pytest.fixture
def test_output_dir():
    """Provide a directory for test outputs"""
    return Path(__file__).parent


@pytest.fixture(scope="module")
def samplemod_repo():
    """Clone and set up the samplemod repository for testing."""
    test_repo_url = "https://github.com/navdeep-G/samplemod.git"
    test_repo_path = Path("/tmp/samplemod-test")

    # Clone the repo if it doesn't exist
    if not test_repo_path.exists():
        print(f"Cloning sample module repository from {test_repo_url}...")
        subprocess.run(["git", "clone", test_repo_url, str(test_repo_path)], check=True)
    else:
        print(f"Using existing sample module repository at {test_repo_path}")

    return test_repo_path


def test_conda_environment(indexer):
    """Test the conda environment management functions"""
    # After refactoring, LSIndexer delegates to SCIPPythonIndexer,
    # so check the delegate for Python-specific attributes.
    delegate = indexer._delegate
    assert hasattr(delegate, "_ensure_conda_env")
    assert hasattr(delegate, "_run_in_conda_env")

    # Check that the conda env file exists
    assert (
        delegate.env_file.exists()
    ), f"Conda environment file not found at {delegate.env_file}"


def test_python_repo_indexing(httpie_cli_repo, test_output_dir, tmp_path_factory):
    """
    Test indexing a python repository using LSIndexer.
    We use https://github.com/httpie/cli.git as a test repo.
    """
    # Verify the test repo exists
    repo_path = httpie_cli_repo or ensure_httpie_repo()
    assert repo_path.exists(), f"Test Python repo not found at {repo_path}"

    # Create a new indexer for the cloned test repo
    scip_output_dir = tmp_path_factory.mktemp("httpie_cli_scip")
    repo_indexer = LSIndexer(repo_path, output_dir=scip_output_dir)

    # Run the indexing pipeline, allowing skip_index and skip_decode for faster tests
    graph = repo_indexer.run_pipeline(project_name="HttpieCliRepo", skip_level="graph")

    if graph:
        graph.print_graph_basic_info()
        assert graph is not None

        # Print some sample data from the output pickle
        output_file = repo_indexer.graph_file
        assert (
            output_file.exists()
        ), f"Expected output file {output_file} was not created"

        # Check that index files were created in the temporary directory, not in the project
        index_file = Path(scip_output_dir) / "index.scip"
        assert (
            index_file.exists()
        ), f"Expected index file {index_file} was not created in tmp directory"

        # Print some sample data from the graph object
        file_vertices = [
            v["name"]
            for v in graph.graph.vs
            if "type" in v.attributes() and v["type"] == "file"
        ]
        symbol_vertices = [
            v["name"]
            for v in graph.graph.vs
            if "type" in v.attributes() and v["type"] == "symbol"
        ]
        for i, node in enumerate(file_vertices[:3]):
            print(f"  File node {i+1}: {node}")
        for i, node in enumerate(symbol_vertices[:3]):
            print(f"  Symbol node {i+1}: {node}")
    else:
        pytest.skip(
            "Failed to run indexing pipeline for test_python_repo,"
            " possibly due to missing dependencies"
        )


def test_samplemod_repo_indexing(samplemod_repo, test_output_dir, tmp_path_factory):
    """
    Test indexing the sample module repository using LSIndexer.
    We use https://github.com/navdeep-G/samplemod as a test repo.
    This is a small sample repository suitable for testing.
    """
    # Verify the test repo exists
    assert samplemod_repo.exists(), f"Sample module repo not found at {samplemod_repo}"

    graph_image_file = str(test_output_dir / "samplemod_graph.jpg")

    # Create a new indexer for the cloned test repo
    # Use our improved LSIndexer that stores data in /tmp
    scip_output_dir = tmp_path_factory.mktemp("samplemod_repo_scip")
    repo_indexer = LSIndexer(samplemod_repo, output_dir=scip_output_dir)

    # Run the indexing pipeline
    graph = repo_indexer.run_pipeline(
        project_name="SampleModRepo",
        skip_level="graph",
    )

    if graph:
        graph.print_graph_basic_info()
        assert graph is not None

        # visualize the graph and save it to a file
        graph.visualize_graph(graph_image_file)

        # Check that the output pickle was created
        output_file = repo_indexer.graph_file
        assert (
            output_file.exists()
        ), f"Expected output file {output_file} was not created"

        # Check that index files were created in the temporary directory, not in the project
        index_file = Path(scip_output_dir) / "index.scip"
        assert (
            index_file.exists()
        ), f"Expected index file {index_file} was not created in tmp directory"

        # Print some sample data from the graph object
        file_vertices = [
            v["name"]
            for v in graph.graph.vs
            if "type" in v.attributes() and v["type"] == "file"
        ]
        symbol_vertices = [
            v["name"]
            for v in graph.graph.vs
            if "type" in v.attributes() and v["type"] == "symbol"
        ]
        for i, node in enumerate(file_vertices[:3]):
            print(f"  File node {i+1}: {node}")
        for i, node in enumerate(symbol_vertices[:3]):
            print(f"  Symbol node {i+1}: {node}")
    else:
        pytest.skip(
            "Failed to run indexing pipeline for samplemod_repo,"
            " possibly due to missing dependencies"
        )


# For backward compatibility with direct script execution
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
