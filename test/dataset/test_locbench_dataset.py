# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Loc-Bench annotations are derived from the golden patch on first use."""

from __future__ import annotations

import json

import pytest

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
        return [{"instance_id": iid, "target_files": ["x.py"]} for iid in ids]

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
