# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_cli_and_static_wiki_do_not_import_optional_runtimes() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import codenib
import codenib.web.app

forbidden = {
    "faiss",
    "igraph",
    "litellm",
    "matplotlib",
    "sentence_transformers",
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
