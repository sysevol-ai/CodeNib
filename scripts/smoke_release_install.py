#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke-test an installed CodeNib CLI in an isolated sample repository."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Sequence


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command))
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _installed_manifest_path(
    repo: Path,
    *,
    root: Path,
    env: dict[str, str],
) -> Path:
    command = (
        "import sys\n"
        "from codenib.paths import repo_index_dir\n"
        "print(repo_index_dir(sys.argv[1]) / 'repo_manifest.json')\n"
    )
    result = _run(
        [sys.executable, "-c", command, str(repo)],
        cwd=root,
        env=env,
    )
    path = result.stdout.strip()
    if not path:
        raise RuntimeError("installed package returned an empty manifest path")
    return Path(path)


def smoke(root: Path, *, executable: str = "codenib") -> None:
    root.mkdir(parents=True, exist_ok=True)
    repo = root / "sample-repository"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def add(left: int, right: int) -> int:\n"
        '    """Return the sum of two integers."""\n'
        "    return left + right\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# Sample repository\n", encoding="utf-8")

    _run(["git", "init", "--quiet"], cwd=repo)
    _run(["git", "config", "user.email", "release-smoke@example.invalid"], cwd=repo)
    _run(["git", "config", "user.name", "CodeNib Release Smoke"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "--quiet", "-m", "initial fixture"], cwd=repo)

    env = os.environ.copy()
    env["CODENIB_HOME"] = str(root / "user-state")
    version = _run([executable, "--version"], cwd=root, env=env).stdout.strip()
    if not version.startswith("codenib "):
        raise RuntimeError(f"unexpected version output: {version!r}")
    _run(
        [executable, "doctor", "--require", "core"],
        cwd=root,
        env=env,
    )
    _run([executable, "index", str(repo)], cwd=root, env=env)

    manifest_path = _installed_manifest_path(repo, root=root, env=env)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    bm25 = payload.get("indexes", {}).get("bm25", {})
    if bm25.get("status") != "fresh":
        raise RuntimeError(f"BM25 view is not fresh: {bm25!r}")
    languages = payload.get("repo", {}).get("languages")
    if languages != ["python"]:
        raise RuntimeError(f"language auto-detection was unexpected: {languages!r}")
    status = _run(["git", "status", "--porcelain"], cwd=repo, env=env).stdout
    if status:
        raise RuntimeError(f"indexing modified the target repository: {status!r}")
    print(f"Installed CLI smoke passed with {version}")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--executable", default="codenib")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    executable = shutil.which(args.executable)
    if executable is None:
        print(
            f"release smoke failed: command not found: {args.executable}",
            file=sys.stderr,
        )
        return 1

    context = (
        nullcontext(args.root.expanduser().resolve())
        if args.root is not None
        else tempfile.TemporaryDirectory(prefix="codenib-release-smoke-")
    )
    try:
        with context as value:
            root = Path(value)
            smoke(root, executable=executable)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"release smoke failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            if exc.stdout:
                print(exc.stdout, file=sys.stderr)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
