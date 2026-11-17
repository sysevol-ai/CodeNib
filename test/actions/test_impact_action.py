# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the pull-request impact action and the workflow that runs it."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION_PATH = ROOT / ".github" / "actions" / "impact" / "action.yml"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "codenib-impact.yml"
PINNED_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+@[0-9a-f]{40}$")
MARKER = "<!-- codenib-impact -->"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the action's steps are bash scripts"
)


def _load(path: Path) -> dict[str, Any]:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def _step(name: str) -> dict[str, Any]:
    steps = _load(ACTION_PATH)["runs"]["steps"]
    return next(step for step in steps if step.get("name") == name)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(root: Path, name: str) -> str:
    (root / name).write_text(name, encoding="utf-8")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        name,
    )
    return _git(root, "rev-parse", "HEAD")


def _resolve(tmp_path: Path, repo: Path, **overrides: str):
    output = tmp_path / "github-output"
    env = {
        **os.environ,
        "ACTION_REF": "0123456789abcdef",
        "GITHUB_ACTION_PATH": str(ACTION_PATH.parent),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_REPOSITORY_ID": "123456",
        "INPUT_BASE": "main",
        "INPUT_CACHE": "true",
        "INPUT_COMMENT": "true",
        "INPUT_DEPTH": "3",
        "INPUT_PULL_REQUEST": "7",
        "INPUT_PYTHON_VERSION": "3.12",
        "INPUT_REPOSITORY_PATH": str(repo),
        "RUNNER_OS": "Linux",
        **overrides,
    }
    result = subprocess.run(
        ["bash", "-c", str(_step("Resolve the comparison")["run"])],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    values = dict(
        line.split("=", 1)
        for line in (output.read_text().splitlines() if output.exists() else [])
    )
    return result, values


def test_action_is_pinned_and_needs_no_model_key() -> None:
    action = _load(ACTION_PATH)

    assert action["runs"]["using"] == "composite"
    assert not {"api-key", "model"} & set(action["inputs"])
    assert {"comment-path", "has-report", "base-commit"} <= set(action["outputs"])
    for step in action["runs"]["steps"]:
        uses = step.get("uses")
        if uses:
            assert PINNED_ACTION_RE.fullmatch(str(uses)), uses
        script = str(step.get("run") or "")
        if script:
            # Inputs reach a script through the environment, never as text.
            assert "${{" not in script, step["name"]
            parsed = subprocess.run(
                ["bash", "-n"], input=script, text=True, capture_output=True
            )
            assert parsed.returncode == 0, parsed.stderr


def test_comparison_starts_where_the_branches_last_met(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    fork_point = _commit(repo, "first")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "feature")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "later-on-main")
    _git(repo, "checkout", "-q", "feature")

    result, values = _resolve(tmp_path, repo)

    assert result.returncode == 0, result.stderr
    # Commits that landed on main after the branch left it are not the change.
    assert values["base_commit"] == fork_point
    assert values["cache_key"].endswith(fork_point)


def test_comparison_rejects_a_checkout_without_the_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "first")

    missing, _ = _resolve(tmp_path, repo, INPUT_BASE="c" * 40)
    assert missing.returncode == 2
    assert "fetch-depth: 0" in missing.stderr

    option, _ = _resolve(tmp_path, repo, INPUT_BASE="--all")
    assert option.returncode == 2

    depth, _ = _resolve(tmp_path, repo, INPUT_DEPTH="3; rm -rf /")
    assert depth.returncode == 2


def _comment(tmp_path: Path, *, existing: str, has_report: bool, refuse: str = ""):
    """Run the comment step against a stand-in ``gh`` that records its calls."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "calls"
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$CALLS"\n'
        'case "$*" in\n'
        '  "api user"*) exit 1 ;;\n'
        '  *"--paginate"*) [[ "$REFUSE" == read ]] && exit 1; '
        'printf "%s" "$EXISTING"; exit 0 ;;\n'
        '  *) [[ "$REFUSE" == write ]] && exit 1; exit 0 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    comment_path = tmp_path / "comment.md"
    comment_path.write_text(f"{MARKER}\nreport\n" if has_report else "")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(calls),
        "EXISTING": existing,
        "REFUSE": refuse,
        "COMMENT_PATH": str(comment_path),
        "GH_TOKEN": "token",
        "GITHUB_REPOSITORY": "example/project",
        "HAS_REPORT": "true" if has_report else "false",
        "PULL_REQUEST": "7",
    }
    result = subprocess.run(
        ["bash", "-c", str(_step("Comment on the pull request")["run"])],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return result, recorded, comment_path.read_text()


def test_first_report_is_posted_and_later_ones_update_it(tmp_path: Path) -> None:
    first, calls, _ = _comment(tmp_path / "a", existing="", has_report=True)
    assert first.returncode == 0, first.stderr
    assert any(
        "--method POST repos/example/project/issues/7/comments" in c for c in calls
    )

    again, calls, _ = _comment(tmp_path / "b", existing="42\n", has_report=True)
    assert again.returncode == 0, again.stderr
    assert any(
        "--method PATCH repos/example/project/issues/comments/42" in c for c in calls
    )
    assert not any("--method POST" in c for c in calls)


def test_change_that_reaches_nothing_stays_silent_or_retracts(tmp_path: Path) -> None:
    silent, calls, _ = _comment(tmp_path / "a", existing="", has_report=False)
    assert silent.returncode == 0
    assert not any("--method" in c for c in calls)

    retract, calls, body = _comment(tmp_path / "b", existing="42\n", has_report=False)
    assert retract.returncode == 0
    assert any("--method PATCH" in c for c in calls)
    assert body.startswith(MARKER)
    assert "no longer changes source" in body


@pytest.mark.parametrize("refuse", ["read", "write"])
def test_read_only_token_leaves_a_notice_and_passes(tmp_path: Path, refuse) -> None:
    result, _, _ = _comment(tmp_path, existing="", has_report=True, refuse=refuse)

    assert result.returncode == 0
    assert "::notice title=CodeNib impact::" in result.stdout


def test_workflow_comments_only_on_this_repository_s_pull_requests() -> None:
    workflow = _load(WORKFLOW_PATH)
    job = workflow["jobs"]["comment"]

    assert workflow["permissions"] == {"contents": "read"}
    assert job["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert "head.repo.full_name == github.repository" in job["if"]
    assert "pull_request_target" not in workflow.get("on", workflow.get("true", {}))
    checkout, impact = job["steps"]
    assert PINNED_ACTION_RE.fullmatch(checkout["uses"])
    assert checkout["with"] == {"fetch-depth": "0", "persist-credentials": "false"}
    assert impact["uses"] == "./.github/actions/impact"
