# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Optional source-level probes for the pinned CoSIL policy contract."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codenib.clients import cosil_protocol
from codenib.integrations.cosil import COSIL_REVISION, get_cosil_tool_schemas

pytestmark = pytest.mark.integration


def _git_show(checkout: Path, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), "show", f"{COSIL_REVISION}:{path}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"{checkout} does not contain {path} at {COSIL_REVISION}")
    return result.stdout


def _static_assignments(source: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            values[target.id] = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            continue
    return values


@pytest.fixture(scope="module")
def upstream_sources() -> dict[str, str]:
    raw_checkout = os.environ.get("COSIL_CHECKOUT")
    if not raw_checkout:
        pytest.skip("set COSIL_CHECKOUT for the upstream CoSIL probe")
    checkout = Path(raw_checkout).expanduser().resolve()
    if not (checkout / ".git").exists():
        pytest.fail("COSIL_CHECKOUT must point to a Git checkout")
    return {
        "prompts": _git_show(checkout, "CoSIL/fl/FL_prompt.py"),
        "tools": _git_show(checkout, "CoSIL/fl/FL_tools.py"),
        "file_runner": _git_show(checkout, "CoSIL/fl/CoSIL_localize_file.py"),
        "function_runner": _git_show(
            checkout,
            "CoSIL/fl/CoSIL_localize_func.py",
        ),
    }


def test_vendored_tool_schemas_match_pinned_source(
    upstream_sources: dict[str, str],
) -> None:
    values = _static_assignments(upstream_sources["tools"])

    assert get_cosil_tool_schemas() == values["CoSIL_LOCATION_TOOL_SCHEMAS"]


def test_vendored_prompts_match_pinned_source(
    upstream_sources: dict[str, str],
) -> None:
    values = _static_assignments(upstream_sources["prompts"])
    expected = {
        "_BUG_REPORT": "bug_report_template",
        "_BUG_REPORT_NO_STRUCTURE": "bug_report_template_wo_repo_struct",
        "_FILE_SYSTEM": "file_system_prompt_without_tool",
        "_FILE_GUIDANCE": "file_guidence_prmpt_without_tool",
        "_FILE_SUMMARY": "file_summary",
        "_FORMAT_CORRECT": "format_correct_prompt",
        "_FILE_REFLECTION": "file_reflection_prompt",
        "_LOCATION_SYSTEM": "location_system_prompt",
        "_LOCATION_GUIDANCE": "location_guidence_prmpt",
        "_LOCATION_SUMMARY": "location_summary",
    }

    assert cosil_protocol.COSIL_PROTOCOL_REVISION == COSIL_REVISION
    for local_name, upstream_name in expected.items():
        assert getattr(cosil_protocol, local_name) == values[upstream_name]


def test_supported_boundary_matches_upstream_rq1_scripts(
    upstream_sources: dict[str, str],
) -> None:
    assert "file_localize_with_g" in upstream_sources["file_runner"]
    assert "localize_with_p" in upstream_sources["function_runner"]
