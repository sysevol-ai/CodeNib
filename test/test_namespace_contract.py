# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

import codenib.agent as agent_api
from codenib.compiler.index_compiler import IndexCompilerConfig
from codenib.dataset.base import DatasetBase
from codenib.dataset.codenib_base import CodeNibBaseDataset
from codenib.dataset.codenib_synthesis import DEFAULT_DATASET
from codenib.mcp.server import mcp
from codenib.web.config import CACHE_DIR_NAME, QAConfig

_MCP_TOOL_NAMES = {
    "search_context",
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


def test_distribution_package_and_commands_use_codenib_only() -> None:
    project = _project_config()
    root = Path(__file__).resolve().parents[1]

    assert project["name"] == "codenib"
    assert not (root / ("code" + "miner")).exists()
    assert (root / "codenib").is_dir()
    assert all(
        name == "codenib" or name.startswith("codenib-") for name in project["scripts"]
    )
    assert all(target.startswith("codenib.") for target in project["scripts"].values())
    assert not hasattr(agent_api, "Code" + "MinerAgentOptions")


def test_runtime_state_roots_use_codenib(tmp_path: Path) -> None:
    base_dataset_default = (
        inspect.signature(CodeNibBaseDataset).parameters["dataset"].default
    )
    dataset = DatasetBase(root=str(tmp_path))
    qa_config = QAConfig()

    # These are immutable remote dataset addresses, not runtime aliases.
    assert base_dataset_default == "fishmingyu/" + "code" + "miner-base-dataset"
    assert DEFAULT_DATASET == "sysevol-ai/" + "code" + "miner-synthesis"
    assert IndexCompilerConfig().cache_dir_name == ".codenib_cache"
    assert CACHE_DIR_NAME == ".codenib_cache"
    assert qa_config.data_dir == ".codenib_qa"
    assert Path(dataset._resolve_root(None)).name == ".codenib"


def test_mcp_protocol_identifiers_use_codenib() -> None:
    async def identifiers() -> tuple[set[str], set[str]]:
        prompts = await mcp.list_prompts()
        tools = await mcp.list_tools()
        return (
            {prompt.name for prompt in prompts},
            {tool.name for tool in tools},
        )

    prompt_names, tool_names = asyncio.run(identifiers())

    assert mcp.name == "codenib"
    assert {"codenib-guide"} <= prompt_names
    assert _MCP_TOOL_NAMES <= tool_names
