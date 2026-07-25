# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for scripts/guardian_replay.py.

These tests cover the worktree helpers and the replay loop logic using
injected fakes — no real git worktrees are created.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Import the module under test.
import scripts.guardian_replay as replay_mod
from scripts.guardian_replay import _read_commits, provision_worktree, teardown_worktree

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    for i in range(3):
        (repo / "mod.py").write_text(f"def f():\n    return {i}\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", f"rev {i}")
    return repo


def _head(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()


# ---------------------------------------------------------------------------
# _read_commits
# ---------------------------------------------------------------------------


class TestReadCommits:
    def test_reads_shas(self, tmp_path):
        f = tmp_path / "commits.txt"
        f.write_text("abc\ndef\n")
        assert _read_commits(str(f)) == ["abc", "def"]

    def test_skips_blank_and_comments(self, tmp_path):
        f = tmp_path / "commits.txt"
        f.write_text("# header\nabc\n\ndef\n# tail\n")
        assert _read_commits(str(f)) == ["abc", "def"]

    def test_strips_whitespace(self, tmp_path):
        f = tmp_path / "commits.txt"
        f.write_text("  abc  \n  def  \n")
        assert _read_commits(str(f)) == ["abc", "def"]


# ---------------------------------------------------------------------------
# provision_worktree / teardown_worktree
# ---------------------------------------------------------------------------


class TestWorktreeHelpers:
    def test_provision_creates_worktree(self, tmp_path):
        repo = _make_repo(tmp_path)
        commit = _head(repo)
        wt_base = str(tmp_path / "wt")

        wt_path = provision_worktree(str(repo), commit, wt_base)
        try:
            assert os.path.isdir(wt_path)
            assert os.path.exists(os.path.join(wt_path, "mod.py"))
        finally:
            teardown_worktree(str(repo), wt_path)

    def test_worktree_path_uses_short_sha(self, tmp_path):
        repo = _make_repo(tmp_path)
        commit = _head(repo)
        wt_base = str(tmp_path / "wt")

        wt_path = provision_worktree(str(repo), commit, wt_base)
        try:
            assert os.path.basename(wt_path) == commit[:8]
        finally:
            teardown_worktree(str(repo), wt_path)

    def test_teardown_removes_worktree(self, tmp_path):
        repo = _make_repo(tmp_path)
        commit = _head(repo)
        wt_base = str(tmp_path / "wt")

        wt_path = provision_worktree(str(repo), commit, wt_base)
        teardown_worktree(str(repo), wt_path)
        assert not os.path.exists(wt_path)

    def test_teardown_tolerates_missing_worktree(self, tmp_path):
        repo = _make_repo(tmp_path)
        # Should not raise even if the path doesn't exist.
        teardown_worktree(str(repo), "/nonexistent/path")


# ---------------------------------------------------------------------------
# run_replay (injected mock run_cycle)
# ---------------------------------------------------------------------------


def _fake_report(commit="abc12345"):
    from codeminer.guardian.loop import Hypothesis
    from codeminer.guardian.report import GuardianReport, report_views

    hypothesis = Hypothesis.create(
        claim="mod.parse accepts invalid input",
        consequence="invalid state reaches callers",
        remedy="validate input and add a regression test",
        origin="exploration",
        locus=["mod.py:parse"],
        evidence=["probe:fixture:1"],
        grade="finding",
    )
    findings, backlog, retractions = report_views([hypothesis])
    return GuardianReport(
        repo="/repo",
        commit=commit,
        generated_at="2026-01-01 00:00:00 UTC",
        churn_window="90 days ago",
        findings=findings,
        backlog=backlog,
        retractions=retractions,
    )


class TestRunReplay:
    def _make_args(self, tmp_path, repo, commits_file, **kwargs):
        import argparse

        args = argparse.Namespace(
            repo=str(repo),
            commits=str(commits_file),
            out=str(tmp_path / "out"),
            arm="memory",
            index_types="bm25",
            since="90 days ago",
            sandbox="worktree",
            use_llm=False,
            llm_model="vertex_ai/gemini-2.5-flash",
            log_level="INFO",
        )
        for k, v in kwargs.items():
            setattr(args, k, v)
        return args

    def test_creates_episode_dirs(self, tmp_path):
        repo = _make_repo(tmp_path)
        commits = [_head(repo)]
        commits_file = tmp_path / "commits.txt"
        commits_file.write_text("\n".join(commits) + "\n")

        args = self._make_args(tmp_path, repo, commits_file)

        with (
            patch(
                "scripts.guardian_replay.provision_worktree", return_value=str(repo)
            ) as mock_prov,
            patch("scripts.guardian_replay.teardown_worktree") as mock_tear,
            patch(
                "codeminer.guardian.cycle.run_cycle",
                return_value=_fake_report(commits[0]),
            ),
        ):
            replay_mod.run_replay(args)

        out = Path(args.out)
        ep_dirs = list((out / "episodes").iterdir())
        assert len(ep_dirs) == 1
        assert ep_dirs[0].name.startswith("0000_")

    def test_writes_report_md_and_json(self, tmp_path):
        repo = _make_repo(tmp_path)
        commit = _head(repo)
        commits_file = tmp_path / "commits.txt"
        commits_file.write_text(commit + "\n")

        args = self._make_args(tmp_path, repo, commits_file)

        with (
            patch("scripts.guardian_replay.provision_worktree", return_value=str(repo)),
            patch("scripts.guardian_replay.teardown_worktree"),
            patch(
                "codeminer.guardian.cycle.run_cycle", return_value=_fake_report(commit)
            ),
        ):
            replay_mod.run_replay(args)

        ep_dir = next((Path(args.out) / "episodes").iterdir())
        assert (ep_dir / "report.md").exists()
        assert (ep_dir / "report.json").exists()

        data = json.loads((ep_dir / "report.json").read_text())
        assert data["commit"] == commit

    def test_teardown_called_even_on_cycle_failure(self, tmp_path):
        repo = _make_repo(tmp_path)
        commit = _head(repo)
        commits_file = tmp_path / "commits.txt"
        commits_file.write_text(commit + "\n")

        args = self._make_args(tmp_path, repo, commits_file)

        with (
            patch("scripts.guardian_replay.provision_worktree", return_value=str(repo)),
            patch("scripts.guardian_replay.teardown_worktree") as mock_tear,
            patch(
                "codeminer.guardian.cycle.run_cycle",
                side_effect=RuntimeError("cycle boom"),
            ),
        ):
            replay_mod.run_replay(args)  # must not raise

        mock_tear.assert_called_once()
        episode = next((Path(args.out) / "episodes").iterdir())
        failure = json.loads((episode / "cycle_failure.json").read_text())
        assert failure["status"] == "failed"
        assert failure["failure_type"] == "RuntimeError"
        assert "cycle boom" in failure["reason"]
        assert not (episode / "report.json").exists()

    def test_multiple_commits_produce_multiple_episodes(self, tmp_path):
        repo = _make_repo(tmp_path)
        # Collect the last 3 commits.
        log = (
            subprocess.check_output(
                ["git", "log", "--format=%H", "-3"], cwd=repo, text=True
            )
            .strip()
            .splitlines()
        )
        commits_file = tmp_path / "commits.txt"
        commits_file.write_text("\n".join(log) + "\n")

        args = self._make_args(tmp_path, repo, commits_file)

        side_effects = [_fake_report(c) for c in log]
        with (
            patch("scripts.guardian_replay.provision_worktree", return_value=str(repo)),
            patch("scripts.guardian_replay.teardown_worktree"),
            patch("codeminer.guardian.cycle.run_cycle", side_effect=side_effects),
        ):
            replay_mod.run_replay(args)

        ep_dirs = sorted((Path(args.out) / "episodes").iterdir())
        assert len(ep_dirs) == 3
        for i, ep in enumerate(ep_dirs):
            assert ep.name.startswith(f"{i:04d}_")

    def test_memory_dir_created(self, tmp_path):
        repo = _make_repo(tmp_path)
        commit = _head(repo)
        commits_file = tmp_path / "commits.txt"
        commits_file.write_text(commit + "\n")

        args = self._make_args(tmp_path, repo, commits_file)

        with (
            patch("scripts.guardian_replay.provision_worktree", return_value=str(repo)),
            patch("scripts.guardian_replay.teardown_worktree"),
            patch(
                "codeminer.guardian.cycle.run_cycle", return_value=_fake_report(commit)
            ),
        ):
            replay_mod.run_replay(args)

        assert (Path(args.out) / "memory").is_dir()


# ---------------------------------------------------------------------------
# Integration — real IndexCompiler (BM25 only, no GPU)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_two_cycle_replay_bm25_only(tmp_path):
    """Full two-cycle replay: real BM25 index, real worktrees, memory accumulates."""
    import argparse

    repo = _make_repo(tmp_path)

    # Collect all 3 commits (oldest to newest) so graph diff has something to compare.
    log = (
        subprocess.check_output(
            ["git", "log", "--format=%H", "--reverse"], cwd=repo, text=True
        )
        .strip()
        .splitlines()
    )
    # Use the last two so the churn window covers both.
    commits = log[-2:]
    commits_file = tmp_path / "commits.txt"
    commits_file.write_text("\n".join(commits) + "\n")

    args = argparse.Namespace(
        repo=str(repo),
        commits=str(commits_file),
        out=str(tmp_path / "out"),
        arm="memory",
        index_types="bm25",
        since="10 years ago",
        sandbox="worktree",
        use_llm=False,
        llm_model="vertex_ai/gemini-2.5-flash",
        log_level="INFO",
    )

    replay_mod.run_replay(args)

    out = Path(args.out)

    # Memory store must have exactly two cycle rows.
    from codeminer.guardian.memory import MemoryStore

    store = MemoryStore(str(out / "memory"))
    assert store.cycle_count() == 2
    # Degraded no-model cycles persist their exits and signals but correctly
    # create no hypotheses, so there is no hypothesis trajectory to assert.
    assert store.recall(k=1) == []

    # Both episodes must have a report.md.
    ep_dirs = sorted((out / "episodes").iterdir())
    assert len(ep_dirs) == 2
    for ep in ep_dirs:
        md = ep / "report.md"
        assert md.exists(), f"Missing report.md in {ep}"
        assert "Repository Guardian Report" in md.read_text()
        assert (ep / "report.json").exists()
