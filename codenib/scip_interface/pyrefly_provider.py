# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Opt-in pyrefly route for the Python symbol graph (prototype).

Set ``CODENIB_PYTHON_GRAPH_PROVIDER=pyrefly`` to build ``index.scip`` from
``pyrefly check --report-glean`` instead of scip-python. The converted index
uses scip-python's symbol conventions, so decoding, the C++ parity decoder and
the occurrence index run unchanged. scip-python stays the default.

``CODENIB_PYREFLY`` names the pyrefly executable; otherwise the managed
toolchain and ``PATH`` are searched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

PYTHON_GRAPH_PROVIDER_ENV = "CODENIB_PYTHON_GRAPH_PROVIDER"
PYREFLY_EXECUTABLE_ENV = "CODENIB_PYREFLY"
SCIP_PYTHON_PROVIDER = "scip-python"
PYREFLY_PROVIDER = "pyrefly"
_PROVIDERS = (SCIP_PYTHON_PROVIDER, PYREFLY_PROVIDER)


def python_graph_provider(environ: Optional[dict] = None) -> str:
    """Return the configured Python graph provider; scip-python by default."""

    environ = os.environ if environ is None else environ
    value = (environ.get(PYTHON_GRAPH_PROVIDER_ENV) or "").strip().lower()
    if not value:
        return SCIP_PYTHON_PROVIDER
    if value not in _PROVIDERS:
        raise ValueError(
            f"{PYTHON_GRAPH_PROVIDER_ENV}={value!r} is not one of {_PROVIDERS}"
        )
    return value


def resolve_pyrefly() -> Optional[str]:
    configured = os.environ.get(PYREFLY_EXECUTABLE_ENV)
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    from ..toolchains import resolve_command

    return resolve_command("pyrefly") or shutil.which("pyrefly")


def pyrefly_version(executable: str) -> str:
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
    return result.stdout.strip().rsplit(" ", 1)[-1]


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
                if (project_root / entry).is_file():
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
    "SCIP_PYTHON_PROVIDER",
    "pyrefly_command",
    "pyrefly_version",
    "python_graph_provider",
    "resolve_pyrefly",
    "write_pyrefly_config",
]
