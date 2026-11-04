# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Choose and run the provider behind the Python symbol graph.

pyrefly builds the graph by default: ``pyrefly check --report-glean`` output is
converted into a SCIP index with scip-python's symbol conventions, so decoding,
the C++ decoder and the occurrence index run unchanged. The ``graph`` extra
installs the pinned pyrefly release. When that release is not installed,
CodeNib falls back to scip-python.

``CODENIB_PYTHON_GRAPH_PROVIDER`` pins the choice: ``pyrefly`` fails instead of
falling back, and ``scip-python`` keeps the previous provider.
``CODENIB_PYREFLY`` names the pyrefly executable; otherwise the installed
distribution, the managed toolchain and ``PATH`` are searched.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from ..toolchains import PYREFLY_VERSION, SCIP_PYTHON_VERSION

PYTHON_GRAPH_PROVIDER_ENV = "CODENIB_PYTHON_GRAPH_PROVIDER"
PYREFLY_EXECUTABLE_ENV = "CODENIB_PYREFLY"
SCIP_PYTHON_PROVIDER = "scip-python"
PYREFLY_PROVIDER = "pyrefly"
_PROVIDERS = (SCIP_PYTHON_PROVIDER, PYREFLY_PROVIDER)


@dataclass(frozen=True, slots=True)
class PythonGraphProvider:
    """The provider that will build a Python graph, and why it was chosen."""

    name: str
    # Set when the provider is pyrefly and the pinned release was found.
    executable: Optional[str] = None
    # Chosen by CODENIB_PYTHON_GRAPH_PROVIDER rather than by default.
    configured: bool = False
    # Why pyrefly is not used, when it is not.
    note: str = ""

    @property
    def ready(self) -> bool:
        return self.name == SCIP_PYTHON_PROVIDER or self.executable is not None

    @property
    def identity(self) -> str:
        """Provider and release, recorded with graph artifacts."""

        if self.name == PYREFLY_PROVIDER:
            return f"{PYREFLY_PROVIDER} {PYREFLY_VERSION}"
        return f"{SCIP_PYTHON_PROVIDER} {SCIP_PYTHON_VERSION}"


