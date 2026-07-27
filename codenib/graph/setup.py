# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Repository-aware setup diagnostics for the optional symbol graph."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal, Sequence

from ..languages import (
    get_language_spec,
    lsp_command_for_language,
    scip_cold_start_command_for_language,
)
from ..repository_filters import DEFAULT_IGNORED_DIRS

GraphSetupState = Literal["ready", "missing", "unsupported"]
CommandResolver = Callable[[str], str | None]
ModuleChecker = Callable[[str], bool]

_TOOL_HINTS = {
    "bear": "Install Bear to generate compile_commands.json for Make projects.",
    "bundle": "Install Bundler, or provide a ruby-lsp fallback.",
    "clangd": "Install clangd from LLVM.",
    "cmake": "Install CMake or provide compile_commands.json.",
    "composer": "Install Composer, or provide an Intelephense LSP command.",
    "intelephense": "Install Intelephense for the PHP LSP fallback.",
    "ruby-lsp": "Install ruby-lsp for the Ruby LSP fallback.",
    "rust-analyzer": "Install rust-analyzer with SCIP support.",
    "scip-dotnet": "Install scip-dotnet or set CODENIB_CSHARP_SCIP_CMD.",
    "scip-go": "Install scip-go and make it available on PATH.",
    "scip-java": "Install scip-java or set the language-specific SCIP command.",
    "scip-python": "Install @sourcegraph/scip-python and expose `scip-python` on PATH.",
    "scip-ruby": "Prepare scip-ruby in the repository bundle.",
    "scip-typescript": (
        "Install @sourcegraph/scip-typescript and expose `scip-typescript` on PATH."
    ),
}


@dataclass(frozen=True, slots=True)
class GraphPrerequisite:
    """One executable, Python package, or repository input needed by a route."""

    name: str
    kind: Literal["command", "python", "project"]
    ready: bool
    detail: str
    resolved: str | None = None


@dataclass(frozen=True, slots=True)
class GraphLanguageSetup:
    """Selected graph provider and readiness for one repository language."""

    language: str
    display_name: str
    state: GraphSetupState
    backend: str | None
    command: list[str] = field(default_factory=list)
    resolved_command: str | None = None
    prerequisites: list[GraphPrerequisite] = field(default_factory=list)
    note: str = ""

    @property
    def missing(self) -> list[str]:
        return [
            prerequisite.name
            for prerequisite in self.prerequisites
            if not prerequisite.ready
        ]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["missing"] = self.missing
        return payload


@dataclass(frozen=True, slots=True)
class GraphSetupReport:
    """Complete graph readiness report shared by CLI and web setup surfaces."""

    repository: str
    runtime: GraphPrerequisite
    languages: list[GraphLanguageSetup]

    @property
    def supported_languages(self) -> list[GraphLanguageSetup]:
        return [item for item in self.languages if item.state != "unsupported"]

    @property
    def buildable_languages(self) -> list[str]:
        return [item.language for item in self.languages if item.state == "ready"]

    @property
    def unavailable_languages(self) -> list[str]:
        return [item.language for item in self.languages if item.state == "missing"]

    @property
    def unsupported_languages(self) -> list[str]:
        return [item.language for item in self.languages if item.state == "unsupported"]

    @property
    def ready(self) -> bool:
        """Whether every graph-capable detected language can be built."""

        supported = self.supported_languages
        return bool(
            self.runtime.ready
            and supported
            and all(item.state == "ready" for item in supported)
        )

    @property
    def partial_ready(self) -> bool:
        """Whether at least one detected language can produce a usable graph."""

        return bool(self.runtime.ready and self.buildable_languages)

    @property
    def install_hints(self) -> list[str]:
        missing_commands = {
            prerequisite.name
            for item in self.languages
            for prerequisite in item.prerequisites
            if not prerequisite.ready and prerequisite.kind == "command"
        }
        hints = [
            _TOOL_HINTS[name]
            for name in sorted(missing_commands)
            if name in _TOOL_HINTS
        ]
        hints.extend(
            prerequisite.detail
            for item in self.languages
            for prerequisite in item.prerequisites
            if not prerequisite.ready and prerequisite.kind == "project"
        )
        hints = list(dict.fromkeys(hints))
        if not self.runtime.ready:
            hints.insert(
                0, 'Install the Python runtime with `pip install "codenib[graph]"`.'
            )
        return hints

    @property
    def setup_commands(self) -> list[str]:
        commands = []
        if not self.runtime.ready:
            commands.append('pip install "codenib[graph]"')
        commands.extend(
            [
                "codenib doctor . --require graph",
                "codenib wiki . --preset graph",
            ]
        )
        return commands

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "partial_ready": self.partial_ready,
            "runtime": asdict(self.runtime),
            "languages": [item.to_dict() for item in self.languages],
            "buildable_languages": self.buildable_languages,
            "unavailable_languages": self.unavailable_languages,
            "unsupported_languages": self.unsupported_languages,
            "install_hints": self.install_hints,
            "commands": self.setup_commands,
        }


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _resolve_command(
    executable: str,
    *,
    repository: Path,
    command_resolver: CommandResolver,
) -> str | None:
    expanded = os.path.expandvars(os.path.expanduser(executable))
    if os.path.isabs(expanded):
        path = Path(expanded)
        return str(path) if path.is_file() else None
    if "/" in expanded or "\\" in expanded:
        path = (repository / expanded).resolve()
        return str(path) if path.is_file() else None
    return command_resolver(expanded)


