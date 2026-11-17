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
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from ..toolchains import PYREFLY_VERSION, SCIP_PYTHON_VERSION

PYTHON_GRAPH_PROVIDER_ENV = "CODENIB_PYTHON_GRAPH_PROVIDER"
PYREFLY_EXECUTABLE_ENV = "CODENIB_PYREFLY"
SCIP_PYTHON_PROVIDER = "scip-python"
PYREFLY_PROVIDER = "pyrefly"
_PROVIDERS = (SCIP_PYTHON_PROVIDER, PYREFLY_PROVIDER)
# How CodeNib configures pyrefly. It is part of a graph's identity, so a graph
# built under an older configuration is rebuilt instead of reused.
#   layout-2: import roots found from the checkout's imports; linked
#   directories checked once.
PYREFLY_CONFIG_REVISION = "layout-2"


def pyrefly_identity(version: str) -> str:
    """Provider, release and configuration, recorded with graph artifacts."""

    return f"{PYREFLY_PROVIDER} {version} {PYREFLY_CONFIG_REVISION}"


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
            return pyrefly_identity(PYREFLY_VERSION)
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


# Directories that never hold a project's own import roots.
_SKIPPED_DIRECTORIES = frozenset(
    {"node_modules", "__pycache__", "site-packages", "venv", "build", "dist"}
)
_MAX_IMPORT_ROOTS = 32
_IMPORT = re.compile(rb"^[ \t]*(?:from|import)[ \t]+([A-Za-z_][A-Za-z0-9_]*)", re.M)
_MAX_SCANNED_BYTES = 256 * 1024
_INITS = ("__init__.py", "__init__.pyi")
_STANDARD_LIBRARY = frozenset(getattr(sys, "stdlib_module_names", ()))


def _importable_names(directory: Path, *, packages_only: bool) -> set[str]:
    """Names an ``import`` can find directly in ``directory``.

    A directory without ``__init__`` is importable too, as a namespace
    package; ``packages_only`` leaves those out.
    """

    names: set[str] = set()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return names
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if not packages_only or any(
                os.path.isfile(os.path.join(entry.path, init)) for init in _INITS
            ):
                names.add(entry.name)
        elif entry.name.endswith((".py", ".pyi")):
            names.add(entry.name.rsplit(".", 1)[0])
    return names


def python_layout(project_root: Path) -> tuple[list[str], list[str]]:
    """Find where a checkout's packages are imported from, and its linked copies.

    Returns ``(import_roots, linked_directories)``, both relative to the root.

    An import root is a directory that is not itself a package and holds one
    that the checkout imports by its bare name: ``lib`` for
    ``lib/streamlit/__init__.py`` when some file says ``import streamlit``.
    Without the root on the search path every such import is unresolved and
    its callers have no edge into the package.

    The import is the evidence. A directory that merely holds packages may be
    a namespace package (``src/poetry``, imported as ``poetry.console``) or a
    test fixture; treating either as a root renames its modules to names
    nothing imports. The checkout's own root and ``src`` are always roots and
    are not returned.

    A linked directory is a symbolic link to a directory inside the checkout,
    such as ``libs/skinny/mlflow -> ../../mlflow``. Checked as its own tree it
    repeats every definition under a second name that nothing refers to.
    """

    root = project_root.resolve()
    candidates: list[Path] = []
    linked: list[str] = []
    imported: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(directory)
        kept = []
        for name in sorted(dirnames):
            path = here / name
            if path.is_symlink():
                try:
                    inside = path.resolve().is_relative_to(root)
                except OSError:
                    inside = False
                if inside:
                    linked.append(path.relative_to(root).as_posix())
            elif not name.startswith(".") and name not in _SKIPPED_DIRECTORIES:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if name.endswith((".py", ".pyi")):
                try:
                    with open(here / name, "rb") as handle:
                        text = handle.read(_MAX_SCANNED_BYTES)
                except OSError:
                    continue
                imported.update(match.decode() for match in _IMPORT.findall(text))
        parent = here.parent
        if (
            here != root
            and parent != root
            and any(init in filenames for init in _INITS)
            and not any((parent / init).is_file() for init in _INITS)
            and parent not in candidates
        ):
            candidates.append(parent)

    own = [root / "src", root] if (root / "src").is_dir() else [root]
    reachable = set().union(
        *(_importable_names(entry, packages_only=False) for entry in own)
    )
    # ``import json`` is no evidence for a package that happens to be named
    # ``json``, and a root holding one would capture that import.
    imported -= _STANDARD_LIBRARY

    def namespace_in_use(candidate: Path) -> bool:
        """``src/poetry`` when the checkout imports ``poetry``."""

        for entry in own:
            if candidate.is_relative_to(entry):
                parts = candidate.relative_to(entry).parts
                if parts and all(part.isidentifier() for part in parts):
                    return parts[0] in imported
        return False

    roots = [
        candidate.relative_to(root).as_posix()
        for candidate in candidates
        if candidate not in own
        and not namespace_in_use(candidate)
        and (_importable_names(candidate, packages_only=True) & imported) - reachable
    ]
    roots.sort(key=lambda entry: (entry.count("/"), entry))
    return roots[:_MAX_IMPORT_ROOTS], linked


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
    import_roots, linked = python_layout(project_root)
    for entry in linked:
        excludes.extend([f"{root}/{entry}", f"{root}/{entry}/**"])
    # Pyright also imports from ``src`` by default. It comes first so a
    # src-layout module is named ``pkg.mod``, as its importers name it.
    own = [f"{root}/src", root] if (project_root / "src").is_dir() else [root]
    # A module is named after the first root that holds it, so another root
    # must also come first for ``lib/shop`` to be ``shop``, the name its
    # importers use. It may only do so when no package of its own is already
    # importable from an earlier root; then every import that resolved
    # before keeps its target. A root that would shadow one goes last.
    taken = set().union(
        *(_importable_names(Path(entry), packages_only=False) for entry in own)
    )
    first: list[str] = []
    last: list[str] = []
    for entry in import_roots:
        names = _importable_names(project_root / entry, packages_only=True)
        if names & taken:
            last.append(f"{root}/{entry}")
        else:
            first.append(f"{root}/{entry}")
            taken |= names
    search_path = [*first, *own, *last]
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
    "PYREFLY_CONFIG_REVISION",
    "PythonGraphProvider",
    "pyrefly_identity",
    "python_layout",
    "SCIP_PYTHON_PROVIDER",
    "configured_python_graph_provider",
    "pyrefly_command",
    "pyrefly_version",
    "resolve_pyrefly",
    "select_python_graph_provider",
    "write_pyrefly_config",
]