def configured_python_graph_provider(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Return the provider pinned by the environment, or ``None`` for the default."""

    environ = os.environ if environ is None else environ
    value = (environ.get(PYTHON_GRAPH_PROVIDER_ENV) or "").strip().lower()
    if not value:
        return None
    if value not in _PROVIDERS:
        raise ValueError(
            f"{PYTHON_GRAPH_PROVIDER_ENV}={value!r} is not one of {_PROVIDERS}"
        )
    return value


def resolve_pyrefly(
    command_resolver: Optional[Callable[[str], Optional[str]]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    environ = os.environ if environ is None else environ
    configured = environ.get(PYREFLY_EXECUTABLE_ENV)
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    if command_resolver is None:
        from ..toolchains import resolve_command

        command_resolver = resolve_command
    return command_resolver("pyrefly")


def select_python_graph_provider(
    environ: Optional[Mapping[str, str]] = None,
    *,
    command_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> PythonGraphProvider:
    """Pick pyrefly when its pinned release is installed, else scip-python.

    A provider pinned by the environment is returned even when it cannot run,
    so the build reports it as unavailable instead of silently switching.
    """

    configured = configured_python_graph_provider(environ)
    if configured == SCIP_PYTHON_PROVIDER:
        return PythonGraphProvider(SCIP_PYTHON_PROVIDER, configured=True)
    executable = resolve_pyrefly(command_resolver, environ)
    version = pyrefly_version(executable) if executable else ""
    if executable and version == PYREFLY_VERSION:
        return PythonGraphProvider(
            PYREFLY_PROVIDER, executable=executable, configured=configured is not None
        )
    if executable:
        note = (
            f"pyrefly at {executable} reports version {version or 'unknown'}; "
            f"CodeNib needs {PYREFLY_VERSION}"
        )
    else:
        note = (
            f"pyrefly {PYREFLY_VERSION} is not installed; "
            'install it with `pip install "codenib[graph]"`'
        )
    if configured == PYREFLY_PROVIDER:
        return PythonGraphProvider(PYREFLY_PROVIDER, configured=True, note=note)
    return PythonGraphProvider(SCIP_PYTHON_PROVIDER, note=note)


_VERSIONS: dict[tuple[str, int, int], str] = {}


def pyrefly_version(executable: str) -> str:
    """Return the executable's release, cached per file revision."""

    try:
        status = os.stat(executable)
    except OSError:
        return ""
    key = (executable, status.st_mtime_ns, status.st_size)
    if key not in _VERSIONS:
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        _VERSIONS[key] = result.stdout.strip().rsplit(" ", 1)[-1]
    return _VERSIONS[key]


def _toml_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def write_pyrefly_config(
    path: Path,
    project_root: Path,
    *,
    exclude_patterns: Iterable[str] = (),
    target_dir: Optional[str] = None,
) -> Path:
    """Write a config that checks every Python source under the root.

    Without one, pyrefly imports a repository's mypy configuration and may
    check only the files that configuration names (254 of 770 on
    strawberry). Globs are absolute so the config can live outside the
    checkout; the checkout itself is never written.
    """

    root = project_root.resolve().as_posix()
    base = f"{root}/{target_dir.strip('/')}" if target_dir else root
    includes = [f"{base}/**/*.py", f"{base}/**/*.pyi"]
    excludes = [f"{root}/{pattern.lstrip('/')}" for pattern in exclude_patterns]
    # scip-python runs Pyright in place when the project configures it; apply
    # the same include/exclude scope so both routes index the same files.
    pyright = _pyright_scope(project_root)
    if pyright is not None and not target_dir:
        scoped, scoped_excludes = pyright
        if scoped:
            includes = []
            for entry in scoped:
                if (
                    any(character in entry for character in "*?[")
                    or (project_root / entry).is_file()
                ):
                    includes.append(f"{root}/{entry}")
                else:
                    includes.extend(
                        [f"{root}/{entry}/**/*.py", f"{root}/{entry}/**/*.pyi"]
                    )
        for entry in scoped_excludes:
            excludes.append(f"{root}/{entry}")
            if not any(character in entry for character in "*?["):
                excludes.append(f"{root}/{entry}/**")
    # Pyright also imports from ``src`` by default. It comes first so a
    # src-layout module is named ``pkg.mod``, as its importers name it.
    search_path = [f"{root}/src", root] if (project_root / "src").is_dir() else [root]
    path.write_text(
        "\n".join(
            [
                f"project-includes = {_toml_list(includes)}",
                f"project-excludes = {_toml_list(excludes)}",
                f"search-path = {_toml_list(search_path)}",
                # pyrefly's own heuristics exclude any path with a hidden
                # component, including the root's parents (~/.codenib/repos/x),
                # which leaves nothing to check. Apply only the listed scope,
                # as scip-python's Pyright config does.
                "disable-project-excludes-heuristics = true",
                # Only the index is needed; diagnostics are not reported.
                'preset = "off"',
                # Pyright, behind scip-python, does not read ignore files.
                "use-ignore-files = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _pyright_scope(project_root: Path):
    """Return the project's Pyright ``include``/``exclude`` lists, if any."""

    config = None
    path = project_root / "pyrightconfig.json"
    if path.is_file():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = None
    else:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10
            import tomli as tomllib
        try:
            data = tomllib.loads(
                (project_root / "pyproject.toml").read_text(encoding="utf-8")
            )
            config = data.get("tool", {}).get("pyright")
        except (OSError, ValueError):
            config = None
    if not isinstance(config, dict):
        return None

    def entries(key):
        values = config.get(key) or []
        return [
            value.strip("/").removeprefix("./")
            for value in values
            if isinstance(value, str) and value.strip("/")
        ]

    return entries("include"), entries("exclude")


def pyrefly_command(
    executable: str,
    config: Path,
    glean_dir: Path,
    *,
    interpreter: Optional[str] = None,
) -> list[str]:
    command = [
        executable,
        "check",
        "--config",
        str(config),
        "--report-glean",
        str(glean_dir),
        "--output-format",
        "omit-errors",
        "--summary=none",
        "--progress-bar",
        "no",
    ]
    if interpreter:
        command.extend(["--python-interpreter-path", interpreter])
    return command


__all__ = [
    "PYREFLY_EXECUTABLE_ENV",
    "PYREFLY_PROVIDER",
    "PYTHON_GRAPH_PROVIDER_ENV",
    "PythonGraphProvider",
    "SCIP_PYTHON_PROVIDER",
    "configured_python_graph_provider",
    "pyrefly_command",
    "pyrefly_version",
    "resolve_pyrefly",
    "select_python_graph_provider",
    "write_pyrefly_config",
]
