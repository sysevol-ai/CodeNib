# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Lossless changed-line mapping for incremental graph anchors."""

from codeminer.graph.incremental.change_mgr import ChangedLineHunk, map_old_line_to_new


def test_map_old_line_through_replacement():
    hunk = ChangedLineHunk(
        old_start=218,
        old_count=11,
        new_start=218,
        new_count=14,
    )

    assert map_old_line_to_new(217, [hunk]) == 217
    assert map_old_line_to_new(220, [hunk]) is None
    assert map_old_line_to_new(231, [hunk]) == 234


def test_map_old_line_through_pure_insertion():
    hunk = ChangedLineHunk(
        old_start=4,
        old_count=0,
        new_start=5,
        new_count=2,
    )

    assert map_old_line_to_new(4, [hunk]) == 4
    assert map_old_line_to_new(5, [hunk]) == 7


def test_map_old_line_through_deletion_and_later_replacement():
    hunks = [
        ChangedLineHunk(
            old_start=2,
            old_count=2,
            new_start=2,
            new_count=0,
        ),
        ChangedLineHunk(
            old_start=8,
            old_count=1,
            new_start=6,
            new_count=3,
        ),
    ]

    assert map_old_line_to_new(1, hunks) == 1
    assert map_old_line_to_new(3, hunks) is None
    assert map_old_line_to_new(7, hunks) == 5
    assert map_old_line_to_new(8, hunks) is None
    assert map_old_line_to_new(10, hunks) == 10


def test_legacy_range_preserves_zero_count_markers():
    insertion = ChangedLineHunk(4, 0, 5, 2)
    deletion = ChangedLineHunk(7, 3, 6, 0)

    assert insertion.as_inclusive_range() == (3, 3, 5, 6)
    assert deletion.as_inclusive_range() == (7, 9, 5, 5)
