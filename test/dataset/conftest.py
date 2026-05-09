#!/usr/bin/env python3
"""Pytest fixtures for dataset tests using real SWE-bench Multilingual data."""

import os
from pathlib import Path

import pytest
from filelock import FileLock

from codeminer.dataset.gt_locate import GTLocator
from codeminer.dataset.swebench_multilingual import SwebenchMultilingualDataset


def _gt_work_dir() -> Path:
    """Per-job dir on a CI runner, persistent /tmp dir locally.

    On GitHub Actions ``RUNNER_TEMP`` points at a directory the runner
    cleans up between jobs, so cross-run ownership drift on a self-hosted
    runner can't accumulate. Locally we keep a persistent path under
    ``/tmp`` so iterating on dataset tests doesn't re-clone every repo.
    """
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "codeminer-gt-test"
    return Path("/tmp/codeminer-gt-test")


GT_TEST_WORK_DIR = _gt_work_dir()

# One representative repo per language we support in gt_locate.
# Chosen for moderate size and sufficient instance count.
REPO_FIXTURES = {
    "go": "caddyserver/caddy",
    "cpp": "redis/redis",
    "rust": "tokio-rs/axum",
    "typescript": "preactjs/preact",
}


class LockedGTLocator:
    """Wraps GTLocator so analyze_instance acquires a per-repo filelock.

    This prevents xdist workers from running git checkout / git apply on the
    same repo directory simultaneously.
    """

    def __init__(self, locator: GTLocator, lock_dir: Path):
        self._locator = locator
        self._lock_dir = lock_dir
        self._lock_dir.mkdir(parents=True, exist_ok=True)

    def analyze_instance(self, instance, **kwargs):
        repo_name = instance["repo"].replace("/", "_")
        lock_path = self._lock_dir / f"{repo_name}.lock"
        with FileLock(str(lock_path)):
            return self._locator.analyze_instance(instance, **kwargs)

    def __getattr__(self, name):
        return getattr(self._locator, name)


@pytest.fixture(scope="session")
def swebench_multilingual_dataset():
    """Load the full SWE-bench Multilingual test split (session-cached)."""
    ds = SwebenchMultilingualDataset(
        dataset="SWE-bench/SWE-bench_Multilingual",
        split="test",
    )
    return ds.load()


def _first_instance_for_repo(dataset, repo: str):
    """Return the first instance whose ``repo`` field matches *repo*."""
    for row in dataset:
        if row["repo"] == repo:
            return dict(row)
    return None


@pytest.fixture(scope="session")
def gt_locator(tmp_path_factory):
    """Shared GTLocator with a persistent work directory and per-repo locking."""
    GT_TEST_WORK_DIR.mkdir(parents=True, exist_ok=True)
    locator = GTLocator(work_dir=str(GT_TEST_WORK_DIR))
    lock_dir = tmp_path_factory.getbasetemp().parent / "gt-locks"
    return LockedGTLocator(locator, lock_dir)


@pytest.fixture(scope="session")
def go_instance(swebench_multilingual_dataset):
    inst = _first_instance_for_repo(swebench_multilingual_dataset, REPO_FIXTURES["go"])
    if inst is None:
        pytest.skip("No Go instance found in SWE-bench Multilingual")
    return inst


@pytest.fixture(scope="session")
def cpp_instance(swebench_multilingual_dataset):
    inst = _first_instance_for_repo(swebench_multilingual_dataset, REPO_FIXTURES["cpp"])
    if inst is None:
        pytest.skip("No C++ instance found in SWE-bench Multilingual")
    return inst


@pytest.fixture(scope="session")
def rust_instance(swebench_multilingual_dataset):
    inst = _first_instance_for_repo(
        swebench_multilingual_dataset, REPO_FIXTURES["rust"]
    )
    if inst is None:
        pytest.skip("No Rust instance found in SWE-bench Multilingual")
    return inst


@pytest.fixture(scope="session")
def typescript_instance(swebench_multilingual_dataset):
    inst = _first_instance_for_repo(
        swebench_multilingual_dataset, REPO_FIXTURES["typescript"]
    )
    if inst is None:
        pytest.skip("No TypeScript instance found in SWE-bench Multilingual")
    return inst
