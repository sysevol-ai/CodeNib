#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exercise the supported CodeNib 0.2.2 to v0.2.3 upgrade boundary."""

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

BASELINE_VERSION = "0.2.2"


def _run(
    command: Sequence[object],
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


def _candidate_install_command(
    pip: Path,
    wheel: Path,
    *,
    expected_version: str,
) -> tuple[object, ...]:
    command: list[object] = [pip, "install", "--upgrade"]
    if expected_version == BASELINE_VERSION:
        # Main can carry v0.2.3 changes before its dedicated version bump.
        # Reinstall the same-version candidate so the push gate still exercises
        # package-file removal instead of letting pip retain the baseline wheel.
        command.append("--force-reinstall")
    command.append(wheel)
    return tuple(command)


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


def _installed_bm25_identity(
    python: Path,
    *,
    root: Path,
    env: dict[str, str],
) -> dict[str, object]:
    result = _run(
        [
            python,
            "-c",
            (
                "import json\n"
                "from codenib.compiler.index_builders import BM25IndexBuilder\n"
                "print(json.dumps(BM25IndexBuilder().artifact_identity()))\n"
            ),
        ],
        cwd=root,
        env=env,
    )
    identity = json.loads(result.stdout)
    if not isinstance(identity, dict):
        raise RuntimeError(f"installed BM25 builder has no identity: {identity!r}")
    return identity


def _assert_builder_contract(
    actual: object,
    expected: dict[str, object],
) -> None:
    if not isinstance(actual, dict):
        raise RuntimeError(f"upgraded BM25 view has no config: {actual!r}")
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"unexpected BM25 builder contract: {mismatches!r}")


