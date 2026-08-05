# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Managed external language tools for graph and LSP providers.

Python extras install CodeNib runtimes. Language indexers and language servers
are separate executables, so this module gives wheel users one repository-aware
place to inspect and install the package-managed subset. System packages and
project-local tools remain explicit rather than invoking ``sudo`` or mutating a
target checkout behind the user's back.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Mapping, Sequence

from .languages import get_language_spec, lsp_command_for_language
from .paths import toolchain_dir

ToolScope = Literal["graph", "lsp"]
ToolManager = Literal["npm", "go", "rustup", "dotnet", "gem", "external"]


@dataclass(frozen=True, slots=True)
class PackagePin:
    """One package-manager coordinate pinned by the release."""

    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ToolRecipe:
    """Installation recipe for one provider executable."""

    manager: ToolManager
    packages: tuple[PackagePin, ...] = ()
    prerequisite: str | None = None
    hint: str = ""


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    """Resolved provider requirement for one repository language."""

    language: str
    display_name: str
    scope: ToolScope
    executable: str
    ready: bool
    resolved: str | None = None
    detail: str = ""

    @property
    def recipe(self) -> ToolRecipe | None:
        return TOOL_RECIPES.get(self.executable)


@dataclass(frozen=True, slots=True)
class ToolchainPlan:
    """Repository-aware graph/LSP tool requirements."""

    repository: Path
    root: Path
    languages: tuple[str, ...]
    requirements: tuple[ToolRequirement, ...]
    notes: tuple[str, ...] = ()

    @property
    def missing(self) -> tuple[ToolRequirement, ...]:
        return tuple(item for item in self.requirements if not item.ready)

    @property
    def ready(self) -> bool:
        return not self.missing and not any(
            note.startswith("MISSING:") for note in self.notes
        )


SCIP_PYTHON_VERSION = "0.6.6"
SCIP_TYPESCRIPT_VERSION = "0.4.0"
TYPESCRIPT_LANGUAGE_SERVER_VERSION = "5.3.0"
TYPESCRIPT_VERSION = "6.0.3"
YARN_VERSION = "1.22.22"
PNPM_VERSION = "10.34.5"
BASEDPYRIGHT_VERSION = "1.39.8"
SCIP_GO_VERSION = "v0.2.7"
GOPLS_VERSION = "v0.22.0"
RUST_TOOLCHAIN = "nightly-2026-02-22"
SCIP_DOTNET_VERSION = "0.2.14"
CSHARP_LS_VERSION = "0.25.0"
BUNDLER_VERSION = "2.6.9"
SCIP_RUBY_VERSION = "0.4.7"
RUBY_LSP_VERSION = "0.26.9"
INTELEPHENSE_VERSION = "1.18.4"


