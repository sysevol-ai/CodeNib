"""Test RegexNodeIndex functionality on a small Python repo.

Uses pallets/flask pinned to a tagged release. Flask was previously httpie/cli,
but scip-python crashes on httpie's dynamic-version setup.py
(``normalizeNameOrVersion`` TypeError in ScipSymbol.ts). Flask is small,
stable, and known-working with scip-python (see PR #120 perf table).
Each test gets a fresh clone under pytest's tmp factory to avoid the
"detected dubious ownership" error from cross-run /tmp reuse.
"""

import subprocess
from pathlib import Path

import pytest

from codeminer import RegexNodeIndex
from codeminer.ls_router import LSIndexer

pytestmark = pytest.mark.integration

FLASK_REPO_URL = "https://github.com/pallets/flask.git"
FLASK_REPO_TAG = "3.1.0"


@pytest.fixture(scope="module")
def flask_repo(tmp_path_factory) -> Path:
    """Shallow-clone pallets/flask at a pinned tag into a per-module tmp dir."""
    repo_path = tmp_path_factory.mktemp("flask_repo") / "flask"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            FLASK_REPO_TAG,
            FLASK_REPO_URL,
            str(repo_path),
        ],
        check=True,
    )
    return repo_path


def test_regex_index_basic(flask_repo: Path, tmp_path: Path):
    """Test RegexNodeIndex on a freshly-indexed flask checkout."""
    output_path = tmp_path / "flask_regex_out"
    output_path.mkdir(parents=True, exist_ok=True)

    indexer = LSIndexer(str(flask_repo), output_dir=str(output_path))
    code_graph = indexer.run_pipeline(
        project_name="flask_regex_test",
        skip_level="graph",
    )
    assert code_graph is not None, (
        "LSIndexer.run_pipeline returned None — likely an upstream scip-python "
        "indexing crash; check captured stderr for the actual error."
    )

    regex_idx = RegexNodeIndex(code_graph=code_graph)

    # Plain string search
    plain = regex_idx.search("Flask", use_regex=False)
    assert isinstance(plain, list)

    # Regex search constrained by file glob
    funcs = regex_idx.search(r"def\s+\w+", file_glob="*.py")
    assert isinstance(funcs, list)

    # File-glob filter
    classes_in_app = regex_idx.search("class", file_glob="*app.py", use_regex=False)
    assert isinstance(classes_in_app, list)

    # Type counts iterates without error
    type_counts: dict[str, int] = {}
    for node in regex_idx.nodes:
        type_counts[node.type] = type_counts.get(node.type, 0) + 1
    assert type_counts, "RegexNodeIndex.nodes was empty"

    # Case-sensitive vs insensitive returns >= same count
    cs = regex_idx.search("CLASS", case_sensitive=True, use_regex=False)
    ci = regex_idx.search("CLASS", case_sensitive=False, use_regex=False)
    assert len(ci) >= len(cs)
