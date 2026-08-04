# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Optional source-level probe for the pinned Agentless v1.5.0 contract."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from codenib.clients import agentless_protocol
from codenib.integrations.agentless import AGENTLESS_REVISION, AGENTLESS_VERSION

pytestmark = pytest.mark.integration

_FL_SOURCE = "agentless/fl/FL.py"


def _git_show(checkout: Path, revision: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "show", f"{revision}:{path}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"{checkout} does not contain {path} at {revision}")
    return result.stdout


def _class_string_assignments(source: str, class_name: str) -> dict[str, str]:
    tree = ast.parse(source)
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    values: dict[str, str] = {}
    for node in target.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        name = node.targets[0]
        if not isinstance(name, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
        if isinstance(value, str):
            values[name.id] = value
    return values


@pytest.fixture(scope="module")
def upstream_agentless() -> tuple[Path, str]:
    raw_checkout = os.environ.get("AGENTLESS_CHECKOUT")
    if not raw_checkout:
        pytest.skip("set AGENTLESS_CHECKOUT for the upstream Agentless probe")
    checkout = Path(raw_checkout).expanduser().resolve()
    if not (checkout / ".git").exists():
        pytest.fail("AGENTLESS_CHECKOUT must point to a Git checkout")
    return checkout, _git_show(checkout, AGENTLESS_REVISION, _FL_SOURCE)


def test_pinned_revision_is_classic_release(
    upstream_agentless: tuple[Path, str],
) -> None:
    checkout, _source = upstream_agentless
    result = subprocess.run(
        ["git", "-C", str(checkout), "tag", "--points-at", AGENTLESS_REVISION],
        check=True,
        capture_output=True,
        text=True,
    )

    assert AGENTLESS_VERSION in result.stdout.splitlines()


def test_vendored_prompts_match_pinned_agentless_source(
    upstream_agentless: tuple[Path, str],
) -> None:
    _checkout, source = upstream_agentless
    values = _class_string_assignments(source, "LLMFL")

    assert agentless_protocol.AGENTLESS_PROTOCOL_REVISION == AGENTLESS_REVISION
    assert agentless_protocol._FILE_PROMPT == values["obtain_relevant_files_prompt"]
    assert (
        agentless_protocol._SYMBOL_PROMPT
        == values[
            "obtain_relevant_functions_and_vars_from_compressed_files_prompt_more"
        ]
    )
    assert (
        agentless_protocol._LINE_PROMPT
        == values["obtain_relevant_code_combine_top_n_prompt"]
    )
