# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Symbol graphs of committed source, for the guard.

The guard compares a working tree with a base commit, so it needs the graph of
the base commit, not of the edited files. Each graph is built from a private
copy of the commit's tracked files (``git archive``), outside the checkout, and
kept for reuse. Building never touches the checkout, its index, or files the
user has not committed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from filelock import FileLock

from ..paths import repo_index_dir, repo_state_dir

KEEP_SNAPSHOTS = 3
FAILURE_TTL = 3600  # seconds before a failed build is tried again
GUARD_LANGUAGES = ("python",)


class SnapshotError(RuntimeError):
    """The graph of a commit cannot be built or loaded."""


def git(repo: Path, *arguments: str, binary: bool = False):
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=not binary,
        check=False,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr
            if not binary
            else completed.stderr.decode("utf-8", "replace")
        )
        raise SnapshotError(
            f"git {arguments[0]} failed: {' '.join(detail.split())[:400]}"
        )
    return completed.stdout


def resolve_commit(repo: Path, revision: str = "HEAD") -> str:
    return git(
        repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"
    ).strip()


def guard_dir(repo: Path) -> Path:
    return repo_state_dir(repo) / "guard"


@dataclass(frozen=True)
class Snapshot:
    commit: str
    source: Path  # the copy of the commit's tracked files
    seconds: Optional[float]

    def load_graph(self):
        """The commit's symbol graph as an igraph graph."""

        from ..compiler.graph_artifact import load_authenticated_graph_artifact
        from ..compiler.manifest import MANIFEST_FILENAME, RepoManifest

        manifest_path = repo_index_dir(self.source) / MANIFEST_FILENAME
        try:
            manifest = RepoManifest.load(manifest_path)
            entry = manifest.indexes["symbol_graph"]
            return load_authenticated_graph_artifact(entry).graph
        except (OSError, KeyError, ValueError, RuntimeError) as exc:
            raise SnapshotError(
                f"cannot load the graph of {self.commit[:12]}: {exc}"
            ) from exc


def _snapshot_root(repo: Path, commit: str) -> Path:
    return guard_dir(repo) / "snapshots" / commit


def cached_snapshot(repo: Path, commit: str) -> Optional[Snapshot]:
    root = _snapshot_root(repo, commit)
    try:
        ready = json.loads((root / "ready.json").read_text())
    except (OSError, ValueError):
        return None
    return Snapshot(commit, root / ready["source"], ready.get("seconds"))


def has_python(repo: Path, commit: str) -> bool:
    """Whether ``commit`` tracks any Python file the guard can graph."""

    names = git(repo, "ls-tree", "-r", "--name-only", "-z", commit).split("\0")
    return any(name.endswith((".py", ".pyi")) for name in names)


def failed_snapshot(repo: Path, commit: str) -> Optional[str]:
    """Why a build of ``commit`` failed within the last hour, or ``None``."""

    path = _snapshot_root(repo, commit) / "failed.json"
    try:
        if time.time() - path.stat().st_mtime > FAILURE_TTL:
            return None
        return json.loads(path.read_text())["error"]
    except (OSError, ValueError, KeyError):
        return None


def _extract(repo: Path, commit: str, destination: Path) -> None:
    archive = git(repo, "archive", "--format=tar", commit, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(destination, filter="data")
            return
        root = destination.resolve()
        members = []
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if root not in target.parents and target != root:
                raise SnapshotError(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk():
                continue
            members.append(member)
        tar.extractall(destination, members=members)


def build_snapshot(repo: Path, commit: str) -> Snapshot:
    """Build, or reuse, the graph of ``commit``. Progress goes to stderr."""

    root = _snapshot_root(repo, commit)
    root.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root) + ".lock"):
        cached = cached_snapshot(repo, commit)
        if cached is not None:
            return cached
        from ..cli import index_repository

        if root.exists():
            _remove_snapshot(root)
        source = root / (repo.resolve().name or "repository")
        source.mkdir(parents=True)
        started = time.monotonic()
        try:
            if not has_python(repo, commit):
                raise SnapshotError(
                    f"{commit[:12]} has no Python files; the guard supports "
                    "Python repositories"
                )
            _extract(repo, commit, source)
            with redirect_stdout(sys.stderr):
                _, failed = index_repository(
                    source,
                    languages=list(GUARD_LANGUAGES),
                    views=["symbol_graph"],
                    allow_partial_graph_languages=False,
                )
            if failed:
                raise SnapshotError(
                    f"graph build failed for {commit[:12]}: {', '.join(failed)}"
                )
        except Exception as exc:
            # Recorded so a waiting hook stops waiting; the next build retries.
            (root / "failed.json").write_text(json.dumps({"error": str(exc)}))
            raise
        seconds = round(time.monotonic() - started, 1)
        (root / "ready.json").write_text(
            json.dumps({"commit": commit, "source": source.name, "seconds": seconds})
        )
    _prune(repo, keep=commit)
    return Snapshot(commit, source, seconds)


def build_in_background(repo: Path, commit: str, log: Path) -> None:
    """Start ``codenib guard prepare`` for ``commit`` without waiting."""

    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as handle:
        # Run outside the repository so ``-m codenib`` cannot import its files.
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codenib",
                "guard",
                "prepare",
                str(repo),
                "--commit",
                commit,
            ],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            cwd=str(log.parent),
            env=os.environ.copy(),
        )


def _remove_snapshot(root: Path) -> None:
    for source in root.iterdir() if root.is_dir() else ():
        if source.is_dir():
            shutil.rmtree(repo_state_dir(source), ignore_errors=True)
    shutil.rmtree(root, ignore_errors=True)


def _prune(repo: Path, keep: str) -> None:
    snapshots = guard_dir(repo) / "snapshots"
    ready = sorted(
        (path for path in snapshots.iterdir() if (path / "ready.json").is_file()),
        key=lambda path: (path / "ready.json").stat().st_mtime,
        reverse=True,
    )
    for path in ready[KEEP_SNAPSHOTS:]:
        if path.name != keep:
            with FileLock(str(path) + ".lock"):
                _remove_snapshot(path)
