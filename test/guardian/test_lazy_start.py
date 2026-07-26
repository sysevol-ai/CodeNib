# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the idempotent Guardian launcher installed in Pier."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from deepsweguardian.lazy_start import guardian_start_script


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_guardian_start_is_idempotent_and_records_baseline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "launcher@example.com")
    _git(repo, "config", "user.name", "Launcher")
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    count_file = tmp_path / "starts"
    child_code = (
        "from pathlib import Path; import time; "
        f"p=Path({str(count_file)!r}); "
        "p.write_text(p.read_text() + '1\\n' if p.exists() else '1\\n'); "
        "time.sleep(30)"
    )
    baseline_file = tmp_path / "base_commit"
    baseline_file.write_text(head + "\n", encoding="utf-8")
    pid_file = tmp_path / "guardian.pid"
    launcher = tmp_path / "guardian-start"
    launcher.write_text(
        guardian_start_script(
            command=[sys.executable, "-c", child_code],
            environment={},
            repo=str(repo),
            baseline_file=str(baseline_file),
            pid_file=str(pid_file),
            log_file=str(tmp_path / "guardian.log"),
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    pid = 0
    try:
        first = subprocess.run(
            [str(launcher)],
            capture_output=True,
            text=True,
            check=True,
        )
        second = subprocess.run(
            [str(launcher)],
            capture_output=True,
            text=True,
            check=True,
        )
        pid = int(pid_file.read_text(encoding="utf-8"))

        assert "Guardian started" in first.stdout
        assert "Guardian already started" in second.stdout
        assert baseline_file.read_text(encoding="utf-8").strip() == head
        assert count_file.read_text(encoding="utf-8").splitlines() == ["1"]
    finally:
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.01)


def test_guardian_start_refuses_to_infer_a_late_baseline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    count_file = tmp_path / "starts"
    pid_file = tmp_path / "guardian.pid"
    launcher = tmp_path / "guardian-start"
    launcher.write_text(
        guardian_start_script(
            command=[
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(count_file)!r}).touch()",
            ],
            environment={},
            repo=str(repo),
            baseline_file=str(tmp_path / "missing_base_commit"),
            pid_file=str(pid_file),
            log_file=str(tmp_path / "guardian.log"),
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    result = subprocess.run(
        [str(launcher)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "setup-time baseline" in result.stderr
    assert not count_file.exists()
    assert not pid_file.exists()
