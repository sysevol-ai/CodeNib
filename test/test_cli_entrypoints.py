# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from codenib import _optional_entrypoints
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


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_web_entrypoint_rejects_invalid_tcp_port(port: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_web_args(["--port", port])

    assert exc_info.value.code == 2


def test_web_entrypoint_validates_environment_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODENIB_DEMO_PORT", "70000")

    with pytest.raises(SystemExit) as exc_info:
        parse_web_args([])

    assert exc_info.value.code == 2


def test_console_entry_points_load() -> None:
    for name, value in _console_scripts().items():
        entry_point = EntryPoint(name=name, value=value, group="console_scripts")
        assert callable(entry_point.load())


def test_optional_entrypoint_help_is_available_without_extra(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = ModuleNotFoundError("No module named 'igraph'", name="igraph")

    def raise_missing(_module: str) -> None:
        raise missing

    monkeypatch.setattr(
        _optional_entrypoints,
        "_load_main",
        raise_missing,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["codenib-lsp-provider-validate", "--help"],
    )

    result = _optional_entrypoints.lsp_provider_validate_main()

    assert result == 0
    output = capsys.readouterr()
    assert "usage: codenib-lsp-provider-validate" in output.out
    assert "pip install 'codenib[graph]" in output.out
    assert output.err == ""


def test_optional_entrypoint_reports_missing_extra_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = ModuleNotFoundError("No module named 'mcp'", name="mcp")

    def raise_missing(_module: str) -> None:
        raise missing

    monkeypatch.setattr(
        _optional_entrypoints,
        "_load_main",
        raise_missing,
    )
    monkeypatch.setattr("sys.argv", ["codenib-mcp", "manifest.json"])

    result = _optional_entrypoints.mcp_main()

    assert result == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "codenib-mcp: error:" in output.err
    assert "pip install 'codenib[mcp]" in output.err
    assert "Traceback" not in output.err


def test_optional_entrypoint_handles_runtime_optional_import(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = ModuleNotFoundError("No module named 'igraph'", name="igraph")

    def fail_during_main() -> None:
        raise missing

    monkeypatch.setattr(
        _optional_entrypoints,
        "_load_main",
        lambda _module: fail_during_main,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["codenib-lsp-provider-validate", "requests.json"],
    )

    assert _optional_entrypoints.lsp_provider_validate_main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "missing: igraph" in output.err
    assert "pip install 'codenib[graph]" in output.err
    assert "Traceback" not in output.err


def test_optional_entrypoint_delegates_when_extra_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _optional_entrypoints,
        "import_module",
        lambda _module: SimpleNamespace(main=lambda: 17),
    )

    assert _optional_entrypoints.mcp_main() == 17


def test_optional_entrypoint_does_not_hide_internal_import_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = ModuleNotFoundError(
        "No module named 'codenib.missing'",
        name="codenib.missing",
    )

    def raise_missing(_module: str) -> None:
        raise missing

    monkeypatch.setattr(
        _optional_entrypoints,
        "_load_main",
        raise_missing,
    )

    with pytest.raises(ModuleNotFoundError, match="codenib.missing"):
        _optional_entrypoints.mcp_main()


def test_optional_entrypoint_reports_the_invoked_alias(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = ModuleNotFoundError("No module named 'igraph'", name="igraph")

    def raise_missing(_module: str) -> None:
        raise missing

    monkeypatch.setattr(_optional_entrypoints, "_load_main", raise_missing)
    monkeypatch.setattr(
        "sys.argv",
        ["codenib-lsp-provider-protocol-check", "--help"],
    )

    assert _optional_entrypoints.lsp_agent_ab_main() == 0
    assert "usage: codenib-lsp-provider-protocol-check" in capsys.readouterr().out


def test_agent_options_has_only_product_name() -> None:
    assert CodeNibAgentOptions.__name__ == "CodeNibAgentOptions"


def test_wheel_includes_skill_definitions() -> None:
    package_data = _project_config()["tool"]["setuptools"]["package-data"]

    assert package_data["codenib.agent.skills"] == [
        "*/config.yaml",
        "*/skill.md",
    ]
