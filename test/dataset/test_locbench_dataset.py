# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Loc-Bench annotations are derived from the golden patch on first use."""

from __future__ import annotations

import json
import threading

import pytest
from filelock import FileLock

from codenib.dataset import gt_locate
from codenib.dataset.locbench import LocbenchDataset


def _row(instance_id: str) -> dict:
    return {
        "instance_id": instance_id,
        "repo": "owner/repo",
        "base_commit": "a" * 40,
        "patch": "diff --git a/x.py b/x.py\n",
    }


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    ds = LocbenchDataset(root=str(tmp_path), log=False)
    rows = [_row("owner__repo-1"), _row("owner__repo-2")]
    monkeypatch.setattr(ds, "load", lambda: rows)
    return ds


@pytest.fixture
def located(monkeypatch):
    calls: list[list[str]] = []

    def fake_build(dataset, work_dir=None, keep_repos=True):
        ids = [row["instance_id"] for row in dataset]
        calls.append(ids)
        return [
            (
                {"instance_id": iid, "error": "clone failed"}
                if iid.endswith("-broken")
                else {"instance_id": iid, "target_files": ["x.py"]}
            )
            for iid in ids
        ]

    monkeypatch.setattr(gt_locate, "build_gt_metadata", fake_build)
    return calls


def test_missing_file_is_built_at_the_default_path(dataset, located, tmp_path):
    metadata = dataset.load_eval_metadata("")

    assert set(metadata) == {"owner__repo-1", "owner__repo-2"}
    assert located == [["owner__repo-1", "owner__repo-2"]]
    written = tmp_path / "locbench_v1_test_gt.json"
    assert dataset.default_eval_metadata_path() == str(written)
    assert len(json.loads(written.read_text())) == 2


def test_only_unlocated_instances_are_added(dataset, located, tmp_path):
    path = tmp_path / "gt.json"
    path.write_text(json.dumps([{"instance_id": "owner__repo-1", "kept": True}]))

    metadata = dataset.load_eval_metadata(str(path))

    assert located == [["owner__repo-2"]]
    assert metadata["owner__repo-1"] == {"instance_id": "owner__repo-1", "kept": True}
    assert metadata["owner__repo-2"]["target_files"] == ["x.py"]


def test_complete_file_is_read_without_locating(dataset, located, tmp_path):
    path = tmp_path / "gt.json"
    path.write_text(
        json.dumps([{"instance_id": "owner__repo-1"}, {"instance_id": "owner__repo-2"}])
    )

    assert set(dataset.load_eval_metadata(str(path))) == {
        "owner__repo-1",
        "owner__repo-2",
    }
    assert located == []


def test_only_the_requested_instances_are_located(dataset, located, tmp_path):
    path = tmp_path / "gt.json"

    metadata = dataset.load_eval_metadata(str(path), instances=[_row("owner__repo-2")])

    assert located == [["owner__repo-2"]]
    assert set(metadata) == {"owner__repo-2"}


def test_failed_records_are_retried_and_left_unscored(dataset, located, tmp_path):
    path = tmp_path / "gt.json"
    path.write_text(
        json.dumps(
            [
                {"instance_id": "owner__repo-1", "error": "timeout"},
                {"instance_id": "owner__repo-broken", "error": "clone failed"},
            ]
        )
    )
    rows = [_row("owner__repo-1"), _row("owner__repo-broken")]

    metadata = dataset.load_eval_metadata(str(path), instances=rows)

    assert located == [["owner__repo-1", "owner__repo-broken"]]
    assert set(metadata) == {"owner__repo-1"}
    written = json.loads(path.read_text())
    assert sorted(entry["instance_id"] for entry in written) == [
        "owner__repo-1",
        "owner__repo-broken",
    ]


def test_wrapped_file_gains_missing_records_and_keeps_its_wrapper(
    dataset, located, tmp_path
):
    path = tmp_path / "gt.json"
    path.write_text(
        json.dumps({"version": 1, "instances": [{"instance_id": "owner__repo-1"}]})
    )

    metadata = dataset.load_eval_metadata(str(path))

    assert located == [["owner__repo-2"]]
    assert set(metadata) == {"owner__repo-1", "owner__repo-2"}
    written = json.loads(path.read_text())
    assert written["version"] == 1
    assert len(written["instances"]) == 2


def test_a_waiting_process_reuses_the_file_published_under_the_lock(
    dataset, located, tmp_path
):
    # Another process holds the lock while it locates and publishes every
    # record; this one must wait and then read that file instead of
    # locating the same instances again.
    path = tmp_path / "gt.json"
    result = {}
    with FileLock(f"{path}.lock"):
        waiter = threading.Thread(
            target=lambda: result.update(dataset.load_eval_metadata(str(path)))
        )
        waiter.start()
        waiter.join(timeout=0.2)
        assert waiter.is_alive()
        path.write_text(
            json.dumps(
                [{"instance_id": "owner__repo-1"}, {"instance_id": "owner__repo-2"}]
            )
        )
    waiter.join(timeout=10)

    assert not waiter.is_alive()
    assert located == []
    assert set(result) == {"owner__repo-1", "owner__repo-2"}
