# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Source-checkout helper for pinned ContextBench repositories, never model calls."""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from codenib.eval.benchmarks.contextbench_runner import (
    _git,
    audit_contextbench_snapshot,
    prepare_contextbench_repo,
)


def prepare_checkout(
    case,
    repos_root: Path,
    *,
    reference: Path | None = None,
    reference_commit: str | None = None,
    minimum_free_bytes: int = 12 * 1024**3,
) -> Path:
    """Prepare an isolated tree, optionally borrowing immutable local Git objects.

    Invariant: every returned tree is clean at exactly case.base_commit; existing
    checkouts are never reset or cleaned. A new tree is audited before its atomic
    rename publishes it. The caller assigns each task ID to one preparation lane;
    no lock spans Git operations. A competing nonempty published tree is not
    overwritten and must pass the same audit.

    The operator retains the reference directory, Git object store and captured
    HEAD for the entire borrowers' lifetime. This helper never changes reference
    refs, configuration or files, and checks it before/after preparation. Do not
    prune, delete or repurpose a donor while borrowers need it. Failed staging
    removes only this call's newly created temporary directory. Recovery reuses
    an audited existing destination; it never repairs one in place.

    The disk floor is a pre-dispatch check, not a filesystem quota. The caller
    owns remaining capacity and concurrency, including any in-flight fetches.
    """
    key = case.instance_id
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key):
        raise ValueError("Invalid source task identity")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", case.base_commit):
        raise ValueError("Source commit must be a full Git SHA")
    if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
        raise ValueError("Invalid source disk floor")
    if bool(reference) != bool(reference_commit):
        raise ValueError("Reference requires its captured commit")
    root = repos_root.resolve()
    destination = root / key
    if destination.exists():
        audit_contextbench_snapshot(destination, case.base_commit)
        return destination
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < minimum_free_bytes:
        raise OSError("Source staging disk floor reached")
    if reference is None:
        return prepare_contextbench_repo(case, root)
    reference = reference.resolve()
    audit_contextbench_snapshot(reference, reference_commit)
    temporary = Path(tempfile.mkdtemp(prefix=".checkout-", dir=root))
    try:
        # Local clone optimizations can be ignored for a shallow donor. Install
        # its alternate explicitly and let Git import the shallow boundary.
        _git(temporary, "init")
        _git(temporary, "config", "gc.auto", "0")
        _git(temporary, "config", "maintenance.auto", "false")
        objects = Path(_git(reference, "rev-parse", "--git-path", "objects"))
        if not objects.is_absolute():
            objects = reference / objects
        (temporary / ".git/objects/info/alternates").write_text(
            str(objects.resolve()) + "\n"
        )
        _git(
            temporary,
            "fetch",
            "--depth",
            "1",
            "--update-shallow",
            str(reference),
            reference_commit,
        )
        _git(temporary, "update-ref", "refs/contextbench/reference", reference_commit)
        _git(temporary, "remote", "add", "origin", case.repo_url)
        _git(temporary, "fetch", "--depth", "1", "origin", case.base_commit)
        _git(temporary, "checkout", "--detach", case.base_commit)
        _git(temporary, "submodule", "update", "--init", "--recursive", "--depth", "1")
        audit_contextbench_snapshot(temporary, case.base_commit)
        audit_contextbench_snapshot(reference, reference_commit)
        try:
            temporary.rename(destination)
        except OSError:
            if not destination.exists():
                raise
            audit_contextbench_snapshot(destination, case.base_commit)
        audit_contextbench_snapshot(destination, case.base_commit)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