def _command_prerequisite(
    executable: str,
    *,
    repository: Path,
    command_resolver: CommandResolver,
    label: str | None = None,
) -> GraphPrerequisite:
    resolved = _resolve_command(
        executable,
        repository=repository,
        command_resolver=command_resolver,
    )
    name = label or Path(executable).name
    return GraphPrerequisite(
        name=name,
        kind="command",
        ready=resolved is not None,
        detail=resolved or "not found",
        resolved=resolved,
    )


def _project_prerequisite(name: str, ready: bool, detail: str) -> GraphPrerequisite:
    return GraphPrerequisite(
        name=name,
        kind="project",
        ready=ready,
        detail=detail,
    )


def _usable_compile_database(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(
        isinstance(payload, list)
        and payload
        and all(isinstance(entry, dict) and entry.get("file") for entry in payload)
    )


def _scip_setup(
    language: str,
    *,
    repository: Path,
    command_resolver: CommandResolver,
    extra: Sequence[GraphPrerequisite] = (),
) -> GraphLanguageSetup:
    spec = get_language_spec(language)
    assert spec is not None
    command = scip_cold_start_command_for_language(language) or []
    prerequisites = list(extra)
    if command:
        prerequisites.insert(
            0,
            _command_prerequisite(
                command[0],
                repository=repository,
                command_resolver=command_resolver,
            ),
        )
    else:
        prerequisites.insert(
            0,
            GraphPrerequisite(
                name="SCIP provider",
                kind="command",
                ready=False,
                detail="no command is registered",
            ),
        )
    ready = all(item.ready for item in prerequisites)
    return GraphLanguageSetup(
        language=spec.key,
        display_name=spec.display_name,
        state="ready" if ready else "missing",
        backend="scip",
        command=list(command),
        resolved_command=prerequisites[0].resolved,
        prerequisites=prerequisites,
    )


def _cpp_setup(
    *,
    repository: Path,
    command_resolver: CommandResolver,
) -> GraphLanguageSetup:
    spec = get_language_spec("cpp")
    assert spec is not None
    command = ["clangd"]
    prerequisites = [
        _command_prerequisite(
            "clangd",
            repository=repository,
            command_resolver=command_resolver,
        )
    ]

    compile_databases = (
        repository / "compile_commands.json",
        repository / "build" / "compile_commands.json",
    )
    existing = next(
        (path for path in compile_databases if _usable_compile_database(path)),
        None,
    )
    cmake_ready = (repository / "CMakeLists.txt").is_file() and bool(
        command_resolver("cmake")
    )
    makefile = any(
        (repository / name).is_file()
        for name in ("Makefile", "GNUmakefile", "makefile")
    )
    bear_ready = (
        makefile and bool(command_resolver("bear")) and bool(command_resolver("make"))
    )
    project_ready = bool(existing or cmake_ready or bear_ready)
    if existing:
        detail = str(existing)
    elif cmake_ready:
        detail = "CMake project; compile_commands.json can be generated"
    elif bear_ready:
        detail = "Make project; compile_commands.json can be generated with Bear"
    else:
        detail = (
            "provide compile_commands.json, or install CMake/Bear for a supported "
            "build project"
        )
    prerequisites.append(
        _project_prerequisite("compilation database", project_ready, detail)
    )
    ready = all(item.ready for item in prerequisites)
    return GraphLanguageSetup(
        language=spec.key,
        display_name=spec.display_name,
        state="ready" if ready else "missing",
        backend="clangd",
        command=command,
        resolved_command=prerequisites[0].resolved,
        prerequisites=prerequisites,
    )


def _lsp_setup(
    language: str,
    *,
    repository: Path,
    command_resolver: CommandResolver,
    note: str = "",
) -> GraphLanguageSetup:
    spec = get_language_spec(language)
    assert spec is not None
    command = lsp_command_for_language(language) or []
    prerequisite = (
        _command_prerequisite(
            command[0],
            repository=repository,
            command_resolver=command_resolver,
        )
        if command
        else GraphPrerequisite(
            name="LSP provider",
            kind="command",
            ready=False,
            detail="no command is registered",
        )
    )
    return GraphLanguageSetup(
        language=spec.key,
        display_name=spec.display_name,
        state="ready" if prerequisite.ready else "missing",
        backend="lsp",
        command=list(command),
        resolved_command=prerequisite.resolved,
        prerequisites=[prerequisite],
        note=note,
    )


def _ruby_setup(
    *,
    repository: Path,
    command_resolver: CommandResolver,
) -> GraphLanguageSetup:
    gemfile_override = os.environ.get("CODENIB_RUBY_BUNDLE_GEMFILE")
    gemfile = (
        Path(gemfile_override).expanduser()
        if gemfile_override
        else repository / "Gemfile"
    )
    use_scip = bool(gemfile_override and gemfile.is_file())
    if gemfile.is_file() and not use_scip:
        try:
            use_scip = "scip-ruby" in gemfile.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            use_scip = False

    if use_scip:
        scip = _scip_setup(
            "ruby",
            repository=repository,
            command_resolver=command_resolver,
        )
        if scip.state == "ready":
            return scip
        lsp = _lsp_setup(
            "ruby",
            repository=repository,
            command_resolver=command_resolver,
            note="Ruby LSP fallback selected because the SCIP bundle is unavailable.",
        )
        if lsp.state == "ready":
            return lsp
        return GraphLanguageSetup(
            language=scip.language,
            display_name=scip.display_name,
            state="missing",
            backend="scip-or-lsp",
            command=scip.command,
            prerequisites=[*scip.prerequisites, *lsp.prerequisites],
            note="Neither the prepared scip-ruby route nor ruby-lsp is available.",
        )
    return _lsp_setup(
        "ruby",
        repository=repository,
        command_resolver=command_resolver,
        note="Ruby LSP is the active route for an unprepared bundle.",
    )


def _php_scip_prerequisites(
    *,
    repository: Path,
    command_resolver: CommandResolver,
) -> list[GraphPrerequisite]:
    local_scip = repository / "vendor" / "bin" / "scip-php"
    php = command_resolver("php")
    composer = command_resolver("composer")
    docker = command_resolver("docker")
    provider_ready = bool(local_scip.is_file() or (php and composer) or docker)
    choices = []
    if local_scip.is_file():
        choices.append(str(local_scip))
    if php and composer:
        choices.append(f"{composer} with {php}")
    if docker:
        choices.append(f"{docker} fallback")
    return [
        GraphPrerequisite(
            name="scip-php",
            kind="command",
            ready=provider_ready,
            detail=", ".join(choices) if choices else "needs PHP+Composer or Docker",
            resolved=choices[0] if choices else None,
        )
    ]


def _php_setup(
    *,
    repository: Path,
    command_resolver: CommandResolver,
) -> GraphLanguageSetup:
    if not (repository / "composer.json").is_file():
        return _lsp_setup(
            "php",
            repository=repository,
            command_resolver=command_resolver,
            note="PHP LSP is the active route for a non-Composer project.",
        )

    scip = _scip_setup(
        "php",
        repository=repository,
        command_resolver=command_resolver,
        extra=_php_scip_prerequisites(
            repository=repository,
            command_resolver=command_resolver,
        ),
    )
    # The registry command is project-local and may be prepared into a disposable
    # worktree by Composer or Docker. Its literal absence is therefore not a
    # blocker when the provider prerequisite above is satisfied.
    if len(scip.prerequisites) >= 2 and scip.prerequisites[1].ready:
        scip = GraphLanguageSetup(
            language=scip.language,
            display_name=scip.display_name,
            state=(
                "ready"
                if scip.prerequisites[1].ready
                and all(item.ready for item in scip.prerequisites[2:])
                else "missing"
            ),
            backend=scip.backend,
            command=scip.command,
            resolved_command=scip.prerequisites[1].resolved,
            prerequisites=scip.prerequisites[1:],
        )
    if scip.state == "ready":
        return scip

    lsp = _lsp_setup(
        "php",
        repository=repository,
        command_resolver=command_resolver,
        note="Intelephense fallback selected because the SCIP route is unavailable.",
    )
    if lsp.state == "ready":
        return lsp
    return GraphLanguageSetup(
        language=scip.language,
        display_name=scip.display_name,
        state="missing",
        backend="scip-or-lsp",
        command=scip.command,
        prerequisites=[*scip.prerequisites, *lsp.prerequisites],
        note="Neither the Composer/Docker SCIP route nor Intelephense is available.",
    )


def _find_project_file(repository: Path, *, suffixes: Sequence[str]) -> Path | None:
    normalized = tuple(suffix.lower() for suffix in suffixes)
    for current, dirs, files in os.walk(repository):
        dirs[:] = sorted(
            directory
            for directory in dirs
            if directory not in DEFAULT_IGNORED_DIRS and not directory.startswith(".")
        )
        for filename in sorted(files):
            if filename.lower().endswith(normalized):
                return Path(current) / filename
    return None


def _language_setup(
    language: str,
    *,
    repository: Path,
    command_resolver: CommandResolver,
) -> GraphLanguageSetup:
    spec = get_language_spec(language)
    if spec is None or not spec.graph_language or not spec.graph_indexer:
        display_name = spec.display_name if spec is not None else language
        normalized = spec.key if spec is not None else language
        return GraphLanguageSetup(
            language=normalized,
            display_name=display_name,
            state="unsupported",
            backend=None,
            note="CodeNib has no symbol-graph provider for this language.",
        )
    if spec.key == "cpp":
        return _cpp_setup(
            repository=repository,
            command_resolver=command_resolver,
        )
    if spec.key == "ruby":
        return _ruby_setup(
            repository=repository,
            command_resolver=command_resolver,
        )
    if spec.key == "php":
        return _php_setup(
            repository=repository,
            command_resolver=command_resolver,
        )
    if spec.cold_start_backend == "lsp":
        return _lsp_setup(
            spec.key,
            repository=repository,
            command_resolver=command_resolver,
        )
    if spec.cold_start_backend != "scip":
        return GraphLanguageSetup(
            language=spec.key,
            display_name=spec.display_name,
            state="missing",
            backend=spec.cold_start_backend,
            note="The registered graph backend has no setup diagnostic.",
        )

    extras = []
    if spec.key == "go":
        marker = repository / "go.mod"
        extras.append(
            _project_prerequisite(
                "go.mod",
                marker.is_file(),
                str(marker) if marker.is_file() else "missing from repository root",
            )
        )
    elif spec.key == "rust":
        marker = repository / "Cargo.toml"
        extras.append(
            _project_prerequisite(
                "Cargo.toml",
                marker.is_file(),
                str(marker) if marker.is_file() else "missing from repository root",
            )
        )
    elif spec.key == "csharp":
        project_file = _find_project_file(
            repository,
            suffixes=(".sln", ".csproj", ".vbproj"),
        )
        extras.append(
            _project_prerequisite(
                "solution or project file",
                project_file is not None,
                str(project_file) if project_file else "no .sln/.csproj found",
            )
        )
    return _scip_setup(
        spec.key,
        repository=repository,
        command_resolver=command_resolver,
        extra=extras,
    )


def diagnose_graph_setup(
    repository: str | os.PathLike[str],
    languages: Sequence[str],
    *,
    command_resolver: CommandResolver | None = None,
    module_checker: ModuleChecker | None = None,
) -> GraphSetupReport:
    """Inspect the exact graph providers needed by ``languages``.

    The function is read-only: it resolves commands and project markers but
    never installs tools, creates environments, or mutates the repository.
    """

    root = Path(repository).expanduser().resolve()
    command_resolver = command_resolver or shutil.which
    module_checker = module_checker or _module_available
    missing_runtime = [
        name for name in ("igraph", "google.protobuf") if not module_checker(name)
    ]
    runtime_ready = not missing_runtime
    runtime = GraphPrerequisite(
        name="Python graph runtime",
        kind="python",
        ready=runtime_ready,
        detail=(
            "installed"
            if runtime_ready
            else (
                f"missing {', '.join(missing_runtime)}; "
                'install with `pip install "codenib[graph]"`'
            )
        ),
    )
    seen: set[str] = set()
    setups = []
    for language in languages:
        spec = get_language_spec(language)
        key = spec.key if spec is not None else language.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        setups.append(
            _language_setup(
                key,
                repository=root,
                command_resolver=command_resolver,
            )
        )
    return GraphSetupReport(
        repository=str(root),
        runtime=runtime,
        languages=setups,
    )


__all__ = [
    "GraphLanguageSetup",
    "GraphPrerequisite",
    "GraphSetupReport",
    "GraphSetupState",
    "diagnose_graph_setup",
]
