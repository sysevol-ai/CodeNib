# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import subprocess

import pytest

from codenib.types import NodeInfo
from scripts.audit_jev_base_labels import align_blocks, rerank_metrics


@pytest.mark.parametrize("duplicate", [False, True])
def test_base_alignment_repairs_shifted_labels_without_reading_dirty_source(
    tmp_path, duplicate
):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    source = tmp_path / "a.py"
    source.write_text(
        "def target():\n    return 1\n\ndef "
        + ("target" if duplicate else "neighbor")
        + "():\n    return 2\n"
    )
    git("add", "a.py")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    commit = git("rev-parse", "HEAD")
    source.write_text("# dirty worktree\n" * 20)
    # A post-patch range no longer identifies the original definition.
    block = {
        "file": "a.py",
        "symbol": "target()",
        "change_type": "modified",
        "start": 40,
        "end": 42,
    }
    node = NodeInfo(
        node_id="a.py:target()",
        file="a.py",
        start_line=3 if duplicate else 0,
        end_line=4 if duplicate else 1,
    )
    case = {
        "instance_id": "example",
        "base_commit": commit,
        "target_files": ["a.py"],
        "target_blocks": [block],
        "candidates": [node.model_dump()],
    }
    row = {
        "ranking": [
            {
                "node_id": node.node_id,
                "start_line": node.start_line,
                "end_line": node.end_line,
            }
        ]
    }
    assert rerank_metrics(case, row)["span_recall@1"] == 0
    blocks, audit = align_blocks(case, tmp_path, {})
    assert rerank_metrics({**case, "target_blocks": blocks}, row)["span_recall@1"] == 1
    assert block["start"] == 40
    assert len(audit[0]["all_base_matches"]) == (2 if duplicate else 1)
    assert audit[0]["base"] == ([4, 5] if duplicate else [1, 2])
    assert source.read_text() == "# dirty worktree\n" * 20
