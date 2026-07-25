# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
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

from codenib.agent import CodeNibAgentOptions
from codenib.mcp.server import _parse_args as parse_mcp_args
from codenib.web.app import _parse_args as parse_web_args


def _project_config() -> dict:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _console_scripts() -> dict[str, str]:
    return _project_config()["project"]["scripts"]


def test_console_scripts_use_only_codenib_namespace() -> None:
    scripts = _console_scripts()

    assert scripts
    assert all(name == "codenib" or name.startswith("codenib-") for name in scripts)
    assert all(target.startswith("codenib.") for target in scripts.values())


@pytest.mark.parametrize("program_name", ("codenib-mcp",))
def test_mcp_help_uses_invoked_command(
    program_name: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_mcp_args(["--help"], prog=program_name)

    assert exc_info.value.code == 0
    assert f"usage: {program_name}" in capsys.readouterr().out


def test_web_help_uses_codenib_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_web_args(["--help"])

    assert exc_info.value.code == 0
    assert "usage: codenib-web" in capsys.readouterr().out


def test_console_entry_points_load() -> None:
    for name, value in _console_scripts().items():
        entry_point = EntryPoint(name=name, value=value, group="console_scripts")
        assert callable(entry_point.load())


def test_agent_options_has_only_product_name() -> None:
    assert CodeNibAgentOptions.__name__ == "CodeNibAgentOptions"


def test_wheel_includes_skill_definitions() -> None:
    package_data = _project_config()["tool"]["setuptools"]["package-data"]

    assert package_data["codenib.agent.skills"] == [
        "*/config.yaml",
        "*/skill.md",
    ]