def _assert_storage_surface(
    python: Path,
    *,
    root: Path,
    env: dict[str, str],
) -> None:
    result = _run(
        [
            python,
            "-c",
            (
                "import argparse\n"
                "import json\n"
                "from pathlib import Path\n"
                "import tempfile\n"
                "import codenib.storage as storage\n"
                "from codenib.cli import build_parser\n"
                "exports_resolved = all(\n"
                "    getattr(storage, name) is not None for name in storage.__all__\n"
                ")\n"
                "with tempfile.TemporaryDirectory(\n"
                "    prefix='codenib-storage-smoke-'\n"
                ") as temporary_directory:\n"
                "    wiki_store = storage.SQLiteWikiStore(\n"
                "        Path(temporary_directory) / 'wiki.sqlite3'\n"
                "    )\n"
                "    published = wiki_store.publish(\n"
                "        entry_id='page:release-smoke',\n"
                "        repository_id='release/upgrade-smoke',\n"
                "        envelope={'data': {'body': 'ok'}},\n"
                "    )\n"
                "    wiki_roundtrip = wiki_store.read(published.entry_id) == published\n"
                "parser = build_parser()\n"
                "top = next(action for action in parser._actions "
                "if isinstance(action, argparse._SubParsersAction))\n"
                "artifact = top.choices['artifact']\n"
                "commands = next(action for action in artifact._actions "
                "if isinstance(action, argparse._SubParsersAction))\n"
                "print(json.dumps({\n"
                "    'storage_kind': "
                "'package' if hasattr(storage, '__path__') else 'module',\n"
                "    'storage_exports': sorted(storage.__all__),\n"
                "    'exports_resolved': exports_resolved,\n"
                "    'wiki_roundtrip': wiki_roundtrip,\n"
                "    'retired_exports': sorted(name for name in "
                "('LocalCAS', 'SQLiteCatalog', 'StorageError') "
                "if hasattr(storage, name)),\n"
                "    'artifact_commands': sorted(commands.choices),\n"
                "}))\n"
            ),
        ],
        cwd=root,
        env=env,
    )
    surface = json.loads(result.stdout)
    if surface != {
        "storage_kind": "module",
        "exports_resolved": True,
        "wiki_roundtrip": True,
        "storage_exports": [
            "SQLiteWikiStore",
            "WIKI_ENVELOPE_MAX_BYTES",
            "WikiStore",
            "WikiStoreCorruptionError",
            "WikiStoreError",
            "WikiStoreSchemaError",
            "WikiStoreValidationError",
            "WikiStoredEntry",
        ],
        "retired_exports": [],
        "artifact_commands": ["fetch", "mcp-config", "pack", "verify"],
    }:
        raise RuntimeError(f"unexpected candidate storage surface: {surface!r}")


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
    catalog = root / "v0.2.2-catalog.sqlite3"
    cas_parent = root / "v0.2.2-cas-parent"
    cas_parent.mkdir()
    cas_root = cas_parent / "cas"
    workspace_root = root / "v0.2.2-workspaces"
    workspace_root.mkdir(mode=0o700)
    portable_artifact = workspace_root / "materialized-context"
    _run(
        [
            python,
            "-c",
            (
                "import sys\n"
                "from codenib.storage import LocalCAS, SQLiteCatalog\n"
                "LocalCAS.provision(sys.argv[1]).close()\n"
                "SQLiteCatalog(sys.argv[2]).close()\n"
            ),
            cas_root,
            catalog,
        ],
        cwd=root,
        env=environment,
    )
    _run(
        [
            codenib,
            "artifact",
            "import-cache",
            repository,
            "--cache-dir",
            manifest_path.parent,
            "--catalog",
            catalog,
            "--cas-root",
            cas_root,
            "--workspace-root",
            workspace_root,
            "--repository",
            "release/upgrade-smoke",
            "--namespace",
            "default",
            "--ref",
            "main",
            "--expected-generation",
            "0",
        ],
        cwd=root,
        env=environment,
    )
    _run(
        [
            codenib,
            "artifact",
            "materialize",
            "--catalog",
            catalog,
            "--cas-root",
            cas_root,
            "--workspace-root",
            workspace_root,
            "--repository",
            "release/upgrade-smoke",
            "--namespace",
            "default",
            "--ref",
            "main",
            "--expected-generation",
            "1",
            "--output",
            portable_artifact,
        ],
        cwd=root,
        env=environment,
    )

    _run(
        _candidate_install_command(
            pip,
            wheel,
            expected_version=expected_version,
        ),
        cwd=root,
        env=environment,
    )
    version = _run([codenib, "--version"], cwd=root, env=environment).stdout.strip()
    if version != f"codenib {expected_version}":
        raise RuntimeError(f"unexpected upgraded version: {version!r}")
    expected_bm25_identity = _installed_bm25_identity(
        python,
        root=root,
        env=environment,
    )
    _assert_storage_surface(python, root=root, env=environment)
    _assert_builder_contract(baseline.get("config"), expected_bm25_identity)
    _run(
        [
            codenib,
            "artifact",
            "verify",
            portable_artifact,
            "--repository",
            "release/upgrade-smoke",
            "--repo",
            repository,
        ],
        cwd=root,
        env=environment,
    )

    _run(
        [codenib, "index", repository, "--preset", "fast"],
        cwd=root,
        env=environment,
    )
    upgraded = _load_bm25(manifest_path)
    if upgraded.get("built_at") != baseline.get("built_at"):
        raise RuntimeError("the candidate rebuilt the compatible 0.2.2 BM25 view")
    _assert_builder_contract(upgraded.get("config"), expected_bm25_identity)

    _run(
        [codenib, "index", repository, "--preset", "fast"],
        cwd=root,
        env=environment,
    )
    reused = _load_bm25(manifest_path)
    if reused.get("built_at") != baseline.get("built_at"):
        raise RuntimeError("the compatible 0.2.2 BM25 view was rebuilt again")
    status = _run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        env=environment,
    ).stdout
    if status:
        raise RuntimeError(f"upgrade modified the target repository: {status!r}")
    print(
        f"Upgrade smoke passed: {BASELINE_VERSION} -> {expected_version}; "
        "BM25 and portable artifact reused; storage is Wiki-only"
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
