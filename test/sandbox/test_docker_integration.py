# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codenib.sandbox import (
    DockerSandboxProvider,
    ExecRequest,
    SandboxError,
    SandboxSpec,
)
from codenib.source_fingerprint import fingerprint_repository

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_rootless_docker_session_end_to_end(tmp_path):
    image = os.environ.get("CODENIB_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("CODENIB_TEST_DOCKER_IMAGE is not configured")
    docker_host = os.environ.get("CODENIB_TEST_DOCKER_HOST")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "CodeNib Test")
    _git(repo, "config", "user.email", "test@codenib.invalid")
    (repo / ".gitignore").write_text(".env/\n", encoding="utf-8")
    (repo / "hello.txt").write_text("before\n", encoding="utf-8")
    (repo / ".env").mkdir()
    (repo / ".env" / "ignored-secret.txt").write_text(
        "must-not-enter-snapshot\n", encoding="utf-8"
    )
    (repo / "scripts").mkdir()
    executable = repo / "scripts" / "run.sh"
    executable.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    executable.chmod(0o755)
    _git(repo, "add", ".gitignore", "hello.txt", "scripts/run.sh")
    _git(repo, "commit", "-qm", "fixture")
    revision = _git(repo, "rev-parse", "HEAD")

    provider = DockerSandboxProvider(
        allowed_images={image},
        docker_host=docker_host,
        work_root=tmp_path / "audit",
        retain_audit_logs=True,
    )
    spec = SandboxSpec(
        source_dir=repo,
        source_revision=revision,
        image=image,
        platform=os.environ.get("CODENIB_TEST_DOCKER_PLATFORM", "linux/amd64"),
        task_id="integration-smoke",
    )

    with provider.create(spec) as session:
        assert session.metadata.source_fingerprint == fingerprint_repository(repo).value
        result = session.execute(
            ExecRequest(argv=("/bin/sh", "-lc", "printf 'after\\n' > hello.txt"))
        )
        assert result.succeeded
        executable_result = session.execute(
            ExecRequest(argv=("/bin/sh", "-lc", "test -x scripts/run.sh"))
        )
        assert executable_result.succeeded
        assert session.read_file("hello.txt") == b"after\n"
        with pytest.raises(SandboxError):
            session.read_file(".env/ignored-secret.txt")

        session.write_file("nested/result.txt", b"ok\n", create_parents=True)
        diff = session.get_diff()
        assert diff.truncated is False
        assert "hello.txt" in diff.patch
        assert "nested/result.txt" in diff.patch
        patch_path = tmp_path / "workspace.patch"
        patch_path.write_text(diff.patch, encoding="utf-8")
        _git(repo, "apply", "--check", str(patch_path))

        bundle = session.collect_artifacts(
            ["nested/result.txt"], tmp_path / "artifacts.zip"
        )
        assert bundle.members[0].path == "nested/result.txt"
        assert bundle.members[0].sha256
