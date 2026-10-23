# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Go module layout for repositories split into nested modules.

scip-go indexes one module at a time. This module finds the nested modules,
reads the ``module`` and local ``replace`` directives of ``go.mod`` and
``go.work`` files, and merges per-module SCIP indexes into one. The merge
prefixes each document with its module's directory and renames module
symbols into the root module's namespace, so packages are named by
directory and references meet definitions across modules.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# Directories the go command ignores, plus vendored trees.
_SKIPPED_DIRS = {"vendor", "testdata", "node_modules"}
# Go lexical elements in go.mod and go.work: quoted and raw strings, the
# replacement arrow, block parentheses, and bare words.
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`|//|=>|[()]|[^\s()"`]+')
_PACKAGE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class Replacement:
    """A local ``replace``: ``path`` (at ``version`` if given) => ``directory``."""

    path: str
    version: Optional[str]
    directory: str


def _tokens(line: str) -> List[str]:
    tokens = []
    for match in _TOKEN.finditer(line):
        token = match.group(0)
        if token == "//":
            break
        if token.startswith('"'):
            # Go's interpreted-string escapes (\x, \u, octal, ...) match
            # Python's for the characters a path can hold.
            try:
                token = ast.literal_eval(token)
            except (ValueError, SyntaxError):
                token = token[1:-1]
        elif token.startswith("`"):
            token = token[1:-1]
        tokens.append(token)
    return tokens