TOOL_RECIPES: Mapping[str, ToolRecipe] = {
    "scip-python": ToolRecipe(
        manager="npm",
        packages=(PackagePin("@sourcegraph/scip-python", SCIP_PYTHON_VERSION),),
        prerequisite="npm",
    ),
    "basedpyright-langserver": ToolRecipe(
        manager="npm",
        packages=(PackagePin("basedpyright", BASEDPYRIGHT_VERSION),),
        prerequisite="npm",
    ),
    "scip-typescript": ToolRecipe(
        manager="npm",
        packages=(
            PackagePin("@sourcegraph/scip-typescript", SCIP_TYPESCRIPT_VERSION),
            PackagePin("yarn", YARN_VERSION),
            PackagePin("pnpm", PNPM_VERSION),
        ),
        prerequisite="npm",
    ),
    "typescript-language-server": ToolRecipe(
        manager="npm",
        packages=(
            PackagePin(
                "typescript-language-server", TYPESCRIPT_LANGUAGE_SERVER_VERSION
            ),
            PackagePin("typescript", TYPESCRIPT_VERSION),
        ),
        prerequisite="npm",
    ),
    "scip-go": ToolRecipe(
        manager="go",
        packages=(
            PackagePin("github.com/scip-code/scip-go/cmd/scip-go", SCIP_GO_VERSION),
        ),
        prerequisite="go",
    ),
    "gopls": ToolRecipe(
        manager="go",
        packages=(PackagePin("golang.org/x/tools/gopls", GOPLS_VERSION),),
        prerequisite="go",
    ),
    "rust-analyzer": ToolRecipe(
        manager="rustup",
        prerequisite="rustup",
        hint=f"install the rust-analyzer component for {RUST_TOOLCHAIN}",
    ),
    "clangd": ToolRecipe(
        manager="external",
        hint="install clangd from the operating system's LLVM packages",
    ),
    "scip-java": ToolRecipe(
        manager="external",
        hint="install the pinned scip-java provider; see SCIP And Graph Indexing",
    ),
    "jdtls": ToolRecipe(
        manager="external",
        hint="install Eclipse JDT LS; see SCIP And Graph Indexing",
    ),
    "kotlin-language-server": ToolRecipe(
        manager="external",
        hint="install the Kotlin language server; see SCIP And Graph Indexing",
    ),
    "scip-dotnet": ToolRecipe(
        manager="dotnet",
        packages=(PackagePin("scip-dotnet", SCIP_DOTNET_VERSION),),
        prerequisite="dotnet",
    ),
    "csharp-ls": ToolRecipe(
        manager="dotnet",
        packages=(PackagePin("csharp-ls", CSHARP_LS_VERSION),),
        prerequisite="dotnet",
    ),
    "bundle": ToolRecipe(
        manager="gem",
        packages=(
            PackagePin("bundler", BUNDLER_VERSION),
            PackagePin("scip-ruby", SCIP_RUBY_VERSION),
        ),
        prerequisite="gem",
    ),
    "ruby-lsp": ToolRecipe(
        manager="gem",
        packages=(PackagePin("ruby-lsp", RUBY_LSP_VERSION),),
        prerequisite="gem",
    ),
    "scip-php": ToolRecipe(
        manager="external",
        hint=(
            "scip-php is project-local; use Composer in a disposable or clean "
            "checkout, or let CodeNib use its Intelephense fallback"
        ),
    ),
    "intelephense": ToolRecipe(
        manager="npm",
        packages=(PackagePin("intelephense", INTELEPHENSE_VERSION),),
        prerequisite="npm",
    ),
}


def managed_bin_dirs(root: Path | None = None) -> tuple[Path, ...]:
    """Return managed executable directories in resolution order."""

    root = (root or toolchain_dir()).expanduser()
    directories = [
        root,
        root / "node-tools" / "node_modules" / ".bin",
        root / "go-tools" / "bin",
        root / "dotnet-tools",
        root / "dotnet",
        root / "gems" / "bin",
        root / "python-tools" / "bin",
    ]
    directories.extend(sorted(root.glob("gradle-*/bin")))
    return tuple(directories)


def managed_path(root: Path | None = None, *, current: str | None = None) -> str:
    """Return a PATH that prefers release-pinned CodeNib tools."""

    entries = [str(path) for path in managed_bin_dirs(root)]
    existing = current if current is not None else os.environ.get("PATH", "")
    entries.extend(part for part in existing.split(os.pathsep) if part)
    return os.pathsep.join(dict.fromkeys(entries))


def activate_managed_toolchain(
    environ: dict[str, str] | None = None,
    *,
    root: Path | None = None,
) -> Path:
    """Expose managed tools to CodeNib subprocesses without shell exports."""

    environ = os.environ if environ is None else environ
    root = (root or toolchain_dir()).expanduser()
    environ["CODENIB_TOOLCHAIN_DIR"] = str(root)
    environ.setdefault("CODENIB_SCIP_TOOLS_DIR", str(root))
    environ["PATH"] = managed_path(root, current=environ.get("PATH", ""))
    dotnet = root / "dotnet"
    if dotnet.is_dir():
        environ.setdefault("DOTNET_ROOT", str(dotnet))
    gems = root / "gems"
    if gems.is_dir():
        environ.setdefault("GEM_HOME", str(gems))
        environ.setdefault("GEM_PATH", str(gems))
    return root


