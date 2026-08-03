# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from scripts.profiling.analyze_snapshot_reuse import summarize_snapshot_reuse


def test_summarize_snapshot_reuse_deduplicates_repo_commit_not_instance():
    rows = [
        {
            "repo": "Org/Repo",
            "base_commit": "a" * 40,
            "instance_id": "issue-1",
        },
        {
            "repo": "https://github.com/org/repo.git",
            "base_commit": "a" * 40,
            "instance_id": "issue-2",
        },
        {
            "repo": "org/repo",
            "base_commit": "b" * 40,
            "instance_id": "issue-3",
        },
    ]

    summary = summarize_snapshot_reuse(rows)

    assert summary["records"] == 3
    assert summary["instances"] == 3
    assert summary["repositories"] == 1
    assert summary["unique_snapshots"] == 2
    assert summary["duplicate_records"] == 1
    assert summary["records_per_snapshot_mean"] == 1.5
