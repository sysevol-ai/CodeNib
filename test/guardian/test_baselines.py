# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scripts/guardian_baselines.py and guardian_eval.py --baseline."""

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# guardian_baselines lives in scripts/; add it to the path.
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from guardian_baselines import (
    churn_ranker,
    get_tracked_files,
    graph_diff_only,
    oracle_baseline,
    random_baseline,
)
from guardian_eval import (
    _extract_target_file,
    score_baseline_arm,
)


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


def _make_repo(tmp_path):
    """Tiny git repo with repeated commits to mod.py so it shows as a churn hotspot."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")

    (repo / "mod.py").write_text("x = 1\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-m", "init")

    for i in range(5):
        (repo / "mod.py").write_text(f"x = {i + 2}\n")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", f"bump {i}")

    return repo


def _make_episode(tmp_path, commit="aabbccddeeff00112233445566778899aabbccdd"):
    """Create a minimal episode directory with a report.json."""
    ep = tmp_path / "ep0"
    ep.mkdir()
    (ep / "report.json").write_text(json.dumps({
        "commit": commit,
        "findings": [],
        "llm_usage": None,
    }))
    return ep


# ---------------------------------------------------------------------------
# TestChurnRanker
# ---------------------------------------------------------------------------

class TestChurnRanker:
    def test_returns_findings_for_churn_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        findings = churn_ranker(str(repo), since="10 years ago", top_k=5)
        assert findings
        titles = [f.title for f in findings]
        assert any("mod.py" in t for t in titles)

    def test_all_findings_are_churn_kind(self, tmp_path):
        repo = _make_repo(tmp_path)
        findings = churn_ranker(str(repo), since="10 years ago", top_k=5)
        assert all(f.kind == "churn" for f in findings)

    def test_title_format_parseable_by_extract_target(self, tmp_path):
        repo = _make_repo(tmp_path)
        findings = churn_ranker(str(repo), since="10 years ago", top_k=5)
        from dataclasses import asdict
        for finding in findings:
            target = _extract_target_file(asdict(finding))
            assert target is not None

    def test_top_k_respected(self, tmp_path):
        repo = _make_repo(tmp_path)
        findings = churn_ranker(str(repo), since="10 years ago", top_k=1)
        assert len(findings) <= 1

    def test_empty_repo_returns_empty(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "T")
        findings = churn_ranker(str(repo), since="10 years ago", top_k=5)
        assert findings == []


# ---------------------------------------------------------------------------
# TestGraphDiffOnly
# ---------------------------------------------------------------------------

class TestGraphDiffOnly:
    def _make_graphs(self):
        from codeminer.graph.code_graph import CodeGraph
        from codeminer.types import EDGE_TYPE_REFERENCE

        prior = CodeGraph()
        prior._add_vertex("A", {"type": "function", "file": "a.py"})
        prior._add_vertex("B", {"type": "function", "file": "b.py"})
        prior._add_edge("A", "B", EDGE_TYPE_REFERENCE)

        # Current graph has a fan-in spike on B that triggers a drift signal.
        current = CodeGraph()
        current._add_vertex("A", {"type": "function", "file": "a.py"})
        current._add_vertex("B", {"type": "function", "file": "b.py"})
        current._add_edge("A", "B", EDGE_TYPE_REFERENCE)
        for i in range(6):
            current._add_vertex(f"C{i}", {"type": "function", "file": f"c{i}.py"})
            current._add_edge(f"C{i}", "B", EDGE_TYPE_REFERENCE)

        return prior, current

    def test_returns_drift_findings_when_graphs_provided(self, tmp_path):
        prior, current = self._make_graphs()
        findings = graph_diff_only(
            str(tmp_path),
            prior_graph=prior,
            current_graph=current,
            top_k=10,
        )
        assert findings  # fan-in spike on B should fire

    def test_returns_empty_when_no_prior(self, tmp_path):
        findings = graph_diff_only(str(tmp_path), prior_graph=None, current_graph=None)
        assert findings == []

    def test_returns_empty_when_no_current(self, tmp_path):
        from codeminer.graph.code_graph import CodeGraph
        prior = CodeGraph()
        findings = graph_diff_only(str(tmp_path), prior_graph=prior, current_graph=None)
        assert findings == []

    def test_returns_empty_when_no_memory_dir_no_graphs(self, tmp_path):
        findings = graph_diff_only(str(tmp_path))
        assert findings == []

    def test_top_k_limits_output(self, tmp_path):
        prior, current = self._make_graphs()
        findings = graph_diff_only(
            str(tmp_path), prior_graph=prior, current_graph=current, top_k=1
        )
        assert len(findings) <= 1

    def test_findings_have_kind(self, tmp_path):
        prior, current = self._make_graphs()
        findings = graph_diff_only(
            str(tmp_path), prior_graph=prior, current_graph=current, top_k=10
        )
        for f in findings:
            assert f.kind in ("churn", "drift")


# ---------------------------------------------------------------------------
# TestRandomBaseline
# ---------------------------------------------------------------------------

class TestRandomBaseline:
    def test_returns_top_k_findings(self):
        files = [f"src/file_{i}.py" for i in range(20)]
        findings = random_baseline(files, top_k=5)
        assert len(findings) == 5

    def test_deterministic_with_same_seed(self):
        files = [f"src/file_{i}.py" for i in range(20)]
        f1 = [f.title for f in random_baseline(files, top_k=5, seed=42)]
        f2 = [f.title for f in random_baseline(files, top_k=5, seed=42)]
        assert f1 == f2

    def test_different_seeds_produce_different_results(self):
        files = [f"src/file_{i}.py" for i in range(20)]
        f1 = [f.title for f in random_baseline(files, top_k=5, seed=42)]
        f2 = [f.title for f in random_baseline(files, top_k=5, seed=99)]
        assert f1 != f2

    def test_top_k_clamped_to_available(self):
        files = ["a.py", "b.py"]
        findings = random_baseline(files, top_k=10)
        assert len(findings) == 2

    def test_all_findings_are_churn_kind(self):
        files = [f"src/file_{i}.py" for i in range(10)]
        findings = random_baseline(files, top_k=5)
        assert all(f.kind == "churn" for f in findings)

    def test_titles_parseable_by_extract_target(self):
        from dataclasses import asdict
        files = ["src/foo.py", "src/bar.py"]
        findings = random_baseline(files, top_k=2)
        for f in findings:
            target = _extract_target_file(asdict(f))
            assert target in files

    def test_empty_input_returns_empty(self):
        findings = random_baseline([], top_k=5)
        assert findings == []


# ---------------------------------------------------------------------------
# TestOracleBaseline
# ---------------------------------------------------------------------------

class TestOracleBaseline:
    def test_returns_all_gt_files(self):
        gt = ["a.py", "b.py", "c.py"]
        findings = oracle_baseline(gt, top_k=10)
        assert len(findings) == 3

    def test_all_findings_parseable(self):
        from dataclasses import asdict
        gt = ["src/a.py", "src/b.py"]
        findings = oracle_baseline(gt, top_k=10)
        for f in findings:
            target = _extract_target_file(asdict(f))
            assert target in gt

    def test_top_k_limits_output(self):
        gt = ["a.py", "b.py", "c.py", "d.py"]
        findings = oracle_baseline(gt, top_k=2)
        assert len(findings) == 2

    def test_empty_gt_returns_empty(self):
        findings = oracle_baseline([], top_k=5)
        assert findings == []

    def test_all_findings_are_churn_kind(self):
        gt = ["a.py", "b.py"]
        findings = oracle_baseline(gt, top_k=5)
        assert all(f.kind == "churn" for f in findings)


# ---------------------------------------------------------------------------
# TestGetTrackedFiles
# ---------------------------------------------------------------------------

class TestGetTrackedFiles:
    def test_returns_tracked_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        files = get_tracked_files(str(repo))
        assert "mod.py" in files

    def test_non_repo_returns_empty(self, tmp_path):
        files = get_tracked_files(str(tmp_path))
        assert files == []


# ---------------------------------------------------------------------------
# TestBaselineEvalCLI  — score_baseline_arm unit tests (no real git history)
# ---------------------------------------------------------------------------

class TestBaselineEvalCLI:
    def _make_episode_dir(self, tmp_path, commit="a" * 40):
        ep = tmp_path / "episodes" / "ep0"
        ep.mkdir(parents=True)
        (ep / "report.json").write_text(json.dumps({
            "commit": commit,
            "findings": [],
            "llm_usage": None,
        }))
        return str(tmp_path / "episodes")

    def test_churn_baseline_returns_rows(self, tmp_path):
        repo = _make_repo(tmp_path)
        eps_dir = self._make_episode_dir(tmp_path)

        # Patch GT extraction to return a known set.
        with patch(
            "guardian_eval.post_t_changed_files",
            return_value={"mod.py": 1},
        ):
            rows = score_baseline_arm(
                "churn",
                eps_dir,
                str(repo),
                horizon_commits=5,
                top_k=5,
                since="10 years ago",
            )

        assert len(rows) == 1
        assert rows[0]["precision_at_k"] is not None

    def test_random_baseline_returns_rows(self, tmp_path):
        repo = _make_repo(tmp_path)
        eps_dir = self._make_episode_dir(tmp_path)

        with patch(
            "guardian_eval.post_t_changed_files",
            return_value={"mod.py": 1},
        ):
            rows = score_baseline_arm(
                "random",
                eps_dir,
                str(repo),
                horizon_commits=5,
                top_k=5,
                seed=42,
            )

        assert len(rows) == 1

    def test_oracle_baseline_achieves_full_recall(self, tmp_path):
        repo = _make_repo(tmp_path)
        eps_dir = self._make_episode_dir(tmp_path)
        gt = {"mod.py": 1, "other.py": 2}

        with patch("guardian_eval.post_t_changed_files", return_value=gt):
            rows = score_baseline_arm(
                "oracle",
                eps_dir,
                str(repo),
                horizon_commits=5,
                top_k=10,
            )

        assert rows[0]["recall"] == 1.0

    def test_graph_diff_baseline_returns_rows(self, tmp_path):
        repo = _make_repo(tmp_path)
        eps_dir = self._make_episode_dir(tmp_path)

        with patch("guardian_eval.post_t_changed_files", return_value={}):
            rows = score_baseline_arm(
                "graph_diff",
                eps_dir,
                str(repo),
                horizon_commits=5,
                top_k=5,
            )

        # graph_diff_only returns [] when no memory_dir; still a valid row.
        assert len(rows) == 1
        assert rows[0]["n_gt_files"] == 0

    def test_missing_episodes_returns_empty(self, tmp_path):
        rows = score_baseline_arm(
            "churn",
            str(tmp_path / "no_such_dir"),
            str(tmp_path),
        )
        assert rows == []

    def test_no_future_commits_sets_metrics_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        eps_dir = self._make_episode_dir(tmp_path)

        with patch("guardian_eval.post_t_changed_files", return_value={}):
            rows = score_baseline_arm(
                "churn",
                eps_dir,
                str(repo),
                horizon_commits=5,
                top_k=5,
                since="10 years ago",
            )

        assert rows[0]["precision_at_k"] is None
        assert rows[0]["recall"] is None
