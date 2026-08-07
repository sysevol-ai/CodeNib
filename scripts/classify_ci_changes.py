#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Classify whether a Git diff requires CodeNib's serial CI chain."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib

_VERSION_FILES = frozenset({"pyproject.toml", "uv.lock"})
_SERIAL_EXACT = frozenset(
    {
        ".github/workflows/ci.yml",
        ".github/workflows/ci-full.yml",
        "Makefile",
        "pyproject.toml",
        "scripts/classify_ci_changes.py",
        "scripts/check_graph_route_alignment.py",
        "scripts/smoke_lsp_graph.py",
        "scripts/smoke_scip_cold_start.py",
        "scripts/swebench_graph_index.py",
        "setup.py",
        "uv.lock",
        "codenib/ls_router.py",
        "codenib/types.py",
    }
)
_SERIAL_PREFIXES = (
    ".github/actions/prewarm-parsers/",
    ".github/actions/setup-env/",
    "third_party/",
    "core/",
    "codenib/code_chunking/",
    "codenib/dataset/",
    "codenib/graph/",
    "codenib/index/",
    "codenib/ls_index/",
    "test/chunker/",
    "test/dataset/",
    "test/graph/",
    "test/index/",
    "test/ls_index/",
    "test/scip/",
)

FileReader = Callable[[str], bytes]


@dataclass(frozen=True, slots=True)
class SerialDecision:
    run_serial: bool
    reason: str


def _affects_serial_chain(path: str) -> bool:
    return path in _SERIAL_EXACT or path.startswith(_SERIAL_PREFIXES)


def _project_without_release_metadata(
    payload: bytes,
) -> tuple[str, Mapping[str, object]]:
    document = tomllib.loads(payload.decode("utf-8"))
    project = document.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        raise ValueError("pyproject.toml has no project.version")
    version = project["version"]
    normalized = copy.deepcopy(document)
    normalized_project = normalized["project"]
    del normalized_project["version"]
    classifiers = normalized_project.get("classifiers", [])
    if not isinstance(classifiers, list):
        raise ValueError("project.classifiers must be an array")
    development = [
        value
        for value in classifiers
        if isinstance(value, str) and value.startswith("Development Status :: ")
    ]
    if len(development) > 1:
        raise ValueError("project has multiple development-status classifiers")
    if development:
        normalized_project["classifiers"] = [
            value for value in classifiers if value != development[0]
        ]
    return version, normalized


def _lock_without_project_version(
    payload: bytes,
) -> tuple[str, Mapping[str, object]]:
    document = tomllib.loads(payload.decode("utf-8"))
    packages = document.get("package")
    if not isinstance(packages, list):
        raise ValueError("uv.lock has no package table")
    matches = [
        index
        for index, package in enumerate(packages)
        if isinstance(package, dict)
        and package.get("name") == "codenib"
        and package.get("source") == {"editable": "."}
    ]
    if len(matches) != 1:
        raise ValueError("uv.lock must contain one editable codenib package")
    normalized = copy.deepcopy(document)
    package = normalized["package"][matches[0]]
    version = package.get("version")
    if not isinstance(version, str):
        raise ValueError("uv.lock codenib package has no version")
    del package["version"]
    return version, normalized


def _is_synchronized_version_update(
    read_before: FileReader,
    read_after: FileReader,
) -> bool:
    old_project_version, old_project = _project_without_release_metadata(
        read_before("pyproject.toml")
    )
    new_project_version, new_project = _project_without_release_metadata(
        read_after("pyproject.toml")
    )
    old_lock_version, old_lock = _lock_without_project_version(read_before("uv.lock"))
    new_lock_version, new_lock = _lock_without_project_version(read_after("uv.lock"))
    return bool(
        old_project_version == old_lock_version
        and new_project_version == new_lock_version
        and old_project_version != new_project_version
        and old_project == new_project
        and old_lock == new_lock
    )


def classify_serial_changes(
    changed_paths: Iterable[str],
    *,
    read_before: FileReader,
    read_after: FileReader,
) -> SerialDecision:
    """Return a conservative serial-chain decision for *changed_paths*."""

    paths = tuple(sorted(set(changed_paths)))
    serial_paths = tuple(path for path in paths if _affects_serial_chain(path))
    if not serial_paths:
        return SerialDecision(False, "no serial-chain paths changed")

    non_version_paths = tuple(
        path for path in serial_paths if path not in _VERSION_FILES
    )
    if non_version_paths:
        preview = ", ".join(json.dumps(path) for path in non_version_paths[:3])
        suffix = " ..." if len(non_version_paths) > 3 else ""
        return SerialDecision(True, f"serial-chain path changed: {preview}{suffix}")

    if set(serial_paths) != _VERSION_FILES:
        return SerialDecision(
            True,
            "release metadata changed without a synchronized pyproject/lock pair",
        )
    try:
        version_only = _is_synchronized_version_update(read_before, read_after)
    except (KeyError, TypeError, ValueError, tomllib.TOMLDecodeError):
        version_only = False
    if version_only:
        return SerialDecision(
            False,
            "synchronized project-version metadata update only",
        )
    return SerialDecision(
        True,
        "pyproject or lock content changed beyond the project version",
    )


def _git(repo: Path, *args: str, text: bool = False) -> bytes | str:
    result = subprocess.run(
        ("git", *args),
        cwd=repo,
        capture_output=True,
        check=True,
        text=text,
        timeout=60,
    )
    return result.stdout


def classify_refs(repo: Path, base: str, head: str) -> SerialDecision:
    raw_paths = _git(
        repo,
        "diff",
        "--name-only",
        "--no-renames",
        "--diff-filter=ACDMRTUXB",
        "-z",
        base,
        head,
    )
    assert isinstance(raw_paths, bytes)
    paths = [
        value.decode("utf-8", errors="surrogateescape")
        for value in raw_paths.split(b"\0")
        if value
    ]

    def reader(revision: str) -> FileReader:
        def read(path: str) -> bytes:
            value = _git(repo, "show", f"{revision}:{path}")
            assert isinstance(value, bytes)
            return value

        return read

    return classify_serial_changes(
        paths,
        read_before=reader(base),
        read_after=reader(head),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        decision = classify_refs(args.repo.resolve(), args.base, args.head)
    except (OSError, subprocess.SubprocessError) as exc:
        decision = SerialDecision(
            True,
            f"change classification failed conservatively: {type(exc).__name__}",
        )
    if args.github_output:
        print(f"run-serial={'true' if decision.run_serial else 'false'}")
        print(f"serial-reason={decision.reason}")
    else:
        print(
            json.dumps(
                {
                    "run_serial": decision.run_serial,
                    "reason": decision.reason,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
