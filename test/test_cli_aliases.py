# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from codeminer.agent import CodeMinerAgentOptions, CodeNibAgentOptions
from codeminer.mcp.server import _parse_args


def _console_scripts() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["scripts"]


@pytest.mark.parametrize(
    ("legacy_name", "product_name"),
    (
        ("codeminer-mcp", "codenib-mcp"),
        ("codeminer-web", "codenib-web"),
    ),
)
def test_console_aliases_load_identical_implementations(
    legacy_name: str,
    product_name: str,
) -> None:
    scripts = _console_scripts()
    legacy = EntryPoint(
        name=legacy_name,
        value=scripts[legacy_name],
        group="console_scripts",
    ).load()
    product = EntryPoint(
        name=product_name,
        value=scripts[product_name],
        group="console_scripts",
    ).load()

    assert product is legacy


@pytest.mark.parametrize("program_name", ("codenib-mcp", "codeminer-mcp"))
def test_mcp_help_uses_invoked_command(
    program_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--help"], prog=program_name)

    assert exc_info.value.code == 0
    assert f"usage: {program_name}" in capsys.readouterr().out


def test_agent_options_product_alias_preserves_class_identity() -> None:
    assert CodeNibAgentOptions is CodeMinerAgentOptions
