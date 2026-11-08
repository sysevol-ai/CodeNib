# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Helpers for invoking rust-analyzer consistently."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import MutableMapping

from ..paths import repo_state_dir
from ..toolchains import RUST_TOOLCHAIN

DEFAULT_RUST_TOOLCHAIN = RUST_TOOLCHAIN
CARGO_LOCKFILE_ENV = "CARGO_RESOLVER_LOCKFILE_PATH"
RUST_ANALYZER_ENV = "CODENIB_RUST_ANALYZER"


def rust_toolchain() -> str:
    """Return the rustup toolchain used for rust-analyzer."""
    return os.environ.get("CODENIB_RUST_TOOLCHAIN") or DEFAULT_RUST_TOOLCHAIN


def rust_analyzer_command(*args: str) -> list[str]:
    """Build a rust-analyzer command.

    An explicit executable is useful when a repository triggers a bug in the
    managed release and an operator needs to validate a newer rust-analyzer
    without replacing the project-wide pin.

    Prefer ``rustup run`` so PATH entries for standalone rust-analyzer binaries
    cannot bypass the requested toolchain.
    """
    executable = os.environ.get(RUST_ANALYZER_ENV)
    if executable:
        return [os.path.expanduser(executable), *args]
    toolchain = rust_toolchain()
    if toolchain and shutil.which("rustup"):
        return ["rustup", "run", toolchain, "rust-analyzer", *args]
    return ["rust-analyzer", *args]


def redirect_cargo_lockfile(
    env: MutableMapping[str, str], project_root: str | Path, purpose: str
) -> None:
    """Point cargo at a lockfile in CodeNib's state instead of the checkout.

    rust-analyzer runs ``cargo metadata``, which writes ``Cargo.lock`` into a
    workspace that has none, as most libraries do. Until nightly-2026-02
    rust-analyzer redirected it with cargo's unstable ``--lockfile-path``; later
    cargo removed that flag in favour of the ``resolver.lockfile-path`` setting,
    so the file landed in the checkout and changed its source fingerprint. A
    repository's own ``Cargo.lock`` is copied first so that resolution matches
    it; the checkout's file is then neither read nor written. ``purpose`` keeps
    concurrent SCIP and LSP runs on separate files. A caller's own setting wins.
    """

    if CARGO_LOCKFILE_ENV in env:
        return
    root = Path(project_root)
    lockfile = repo_state_dir(root) / "cargo" / purpose / "Cargo.lock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    own = root / "Cargo.lock"
    if own.is_file() and not own.is_symlink():
        shutil.copyfile(own, lockfile)
    env[CARGO_LOCKFILE_ENV] = str(lockfile)
