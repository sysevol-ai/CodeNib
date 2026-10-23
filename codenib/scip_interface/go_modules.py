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

import json
import os
import re
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
            try:
                token = json.loads(token)
            except ValueError:
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

    A target is local when it starts with ``./``, ``../``, or ``/`` and has
    no version; a version on the left limits the replacement to that version.
    """
    root = project_root.resolve()
    found = []
    for verb, arguments in go_directives(go_file):
        if verb != "replace" or "=>" not in arguments:
            continue
        arrow = arguments.index("=>")
        old, new = arguments[:arrow], arguments[arrow + 1 :]
        if not old or len(new) != 1 or not new[0].startswith(("./", "../", "/")):
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


def active_workspace(project_root: Path) -> Optional[Path]:
    """The go.work file the root scip-go run uses, honoring ``GOWORK``."""
    setting = os.environ.get("GOWORK")
    if setting == "off":
        return None
    candidate = Path(setting) if setting else project_root / "go.work"
    return candidate if candidate.is_file() else None


# A mapping entry: (directory, required version or None). Directory "" is the
# root module itself, whose symbols keep their packages.
_Mapping = Dict[str, Tuple[str, Optional[str]]]


def rename_module_symbol(
    symbol: str, root_module: str, mapping: _Mapping, version: Optional[str] = None
) -> str:
    """Rename a module's scip-go symbol into the root module's namespace.

    ``version``, when given, replaces the symbol's version, so a reference to
    a required release meets the checked-out definition. An entry with a
    required version applies only to symbols at that version.
    """
    parts = symbol.split(" ", 4)
    if len(parts) < 5 or parts[:2] != ["scip-go", "gomod"]:
        return symbol
    module = parts[2]
    entry = mapping.get(module)
    if entry is None or (entry[1] is not None and entry[1] != parts[3]):
        return symbol
    directory = entry[0]

    def package(match: re.Match) -> str:
        path = match.group(1)
        if path == module or path.startswith(module + "/"):
            prefix = f"{root_module}/{directory}" if directory else root_module
            path = prefix + path[len(module) :]
        return f"`{path}`"

    parts[2] = root_module
    if version:
        parts[3] = version
    parts[4] = _PACKAGE.sub(package, parts[4])
    return " ".join(parts)


def _replacing(replacements: Iterable[Replacement]) -> _Mapping:
    return {r.path: (r.directory, r.version) for r in replacements}


def merge_module_indexes(
    root,
    nested,
    root_module: str,
    modules: Iterable[Tuple[str, str]] = (),
    replacements: Optional[Dict[str, List[Replacement]]] = None,
    workspace: Iterable[Replacement] = (),
):
    """Merge per-module SCIP indexes into ``root`` (``scip_pb2.Index`` objects).

    ``nested`` holds ``(module path, directory, index)`` for each module
    indexed on its own, and ``modules`` the ``(module path, directory)`` of
    nested modules the root run already covered through a workspace.
    ``replacements`` maps each go.mod directory (``""`` for the root) to its
    local replacements, which apply only to that module's documents;
    ``workspace`` holds the active go.work replacements, which take
    precedence in the root run, as in Go.

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
        module: (next(iter(directories)), None)
        for module, directories in places.items()
        if len(directories) == 1
    }
    unique[root_module] = ("", None)
    work = _replacing(workspace)
    covered = sorted({directory for _module, directory in modules}, key=len)

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

    # Root-run documents use the replacements of the module that holds them:
    # the deepest covered module directory, else the root module.
    root_renamers = {
        directory: renamer(
            {**unique, **_replacing(replacements.get(directory, ())), **work}
        )
        for directory in ["", *covered]
    }
    for document in root.documents:
        owner = ""
        for directory in covered:
            if document.relative_path.startswith(directory + "/"):
                owner = directory
        rename_document(document, root_renamers[owner])
    for information in root.external_symbols:
        rename_information(information, root_renamers[""])
    known = {information.symbol for information in root.external_symbols}
    for module, directory, index in nested:
        own = renamer(
            {
                **unique,
                **_replacing(replacements.get(directory, ())),
                module: (directory, None),
            }
        )
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
