#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exercise the supported CodeNib 0.1 to 0.2 package upgrade boundary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Sequence

BASELINE_VERSION = "0.1.0"


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(str(part) for part in command), flush=True)
    return subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _venv_command(root: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return root / directory / f"{name}{suffix}"


def _manifest_path(
    python: Path,
    repo: Path,
    *,
    root: Path,
    env: dict[str, str],
) -> Path:
    result = _run(
        [
            python,
            "-c",
            (
                "import sys\n"
                "from codenib.paths import repo_index_dir\n"
                "print(repo_index_dir(sys.argv[1]) / 'repo_manifest.json')\n"
            ),
            repo,
        ],
        cwd=root,
        env=env,
    )
    return Path(result.stdout.strip())


def _load_bm25(manifest_path: Path) -> dict[str, object]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    bm25 = payload.get("indexes", {}).get("bm25")
    if not isinstance(bm25, dict) or bm25.get("status") != "fresh":
        raise RuntimeError(f"BM25 view is not fresh: {bm25!r}")
    return bm25


def smoke(wheel: Path, *, expected_version: str, root: Path) -> None:
    repository = root / "upgrade-repository"
    repository.mkdir(parents=True)
    (repository / "calculator.py").write_text(
        "def stable_upgrade_probe(left: int, right: int) -> int:\n"
        "    return left + right\n",
        encoding="utf-8",
    )
    _run(["git", "init", "--quiet"], cwd=repository, env=os.environ.copy())
    _run(
        ["git", "config", "user.email", "release-smoke@example.invalid"],
        cwd=repository,
        env=os.environ.copy(),
    )
    _run(
        ["git", "config", "user.name", "CodeNib Release Smoke"],
        cwd=repository,
        env=os.environ.copy(),
    )
    _run(["git", "add", "."], cwd=repository, env=os.environ.copy())
    _run(
        ["git", "commit", "--quiet", "-m", "initial fixture"],
        cwd=repository,
        env=os.environ.copy(),
    )

    environment = os.environ.copy()
    environment["HOME"] = str(root / "home")
    environment["CODENIB_HOME"] = str(root / "state")
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    Path(environment["HOME"]).mkdir()

    virtualenv = root / "venv"
    venv.EnvBuilder(with_pip=True).create(virtualenv)
    python = _venv_command(virtualenv, "python")
    pip = _venv_command(virtualenv, "pip")
    codenib = _venv_command(virtualenv, "codenib")

    _run(
        [pip, "install", f"codenib=={BASELINE_VERSION}"],
        cwd=root,
        env=environment,
    )
    _run(
        [codenib, "index", repository, "--preset", "fast"],
        cwd=root,
        env=environment,
    )
    manifest_path = _manifest_path(
        python,
        repository,
        root=root,
        env=environment,
    )
    baseline = _load_bm25(manifest_path)

    _run([pip, "install", "--upgrade", wheel], cwd=root, env=environment)
    version = _run([codenib, "--version"], cwd=root, env=environment).stdout.strip()
    if version != f"codenib {expected_version}":
        raise RuntimeError(f"unexpected upgraded version: {version!r}")

    _run(
        [codenib, "index", repository, "--preset", "fast"],
        cwd=root,
        env=environment,
    )
    upgraded = _load_bm25(manifest_path)
    if upgraded.get("built_at") == baseline.get("built_at"):
        raise RuntimeError("0.2 reused the incompatible 0.1 BM25 view")
    upgraded_config = upgraded.get("config")
    if not isinstance(upgraded_config, dict):
        raise RuntimeError(f"upgraded BM25 view has no config: {upgraded!r}")
    if upgraded_config.get("builder_schema") != 6:
        raise RuntimeError(f"unexpected BM25 builder schema: {upgraded_config!r}")
    if upgraded_config.get("repository_filter_policy") != 3:
        raise RuntimeError(f"unexpected repository filter policy: {upgraded_config!r}")

    _run(
        [codenib, "index", repository, "--preset", "fast"],
        cwd=root,
        env=environment,
    )
    reused = _load_bm25(manifest_path)
    if reused.get("built_at") != upgraded.get("built_at"):
        raise RuntimeError("the compatible 0.2 BM25 view was rebuilt again")
    status = _run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        env=environment,
    ).stdout
    if status:
        raise RuntimeError(f"upgrade modified the target repository: {status!r}")
    print(
        f"Upgrade smoke passed: {BASELINE_VERSION} -> {expected_version}; "
        "incompatible BM25 rebuilt once and then reused"
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    wheel = args.wheel.expanduser().resolve()
    if not wheel.is_file():
        print(f"upgrade smoke failed: wheel not found: {wheel}", file=sys.stderr)
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="codenib-upgrade-smoke-") as value:
            smoke(wheel, expected_version=args.expected_version, root=Path(value))
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"upgrade smoke failed: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            if exc.stdout:
                print(exc.stdout, file=sys.stderr)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