def resolve_command(command: str, *, root: Path | None = None) -> str | None:
    """Resolve a command from the managed root first, then the host PATH."""

    suffixes = ("",)
    if os.name == "nt":
        suffixes += tuple(
            suffix.lower()
            for suffix in os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(";")
            if suffix
        )
    for directory in managed_bin_dirs(root):
        for suffix in suffixes:
            candidate = directory / f"{command}{suffix}"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return shutil.which(command)


def _canonical_lsp_executable(language: str, command: Sequence[str]) -> str:
    if language == "rust":
        return "rust-analyzer"
    return Path(command[0]).name


def plan_repository_toolchain(
    repository: str | os.PathLike[str],
    languages: Sequence[str],
    *,
    scopes: Iterable[ToolScope] = ("graph",),
    root: Path | None = None,
    command_resolver: Callable[[str], str | None] | None = None,
    module_checker: Callable[[str], bool] | None = None,
) -> ToolchainPlan:
    """Build a read-only install/status plan for a repository."""

    from .graph.setup import diagnose_graph_setup

    repo = Path(repository).expanduser().resolve()
    root = (root or toolchain_dir()).expanduser()
    resolver = command_resolver or (lambda command: resolve_command(command, root=root))
    selected_scopes = frozenset(scopes)
    requirements: list[ToolRequirement] = []
    notes: list[str] = []

    if "graph" in selected_scopes:
        report = diagnose_graph_setup(
            repo,
            languages,
            command_resolver=resolver,
            module_checker=module_checker,
        )
        if not report.runtime.ready:
            notes.append(
                'MISSING: Python graph runtime; install with `pip install "codenib[graph]"`'
            )
        for setup in report.languages:
            if setup.state == "unsupported":
                notes.append(
                    f"INFO: {setup.display_name} has no registered symbol-graph provider"
                )
                continue
            for prerequisite in setup.prerequisites:
                if prerequisite.kind == "command":
                    requirements.append(
                        ToolRequirement(
                            language=setup.language,
                            display_name=setup.display_name,
                            scope="graph",
                            executable=prerequisite.name,
                            ready=prerequisite.ready,
                            resolved=prerequisite.resolved,
                            detail=prerequisite.detail,
                        )
                    )
                elif not prerequisite.ready:
                    notes.append(
                        f"MISSING: {setup.display_name} project prerequisite "
                        f"{prerequisite.name}: {prerequisite.detail}"
                    )

    if "lsp" in selected_scopes:
        for language in languages:
            spec = get_language_spec(language)
            if spec is None:
                continue
            command = lsp_command_for_language(spec.key) or []
            if not command:
                notes.append(
                    f"INFO: {spec.display_name} has no registered live LSP provider"
                )
                continue
            executable = _canonical_lsp_executable(spec.key, command)
            resolved = resolver(executable)
            requirements.append(
                ToolRequirement(
                    language=spec.key,
                    display_name=spec.display_name,
                    scope="lsp",
                    executable=executable,
                    ready=resolved is not None,
                    resolved=resolved,
                    detail=" ".join(command),
                )
            )

    unique: dict[tuple[str, ToolScope, str], ToolRequirement] = {}
    for requirement in requirements:
        unique[(requirement.language, requirement.scope, requirement.executable)] = (
            requirement
        )
    return ToolchainPlan(
        repository=repo,
        root=root,
        languages=tuple(languages),
        requirements=tuple(unique.values()),
        notes=tuple(dict.fromkeys(notes)),
    )


def _package_coordinate(package: PackagePin) -> str:
    return f"{package.name}@{package.version}"


