# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codenib import toolchains
from codenib.paths import toolchain_dir


@pytest.mark.parametrize(
    ("make_name", "packaged_value"),
    (
        ("SCIP_PYTHON_VERSION", toolchains.SCIP_PYTHON_VERSION),
        ("SCIP_TYPESCRIPT_VERSION", toolchains.SCIP_TYPESCRIPT_VERSION),
        (
            "TYPESCRIPT_LANGUAGE_SERVER_VERSION",
            toolchains.TYPESCRIPT_LANGUAGE_SERVER_VERSION,
        ),
        ("TYPESCRIPT_VERSION", toolchains.TYPESCRIPT_VERSION),
        ("YARN_VERSION", toolchains.YARN_VERSION),
        ("PNPM_VERSION", toolchains.PNPM_VERSION),
        ("BASEDPYRIGHT_VERSION", toolchains.BASEDPYRIGHT_VERSION),
        ("SCIP_GO_VERSION", toolchains.SCIP_GO_VERSION),
        ("GOPLS_VERSION", toolchains.GOPLS_VERSION),
        ("RUST_TOOLCHAIN", toolchains.RUST_TOOLCHAIN),
        ("SCIP_DOTNET_VERSION", toolchains.SCIP_DOTNET_VERSION),
        ("CSHARP_LS_VERSION", toolchains.CSHARP_LS_VERSION),
        ("BUNDLER_VERSION", toolchains.BUNDLER_VERSION),
        ("SCIP_RUBY_VERSION", toolchains.SCIP_RUBY_VERSION),
        ("RUBY_LSP_VERSION", toolchains.RUBY_LSP_VERSION),
        ("INTELEPHENSE_VERSION", toolchains.INTELEPHENSE_VERSION),
    ),
)
def test_packaged_tool_versions_match_source_bootstrap(
    make_name: str,
    packaged_value: str,
) -> None:
    makefile = Path(__file__).resolve().parents[1] / "Makefile"
    match = re.search(
        rf"^{re.escape(make_name)} \?= (\S+)$",
        makefile.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )

    assert match is not None
    assert match.group(1) == packaged_value


def test_go_bootstrap_binds_cached_tool_to_detected_platform() -> None:
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text(
        encoding="utf-8"
    )

    assert (
        "GO_ARCH ?= $(if $(filter aarch64 arm64,$(shell uname -m)),arm64,amd64)"
        in makefile
    )
    assert "GO_TOOL_ID = $(GO_VERSION)-$(GO_OS)-$(GO_ARCH)" in makefile
    assert 'grep -qx "$(GO_TOOL_ID)"' in makefile
    assert 'echo "$(GO_TOOL_ID)"' in makefile


def test_toolchain_dir_is_durable_and_supports_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CODENIB_TOOLCHAIN_DIR", raising=False)
    monkeypatch.delenv("CODENIB_SCIP_TOOLS_DIR", raising=False)

    assert toolchain_dir() == tmp_path / "home" / "toolchains"

    monkeypatch.setenv("CODENIB_TOOLCHAIN_DIR", str(tmp_path / "custom"))
    assert toolchain_dir() == tmp_path / "custom"


def test_activate_managed_toolchain_prepends_all_provider_bins(
    tmp_path: Path,
) -> None:
    env = {"PATH": "/usr/bin"}

    root = toolchains.activate_managed_toolchain(env, root=tmp_path / "tools")

    path = env["PATH"].split(":")
    assert root == tmp_path / "tools"
    assert path[0] == str(root)
    assert str(root / "node-tools" / "node_modules" / ".bin") in path
    assert str(root / "go" / "bin") in path
    assert str(root / "go-tools" / "bin") in path
    assert path[-1] == "/usr/bin"


