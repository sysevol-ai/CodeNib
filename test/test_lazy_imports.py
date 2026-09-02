# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_cli_and_static_wiki_do_not_import_optional_runtimes() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import codenib
import codenib.compiler.index_builders
import codenib.web.app

forbidden = {
    "faiss",
    "igraph",
    "litellm",
    "matplotlib",
    "sentence_transformers",
    "torch",
    "transformers",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit(f"optional runtimes imported eagerly: {loaded}")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_non_slow_collection_does_not_probe_ml_runtimes() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib.abc
import sys

import pytest

watched = {"sentence_transformers", "torch", "transformers"}
attempts = set()

class RuntimeProbe(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.partition(".")[0] in watched:
            attempts.add(fullname)
            raise ModuleNotFoundError(
                f"blocked ML runtime probe: {fullname}",
                name=fullname,
            )
        return None

probe = RuntimeProbe()
sys.meta_path.insert(0, probe)
try:
    exit_code = pytest.main(
        [
            "--collect-only",
            "-q",
            "-m",
            "not slow",
            "test/agent/test_embedding_search_e2e.py",
            "test/serving/test_hf_engine.py",
        ]
    )
finally:
    sys.meta_path.remove(probe)

allowed_exit_codes = {pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED}
if exit_code not in allowed_exit_codes:
    raise SystemExit(f"pytest collection failed with exit code {exit_code}")
if attempts:
    raise SystemExit(f"ML runtimes probed during collection: {sorted(attempts)}")
"""

    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_sparse_ask_runtime_does_not_require_semantic_or_graph_extras() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import time

for name in ("faiss", "igraph", "sentence_transformers"):
    sys.modules[name] = None
sys.modules["codenib.ops.rerank"] = None

from codenib.compiler.manifest import IndexEntry, RepoManifest
from codenib.web.config import QAConfig, RepoEntry
from codenib.web.repo_registry import RepoBundle, RepoRegistry

entry = RepoEntry(
    instance_id="sparse-repo",
    repo="sparse-repo",
    base_commit="abc123",
    language="python",
    repo_dir="/tmp/sparse-repo",
    manifest_path="/tmp/sparse-repo/repo_manifest.json",
)
manifest = RepoManifest(
    repo_path=entry.repo_dir,
    commit=entry.base_commit,
    languages=["python"],
    file_count=1,
    indexes={
        "bm25": IndexEntry(
            index_type="bm25",
            path="/tmp/sparse-repo/bm25",
            built_at="now",
            built_at_epoch=time.time(),
            status="fresh",
            commit=entry.base_commit,
        )
    },
)
bundle = RepoBundle(
    entry=entry,
    manifest=manifest,
    bm25=object(),
)
RepoRegistry(
    QAConfig(model="openai/fake-model", model_api_key="fake-key")
)._load_repo_runtime(bundle)

if bundle.runner is None:
    raise SystemExit("sparse Ask runtime was not created")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_retrieval_planner_export_does_not_import_pipeline_extras() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import sys

for name in ("faiss", "igraph", "litellm", "sentence_transformers"):
    sys.modules[name] = None

from codenib.model import RetrievalPlanner
from codenib.agent.runtime import RepositoryContextExplorer

if RetrievalPlanner.__name__ != "RetrievalPlanner":
    raise SystemExit("planner export did not resolve")
if RepositoryContextExplorer.__name__ != "RepositoryContextExplorer":
    raise SystemExit("explorer export did not resolve")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_agent_runner_packages_do_not_import_optional_runtimes() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import sys

for name in ("faiss", "igraph", "litellm", "sentence_transformers"):
    sys.modules[name] = None

import codenib.eval.agent_runner
import codenib.graph.incremental

if "codenib.eval.agent_runner.live_lsp_provider" in sys.modules:
    raise SystemExit("agent-runner implementation imported eagerly")
if "codenib.graph.incremental.graph_patcher" in sys.modules:
    raise SystemExit("incremental graph patcher imported eagerly")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
