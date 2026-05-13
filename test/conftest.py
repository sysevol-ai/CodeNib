#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pytest fixtures shared across test modules."""

import shutil
import subprocess
from pathlib import Path

import pytest
from filelock import FileLock

HTTPIE_REPO_URL = "https://github.com/httpie/cli.git"
HTTPIE_REPO_PATH = Path("/tmp/httpie-cli")
HTTPIE_REPO_REF = "3.2.4"

EXPRESS_REPO_URL = "https://github.com/expressjs/express.git"
EXPRESS_REPO_PATH = Path("/tmp/expressjs_express_test_repo")
EXPRESS_REPO_REF = "4.19.2"

FMT_REPO_URL = "https://github.com/fmtlib/fmt.git"
FMT_REPO_PATH = Path("/tmp/fmt-cpp-chunker")
FMT_REPO_REF = "10.2.1"

ARRAYVEC_REPO_URL = "https://github.com/bluss/arrayvec.git"
ARRAYVEC_REPO_PATH = Path("/tmp/arrayvec-chunker")
ARRAYVEC_REPO_REF = "0.7.4"

GJSON_REPO_URL = "https://github.com/tidwall/gjson.git"
GJSON_REPO_PATH = Path("/tmp/tidwall_gjson_test_repo")
GJSON_REPO_REF = "v1.17.1"


def ensure_repo_checkout(repo_url: str, repo_path: Path, ref: str) -> Path:
    """Clone and pin a repository checkout. Skip tests when network is unavailable."""

    def _checkout(target_ref: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "checkout", target_ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    if not repo_path.exists() or not (repo_path / ".git").exists():
        if repo_path.exists():
            shutil.rmtree(repo_path)
        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    ref,
                    repo_url,
                    str(repo_path),
                ],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            pytest.skip(f"Unable to clone {repo_url}@{ref}: {exc}")
    else:
        if _checkout(ref):
            return repo_path
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "fetch", "--tags"],
                check=True,
            )
            if not _checkout(ref):
                raise subprocess.CalledProcessError(1, ["git", "checkout", ref])
        except subprocess.CalledProcessError as exc:
            pytest.skip(f"Unable to update {repo_path} to {ref}: {exc}")
    return repo_path


def _locked_repo_checkout(
    tmp_path_factory, repo_url: str, repo_path: Path, ref: str
) -> Path:
    """Acquire a cross-worker filelock before cloning/checking out a repo."""
    lock_path = tmp_path_factory.getbasetemp().parent / f"{repo_path.name}.lock"
    with FileLock(str(lock_path)):
        return ensure_repo_checkout(repo_url, repo_path, ref)


@pytest.fixture(scope="session")
def httpie_cli_repo(tmp_path_factory):
    """Provide a pinned checkout of the httpie/cli repository."""
    return _locked_repo_checkout(
        tmp_path_factory, HTTPIE_REPO_URL, HTTPIE_REPO_PATH, HTTPIE_REPO_REF
    )


@pytest.fixture(scope="session")
def express_repo(tmp_path_factory):
    """Provide a pinned checkout of the express repository."""
    return _locked_repo_checkout(
        tmp_path_factory, EXPRESS_REPO_URL, EXPRESS_REPO_PATH, EXPRESS_REPO_REF
    )


@pytest.fixture(scope="session")
def fmt_repo(tmp_path_factory):
    """Provide a pinned checkout of the fmt repository."""
    return _locked_repo_checkout(
        tmp_path_factory, FMT_REPO_URL, FMT_REPO_PATH, FMT_REPO_REF
    )


@pytest.fixture(scope="session")
def arrayvec_repo(tmp_path_factory):
    """Provide a pinned checkout of the arrayvec repository."""
    return _locked_repo_checkout(
        tmp_path_factory, ARRAYVEC_REPO_URL, ARRAYVEC_REPO_PATH, ARRAYVEC_REPO_REF
    )


@pytest.fixture(scope="session")
def gjson_repo(tmp_path_factory):
    """Provide a pinned checkout of the gjson repository."""
    return _locked_repo_checkout(
        tmp_path_factory, GJSON_REPO_URL, GJSON_REPO_PATH, GJSON_REPO_REF
    )
