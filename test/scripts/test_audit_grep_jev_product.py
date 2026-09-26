# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import subprocess

import pytest

from codenib.types import NODE_TYPE_FUNCTION, NodeInfo
from scripts.audit_grep_jev_product import compare_candidates, materialize_commit


def fixture_case():
    node = NodeInfo(
        node_id="app.py:run()",
        node_name="run",
        file="app.py",
        type=NODE_TYPE_FUNCTION,
        start_line=0,
        end_line=2,
        content="app.py:run()\ndef run():\n    return 1",
        score=0,
    )
    case = {
        "target_files": ["app.py"],
        "target_blocks": [{"file": "app.py", "start": 1, "end": 2}],
        "candidates": [node.model_dump()],
    }
    ranking = [{"node_id": node.node_id, "start_line": 0, "end_line": 2, "score": 1}]
    return case, node.model_copy(update={"end_line": 1}), ranking


def test_range_correction_keeps_text_parity_and_score_sensitivity():
    case, product, ranking = fixture_case()
    result = compare_candidates(case, [product], ranking)
    assert result["ordered_text_match"]
    assert result["corrected_span_order_match"]
    assert result["product_grep_recall_at_5"] == 1
    assert result["frozen_score_sensitivity_recall_at_5"] == 1


def test_changed_text_cannot_inherit_a_frozen_score_from_the_same_symbol():
    case, product, ranking = fixture_case()
    product.content = product.content.replace("return 1", "return 2")
    result = compare_candidates(case, [product], ranking)
    assert not result["ordered_text_match"]
    assert not result["all_product_texts_have_frozen_scores"]
    assert result["frozen_score_sensitivity_recall_at_5"] is None


def test_duplicate_candidates_cannot_inflate_parity_or_metrics():
    case, product, ranking = fixture_case()
    with pytest.raises(ValueError, match="Duplicate candidate"):
        compare_candidates(case, [product, product], ranking)


def test_git_snapshot_uses_pinned_bytes_without_modifying_the_dirty_checkout(tmp_path):
    repo = tmp_path / "original checkout"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "-q")
    (repo / "file.bin").write_bytes(b"\x00\x01\xff\r\n")
    (repo / "source.py").write_text("def run():\n    return 1\n")
    (repo / "source-link.py").symlink_to("source.py")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-qm",
        "fixture",
    )
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    (repo / "source.py").write_text("uncommitted changes\n")
    before = git("status", "--porcelain")
    target = tmp_path / "snapshot"
    target.mkdir()

    report = materialize_commit(repo, commit, tree, target)

    assert report["files"] == 3
    assert (target / "file.bin").read_bytes() == b"\x00\x01\xff\r\n"
    assert (target / "source.py").read_text() == "def run():\n    return 1\n"
    assert (target / "source-link.py").is_symlink()
    assert (repo / "source.py").read_text() == "uncommitted changes\n"
    assert git("status", "--porcelain") == before
    with pytest.raises(ValueError, match="tree differs"):
        materialize_commit(repo, commit, "0" * 40, target)
