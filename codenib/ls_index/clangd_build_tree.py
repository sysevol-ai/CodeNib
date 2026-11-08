# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Out-of-tree builds for C/C++ compilation-database generation.

clangd needs a compilation database, and Make/Autotools projects only emit
one by running their build. Building in the user's checkout leaves objects,
configure output and generated headers behind, which also changes the
repository source fingerprint the index compiler checks around a build. This
module keeps that build in CodeNib state instead:

* :func:`copy_source_tree` copies the checkout (without ``.git`` and
  CodeNib's own directories) into a private, freshly made copy.
* :func:`isolated_build_environment` stops git commands in the copy from
  discovering or writing any enclosing repository.
* :func:`rewrite_compile_database` points the copy's compile commands back at
  the checkout so clangd indexes the user's files, with copy include
  directories kept as fallbacks for headers the build generated.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..log_utils import get_logger

logger = get_logger("clangd_build_tree")

EXCLUDED_NAMES = frozenset({".git"})
EXCLUDED_PREFIXES = (".codenib",)

# Repository-selecting git variables a caller (e.g. a git hook) may export.
# Inherited by a build in the copy, they would point git at the user's repo.
_GIT_REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)

# Flags whose value is a header search directory: the checkout directory is
# searched first and the copy directory after it, so tracked headers resolve
# to the user's files and generated ones still resolve to the copy.
_SEARCH_DIR_FLAGS = ("-I", "-iquote", "-isystem", "-idirafter")
_LONG_SEARCH_DIR_FLAGS = ("--include-directory",)
# Flags naming one header file (forced includes).
_INCLUDE_FILE_FLAGS = ("-include", "-imacros")
# Flags naming files the compiler writes; they stay in the copy.
_OUTPUT_FLAGS = ("-o", "-MF", "-MJ", "--serialize-diagnostics")


@dataclass(frozen=True)
class CopyReport:
    """What one :func:`copy_source_tree` call put in the copy."""

    files: int
    links: int
    skipped: int


def is_excluded_name(name: str) -> bool:
    """Whether a checkout entry with this name stays out of the copy."""

    return name in EXCLUDED_NAMES or name.startswith(EXCLUDED_PREFIXES)


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _copy_symlink(
    source: Path, destination: Path, *, checkout: str, copy_root: str
) -> None:
    target = os.readlink(source)
    if os.path.isabs(target):
        normalized = os.path.normpath(target)
        if _is_within(normalized, checkout):
            # An absolute link into the checkout would let the build write
            # there; point it at the same path inside the copy instead.
            target = os.path.join(copy_root, os.path.relpath(normalized, checkout))
    os.symlink(target, destination)


def copy_source_tree(
    checkout: str | Path,
    copy_root: str | Path,
    *,
    exclude: Iterable[str | Path] = (),
) -> CopyReport:
    """Replace ``copy_root`` with a fresh, independent copy of ``checkout``.

    ``.git`` (directory or gitlink file, at any depth) and ``.codenib*``
    entries are skipped; submodule working trees are copied as they are.
    Regular files are copied byte for byte into new inodes (never hard
    links) and keep their mtimes, which Make and Autotools compare. Symlinks
    are recreated; absolute ones into the checkout are redirected into the
    copy. Other special files are skipped.

    The copy is always rebuilt from scratch: bear only records compiler
    runs, so an incremental build in a reused copy misses every translation
    unit that is already up to date (redis' ``make clean`` keeps ``deps/``).
    """

    source_root = Path(checkout).resolve()
    destination_root = Path(copy_root).absolute()
    source_text = str(source_root)
    destination_text = str(destination_root)
    if _is_within(source_text, destination_text):
        raise ValueError(
            f"refusing to replace {destination_root}: it contains the checkout"
        )
    # Never copy the copy into itself when CodeNib state lives in the checkout.
    excluded = {str(Path(path).absolute()) for path in (*exclude, destination_root)}
    if destination_root.is_symlink() or destination_root.is_file():
        destination_root.unlink()
    elif destination_root.exists():
        shutil.rmtree(destination_root)
    destination_root.mkdir(parents=True)

    files = links = skipped = 0
    for directory, dirnames, filenames in os.walk(source_root, followlinks=False):
        relative_directory = os.path.relpath(directory, source_root)
        target_directory = destination_root / relative_directory
        kept = []
        for name in sorted(dirnames):
            path = os.path.join(directory, name)
            if is_excluded_name(name) or path in excluded:
                continue
            if os.path.islink(path):
                filenames.append(name)
                continue
            (target_directory / name).mkdir()
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if is_excluded_name(name):
                continue
            source = Path(directory) / name
            destination = target_directory / name
            try:
                metadata = source.lstat()
            except OSError:
                skipped += 1
                continue
            if stat.S_ISLNK(metadata.st_mode):
                _copy_symlink(
                    source,
                    destination,
                    checkout=source_text,
                    copy_root=destination_text,
                )
                links += 1
            elif stat.S_ISREG(metadata.st_mode):
                shutil.copy2(source, destination, follow_symlinks=False)
                files += 1
            else:
                skipped += 1
    report = CopyReport(files, links, skipped)
    logger.info(
        "Copied %d files and %d links from %s to %s",
        report.files,
        report.links,
        source_root,
        destination_root,
    )
    return report


