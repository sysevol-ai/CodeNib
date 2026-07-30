# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Optional source-level probe for the pinned LocAgent/OpenHands contracts."""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from codenib.integrations.locagent import (
    LOCAGENT_REVISION,
    OPENHANDS_LOCAGENT_REVISION,
    LocAgentToolProvider,
    get_locagent_tool_schemas,
)

pytestmark = pytest.mark.integration

_LOCAGENT_REPO_OPS = "plugins/location_tools/repo_ops/repo_ops.py"
_OPENHANDS_TOOL_FILES = (
    "openhands/agenthub/loc_agent/tools/search_content.py",
    "openhands/agenthub/loc_agent/tools/explore_structure.py",
)
_LOCAGENT_FUNCTIONS = (
    "search_code_snippets",
    "get_entity_contents",
    "explore_graph_structure",
    "explore_tree_structure",
)


def _git_show(checkout: Path, revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "show", f"{revision}:{path}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"{checkout} does not contain {path} at pinned revision {revision}")
    return result.stdout


@pytest.fixture(scope="module")
def upstream_sources() -> SimpleNamespace:
    raw_locagent = os.environ.get("LOCAGENT_CHECKOUT")
    raw_openhands = os.environ.get("OPENHANDS_CHECKOUT")
    if not raw_locagent or not raw_openhands:
        pytest.skip(
            "set LOCAGENT_CHECKOUT and OPENHANDS_CHECKOUT for the upstream probe"
        )
    locagent = Path(raw_locagent).expanduser().resolve()
    openhands = Path(raw_openhands).expanduser().resolve()
    if not (locagent / ".git").exists() or not (openhands / ".git").exists():
        pytest.fail("upstream probe paths must be Git checkouts")

    return SimpleNamespace(
        locagent_repo_ops=_git_show(
            locagent,
            LOCAGENT_REVISION,
            _LOCAGENT_REPO_OPS,
        ),
        openhands_tools=[
            _git_show(openhands, OPENHANDS_LOCAGENT_REVISION, path)
            for path in _OPENHANDS_TOOL_FILES
        ],
    )


def _function_signatures(source: str) -> dict[str, dict[str, Any]]:
    tree = ast.parse(source)
    signatures = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in _LOCAGENT_FUNCTIONS:
            continue
        parameters = [argument.arg for argument in node.args.args]
        defaults = {}
        offset = len(parameters) - len(node.args.defaults)
        for parameter, default in zip(
            parameters[offset:],
            node.args.defaults,
            strict=True,
        ):
            defaults[parameter] = ast.literal_eval(default)
        signatures[node.name] = {
            "parameters": parameters,
            "defaults": defaults,
        }
    return signatures


def _provider_signatures(provider: LocAgentToolProvider) -> dict[str, dict[str, Any]]:
    signatures = {}
    for name in _LOCAGENT_FUNCTIONS:
        signature = inspect.signature(getattr(provider, name))
        signatures[name] = {
            "parameters": list(signature.parameters),
            "defaults": {
                parameter.name: parameter.default
                for parameter in signature.parameters.values()
                if parameter.default is not inspect.Parameter.empty
            },
        }
    return signatures


def _dict_items(node: ast.AST) -> dict[str, ast.AST]:
    if not isinstance(node, ast.Dict):
        raise AssertionError(f"expected dictionary, found {ast.dump(node)}")
    return {
        str(ast.literal_eval(key)): value
        for key, value in zip(node.keys, node.values, strict=True)
        if key is not None
    }


def _parameter_shape(
    node: ast.AST,
    assignments: dict[str, ast.AST],
) -> dict[str, Any]:
    if isinstance(node, ast.Name):
        node = assignments[node.id]
    parameters = _dict_items(node)
    properties = _dict_items(parameters["properties"])
    return {
        "required": ast.literal_eval(parameters["required"]),
        "properties": {
            name: ast.literal_eval(_dict_items(specification)["type"])
            for name, specification in properties.items()
        },
    }


def _openhands_tool_contract(sources: list[str]) -> dict[str, dict[str, Any]]:
    tools = {}
    for source in sources:
        tree = ast.parse(source)
        assignments = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            function_name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else getattr(call.func, "attr", "")
            )
            if function_name != "ChatCompletionToolParamFunctionChunk":
                continue
            keywords = {
                keyword.arg: keyword.value
                for keyword in call.keywords
                if keyword.arg is not None
            }
            name_node = keywords.get("name")
            parameters_node = keywords.get("parameters")
            if not isinstance(name_node, ast.Constant) or parameters_node is None:
                continue
            name = str(name_node.value)
            if name in {
                "search_code_snippets",
                "get_entity_contents",
                "explore_tree_structure",
            }:
                tools[name] = _parameter_shape(parameters_node, assignments)
    return dict(sorted(tools.items()))


def test_original_locagent_function_signatures(
    upstream_sources: SimpleNamespace,
    integration_manifest: Path,
) -> None:
    provider = LocAgentToolProvider.from_manifest(integration_manifest)

    assert _provider_signatures(provider) == _function_signatures(
        upstream_sources.locagent_repo_ops
    )


def test_openhands_function_schemas_match_frozen_contract(
    upstream_sources: SimpleNamespace,
) -> None:
    contract_path = (
        Path(__file__).parent / "contracts" / "locagent_openhands_efe287c.yaml"
    )
    expected = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    current = {
        schema["function"]["name"]: {
            "required": schema["function"]["parameters"].get("required", []),
            "properties": {
                name: specification["type"]
                for name, specification in schema["function"]["parameters"][
                    "properties"
                ].items()
            },
        }
        for schema in get_locagent_tool_schemas()
    }

    assert (
        _openhands_tool_contract(upstream_sources.openhands_tools) == expected["tools"]
    )
    assert dict(sorted(current.items())) == expected["tools"]
