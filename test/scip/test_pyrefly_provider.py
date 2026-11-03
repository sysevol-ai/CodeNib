# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Which provider builds the Python graph, and what the build records about it."""

import os
from pathlib import Path

import pytest

from codenib.scip_interface.pyrefly_provider import (
    PYREFLY_EXECUTABLE_ENV,
    PYTHON_GRAPH_PROVIDER_ENV,
    select_python_graph_provider,
)
from codenib.scip_interface.scip_indexer_python import (
    SCIPPythonIndexer,
    _scip_tool_name,
)
from codenib.scip_interface.scip_pb2 import Index
from codenib.toolchains import PYREFLY_VERSION, SCIP_PYTHON_VERSION


def _fake_pyrefly(tmp_path: Path, version: str) -> str:
    script = tmp_path / f"pyrefly-{version}"
    script.write_text(f'#!/bin/sh\necho "pyrefly {version}"\n')
    script.chmod(0o755)
    return os.fspath(script)


@pytest.fixture
def environ(monkeypatch):
    monkeypatch.delenv(PYTHON_GRAPH_PROVIDER_ENV, raising=False)
    monkeypatch.delenv(PYREFLY_EXECUTABLE_ENV, raising=False)
    return monkeypatch


def test_pyrefly_is_the_default_when_its_pinned_release_is_installed(tmp_path, environ):
    executable = _fake_pyrefly(tmp_path, PYREFLY_VERSION)

    provider = select_python_graph_provider(command_resolver=lambda _: executable)

    assert provider.name == "pyrefly"
    assert provider.executable == executable
    assert not provider.configured
    assert provider.identity == f"pyrefly {PYREFLY_VERSION}"


@pytest.mark.parametrize("installed", [None, "1.5.0"])
def test_default_falls_back_to_scip_python_without_the_pinned_release(
    tmp_path, environ, installed
):
    executable = _fake_pyrefly(tmp_path, installed) if installed else None

    provider = select_python_graph_provider(command_resolver=lambda _: executable)

    assert provider.name == "scip-python"
    assert provider.ready
    assert provider.identity == f"scip-python {SCIP_PYTHON_VERSION}"
    assert (installed or "not installed") in provider.note


def test_environment_pins_the_provider(tmp_path, environ):
    environ.setenv(PYTHON_GRAPH_PROVIDER_ENV, "Scip-Python")
    executable = _fake_pyrefly(tmp_path, PYREFLY_VERSION)
    pinned = select_python_graph_provider(command_resolver=lambda _: executable)
    assert (pinned.name, pinned.configured) == ("scip-python", True)

    # A pinned pyrefly that cannot run is reported, never swapped for scip-python.
    environ.setenv(PYTHON_GRAPH_PROVIDER_ENV, "pyrefly")
    missing = select_python_graph_provider(command_resolver=lambda _: None)
    assert (missing.name, missing.configured, missing.ready) == (
        "pyrefly",
        True,
        False,
    )

    environ.setenv(PYTHON_GRAPH_PROVIDER_ENV, "ty")
    with pytest.raises(ValueError):
        select_python_graph_provider()


def test_executable_override_wins_over_the_resolver(tmp_path, environ):
    executable = _fake_pyrefly(tmp_path, PYREFLY_VERSION)
    environ.setenv(PYREFLY_EXECUTABLE_ENV, executable)

    provider = select_python_graph_provider(command_resolver=lambda _: None)

    assert provider.executable == executable


def test_missing_pinned_pyrefly_reports_an_unavailable_provider(tmp_path, environ):
    environ.setenv(PYTHON_GRAPH_PROVIDER_ENV, "pyrefly")
    environ.setenv(PYREFLY_EXECUTABLE_ENV, os.fspath(tmp_path / "missing"))
    (tmp_path / "repo").mkdir()

    indexer = SCIPPythonIndexer(tmp_path / "repo", output_dir=tmp_path / "out")

    assert indexer.generate_index() is False
    report = indexer.index_generation_report
    assert (report["backend"], report["status"]) == ("pyrefly", "unavailable")
    assert "not installed" in report["detail"]