def test_repository_plan_uses_language_registry_for_graph_and_lsp(
    tmp_path: Path,
) -> None:
    available = {
        "scip-python": "/managed/scip-python",
        "basedpyright-langserver": "/managed/basedpyright-langserver",
    }
    package = tmp_path / "tools/node-tools/node_modules/browser-basedpyright"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text(
        '{"version": "' + toolchains.BASEDPYRIGHT_VERSION + '"}'
    )
    (package / "dist/pyright.worker.js").write_text("unit installation marker")

    plan = toolchains.plan_repository_toolchain(
        tmp_path,
        ["python"],
        scopes=("graph", "lsp"),
        root=tmp_path / "tools",
        command_resolver=available.get,
        module_checker=lambda _name: True,
    )

    assert plan.ready is True
    assert [
        (item.scope, item.executable, item.resolved) for item in plan.requirements
    ] == [
        ("graph", "scip-python", "/managed/scip-python"),
        ("graph", "basedpyright-langserver", "/managed/basedpyright-langserver"),
        ("lsp", "basedpyright-langserver", "/managed/basedpyright-langserver"),
    ]


def test_repository_plan_forwards_read_only_project_policy(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("project(example)\n", encoding="utf-8")
    available = {"clangd": "/tools/clangd", "cmake": "/tools/cmake"}

    plan = toolchains.plan_repository_toolchain(
        tmp_path,
        ["cpp"],
        command_resolver=available.get,
        module_checker=lambda _name: True,
        allow_project_preparation=False,
    )

    assert plan.ready is False
    assert any("existing usable compile_commands.json" in note for note in plan.notes)


def test_lsp_plan_repairs_an_old_python_install_without_the_source_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tools"
    plan = toolchains.plan_repository_toolchain(
        tmp_path,
        ["python"],
        scopes=("lsp",),
        root=root,
        command_resolver=lambda command: "/tools/" + command,
    )
    assert len(plan.missing) == 1
    assert "browser-basedpyright" in plan.missing[0].detail
    monkeypatch.setattr(
        toolchains, "resolve_command", lambda *_a, **_kw: "/usr/bin/npm"
    )
    commands, manual = toolchains.install_requirements(
        plan.missing, root=root, dry_run=True
    )
    assert not manual
    assert len(commands) == 1
    assert "basedpyright@" + toolchains.BASEDPYRIGHT_VERSION in commands[0]
    assert "browser-basedpyright@" + toolchains.BASEDPYRIGHT_VERSION in commands[0]


@pytest.mark.parametrize("include_lsp", (False, True))
def test_python_source_assets_do_not_tax_graph_only_installs_or_duplicate_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, include_lsp: bool
) -> None:
    requirements = [
        toolchains.ToolRequirement(
            language="python",
            display_name="Python",
            scope="graph",
            executable="basedpyright-langserver",
            ready=False,
        )
    ]
    if include_lsp:
        requirements.append(
            toolchains.ToolRequirement(
                language="python",
                display_name="Python",
                scope="lsp",
                executable="basedpyright-langserver",
                ready=False,
            )
        )
    monkeypatch.setattr(
        toolchains, "resolve_command", lambda *_a, **_kw: "/usr/bin/npm"
    )
    commands, manual = toolchains.install_requirements(
        requirements, root=tmp_path / "tools", dry_run=True
    )
    assert not manual and len(commands) == 1
    assert (
        "browser-basedpyright@" + toolchains.BASEDPYRIGHT_VERSION in commands[0]
    ) == include_lsp


def test_dry_run_renders_pinned_npm_install_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tools"
    requirement = toolchains.ToolRequirement(
        language="python",
        display_name="Python",
        scope="graph",
        executable="scip-python",
        ready=False,
    )
    monkeypatch.setattr(
        toolchains,
        "resolve_command",
        lambda command, **_kwargs: f"/usr/bin/{command}",
    )

    commands, manual = toolchains.install_requirements(
        [requirement],
        root=root,
        dry_run=True,
    )

    assert manual == []
    assert commands == [
        (
            "npm",
            "install",
            "--prefix",
            str(root / "node-tools"),
            "--no-audit",
            "--no-fund",
            "@sourcegraph/scip-python@0.6.6",
        )
    ]
    assert not root.exists()


def test_external_provider_remains_an_explicit_manual_prerequisite(
    tmp_path: Path,
) -> None:
    requirement = toolchains.ToolRequirement(
        language="cpp",
        display_name="C/C++",
        scope="graph",
        executable="clangd",
        ready=False,
    )

    commands, manual = toolchains.install_requirements(
        [requirement],
        root=tmp_path / "tools",
        dry_run=True,
    )

    assert commands == []
    assert manual == [
        "clangd: install clangd from the operating system's LLVM packages"
    ]
