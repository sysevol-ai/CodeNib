# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for codeminer.guardian.cycle.run_cycle.

The orchestration test injects a fake manifest + investigator (unit tier). The
end-to-end test drives the real IndexCompiler with BM25 only (integration tier).
"""

import os
import subprocess

import pytest

from codeminer.guardian.cycle import GuardianConfig, run_cycle
from codeminer.guardian.investigate import Evidence
from codeminer.guardian.report import render_markdown


class _FakeManifest:
    commit = "deadbeefcafebabe"
    file_count = 7
    capabilities = {"sparse_search": True}


def _git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    for i in range(3):
        (repo / "mod.py").write_text(f"def f():\n    return {i}\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", f"rev {i}")
    return repo


def test_run_cycle_with_injected_manifest_and_investigator(tmp_path):
    repo = _make_repo(tmp_path)
    captured = []

    def fake_investigator(hotspot):
        captured.append(hotspot.path)
        return [Evidence("mod.py", "f", "function", 1, 2, 0.5)]

    config = GuardianConfig(repo_path=str(repo), since="10 years ago", investigate=True)
    report = run_cycle(
        config, investigator=fake_investigator, manifest=_FakeManifest()
    )

    assert report.commit == "deadbeefcafebabe"  # from injected manifest
    assert report.file_count == 7
    assert captured == ["mod.py"]  # investigator was called per hotspot
    assert len(report.findings) == 1
    assert report.findings[0].kind == "churn"
    assert report.findings[0].evidence[0].node_name == "f"

    md = render_markdown(report)
    assert "mod.py" in md and "non-modifying" in md.lower()


def test_run_cycle_no_investigate_skips_evidence(tmp_path):
    repo = _make_repo(tmp_path)
    config = GuardianConfig(
        repo_path=str(repo), since="10 years ago", investigate=False
    )
    report = run_cycle(config, manifest=_FakeManifest())
    assert report.findings  # still surfaces churn
    assert all(not f.evidence for f in report.findings)


@pytest.mark.integration
def test_run_cycle_end_to_end_bm25_only(tmp_path):
    """One real cycle: compile a BM25 index (no embeddings) and render a report."""
    repo = _make_repo(tmp_path)
    config = GuardianConfig(
        repo_path=str(repo),
        since="10 years ago",
        index_types=("bm25",),
        investigate=False,  # avoid embedding-model download in CI
    )
    report = run_cycle(config)

    assert report.commit  # real HEAD from compile/git
    assert report.findings  # mod.py is a hotspot
    md = render_markdown(report)
    assert "Repository Guardian Report" in md
    assert "diff --git" not in md  # non-modifying invariant
