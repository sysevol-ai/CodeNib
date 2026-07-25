# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import pickle
from importlib.metadata import EntryPoint
from pathlib import Path

import numpy as np

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from codeminer.compiler.index_compiler import IndexCompilerConfig
from codeminer.compiler.manifest import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    RepoManifest,
)
from codeminer.dataset.base import DatasetBase
from codeminer.dataset.codeminer_base import CodeMinerBaseDataset
from codeminer.dataset.codeminer_synthesis import DEFAULT_DATASET
from codeminer.graph.code_graph import _SCHEMA_VERSION, CodeGraph
from codeminer.index.incremental.embeddings_cache import EmbeddingsCache
from codeminer.index.incremental.state import _STATE_FILENAME, IncrementalState
from codeminer.ls_index.lsp_indexer import GenericLSPIndexer
from codeminer.mcp.server import mcp
from codeminer.web.config import CACHE_DIR_NAME, REGISTRY_FILENAME, QAConfig

_LEGACY_SCRIPTS = {
    "codeminer-mcp": "codeminer.mcp.server:main",
    "codeminer-web": "codeminer.web.app:main",
    "codeminer-lsp-provider-validate": (
        "codeminer.eval.agent_runner.lsp_provider_cli:main"
    ),
    "codeminer-lsp-replay-benchmark": (
        "codeminer.eval.agent_runner.lsp_replay_benchmark:main"
    ),
    "codeminer-prebuilt-normalize-graphs": (
        "codeminer.eval.agent_runner.prebuilt:main"
    ),
    "codeminer-lsp-agent-ab": "codeminer.eval.agent_runner.lsp_agent_ab:main",
    "codeminer-lsp-provider-protocol-check": (
        "codeminer.eval.agent_runner.lsp_agent_ab:main"
    ),
    "codeminer-lsp-agent-study-manifest": (
        "codeminer.eval.agent_runner.lsp_agent_study_manifest:main"
    ),
    "codeminer-lsp-agent-study-artifacts": (
        "codeminer.eval.agent_runner.lsp_agent_study_artifacts:main"
    ),
    "codeminer-lsp-agent-study-run": (
        "codeminer.eval.agent_runner.lsp_agent_study_runner:main"
    ),
    "codeminer-artifact-bundle": "codeminer.eval.artifact_bundle:main",
}

_MCP_TOOL_NAMES = {
    "search_semantic",
    "search_bm25",
    "search_regex",
    "search_zoekt",
    "dependency_subgraph",
    "lsp_definition",
    "lsp_references",
    "lsp_route",
    "get_manifest",
}


def _project_config() -> dict:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_legacy_distribution_and_commands_remain_installed() -> None:
    project = _project_config()

    assert project["name"] == "codeminer"
    for command, target in _LEGACY_SCRIPTS.items():
        assert project["scripts"][command] == target
        entry_point = EntryPoint(name=command, value=target, group="console_scripts")
        assert callable(entry_point.load())


def test_artifact_names_versions_and_cache_roots_remain_stable(tmp_path: Path) -> None:
    base_dataset_default = (
        inspect.signature(CodeMinerBaseDataset).parameters["dataset"].default
    )
    dataset = DatasetBase(root=str(tmp_path))
    incremental_state = IncrementalState()
    lsp_indexer = GenericLSPIndexer(
        project_root=tmp_path / "repo",
        output_dir=tmp_path / "lsp-index",
    )
    qa_config = QAConfig()

    assert base_dataset_default == "fishmingyu/codeminer-base-dataset"
    assert DEFAULT_DATASET == "sysevol-ai/codeminer-synthesis"
    assert MANIFEST_FILENAME == "repo_manifest.json"
    assert MANIFEST_VERSION == "1.0"
    assert _SCHEMA_VERSION == 4
    assert lsp_indexer.graph_file.name == "graph.pkl"
    assert _STATE_FILENAME == "incremental_state.json"
    assert incremental_state.chunk_store_path == "chunk_store.pkl"
    assert incremental_state.embeddings_cache_path == "embeddings_cache.pkl"
    assert IndexCompilerConfig().cache_dir_name == ".codeminer_cache"
    assert CACHE_DIR_NAME == ".codeminer_cache"
    assert qa_config.data_dir == ".codeminer_qa"
    assert REGISTRY_FILENAME == "qa_registry.json"
    assert Path(dataset._resolve_root(None)).name == ".codeminer"


def test_mcp_protocol_identifiers_remain_stable() -> None:
    async def identifiers() -> tuple[set[str], set[str]]:
        prompts = await mcp.list_prompts()
        tools = await mcp.list_tools()
        return (
            {prompt.name for prompt in prompts},
            {tool.name for tool in tools},
        )

    prompt_names, tool_names = asyncio.run(identifiers())

    assert mcp.name == "codeminer"
    assert {"codeminer-guide"} <= prompt_names
    assert _MCP_TOOL_NAMES <= tool_names


def test_pre_brand_manifest_load_is_read_only(tmp_path: Path) -> None:
    payload = {
        "version": "1.0",
        "repo": {
            "path": "/repo",
            "commit": "abc123",
            "last_indexed_commit": "abc123",
            "languages": ["python"],
            "file_count": 1,
        },
        "indexes": {},
        "capabilities": {"sparse_search": False},
        "compiled_at": "2026-01-01T00:00:00Z",
        "compiled_at_epoch": 1767225600.0,
    }
    path = tmp_path / "repo_manifest.json"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()

    manifest = RepoManifest.load(path)

    assert path.read_bytes() == before
    assert manifest.to_dict() == payload


def test_graph_pickle_load_is_read_only(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parent
    fixture_path = root / "fixtures/branding/pre_brand_graph_schema4.fixture"
    fixture = json.loads(fixture_path.read_text())
    payload = base64.b64decode(fixture["payload_base64"], validate=True)

    assert fixture["source_commit"] == "e1eb03c84372e9b9d26ee113d202c66e2f725ec8"
    assert fixture["schema_version"] == 4
    assert hashlib.sha256(payload).hexdigest() == fixture["sha256"]

    path = tmp_path / "graph.pkl"
    path.write_bytes(payload)
    before = path.read_bytes()

    loaded = CodeGraph.load_graph(path)

    assert path.read_bytes() == before
    assert str(loaded.project_root) == "/legacy/repository"
    assert "module.py:run" in loaded.name_to_vertex


def test_legacy_embedding_pickle_load_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "embeddings_cache.pkl"
    expected = np.array([0.25, 0.75], dtype=np.float32)
    with path.open("wb") as handle:
        pickle.dump({"content-hash": expected}, handle)
    before = path.read_bytes()

    loaded = EmbeddingsCache.load(path)

    assert path.read_bytes() == before
    np.testing.assert_array_equal(loaded.get("content-hash"), expected)
