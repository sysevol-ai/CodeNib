# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""What a test is, and how to run it, in each language the guard covers.

The reach from a change to the code that calls it is the same graph walk in
every language. What differs is where the walk may stop: which definitions
are tests. Python and Go name them. Rust marks them with an attribute.
JavaScript and TypeScript write them as callbacks passed to ``it(...)``,
which the graph does not record as definitions, so there the test file is
the unit.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from ..utils import is_test_file

FUNCTION = "function"
FILE = "file"
_GO_TEST = re.compile(r"(Test|Benchmark|Fuzz|Example)(_|[A-Z0-9]|$)")
_RUST_TEST = re.compile(r"#\[\s*(?:\w+::)*(?:test|rstest|test_case|bench)\b")


@dataclass(frozen=True)
class Convention:
    language: str  # the key the graph is built with
    extensions: tuple[str, ...]
    unit: str  # FUNCTION: test definitions; FILE: whole test files


PYTHON = Convention("python", (".py", ".pyi"), FUNCTION)
GO = Convention("go", (".go",), FUNCTION)
RUST = Convention("rust", (".rs",), FUNCTION)
JAVASCRIPT = Convention("javascript", (".js", ".jsx", ".mjs", ".cjs"), FILE)
TYPESCRIPT = Convention("typescript", (".ts", ".tsx", ".mts", ".cts"), FILE)
CONVENTIONS = (PYTHON, GO, RUST, JAVASCRIPT, TYPESCRIPT)
_BY_EXTENSION: Dict[str, Convention] = {
    extension: convention
    for convention in CONVENTIONS
    for extension in convention.extensions
}
GUARD_EXTENSIONS = tuple(_BY_EXTENSION)


def convention_for(path: Optional[str]) -> Optional[Convention]:
    """The convention for a source path, or ``None`` when the guard skips it."""

    if not path or path.endswith(".d.ts"):
        return None
    return _BY_EXTENSION.get(PurePosixPath(path).suffix)


def languages_in(paths: Iterable[str]) -> List[str]:
    """Graph languages with at least one source file among ``paths``."""

    found = {
        convention.language
        for convention in map(convention_for, paths)
        if convention is not None
    }
    return [c.language for c in CONVENTIONS if c.language in found]


def is_test_definition(
    convention: Convention,
    name: str,
    path: str,
    start_line: Optional[int],
    lines: Callable[[str], Sequence[str]],
) -> bool:
    """Whether the function ``name`` defined in ``path`` is a test.

    ``start_line`` is 0-based; ``lines`` reads a file of the graph's source.
    """

    if convention is PYTHON:
        return is_test_file(path) and name.startswith("test")
    if convention is GO:
        return path.endswith("_test.go") and _GO_TEST.match(name) is not None
    if convention is RUST and start_line is not None:
        # ``#[test]`` sits on the definition's first lines or just above it.
        text = lines(path)
        window = text[max(0, start_line - 4) : start_line + 3]
        return any(_RUST_TEST.search(line) for line in window)
    return False


def test_id(convention: Convention, symbol: str) -> str:
    """How a runner names a test: ``path::Class::name``, or the file itself."""

    path, _, qualified = symbol.partition(":")
    if convention.unit == FILE or not qualified:
        return path
    return "::".join([path, *qualified.split(".")])


def test_commands(test_ids: Sequence[str], limit: int) -> List[str]:
    """One command per language that runs the first ``limit`` tests.

    JavaScript and TypeScript projects choose their own runner, so their test
    files are listed without a command.
    """

    by_language: Dict[str, List[str]] = {}
    for identifier in test_ids[:limit]:
        convention = convention_for(identifier.split("::", 1)[0])
        if convention is not None:
            by_language.setdefault(convention.language, []).append(identifier)
    commands: List[str] = []
    python = by_language.get("python")
    if python:
        commands.append("python -m pytest " + " ".join(map(shlex.quote, python)))
    go = by_language.get("go")
    if go:
        by_directory: Dict[str, List[str]] = {}
        for identifier in go:
            path, _, name = identifier.partition("::")
            directory = str(PurePosixPath(path).parent)
            by_directory.setdefault(directory, []).append(name.split("::")[-1])
        commands.append(
            " && ".join(
                f"go test ./{directory} -run "
                + shlex.quote("^(" + "|".join(dict.fromkeys(names)) + ")$")
                for directory, names in by_directory.items()
            )
        )
    rust = by_language.get("rust")
    if rust:
        names = dict.fromkeys(identifier.split("::")[-1] for identifier in rust)
        commands.append("cargo test -- " + " ".join(map(shlex.quote, names)))
    return commands
