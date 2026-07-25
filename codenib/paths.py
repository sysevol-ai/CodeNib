# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical filesystem locations owned by CodeNib."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

CODENIB_HOME_ENV = "CODENIB_HOME"
CODENIB_PREBUILT_DIR_ENV = "CODENIB_PREBUILT_DIR"
CODENIB_RESULTS_DIR_ENV = "CODENIB_RESULTS_DIR"
CODENIB_TEMP_DIR_ENV = "CODENIB_TEMP_DIR"

USER_STATE_DIRNAME = ".codenib"
REPO_INDEX_DIRNAME = ".codenib_cache"
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


def repo_index_dir(repo_root: str | Path) -> Path:
    """Return the default index directory for one repository checkout."""

    return Path(repo_root).resolve() / REPO_INDEX_DIRNAME
