# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import inspect
import json
import pickle
from pathlib import Path

import numpy as np

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from codeminer.compiler.index_compiler import IndexCompilerConfig
from codeminer.compiler.manifest import MANIFEST_VERSION, RepoManifest
from codeminer.dataset.base import DatasetBase
from codeminer.dataset.codeminer_base import CodeMinerBaseDataset
from codeminer.dataset.codeminer_synthesis import DEFAULT_DATASET
from codeminer.graph.code_graph import _SCHEMA_VERSION, CodeGraph
from codeminer.index.incremental.embeddings_cache import EmbeddingsCache
from codeminer.mcp.server import mcp

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


def test_artifact_names_versions_and_cache_roots_remain_stable(tmp_path: Path) -> None:
    base_dataset_default = (
        inspect.signature(CodeMinerBaseDataset).parameters["dataset"].default
    )
    dataset = DatasetBase(root=str(tmp_path))

    assert base_dataset_default == "fishmingyu/codeminer-base-dataset"
    assert DEFAULT_DATASET == "sysevol-ai/codeminer-synthesis"
    assert MANIFEST_VERSION == "1.0"
    assert _SCHEMA_VERSION == 4
    assert IndexCompilerConfig().cache_dir_name == ".codeminer_cache"
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
    assert prompt_names == {"codeminer-guide"}
    assert tool_names == _MCP_TOOL_NAMES


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
    graph = CodeGraph(project_root="/repo")
    graph.add_file_node("module.py")
    graph.add_symbol_node("module.py:run", 0, 0, 1, "function")
    graph.build_range_indexes()
    path = tmp_path / "graph.pkl"
    graph.save_graph(path)
    before = path.read_bytes()

    loaded = CodeGraph.load_graph(path)

    assert path.read_bytes() == before
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