def go_directives(path: Path) -> List[Tuple[str, List[str]]]:
    """``(verb, arguments)`` for every directive in a go.mod or go.work file.

    Block forms such as ``replace ( ... )`` yield one directive per line.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    directives: List[Tuple[str, List[str]]] = []
    block: Optional[str] = None
    for line in text.splitlines():
        tokens = _tokens(line)
        if not tokens:
            continue
        if block is not None:
            if tokens[0] == ")":
                block = None
            else:
                directives.append((block, tokens))
            continue
        if len(tokens) >= 2 and tokens[1] == "(":
            block = tokens[0]
            continue
        directives.append((tokens[0], tokens[1:]))
    return directives


def go_module_path(go_mod: Path) -> Optional[str]:
    """The module path declared in ``go_mod``, if any."""
    for verb, arguments in go_directives(go_mod):
        if verb == "module" and arguments:
            return arguments[0]
    return None


def nested_go_modules(project_root: Path) -> List[Tuple[str, str]]:
    """``(module path, directory)`` of modules below the root, in path order."""
    modules = []
    for directory, subdirs, files in os.walk(project_root):
        subdirs[:] = sorted(
            name
            for name in subdirs
            if name not in _SKIPPED_DIRS and not name.startswith((".", "_"))
        )
        relative = Path(directory).relative_to(project_root)
        if relative != Path(".") and "go.mod" in files:
            module = go_module_path(Path(directory) / "go.mod")
            if module:
                modules.append((module, relative.as_posix()))
    return modules


def local_replacements(go_file: Path, project_root: Path) -> List[Replacement]:
    """Local ``replace`` directives in ``go_file`` that point inside the checkout.

    A target is local when it is ``.`` or ``..`` or starts with ``./``,
    ``../``, or ``/``, and has no version. A version on the left limits the
    replacement to that version.
    """
    root = project_root.resolve()
    found = []
    for verb, arguments in go_directives(go_file):
        if verb != "replace" or "=>" not in arguments:
            continue
        arrow = arguments.index("=>")
        old, new = arguments[:arrow], arguments[arrow + 1 :]
        if not old or len(new) != 1 or not _local_path(new[0]):
            continue
        try:
            target = (go_file.parent / new[0]).resolve().relative_to(root)
        except ValueError:
            continue
        directory = target.as_posix()
        found.append(
            Replacement(
                old[0],
                old[1] if len(old) > 1 else None,
                "" if directory == "." else directory,
            )
        )
    return found


def _local_path(target: str) -> bool:
    """Whether a replacement target is a filesystem path, as Go decides."""
    return target in {".", ".."} or target.startswith(("./", "../", "/"))


def active_workspace(project_root: Path) -> Optional[Path]:
    """The go.work file a go command run in ``project_root`` would use.

    ``GOWORK`` names the file or ``off``; unset or ``auto`` searches the
    directory and its parents. When a candidate exists, ``go env GOWORK``
    has the final word.
    """
    setting = os.environ.get("GOWORK", "")
    if setting == "off":
        return None
    if setting and setting != "auto":
        candidate: Optional[Path] = Path(setting)
    else:
        candidate = next(
            (
                directory / "go.work"
                for directory in [project_root, *project_root.resolve().parents]
                if (directory / "go.work").is_file()
            ),
            None,
        )
    if candidate is None:
        return None
    try:
        result = subprocess.run(
            ["go", "env", "GOWORK"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return candidate if candidate.is_file() else None
    found = result.stdout.strip()
    return Path(found) if found and found != "off" else None


def workspace_includes(go_work: Path, directory: Path) -> bool:
    """Whether ``go_work`` lists ``directory`` in a ``use`` directive."""
    target = directory.resolve()
    return any(
        (go_work.parent / argument).resolve() == target
        for verb, arguments in go_directives(go_work)
        if verb == "use"
        for argument in arguments
    )


# Module path -> {required version (None for any) -> directory}. Directory ""
# is the root module itself, whose symbols keep their packages.
_Mapping = Dict[str, Dict[Optional[str], str]]


def _merged(*mappings: _Mapping) -> _Mapping:
    """Later mappings override earlier ones per module path and version."""
    result: _Mapping = {}
    for mapping in mappings:
        for module, versions in mapping.items():
            result[module] = {**result.get(module, {}), **versions}
    return result


def rename_module_symbol(
    symbol: str, root_module: str, mapping: _Mapping, version: Optional[str] = None
) -> str:
    """Rename a module's scip-go symbol into the root module's namespace.

    ``version``, when given, replaces the symbol's version, so a reference to
    a required release meets the checked-out definition. A version-specific
    entry applies only to symbols at that version and takes precedence over
    an entry for any version.
    """
    parts = symbol.split(" ", 4)
    if len(parts) < 5 or parts[:2] != ["scip-go", "gomod"]:
        return symbol
    module = parts[2]
    versions = mapping.get(module, {})
    directory = versions.get(parts[3], versions.get(None))
    if directory is None:
        return symbol

    def package(match: re.Match) -> str:
        path = match.group(1)
        if path == module or path.startswith(module + "/"):
            prefix = (
                f"{root_module}/{_escaped(directory)}" if directory else root_module
            )
            path = prefix + path[len(module) :]
        return f"`{path}`"

    parts[2] = root_module
    if version:
        parts[3] = version
    parts[4] = _PACKAGE.sub(package, parts[4])
    return " ".join(parts)


def _escaped(directory: str) -> str:
    """``directory`` with whitespace and backticks percent-encoded.

    Both decoders split symbols on whitespace, and a backtick would end the
    package descriptor; Go import paths contain neither.
    """
    return re.sub(r"[\s`%]", lambda match: f"%{ord(match.group()):02X}", directory)


def _replacing(replacements: Iterable[Replacement]) -> _Mapping:
    return _merged(*({r.path: {r.version: r.directory}} for r in replacements))


def merge_module_indexes(
    root,
    nested,
    root_module: str,
    modules: Iterable[Tuple[str, str]] = (),
    replacements: Optional[Dict[str, List[Replacement]]] = None,
    workspace: Iterable[Replacement] = (),
    members: Iterable[str] = (),
):
    """Merge per-module SCIP indexes into ``root`` (``scip_pb2.Index`` objects).

    ``nested`` holds ``(module path, directory, index)`` for each module
    indexed on its own, and ``modules`` the ``(module path, directory)`` of
    nested modules the root run already covered through a workspace.
    ``replacements`` maps each go.mod directory (``""`` for the root) to its
    local replacements. ``members`` lists the directories the active go.work
    uses; their runs build under the workspace, so they see the replacements
    of every member with go.work's ``workspace`` replacements taking
    precedence, as Go applies them. Any other run sees only its own go.mod.

    A module path declared in one directory is renamed everywhere. A path
    declared in several directories is renamed only inside its own index, so
    references to it from elsewhere stay unresolved rather than pick a
    directory. Root-module symbols keep their packages and take the
    canonical version.
    """
    replacements = replacements or {}
    pairs = {(module, directory) for module, directory, _ in nested}
    pairs |= set(modules)
    places: Dict[str, set] = {}
    for module, directory in pairs:
        if module != root_module:
            places.setdefault(module, set()).add(directory)
    unique: _Mapping = {
        module: {None: next(iter(directories))}
        for module, directories in places.items()
        if len(directories) == 1
    }
    unique[root_module] = {None: ""}
    members = set(members)

    # The root module's own version, else the first nested module's, so the
    # choice is deterministic even when the root holds no Go sources.
    version = _module_version(root, root_module)
    for module, _directory, index in nested:
        version = version or _module_version(index, module)

    def renamer(mapping: _Mapping):
        def rename(value: str) -> str:
            return rename_module_symbol(value, root_module, mapping, version)

        return rename

    def rename_information(information, rename) -> None:
        information.symbol = rename(information.symbol)
        information.enclosing_symbol = rename(information.enclosing_symbol)
        for relationship in information.relationships:
            relationship.symbol = rename(relationship.symbol)

    def rename_document(document, rename) -> None:
        for occurrence in document.occurrences:
            occurrence.symbol = rename(occurrence.symbol)
        for information in document.symbols:
            rename_information(information, rename)

    in_workspace = _merged(
        unique, *(_replacing(replacements.get(d, ())) for d in sorted(members))
    )
    for module, versions in _replacing(workspace).items():
        # go.work takes precedence: an any-version entry replaces every
        # module replacement of that path, a versioned one only its version.
        in_workspace[module] = (
            dict(versions)
            if None in versions
            else {**in_workspace.get(module, {}), **versions}
        )

    def effective(directory: str) -> _Mapping:
        if directory in members:
            return in_workspace
        return _merged(unique, _replacing(replacements.get(directory, ())))

    shared = renamer(effective(""))
    for document in root.documents:
        rename_document(document, shared)
    for information in root.external_symbols:
        rename_information(information, shared)
    known = {information.symbol for information in root.external_symbols}
    for module, directory, index in nested:
        own = renamer(_merged(effective(directory), {module: {None: directory}}))
        for document in index.documents:
            rename_document(document, own)
            document.relative_path = f"{directory}/{document.relative_path}"
            root.documents.append(document)
        for information in index.external_symbols:
            rename_information(information, own)
            if information.symbol not in known:
                known.add(information.symbol)
                root.external_symbols.append(information)
    return root


def _module_version(index, module: str) -> Optional[str]:
    """The version scip-go stamped on ``module``'s own symbols in ``index``."""
    for document in index.documents:
        for occurrence in document.occurrences:
            parts = occurrence.symbol.split(" ", 4)
            if len(parts) == 5 and parts[:3] == ["scip-go", "gomod", module]:
                return parts[3]
    return None