def recipe_commands(recipe: ToolRecipe, root: Path) -> tuple[tuple[str, ...], ...]:
    """Render non-shell installation commands for a managed recipe."""

    if recipe.manager == "npm":
        return (
            (
                "npm",
                "install",
                "--prefix",
                str(root / "node-tools"),
                "--no-audit",
                "--no-fund",
                *(_package_coordinate(package) for package in recipe.packages),
            ),
        )
    if recipe.manager == "go":
        return tuple(
            ("go", "install", _package_coordinate(package))
            for package in recipe.packages
        )
    if recipe.manager == "rustup":
        return (
            ("rustup", "toolchain", "install", RUST_TOOLCHAIN),
            (
                "rustup",
                "component",
                "add",
                "rust-analyzer",
                "--toolchain",
                RUST_TOOLCHAIN,
            ),
        )
    if recipe.manager == "dotnet":
        return tuple(
            (
                "dotnet",
                "tool",
                "update",
                "--tool-path",
                str(root / "dotnet-tools"),
                package.name,
                "--version",
                package.version,
            )
            for package in recipe.packages
        )
    if recipe.manager == "gem":
        return tuple(
            (
                "gem",
                "install",
                package.name,
                "-v",
                package.version,
                "--no-document",
            )
            for package in recipe.packages
        )
    return ()


def _install_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    activate_managed_toolchain(env, root=root)
    env["GOBIN"] = str(root / "go-tools" / "bin")
    env["GOPATH"] = str(root / "go-tools")
    env["GEM_HOME"] = str(root / "gems")
    env["GEM_PATH"] = str(root / "gems")
    return env


def install_requirements(
    requirements: Sequence[ToolRequirement],
    *,
    root: Path | None = None,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[list[tuple[str, ...]], list[str]]:
    """Install missing package-managed requirements and return an audit log."""

    root = (root or toolchain_dir()).expanduser()
    env = _install_environment(root)
    commands: list[tuple[str, ...]] = []
    manual: list[str] = []
    seen_recipes: set[ToolRecipe] = set()
    installs_rust_analyzer = False

    for requirement in requirements:
        if requirement.ready:
            continue
        recipe = requirement.recipe
        if recipe is None:
            manual.append(f"{requirement.executable}: no install recipe registered")
            continue
        if recipe.manager == "external":
            manual.append(f"{requirement.executable}: {recipe.hint}")
            continue
        if recipe in seen_recipes:
            continue
        seen_recipes.add(recipe)
        prerequisite = recipe.prerequisite or recipe.manager
        if resolve_command(prerequisite, root=root) is None:
            manual.append(
                f"{requirement.executable}: install prerequisite `{prerequisite}` first"
            )
            continue
        commands.extend(recipe_commands(recipe, root))
        installs_rust_analyzer = installs_rust_analyzer or recipe.manager == "rustup"

    if dry_run:
        return commands, manual

    root.mkdir(parents=True, exist_ok=True)
    (root / "node-tools").mkdir(exist_ok=True)
    (root / "go-tools" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "dotnet-tools").mkdir(exist_ok=True)
    (root / "gems").mkdir(exist_ok=True)

    for command in tuple(commands):
        result = runner(command, check=False, env=env)
        if result.returncode == 0:
            continue
        if len(command) >= 3 and command[0:3] == ("dotnet", "tool", "update"):
            fallback = list(command)
            fallback[2] = "install"
            fallback_command = tuple(fallback)
            commands.append(fallback_command)
            fallback_result = runner(fallback_command, check=False, env=env)
            if fallback_result.returncode == 0:
                continue
        raise RuntimeError(
            f"tool installation failed ({result.returncode}): {' '.join(command)}"
        )

    if installs_rust_analyzer:
        resolved = runner(
            ("rustup", "which", "--toolchain", RUST_TOOLCHAIN, "rust-analyzer"),
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if resolved.returncode == 0 and resolved.stdout.strip():
            link = root / "rust-analyzer"
            link.unlink(missing_ok=True)
            link.symlink_to(resolved.stdout.strip())

    return commands, manual


__all__ = [
    "ToolRecipe",
    "ToolRequirement",
    "ToolScope",
    "ToolchainPlan",
    "activate_managed_toolchain",
    "install_requirements",
    "managed_bin_dirs",
    "managed_path",
    "plan_repository_toolchain",
    "recipe_commands",
    "resolve_command",
]