def isolated_build_environment(
    build_root: str | Path,
    base: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Environment for build commands run inside a build copy.

    The copy has no ``.git``, so git run by the build (version scripts,
    ``git describe``) finds no repository; the ceiling keeps discovery from
    climbing into a repository that encloses CodeNib state, and inherited
    repository variables are dropped.
    """

    environment = dict(os.environ if base is None else base)
    for name in _GIT_REPOSITORY_VARIABLES:
        environment.pop(name, None)
    ceiling = str(Path(build_root).absolute().parent)
    existing = environment.get("GIT_CEILING_DIRECTORIES")
    environment["GIT_CEILING_DIRECTORIES"] = (
        f"{ceiling}{os.pathsep}{existing}" if existing else ceiling
    )
    return environment


def without_git_repository_variables(
    base: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """The current environment minus inherited repository-selecting variables."""

    environment = dict(os.environ if base is None else base)
    for name in _GIT_REPOSITORY_VARIABLES:
        environment.pop(name, None)
    return environment


class _PathMapper:
    def __init__(self, copy_root: Path, checkout_root: Path):
        self.copy_root = os.path.normpath(str(copy_root.absolute()))
        self.checkout_root = os.path.normpath(str(checkout_root.absolute()))

    def absolute(self, value: str, directory: str) -> str:
        return os.path.normpath(os.path.join(directory, value))

    def in_copy(self, path: str) -> bool:
        return _is_within(path, self.copy_root)

    def checkout_twin(self, path: str) -> str:
        return os.path.join(
            self.checkout_root, os.path.relpath(path, self.copy_root)
        ).rstrip(os.sep)

    def to_checkout_if(self, path: str, exists) -> str:
        if self.in_copy(path):
            twin = os.path.normpath(self.checkout_twin(path))
            if exists(twin):
                return twin
        return path


def _split_flag(argument: str, flags: Iterable[str]) -> Optional[tuple[str, str]]:
    """Return (flag, joined value) for ``-Ifoo`` / ``--flag=foo`` forms."""

    for flag in flags:
        if argument.startswith(f"{flag}=") and flag.startswith("--"):
            return flag, argument[len(flag) + 1 :]
        if (
            argument.startswith(flag)
            and len(argument) > len(flag)
            and (not flag.startswith("--"))
        ):
            return flag, argument[len(flag) :]
    return None


def _rewrite_arguments(
    arguments: list[str],
    *,
    directory: str,
    source: str,
    new_source: str,
    mapper: _PathMapper,
) -> list[str]:
    def search_dirs(flag: str, value: str, joined: bool) -> list[str]:
        if value == "-":  # the legacy ``-I-`` split marker is not a path
            return [f"{flag}{value}" if joined else flag] + ([] if joined else [value])
        path = mapper.absolute(value, directory)
        candidates = [path]
        if mapper.in_copy(path):
            twin = os.path.normpath(mapper.checkout_twin(path))
            if os.path.isdir(twin):
                candidates = [twin, path]
        rewritten: list[str] = []
        for candidate in candidates:
            if flag.startswith("--"):
                rewritten.append(f"{flag}={candidate}")
            elif joined:
                rewritten.append(f"{flag}{candidate}")
            else:
                rewritten.extend([flag, candidate])
        return rewritten

    result: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        following = arguments[index + 1] if index + 1 < len(arguments) else None
        if index > 0 and argument in (*_SEARCH_DIR_FLAGS, *_LONG_SEARCH_DIR_FLAGS):
            if following is None:
                result.append(argument)
                break
            result.extend(search_dirs(argument, following, joined=False))
            index += 2
            continue
        if index > 0 and argument in _INCLUDE_FILE_FLAGS and following is not None:
            path = mapper.absolute(following, directory)
            result.extend([argument, mapper.to_checkout_if(path, os.path.isfile)])
            index += 2
            continue
        if index > 0 and argument in _OUTPUT_FLAGS and following is not None:
            result.extend([argument, mapper.absolute(following, directory)])
            index += 2
            continue
        joined = (
            _split_flag(argument, (*_SEARCH_DIR_FLAGS, *_LONG_SEARCH_DIR_FLAGS))
            if index > 0
            else None
        )
        if joined is not None:
            result.extend(search_dirs(joined[0], joined[1], joined=True))
            index += 1
            continue
        if (
            index > 0
            and new_source != source
            and not argument.startswith("-")
            and mapper.absolute(argument, directory) == source
        ):
            result.append(new_source)
            index += 1
            continue
        result.append(argument)
        index += 1
    return result


def rewrite_compile_entry(
    entry: Mapping[str, Any], *, copy_root: Path, checkout_root: Path
) -> dict[str, Any]:
    """Point one compile command from the build copy back at the checkout."""

    mapper = _PathMapper(copy_root, checkout_root)
    directory = os.path.normpath(str(entry.get("directory") or mapper.copy_root))
    if not os.path.isabs(directory):
        directory = os.path.normpath(os.path.join(mapper.copy_root, directory))
    source = mapper.absolute(str(entry.get("file") or ""), directory)
    new_source = mapper.to_checkout_if(source, os.path.isfile)
    moved = new_source != source
    new_directory = (
        mapper.to_checkout_if(directory, os.path.isdir) if moved else directory
    )

    if isinstance(entry.get("arguments"), list):
        arguments = [str(argument) for argument in entry["arguments"]]
    else:
        arguments = shlex.split(str(entry.get("command") or ""))
    rewritten = _rewrite_arguments(
        arguments,
        directory=directory,
        source=source,
        new_source=new_source,
        mapper=mapper,
    )
    if moved and rewritten:
        # The compiler searched the main file's own directory first for
        # quoted includes; keep a header the build generated beside it
        # (redis src/release.h, jq src/version.h) reachable from the
        # checkout source.
        rewritten[1:1] = ["-iquote", os.path.dirname(source)]

    updated: dict[str, Any] = {}
    for key, value in entry.items():
        if key == "directory":
            updated[key] = new_directory
        elif key == "file":
            updated[key] = new_source
        elif key in ("arguments", "command"):
            if "arguments" not in updated:
                updated["arguments"] = rewritten
        elif key == "output":
            # The object is a product of the copy build and stays there.
            updated[key] = mapper.absolute(str(value), directory)
        else:
            updated[key] = value
    return updated


def rewrite_compile_database(
    source: str | Path,
    output: str | Path,
    *,
    copy_root: str | Path,
    checkout_root: str | Path,
) -> int:
    """Rewrite a compilation database produced in a build copy.

    Returns the number of entries whose main file moved to the checkout.
    Sources that exist only in the copy (generated during the build) keep
    their copy paths, so clangd indexes them outside the project root.
    """

    from .index_quality import load_compilation_database

    entries = load_compilation_database(source)
    copy_path = Path(copy_root)
    checkout_path = Path(checkout_root)
    rewritten = [
        rewrite_compile_entry(entry, copy_root=copy_path, checkout_root=checkout_path)
        for entry in entries
    ]
    copy_text = os.path.normpath(str(copy_path.absolute()))
    moved = sum(
        1 for entry in rewritten if not _is_within(str(entry.get("file")), copy_text)
    )
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}."
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(rewritten, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, destination)
    logger.info(
        "Rewrote %d of %d compile commands from %s to the checkout",
        moved,
        len(rewritten),
        copy_path,
    )
    return moved


__all__ = [
    "CopyReport",
    "copy_source_tree",
    "isolated_build_environment",
    "is_excluded_name",
    "rewrite_compile_database",
    "rewrite_compile_entry",
    "without_git_repository_variables",
]
