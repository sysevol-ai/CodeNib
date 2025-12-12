"""
Common utility functions: cache directory management, Git repository operations, etc.

These functions can be reused by multiple modules such as SWE-bench, LOC-bench, etc.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from ..log_utils import get_logger

logger = get_logger(__name__)


def get_cache_dir() -> str:
    """
    Get default cache directory path.

    Returns:
        Absolute path to cache directory (~/.codeminer)
    """
    cache_dir = str(Path.home()) + "/.codeminer"
    cache_dir = os.path.abspath(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def get_repo_path(repo: str, cache_dir: Optional[str] = None) -> str:
    """
    Get local storage path based on repo name.

    Naming rule: replace "/" in repo name with "_"
    Example: tokio-rs/tokio -> ~/.codeminer/tokio-rs_tokio

    Args:
        repo: Repository name, e.g., "tokio-rs/tokio"
        cache_dir: Cache directory, defaults to ~/.codeminer

    Returns:
        Local repository path
    """
    if cache_dir is None:
        cache_dir = get_cache_dir()
    repo_dir_name = repo.replace("/", "_")
    return os.path.join(cache_dir, repo_dir_name)


def clone_repo(
    repo: str,
    cache_dir: Optional[str] = None,
    shallow: bool = False,
) -> str:
    """
    Clone GitHub repository to local cache directory.

    Args:
        repo: Repository name, e.g., "tokio-rs/tokio"
        cache_dir: Cache directory, defaults to ~/.codeminer
        shallow: Whether to use shallow clone (--depth=1), default is full clone

    Returns:
        Local path after cloning

    Raises:
        RuntimeError: Raised when cloning fails
    """
    if cache_dir is None:
        cache_dir = get_cache_dir()

    repo_path = get_repo_path(repo, cache_dir)

    if os.path.exists(repo_path):
        logger.info(f"Repository {repo} already exists at {repo_path}")
        return repo_path

    git_url = f"https://github.com/{repo}.git"
    logger.info(f"Cloning repository {repo} to {repo_path}")

    cmd = ["git", "clone"]
    if shallow:
        cmd.extend(["--depth=1"])
    cmd.extend([git_url, repo_path])

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info(f"Successfully cloned {repo}")
        return repo_path
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to clone repository: {e}")
        logger.error(f"STDERR: {e.stderr.decode('utf-8')}")
        raise RuntimeError(f"Failed to clone repository {repo}") from e


def checkout_commit(repo_path: str, commit: str, fetch: bool = True) -> None:
    """
    Checkout repository to specified commit.

    Args:
        repo_path: Local repository path
        commit: Target commit hash
        fetch: Whether to fetch all first (default True)

    Raises:
        RuntimeError: Raised when checkout fails
    """
    original_dir = os.getcwd()
    os.chdir(repo_path)

    try:
        if fetch:
            logger.info("Fetching updates from remote repository")
            subprocess.run(
                ["git", "fetch", "--all"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        logger.info(f"Checking out commit {commit}")
        subprocess.run(
            ["git", "checkout", commit],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info(f"Successfully checked out commit {commit}")

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to checkout commit {commit}: {e}")
        logger.error(f"STDERR: {e.stderr.decode('utf-8')}")
        raise RuntimeError(f"Failed to checkout commit {commit}") from e
    finally:
        os.chdir(original_dir)
