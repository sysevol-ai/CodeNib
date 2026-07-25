# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the container sandbox path in guardian_replay.py."""

import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    for i in range(3):
        (repo / "mod.py").write_text(f"def f():\n    return {i}\n", encoding="utf-8")
        _git(repo, "add", "mod.py")
        _git(repo, "commit", "-m", f"rev {i}")
    return repo


# ---------------------------------------------------------------------------
# _assert_mirror_unchanged
# ---------------------------------------------------------------------------


class TestAssertMirrorUnchanged:
    def test_passes_when_head_is_same(self, tmp_path):
        repo = _make_repo(tmp_path)
        from scripts.guardian_replay import _assert_mirror_unchanged

        before = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        _assert_mirror_unchanged(str(repo), before)  # must not raise

    def test_raises_when_head_moved(self, tmp_path):
        repo = _make_repo(tmp_path)
        from scripts.guardian_replay import _assert_mirror_unchanged

        before = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()

        # Move HEAD to parent
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        with pytest.raises(AssertionError, match="Non-modifying invariant violated"):
            _assert_mirror_unchanged(str(repo), before)


# ---------------------------------------------------------------------------
# provision_container
# ---------------------------------------------------------------------------


class TestProvisionContainer:
    def _make_cfg(self):
        from codeminer.guardian.cycle import GuardianConfig

        return GuardianConfig(
            repo_path="/repo",
            arm="memory",
            index_types=("bm25",),
            since="90 days ago",
        )

    def test_calls_docker_run_with_correct_mounts(self, tmp_path):
        from scripts.guardian_replay import provision_container

        wt = tmp_path / "wt"
        mem = tmp_path / "memory"
        ep = tmp_path / "ep"
        for d in (wt, mem, ep):
            d.mkdir()

        cfg = self._make_cfg()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            provision_container(
                str(wt), str(mem), str(ep), cfg, image="guardian-runtime:latest"
            )

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "docker"
        assert "run" in cmd
        assert "--rm" in cmd
        assert "--network" in cmd
        assert "none" in cmd

        # All three mounts present
        joined = " ".join(cmd)
        assert "/repo:ro" in joined
        assert "/memory:ro" in joined
        assert "/out:rw" in joined

    def test_passes_arm_and_since_to_entrypoint(self, tmp_path):
        from scripts.guardian_replay import provision_container

        wt = tmp_path / "wt"
        mem = tmp_path / "memory"
        ep = tmp_path / "ep"
        for d in (wt, mem, ep):
            d.mkdir()

        cfg = self._make_cfg()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            provision_container(str(wt), str(mem), str(ep), cfg)

        cmd = mock_run.call_args[0][0]
        assert "--arm" in cmd
        assert "memory" in cmd
        assert "--since" in cmd
        assert "90 days ago" in cmd

    def test_uses_custom_image_name(self, tmp_path):
        from scripts.guardian_replay import provision_container

        wt = tmp_path / "wt"
        mem = tmp_path / "memory"
        ep = tmp_path / "ep"
        for d in (wt, mem, ep):
            d.mkdir()

        cfg = self._make_cfg()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            provision_container(str(wt), str(mem), str(ep), cfg, image="my-image:v2")

        cmd = mock_run.call_args[0][0]
        assert "my-image:v2" in cmd


# ---------------------------------------------------------------------------
# Integration smoke (skipped when docker is absent)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_container_sandbox_runs_one_cycle(tmp_path):
    """smoke: docker run guardian-runtime produces report.json in /out."""
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    daemon = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
    )
    if daemon.returncode != 0:
        pytest.skip("docker daemon is not accessible")

    import json

    repo = _make_repo(tmp_path)
    mem = tmp_path / "memory"
    ep = tmp_path / "out"
    mem.mkdir()
    ep.mkdir()

    from codeminer.guardian.cycle import GuardianConfig
    from scripts.guardian_replay import provision_container

    cfg = GuardianConfig(
        repo_path=str(repo),
        arm="memory",
        index_types=("bm25",),
        since="10 years ago",
    )
    provision_container(str(repo), str(mem), str(ep), cfg)

    report_path = ep / "report.json"
    assert report_path.exists(), "Container did not write report.json"
    data = json.loads(report_path.read_text())
    assert "findings" in data
