# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical filesystem locations owned by CodeNib."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

CODENIB_HOME_ENV = "CODENIB_HOME"
CODENIB_PREBUILT_DIR_ENV = "CODENIB_PREBUILT_DIR"
CODENIB_RESULTS_DIR_ENV = "CODENIB_RESULTS_DIR"
CODENIB_TEMP_DIR_ENV = "CODENIB_TEMP_DIR"

USER_STATE_DIRNAME = ".codenib"
REPO_INDEX_DIRNAME = ".codenib_cache"
REPOSITORIES_DIRNAME = "repositories"
QA_DATA_DIRNAME = ".codenib_qa"
CLANGD_INDEX_DIRNAME = ".codenib-index"
PROJECT_INSTALL_SENTINEL = ".codenib_install_cache"


def _configured_path(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name)
    return Path(value).expanduser() if value else default


def user_state_dir() -> Path:
    """Return the user-owned root for datasets, checkouts, and runtime state."""

    return _configured_path(CODENIB_HOME_ENV, Path.home() / USER_STATE_DIRNAME)


def prebuilt_data_dir() -> Path:
    """Return the root containing reusable prebuilt repository artifacts."""

    return _configured_path(
        CODENIB_PREBUILT_DIR_ENV,
        user_state_dir() / "prebuilt",
    )


def results_dir() -> Path:
    """Return the root for benchmark and experiment outputs."""

    return _configured_path(
        CODENIB_RESULTS_DIR_ENV,
        user_state_dir() / "results",
    )


def temp_state_dir() -> Path:
    """Return the root for disposable indexer and tool working files."""

    return _configured_path(
        CODENIB_TEMP_DIR_ENV,
        Path(tempfile.gettempdir()) / "codenib",
    )


def repository_state_key(repo_root: str | Path) -> str:
    """Return a readable, collision-resistant key for one local checkout."""

    root = Path(repo_root).expanduser().resolve()
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", root.name).strip("-").lower()
    digest_input = os.path.normcase(str(root)).encode("utf-8", errors="surrogatepass")
    digest = hashlib.sha256(digest_input).hexdigest()[:12]
    return f"{slug or 'repository'}-{digest}"


def repo_state_dir(repo_root: str | Path) -> Path:
    """Return the user-owned state directory for one repository checkout."""

    return user_state_dir() / REPOSITORIES_DIRNAME / repository_state_key(repo_root)


def repo_index_dir(repo_root: str | Path) -> Path:
    """Return the default user-owned index directory for one repository."""

    return repo_state_dir(repo_root) / "indexes"


def legacy_repo_index_dir(repo_root: str | Path) -> Path:
    """Return the pre-0.1 repository-local index location."""

    return Path(repo_root).resolve() / REPO_INDEX_DIRNAME