def test_default_pyrefly_failure_falls_back_to_scip_python(tmp_path, environ):
    environ.setenv(PYREFLY_EXECUTABLE_ENV, _fake_pyrefly(tmp_path, PYREFLY_VERSION))
    (tmp_path / "repo").mkdir()
    indexer = SCIPPythonIndexer(tmp_path / "repo", output_dir=tmp_path / "out")
    assert indexer.graph_provider == "pyrefly"
    calls = []

    def pyrefly_fails(executable, **kwargs):
        calls.append("pyrefly")
        indexer.index_generation_report = {"backend": "pyrefly", "status": "failed"}
        return False

    def scip_python_builds(**kwargs):
        calls.append("scip-python")
        indexer.index_generation_report = {"backend": "scip-python"}
        return True

    environ.setattr(indexer, "_generate_pyrefly_index", pyrefly_fails)
    environ.setattr(indexer, "_generate_scip_python_index", scip_python_builds)

    assert indexer.generate_index() is True
    assert calls == ["pyrefly", "scip-python"]
    assert indexer.graph_provider == "scip-python"
    assert indexer.index_generation_report["fallback_from"] == {
        "provider": "pyrefly",
        "reason": "pyrefly failed",
    }


def test_recovery_follows_the_tool_named_in_the_index(tmp_path):
    for tool in ("pyrefly", "scip-python"):
        index = Index()
        index.metadata.tool_info.name = tool
        index.metadata.project_root = "file:///repo"
        index.documents.add().relative_path = "a.py"
        path = tmp_path / f"{tool}.scip"
        path.write_bytes(index.SerializeToString())
        assert _scip_tool_name(path) == tool
    (tmp_path / "garbage.scip").write_bytes(b"\x12\x05hello")
    assert _scip_tool_name(tmp_path / "garbage.scip") == ""
    assert _scip_tool_name(tmp_path / "absent.scip") == ""


def _installed_pyrefly():
    provider = select_python_graph_provider(environ={})
    return provider.executable if provider.name == "pyrefly" else None


@pytest.mark.skipif(
    _installed_pyrefly() is None,
    reason=f"pyrefly {PYREFLY_VERSION} is not installed",
)
def test_default_build_uses_pyrefly_and_keeps_only_the_index(tmp_path, environ):
    # Under a hidden directory, as CodeNib's own clones are (~/.codenib/repos).
    root = tmp_path / ".codenib" / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text("def helper():\n    return 1\n")
    (root / "pkg" / "cli.py").write_text(
        "from pkg.core import helper\n\n\ndef main():\n    return helper()\n"
    )
    indexer = SCIPPythonIndexer(root, output_dir=tmp_path / "out")

    assert indexer.generate_index(project_name="repo") is True

    report = indexer.index_generation_report
    assert (report["backend"], report["status"]) == ("pyrefly", "complete")
    assert report["provider_version"] == PYREFLY_VERSION
    assert _scip_tool_name(indexer.index_file) == "pyrefly"
    assert not (tmp_path / "out" / "pyrefly-glean").exists()
    assert not any(path.name.startswith("pyrefly") for path in root.rglob("*"))


@pytest.mark.skipif(
    _installed_pyrefly() is None,
    reason=f"pyrefly {PYREFLY_VERSION} is not installed",
)
def test_parameter_named_like_a_module_function_is_not_a_call(tmp_path, environ):
    # After a branch that may rebind a parameter, pyrefly 1.4 names the read
    # ``mod.revision``, the module function's name; the function scope must
    # win, as in Python (alembic's command.upgrade has this shape).
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mod.py").write_text(
        "def revision():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def upgrade(config, revision):\n"
        '    if ":" in revision:\n'
        '        start, revision = revision.split(":", 2)\n'
        "    return run(destination=revision)\n"
        "\n"
        "\n"
        "def run(destination):\n"
        "    return destination\n"
    )
    indexer = SCIPPythonIndexer(root, output_dir=tmp_path / "out")
    assert indexer.generate_index(project_name="repo") is True

    index = Index()
    index.ParseFromString(indexer.index_file.read_bytes())
    document = next(d for d in index.documents if d.relative_path == "mod.py")
    read = [
        occurrence.symbol
        for occurrence in document.occurrences
        if list(occurrence.range)[:2] == [7, 27]
    ]
    assert read and all(symbol.endswith("upgrade().(revision)") for symbol in read)
