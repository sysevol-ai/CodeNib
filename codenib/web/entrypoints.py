# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Where a repository says its own code starts, read from its build manifest.

Ranking by graph degree answers "which symbol is busiest", which is a
graph-theoretic answer to a product question. A reader opening a repo wants
"what does this thing expose". The repo already states that, precisely, in a
file the build system depends on being correct:

* ``package.json`` — ``source`` / ``exports`` / ``module`` / ``main`` / ``bin``
* ``pyproject.toml`` — ``[project.scripts]``, ``[project.gui-scripts]``
* ``Cargo.toml`` — ``[[bin]]`` / ``[lib]``, plus the conventional
  ``src/main.rs`` / ``src/lib.rs``
* ``go.mod`` — the module, plus ``main.go`` and ``cmd/*/main.go``

Two wrinkles this has to handle, both seen in the demo pool:

* ``main`` usually points at build output. preact's is ``dist/preact.mjs`` and
  axios's is ``./dist/node/axios.cjs`` — neither is in the symbol graph. Entries
  under a build directory are dropped, and ``source`` (preact: ``src/index.js``)
  is preferred precisely because it is the unbundled one.
* An ``exports`` subpath often names only its ``.d.ts``. preact's ``./compat``
  resolves to ``compat/src/index.d.ts``; the implementation next to it
  (``compat/src/index.js``) is what belongs on a map, so declaration entries are
  remapped to a sibling source file when one exists.

Everything here is best-effort: a malformed or missing manifest yields no entry
points, never an exception.
"""

from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Set

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 path
    import tomli as tomllib  # type: ignore[no-redef]

from ..utils import is_non_source_file, is_source_file

# Directories whose contents are build output rather than authored source.
_BUILD_DIRS = frozenset(
    {"dist", "build", "out", "lib-esm", "es", "umd", "node_modules"}
)
# Declaration suffix -> the implementation extensions to look for beside it.
_DECLARATION_SIBLINGS = {
    ".d.ts": (".ts", ".tsx", ".js", ".jsx", ".mjs"),
    ".d.cts": (".cts", ".cjs", ".ts", ".js"),
    ".d.mts": (".mts", ".mjs", ".ts", ".js"),
    ".pyi": (".py",),
}
_MAX_ENTRIES = 24


class EntryPoint:
    """A repo-relative path the manifest names as an entry, and why."""

    __slots__ = ("path", "kind", "label")

    def __init__(self, path: str, kind: str, label: str) -> None:
        self.path = path
        self.kind = kind  # "package" | "subpath" | "script" | "binary" | "library"
        self.label = label  # what the manifest called it, e.g. "preact/compat"

    def to_dict(self) -> Dict[str, str]:
        return {"path": self.path, "kind": self.kind, "label": self.label}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EntryPoint({self.path!r}, {self.kind!r}, {self.label!r})"


def _normalize(path: str) -> Optional[str]:
    """Return a lexical repository-relative manifest path, or ``None``."""

    raw = path.replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    if not raw or "\x00" in raw:
        return None

    value = PurePosixPath(raw)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        return None
    first = value.parts[0]
    if len(first) >= 2 and first[0].isalpha() and first[1] == ":":
        return None
    return value.as_posix()


def _repo_path_is(repo_dir: str, relative: str, *, directory: bool) -> bool:
    """Return whether *relative* resolves to the requested kind inside the repo."""

    normalized = _normalize(relative)
    if normalized is None:
        return False
    root = os.path.realpath(repo_dir)
    candidate = os.path.realpath(os.path.join(root, *PurePosixPath(normalized).parts))
    try:
        if os.path.commonpath((root, candidate)) != root:
            return False
    except ValueError:
        return False
    return os.path.isdir(candidate) if directory else os.path.isfile(candidate)


def _under_build_dir(path: str) -> bool:
    parts = path.split("/")
    return any(part in _BUILD_DIRS for part in parts[:-1])


def _resolve_in_repo(repo_dir: str, path: str) -> Optional[str]:
    """Map a manifest path to an existing repo-relative source file.

    Drops build output, and rewrites a declaration file to the implementation
    beside it so the map shows code rather than types.
    """
    relative = _normalize(path)
    if not relative or not is_source_file(relative):
        # babel's workspaces export "./package.json": "./package.json", which
        # resolves to a real file that is nonetheless not code.
        return None
    if _under_build_dir(relative) or is_non_source_file(relative):
        # A declaration entry is worth a second look; a bundle is not.
        stem = _declaration_sibling(repo_dir, relative)
        return stem
    if _repo_path_is(repo_dir, relative, directory=False):
        return relative
    return None


def _declaration_sibling(repo_dir: str, relative: str) -> Optional[str]:
    """``compat/src/index.d.ts`` -> ``compat/src/index.js`` when that exists."""
    if _under_build_dir(relative):
        return None
    for suffix, candidates in _DECLARATION_SIBLINGS.items():
        if not relative.endswith(suffix):
            continue
        stem = relative[: -len(suffix)]
        for extension in candidates:
            candidate = f"{stem}{extension}"
            if _repo_path_is(repo_dir, candidate, directory=False):
                return candidate
        return None
    return None


def _iter_export_targets(value: Any) -> Iterable[str]:
    """Yield every path string inside a package.json ``exports`` subtree."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_export_targets(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_export_targets(nested)


def _package_json_entries(repo_dir: str) -> List[EntryPoint]:
    path = os.path.join(repo_dir, "package.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict):
        return []
    name = manifest.get("name") if isinstance(manifest.get("name"), str) else "package"

    entries: List[EntryPoint] = []

    def add(raw: Any, kind: str, label: str) -> None:
        if not isinstance(raw, str):
            return
        resolved = _resolve_in_repo(repo_dir, raw)
        if resolved:
            entries.append(EntryPoint(resolved, kind, label))

    # ``source`` is the unbundled entry by convention, so it beats ``main``.
    add(manifest.get("source"), "package", name)
    exports = manifest.get("exports")
    if isinstance(exports, dict):
        for subpath, target in exports.items():
            if not isinstance(subpath, str):
                continue
            label = name if subpath in (".", "") else f"{name}{subpath.lstrip('.')}"
            for candidate in _iter_export_targets(target):
                resolved = _resolve_in_repo(repo_dir, candidate)
                if resolved:
                    entries.append(EntryPoint(resolved, "subpath", label))
                    break
    elif isinstance(exports, str):
        add(exports, "package", name)
    add(manifest.get("module"), "package", name)
    add(manifest.get("main"), "package", name)
    binaries = manifest.get("bin")
    if isinstance(binaries, str):
        add(binaries, "binary", name)
    elif isinstance(binaries, dict):
        for binary_name, target in binaries.items():
            add(target, "binary", str(binary_name))

    # A monorepo root (babel, docusaurus, vue) exports nothing itself; each
    # workspace package does. One level only — that is where the packages are.
    if not entries:
        entries.extend(_npm_workspaces(repo_dir, manifest.get("workspaces")))
    return entries


def _npm_workspaces(repo_dir: str, workspaces: Any) -> List[EntryPoint]:
    """Entries declared by each npm workspace package, one level deep."""
    patterns = workspaces
    if isinstance(workspaces, dict):
        patterns = workspaces.get("packages")
    if not isinstance(patterns, list):
        return []
    entries: List[EntryPoint] = []
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        for directory in _expand_glob(repo_dir, pattern):
            member_root = os.path.join(repo_dir, directory)
            manifest = f"{directory}/package.json"
            if not _repo_path_is(repo_dir, manifest, directory=False):
                continue
            for entry in _package_json_entries(member_root):
                entries.append(
                    EntryPoint(f"{directory}/{entry.path}", entry.kind, entry.label)
                )
            if len(entries) >= _MAX_ENTRIES:
                return entries
    return entries


def _pyproject_entries(repo_dir: str) -> List[EntryPoint]:
    path = os.path.join(repo_dir, "pyproject.toml")
    try:
        with open(path, "rb") as handle:
            manifest = tomllib.load(handle)
    except (OSError, ValueError):
        return []
    project = manifest.get("project")
    if not isinstance(project, dict):
        return []

    entries: List[EntryPoint] = []
    for table in ("scripts", "gui-scripts"):
        scripts = project.get(table)
        if not isinstance(scripts, dict):
            continue
        for script_name, target in scripts.items():
            if not isinstance(target, str):
                continue
            # "pkg.module:func" -> pkg/module.py
            module = target.split(":", 1)[0].strip()
            resolved = _module_to_file(repo_dir, module)
            if resolved:
                entries.append(EntryPoint(resolved, "script", str(script_name)))

    # A library declares no scripts — xarray, matplotlib, scikit-learn and sympy
    # all fall in that bucket. Its entry point is the package itself.
    name = project.get("name")
    if isinstance(name, str) and name:
        package = _module_to_file(repo_dir, name.replace("-", "_"))
        if package:
            entries.append(EntryPoint(package, "package", name))
    return entries


def _module_to_file(repo_dir: str, module: str) -> Optional[str]:
    """``xarray.cli.main`` -> ``xarray/cli/main.py`` (or its package __init__)."""
    if not module:
        return None
    stem = module.replace(".", "/")
    for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
        for prefix in ("", "src/", "lib/"):
            relative = _normalize(f"{prefix}{candidate}")
            if relative and _repo_path_is(repo_dir, relative, directory=False):
                return relative
    return None


def _cargo_entries(repo_dir: str) -> List[EntryPoint]:
    path = os.path.join(repo_dir, "Cargo.toml")
    try:
        with open(path, "rb") as handle:
            manifest = tomllib.load(handle)
    except (OSError, ValueError):
        return []

    entries: List[EntryPoint] = []
    package = manifest.get("package")
    name = package.get("name") if isinstance(package, dict) else None
    name = name if isinstance(name, str) else "crate"

    for binary in manifest.get("bin") or []:
        if not isinstance(binary, dict):
            continue
        target = binary.get("path")
        resolved = (
            _resolve_in_repo(repo_dir, target) if isinstance(target, str) else None
        )
        if resolved:
            entries.append(
                EntryPoint(resolved, "binary", str(binary.get("name") or name))
            )
    library = manifest.get("lib")
    if isinstance(library, dict) and isinstance(library.get("path"), str):
        resolved = _resolve_in_repo(repo_dir, library["path"])
        if resolved:
            entries.append(EntryPoint(resolved, "library", name))
    # Cargo's conventional targets need no declaration.
    for relative, kind in (("src/main.rs", "binary"), ("src/lib.rs", "library")):
        if _repo_path_is(repo_dir, relative, directory=False):
            entries.append(EntryPoint(relative, kind, name))

    # A workspace root (tokio, ruff) has no targets of its own — its members do.
    workspace = manifest.get("workspace")
    if isinstance(workspace, dict):
        entries.extend(_workspace_members(repo_dir, workspace.get("members")))
    return entries


def _workspace_members(repo_dir: str, members: Any) -> List[EntryPoint]:
    """Conventional lib/bin targets of each Cargo workspace member."""
    if not isinstance(members, list):
        return []
    entries: List[EntryPoint] = []
    for member in members:
        if not isinstance(member, str):
            continue
        for directory in _expand_glob(repo_dir, member):
            for suffix, kind in (("src/lib.rs", "library"), ("src/main.rs", "binary")):
                relative = f"{directory}/{suffix}"
                if _repo_path_is(repo_dir, relative, directory=False):
                    entries.append(
                        EntryPoint(relative, kind, directory.rsplit("/", 1)[-1])
                    )
                    break
    return entries


def _expand_glob(repo_dir: str, pattern: str) -> List[str]:
    """Expand a manifest directory glob (``crates/*``) to repo-relative dirs."""
    import glob as _glob

    normalized = _normalize(pattern)
    if normalized is None:
        return []
    if "*" not in normalized:
        return (
            [normalized] if _repo_path_is(repo_dir, normalized, directory=True) else []
        )
    matches = _glob.glob(os.path.join(repo_dir, *PurePosixPath(normalized).parts))
    return sorted(
        relative
        for match in matches
        if (
            (relative := _normalize(os.path.relpath(match, repo_dir))) is not None
            and _repo_path_is(repo_dir, relative, directory=True)
        )
    )


def _go_entries(repo_dir: str) -> List[EntryPoint]:
    if not _repo_path_is(repo_dir, "go.mod", directory=False):
        return []
    entries: List[EntryPoint] = []
    if _repo_path_is(repo_dir, "main.go", directory=False):
        entries.append(EntryPoint("main.go", "binary", os.path.basename(repo_dir)))
    cmd_dir = os.path.join(repo_dir, "cmd")
    if not _repo_path_is(repo_dir, "cmd", directory=True):
        return entries
    try:
        children = sorted(os.listdir(cmd_dir))
    except OSError:
        return entries
    for child in children:
        relative = f"cmd/{child}/main.go"
        if _repo_path_is(repo_dir, relative, directory=False):
            entries.append(EntryPoint(relative, "binary", child))
    return entries


def discover_entry_points(repo_dir: Optional[str]) -> List[EntryPoint]:
    """Entry points this repo declares, most authoritative first, deduplicated.

    Returns ``[]`` for an unknown/absent repo root — callers fall back to their
    existing ranking rather than showing nothing.
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        return []
    found: List[EntryPoint] = []
    for reader in (
        _package_json_entries,
        _pyproject_entries,
        _cargo_entries,
        _go_entries,
    ):
        try:
            found.extend(reader(repo_dir))
        except Exception:  # noqa: BLE001 - a broken manifest must not 500 a map
            continue

    seen: Set[str] = set()
    unique: List[EntryPoint] = []
    for entry in found:
        if entry.path in seen:
            continue
        seen.add(entry.path)
        unique.append(entry)
        if len(unique) >= _MAX_ENTRIES:
            break
    return unique


def entry_point_files(repo_dir: Optional[str]) -> Set[str]:
    """Just the repo-relative paths, for ranking lookups."""
    return {entry.path for entry in discover_entry_points(repo_dir)}
