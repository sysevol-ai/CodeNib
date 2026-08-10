# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib.repository_filters import REPOSITORY_FILTER_POLICY_VERSION
from codenib.sandbox import (
    DockerSandboxProvider,
    ExecRequest,
    ExecResult,
    SandboxClosedError,
    SandboxError,
    SandboxLimits,
    SandboxPolicy,
    SandboxPolicyError,
    SandboxSpec,
    SandboxUnavailableError,
)
from codenib.sandbox.docker import _FINGERPRINT_SOURCE_SCRIPT, _normalize_no_index_patch
from codenib.source_fingerprint import (
    SOURCE_FINGERPRINT_VERSION,
    RepositoryChangedError,
    fingerprint_repository,
)

DIGEST = "sha256:" + "a" * 64
IMAGE = f"registry.example/codenib-agent@{DIGEST}"
SOURCE_FINGERPRINT = "sha256-v2:" + "e" * 64
TEST_DOCKER_BINARY = str(Path(sys.executable).resolve(strict=True))


class RecordingCLI:
    def __init__(self, *, rootless: bool = True, image_overrides=None):
        self.calls = []
        self.rootless = rootless
        self.image_overrides = dict(image_overrides or {})

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        command = argv[3:] if argv[1:2] == ["--host"] else argv[1:]
        if command[:1] == ["info"]:
            options = ["name=seccomp,profile=builtin"]
            if self.rootless:
                options.append("name=rootless")
            stdout = json.dumps(
                {
                    "SecurityOptions": options,
                    "CgroupVersion": "2",
                    "CgroupDriver": "systemd",
                }
            )
        elif command[:2] == ["image", "inspect"]:
            image = {
                "Id": "sha256:" + "b" * 64,
                "RepoDigests": [IMAGE],
                "Os": "linux",
                "Architecture": "amd64",
                "Variant": "",
                "Config": {"Env": ["PATH=/usr/bin:/bin"], "Volumes": None},
            }
            image.update(self.image_overrides)
            stdout = json.dumps([image])
        elif command[:2] == ["volume", "create"]:
            stdout = command[-1]
        elif command[:1] == ["run"] and "memory.max" in " ".join(command):
            stdout = "memory=2147483648\npids=256\ncpu=200000 100000\n"
        elif command[:1] == ["run"] and "codenib-source-fingerprint" in " ".join(
            command
        ):
            stdout = json.dumps(
                {
                    "file_count": 2,
                    "source_fingerprint": SOURCE_FINGERPRINT,
                }
            )
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class CapturingBytesIO(io.BytesIO):
    captured = b""

    def close(self):
        self.captured = self.getvalue()
        super().close()


class FinishedProcess:
    def __init__(
        self,
        argv,
        *,
        stdout_data=b"ok\n",
        stderr_data=b"",
        returncode=0,
        **kwargs,
    ):
        self.argv = argv
        self.stdout = io.BytesIO(stdout_data)
        self.stderr = io.BytesIO(stderr_data)
        self.stdin = CapturingBytesIO()
        self.returncode = returncode
        self.pid = 99999999

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class HangingProcess(FinishedProcess):
    def __init__(self, argv, **kwargs):
        super().__init__(
            argv,
            stdout_data=b"",
            stderr_data=b"",
            returncode=None,
            **kwargs,
        )


def test_container_fingerprint_helper_matches_host_v2_framing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    baseline = tmp_path / "baseline"
    source.mkdir()
    (source / "a").write_bytes(b"")
    (source / "b").write_bytes(b"X\0F\0c\0Y")
    (source / "package").mkdir()
    (source / "package" / "module.py").write_bytes(b"VALUE = 1\n")
    try:
        (source / "relative-link").symlink_to("b")
        (source / "absolute-link").symlink_to(source / "b")
        (source / "relative-directory-link").symlink_to("package")
        (source / "absolute-directory-link").symlink_to(source / "package")
        (source / "relative-root-link").symlink_to(".", target_is_directory=True)
        (source / "absolute-root-link").symlink_to(
            source,
            target_is_directory=True,
        )
        (source / "broken-link").symlink_to("missing")
        (source / "loop-link").symlink_to("loop-link")
        os.mkfifo(source / ".codenib-hidden-pipe")
        (source / "fifo-link").symlink_to(".codenib-hidden-pipe")
    except OSError as exc:  # pragma: no cover - platform capability
        pytest.skip(f"source fingerprint parity needs symlink support: {exc}")

    expected = fingerprint_repository(source)
    baseline.mkdir()
    for path in source.iterdir():
        destination = baseline / path.name
        if path.is_symlink():
            destination.symlink_to(os.readlink(path))
        elif path.name == ".codenib-hidden-pipe":
            os.mkfifo(destination)
        elif path.is_dir():
            shutil.copytree(path, destination, symlinks=True)
        else:
            shutil.copy2(path, destination)
    source_label = str(source)
    source.rename(tmp_path / "source-unmounted")
    script = _FINGERPRINT_SOURCE_SCRIPT.replace(
        "pathlib.Path('/workspace')",
        f"pathlib.Path({str(baseline)!r})",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            "[]",
            str(SOURCE_FINGERPRINT_VERSION),
            str(REPOSITORY_FILTER_POLICY_VERSION),
            source_label,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    observed = json.loads(completed.stdout)

    assert observed == {
        "file_count": expected.file_count,
        "source_fingerprint": expected.value,
    }


@pytest.mark.parametrize("absolute", [False, True])
def test_container_fingerprint_helper_rejects_outside_directory_symlink(
    tmp_path: Path,
    absolute: bool,
) -> None:
    baseline = tmp_path / "baseline"
    outside = tmp_path / "outside"
    baseline.mkdir()
    outside.mkdir()
    (baseline / "outside-link").symlink_to(
        outside if absolute else "../outside",
        target_is_directory=True,
    )
    with pytest.raises(RepositoryChangedError):
        fingerprint_repository(baseline)

    script = _FINGERPRINT_SOURCE_SCRIPT.replace(
        "pathlib.Path('/workspace')",
        f"pathlib.Path({str(baseline)!r})",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            "[]",
            str(SOURCE_FINGERPRINT_VERSION),
            str(REPOSITORY_FILTER_POLICY_VERSION),
            str(baseline),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "outside the repository" in completed.stderr


def _spec(tmp_path: Path, *, limits=None):
    source = tmp_path / "repo"
    source.mkdir(exist_ok=True)
    return SandboxSpec(
        source_dir=source,
        image=IMAGE,
        platform="linux/amd64",
        task_id="issue-1",
        policy=SandboxPolicy(
            require_source_revision=False,
            limits=limits or SandboxLimits(),
        ),
    )


def _provider(tmp_path, cli, *, popen_factory=FinishedProcess, retain=True):
    return DockerSandboxProvider(
        allowed_images={IMAGE},
        git_binary="/usr/bin/git",
        work_root=tmp_path / "audit",
        retain_audit_logs=retain,
        run_cli=cli,
        popen_factory=popen_factory,
    )


def _docker_subcommand(argv):
    return argv[3:] if argv[1:2] == ["--host"] else argv[1:]


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "versioned-repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / ".gitignore").write_text(".env\n", encoding="utf-8")
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (source / ".env").write_text("IGNORED_SECRET=never-copy\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(source), "add", ".gitignore", "tracked.txt"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CodeNib Test",
            "-c",
            "user.email=test@example.com",
            "-C",
            str(source),
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return source, revision


def test_create_replays_default_security_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI()
    session = _provider(tmp_path, cli).create(_spec(tmp_path))

    run_calls = [
        argv for argv, _ in cli.calls if _docker_subcommand(argv)[:1] == ["run"]
    ]
    assert len(run_calls) == 3  # cgroup probe + baseline/workspace init
    for argv in run_calls:
        joined = " ".join(argv)
        assert "--network none" in joined
        assert "--read-only" in argv
        assert "--cap-drop" in argv and "ALL" in argv
        assert "no-new-privileges=true" in argv
        assert "seccomp=builtin" in argv
        assert "--pids-limit" in argv
        assert "--memory" in argv
        assert "--memory-swap" in argv
        assert "--cpus" in argv
        assert "--pull" in argv and "never" in argv
        assert "--privileged" not in argv
        assert "/var/run/docker.sock" not in joined
        assert all(kwargs.get("shell") is False for _, kwargs in cli.calls)

    copy_calls = [call for call in run_calls if "tar -C /workspace" in " ".join(call)]
    assert len(copy_calls) == 2
    assert all("--no-same-owner" in " ".join(call) for call in copy_calls)

    session.close()


def test_create_rejects_symlinked_source_path(tmp_path):
    real_source = tmp_path / "real-repo"
    real_source.mkdir()
    linked_source = tmp_path / "linked-repo"
    linked_source.symlink_to(real_source, target_is_directory=True)
    spec = SandboxSpec(
        source_dir=linked_source,
        image=IMAGE,
        platform="linux/amd64",
        policy=SandboxPolicy(require_source_revision=False),
    )
    provider = _provider(tmp_path, RecordingCLI())

    with pytest.raises(SandboxPolicyError, match="must not be symlinks"):
        provider.create(spec)


def test_agent_command_is_after_image_and_uses_non_root_user(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI()
    processes = []

    def popen(argv, **kwargs):
        process = FinishedProcess(argv, **kwargs)
        processes.append((process, kwargs))
        return process

    session = _provider(tmp_path, cli, popen_factory=popen).create(_spec(tmp_path))
    result = session.execute(
        ExecRequest(argv=("/bin/sh", "-lc", "echo hi; --privileged"))
    )

    argv = processes[0][0].argv
    image_index = argv.index(IMAGE)
    assert argv[argv.index("--user") + 1] == "65532:65532"
    assert argv[image_index + 1 :] == [
        "/bin/sh",
        "-lc",
        "echo hi; --privileged",
    ]
    assert processes[0][1]["shell"] is False
    assert result.exit_code == 0
    session.close()


def test_write_file_enables_interactive_stdin_and_isolates_helper(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI()
    processes = []

    def popen(argv, **kwargs):
        process = FinishedProcess(argv, **kwargs)
        processes.append(process)
        return process

    session = _provider(tmp_path, cli, popen_factory=popen).create(_spec(tmp_path))
    session.write_file("src/new.py", b"print('written')\n", create_parents=True)

    argv = processes[0].argv
    assert "--interactive" in argv
    image_index = argv.index(IMAGE)
    assert argv[image_index + 1 : image_index + 5] == [
        "python3",
        "-I",
        "-S",
        "-c",
    ]
    assert argv[argv.index("--workdir") + 1] == "/"
    assert processes[0].stdin.captured == b"print('written')\n"
    session.close()


def test_read_file_uses_isolated_python_and_read_only_mount(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI()
    processes = []

    def popen(argv, **kwargs):
        process = FinishedProcess(argv, stdout_data=b"contents", **kwargs)
        processes.append(process)
        return process

    session = _provider(tmp_path, cli, popen_factory=popen).create(_spec(tmp_path))
    assert session.read_file("tracked.txt") == b"contents"

    argv = processes[0].argv
    image_index = argv.index(IMAGE)
    assert argv[image_index + 1 : image_index + 5] == [
        "python3",
        "-I",
        "-S",
        "-c",
    ]
    assert "dst=/workspace,readonly,volume-nocopy" in " ".join(argv)
    assert "--interactive" not in argv
    session.close()


def test_file_byte_limit_zero_fails_instead_of_using_default(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    session = _provider(tmp_path, RecordingCLI()).create(_spec(tmp_path))

    with pytest.raises(SandboxPolicyError, match="file byte limit"):
        session.read_file("tracked.txt", max_bytes=0)

    session.close()


def test_host_secrets_are_not_forwarded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-forward")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    cli = RecordingCLI()
    processes = []

    def popen(argv, **kwargs):
        processes.append((argv, kwargs))
        return FinishedProcess(argv, **kwargs)

    session = _provider(tmp_path, cli, popen_factory=popen).create(_spec(tmp_path))
    session.execute(ExecRequest(argv=("env",)))
    rendered = " ".join(processes[0][0])

    assert "GITHUB_TOKEN" not in rendered
    assert "OPENAI_API_KEY" not in rendered
    assert "do-not-forward" not in rendered
    assert processes[0][1]["env"] == {
        "DOCKER_CONFIG": "/nonexistent/codenib-docker-config",
        "DOCKER_CONTEXT": "",
        "DOCKER_HOST": f"unix:///run/user/{os.getuid()}/docker.sock",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    assert processes[0][0][:3] == [
        TEST_DOCKER_BINARY,
        "--host",
        f"unix:///run/user/{os.getuid()}/docker.sock",
    ]
    session.close()


def test_revision_snapshot_archives_git_objects_not_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    source, revision = _git_repo(tmp_path)
    spec = SandboxSpec(
        source_dir=source,
        source_revision=revision,
        image=IMAGE,
        platform="linux/amd64",
    )
    cli = RecordingCLI()

    session = _provider(tmp_path, cli).create(spec)

    run_calls = [
        _docker_subcommand(argv)
        for argv, _ in cli.calls
        if _docker_subcommand(argv)[:1] == ["run"]
    ]
    archive_call = next(
        call for call in run_calls if "archive --format=tar" in " ".join(call)
    )
    rendered = " ".join(archive_call)
    assert "dst=/repository.git,readonly" in rendered
    assert f"type=bind,src={source},dst=/source" not in rendered
    assert revision in archive_call
    assert session.metadata.source_fingerprint == SOURCE_FINGERPRINT
    created = json.loads(
        (session.audit_dir / "events.jsonl").read_text().splitlines()[0]
    )
    assert created["source_fingerprint"] == session.metadata.source_fingerprint
    assert created["policy"]["limits"]["memory_bytes"] == 2 * 1024**3
    assert created["capabilities"]["process_tree_cleanup"] is True
    session.close()


def test_revision_snapshot_rejects_submodules(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    source, first_revision = _git_repo(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{first_revision},vendor/dependency",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CodeNib Test",
            "-c",
            "user.email=test@example.com",
            "-C",
            str(source),
            "commit",
            "-qm",
            "add gitlink",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    spec = SandboxSpec(
        source_dir=source,
        source_revision=revision,
        image=IMAGE,
        platform="linux/amd64",
    )

    with pytest.raises(SandboxPolicyError, match="submodules"):
        _provider(tmp_path, RecordingCLI()).create(spec)


def test_revision_snapshot_rejects_tracked_symlinks(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    source, _ = _git_repo(tmp_path)
    os.symlink("tracked.txt", source / "tracked-link")
    subprocess.run(["git", "-C", str(source), "add", "tracked-link"], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CodeNib Test",
            "-c",
            "user.email=test@example.com",
            "-C",
            str(source),
            "commit",
            "-qm",
            "add symlink",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(SandboxPolicyError, match="symlinks"):
        _provider(tmp_path, RecordingCLI()).create(
            SandboxSpec(
                source_dir=source,
                source_revision=revision,
                image=IMAGE,
                platform="linux/amd64",
            )
        )


def test_revision_snapshot_rejects_archive_mutating_attributes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    source, _ = _git_repo(tmp_path)
    (source / ".gitattributes").write_text(
        "tracked.txt export-ignore\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(source), "add", ".gitattributes"], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CodeNib Test",
            "-c",
            "user.email=test@example.com",
            "-C",
            str(source),
            "commit",
            "-qm",
            "add archive attributes",
        ],
        check=True,
    )
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with pytest.raises(SandboxPolicyError, match="export-ignore"):
        _provider(tmp_path, RecordingCLI()).create(
            SandboxSpec(
                source_dir=source,
                source_revision=revision,
                image=IMAGE,
                platform="linux/amd64",
            )
        )


def test_revision_snapshot_requires_repository_top_level(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    source, revision = _git_repo(tmp_path)
    subdirectory = source / "nested"
    subdirectory.mkdir()

    with pytest.raises(SandboxPolicyError, match="top-level"):
        _provider(tmp_path, RecordingCLI()).create(
            SandboxSpec(
                source_dir=subdirectory,
                source_revision=revision,
                image=IMAGE,
                platform="linux/amd64",
            )
        )


def test_non_rootless_daemon_fails_before_volume_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI(rootless=False)

    with pytest.raises(SandboxPolicyError, match="rootless"):
        _provider(tmp_path, cli).create(_spec(tmp_path))

    assert not any(
        _docker_subcommand(argv)[:2] == ["volume", "create"] for argv, _ in cli.calls
    )


def test_image_declared_volume_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI(
        image_overrides={"Config": {"Env": [], "Volumes": {"/data": {}}}}
    )

    with pytest.raises(SandboxPolicyError, match="VOLUME"):
        _provider(tmp_path, cli).create(_spec(tmp_path))


def test_timeout_kills_and_removes_container(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI()
    process = None

    def popen(argv, **kwargs):
        nonlocal process
        process = HangingProcess(argv, **kwargs)
        return process

    limits = SandboxLimits(command_timeout_seconds=0.01)
    session = _provider(tmp_path, cli, popen_factory=popen).create(
        _spec(tmp_path, limits=limits)
    )
    result = session.execute(ExecRequest(argv=("sleep", "100")))

    assert result.timed_out is True
    assert result.exit_code is None
    command_name = process.argv[process.argv.index("--name") + 1]
    assert any(
        _docker_subcommand(argv)[:2] == ["kill", command_name] for argv, _ in cli.calls
    )
    assert any(
        _docker_subcommand(argv)[:3] == ["rm", "-f", command_name]
        for argv, _ in cli.calls
    )
    session.close()


def test_output_limit_is_hard_and_audited(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI()

    def popen(argv, **kwargs):
        return FinishedProcess(argv, stdout_data=b"x" * 1024, **kwargs)

    limits = SandboxLimits(output_bytes=64, audit_log_bytes=128)
    session = _provider(tmp_path, cli, popen_factory=popen).create(
        _spec(tmp_path, limits=limits)
    )
    result = session.execute(ExecRequest(argv=("yes",)))

    assert result.output_limited is True
    assert result.stdout_truncated is True
    assert len(result.stdout.encode()) <= 64
    session.close()


def test_model_preview_budget_is_shared_across_stdout_and_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )

    def popen(argv, **kwargs):
        return FinishedProcess(
            argv,
            stdout_data=b"a" * 50,
            stderr_data=b"b" * 50,
            **kwargs,
        )

    limits = SandboxLimits(output_bytes=64, audit_log_bytes=128)
    session = _provider(tmp_path, RecordingCLI(), popen_factory=popen).create(
        _spec(tmp_path, limits=limits)
    )
    result = session.execute(ExecRequest(argv=("emit",)))

    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 64
    assert result.stdout_truncated is False
    assert result.stderr_truncated is True
    session.close()


def test_diff_output_limit_fails_without_returning_partial_patch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )

    def popen(argv, **kwargs):
        return FinishedProcess(argv, stdout_data=b"x" * 256, **kwargs)

    limits = SandboxLimits(output_bytes=32, audit_log_bytes=64)
    session = _provider(tmp_path, RecordingCLI(), popen_factory=popen).create(
        _spec(tmp_path, limits=limits)
    )

    with pytest.raises(SandboxPolicyError, match="partial patches"):
        session.get_diff()

    session.close()


def test_timeout_cleanup_failure_poisons_session(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )

    class FailingCleanupCLI(RecordingCLI):
        fail_container_cleanup = False

        def __call__(self, argv, **kwargs):
            command = _docker_subcommand(argv)
            if self.fail_container_cleanup and tuple(command[:1]) in {
                ("kill",),
                ("rm",),
                ("container",),
            }:
                self.calls.append((list(argv), dict(kwargs)))
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="daemon unavailable"
                )
            return super().__call__(argv, **kwargs)

    cli = FailingCleanupCLI()
    limits = SandboxLimits(command_timeout_seconds=0.01)
    session = _provider(tmp_path, cli, popen_factory=HangingProcess).create(
        _spec(tmp_path, limits=limits)
    )
    cli.fail_container_cleanup = True

    with pytest.raises(SandboxUnavailableError, match="quarantine"):
        session.execute(ExecRequest(argv=("sleep", "100")))
    with pytest.raises(SandboxUnavailableError, match="quarantine"):
        session.execute(ExecRequest(argv=("true",)))

    cli.fail_container_cleanup = False
    session.close()


def test_close_is_idempotent_and_removes_both_volumes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    cli = RecordingCLI()
    session = _provider(tmp_path, cli).create(_spec(tmp_path))

    session.close()
    session.close()

    volume_removes = [
        argv
        for argv, _ in cli.calls
        if _docker_subcommand(argv)[:3] == ["volume", "rm", "-f"]
    ]
    assert len(volume_removes) == 2


def test_close_remains_closed_when_final_audit_write_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    session = _provider(tmp_path, RecordingCLI()).create(_spec(tmp_path))
    original_record = session._record_audit

    def fail_close_event(event, **data):
        if event == "sandbox_closed":
            raise OSError("audit disk unavailable")
        return original_record(event, **data)

    monkeypatch.setattr(session, "_record_audit", fail_close_event)
    with pytest.raises(SandboxError, match="final audit"):
        session.close()
    with pytest.raises(SandboxClosedError):
        session.execute(ExecRequest(argv=("true",)))


@pytest.mark.parametrize("failed_phase", ["command_started", "command_finished"])
def test_command_audit_failure_poisons_session(monkeypatch, tmp_path, failed_phase):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    session = _provider(tmp_path, RecordingCLI()).create(_spec(tmp_path))
    original_record = session._record_audit

    def fail_selected_event(event, **data):
        if event == failed_phase:
            raise OSError("audit disk unavailable")
        return original_record(event, **data)

    monkeypatch.setattr(session, "_record_audit", fail_selected_event)
    with pytest.raises(SandboxUnavailableError, match="audit event"):
        session.execute(ExecRequest(argv=("true",)))
    with pytest.raises(SandboxUnavailableError, match="quarantine"):
        session.execute(ExecRequest(argv=("true",)))
    session.close()


def test_artifact_bundle_size_is_bounded_and_partial_file_removed(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    provider = _provider(tmp_path, RecordingCLI())
    session = provider.create(_spec(tmp_path))
    listing = json.dumps([{"path": "x" * 200, "size": 0}])
    monkeypatch.setattr(
        provider,
        "_run_session_container",
        lambda *args, **kwargs: SimpleNamespace(
            result=ExecResult(
                command_id="listing",
                argv=("python3",),
                exit_code=0,
                stdout=listing,
                stderr="",
                duration_ms=1,
            )
        ),
    )
    monkeypatch.setattr(session, "read_file", lambda *args, **kwargs: b"")
    destination = tmp_path / "artifacts.zip"

    with pytest.raises(SandboxPolicyError, match="bundle exceeds"):
        session.collect_artifacts(["result"], destination, max_bytes=64)

    assert not destination.exists()
    session.close()


def test_artifact_bundle_is_controller_private(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "codenib.sandbox.docker.shutil.which", lambda _: TEST_DOCKER_BINARY
    )
    provider = _provider(tmp_path, RecordingCLI())
    session = provider.create(_spec(tmp_path))
    listing = json.dumps([{"path": "result.txt", "size": 0}])
    monkeypatch.setattr(
        provider,
        "_run_session_container",
        lambda *args, **kwargs: SimpleNamespace(
            result=ExecResult(
                command_id="listing",
                argv=("python3",),
                exit_code=0,
                stdout=listing,
                stderr="",
                duration_ms=1,
            )
        ),
    )
    monkeypatch.setattr(session, "read_file", lambda *args, **kwargs: b"")
    destination = tmp_path / "private.zip"

    bundle = session.collect_artifacts(["result.txt"], destination, max_bytes=4096)

    assert bundle.path == destination
    assert destination.stat().st_mode & 0o777 == 0o600
    session.close()


def test_diff_path_normalization_does_not_rewrite_source_content():
    raw = (
        "diff --git a/baseline/src/a.py b/workspace/src/a.py\n"
        "--- a/baseline/src/a.py\n"
        "+++ b/workspace/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-literal a/baseline/value\n"
        "+literal b/workspace/value\n"
    )

    normalized = _normalize_no_index_patch(raw)

    assert "diff --git a/src/a.py b/src/a.py" in normalized
    assert "--- a/src/a.py" in normalized
    assert "+++ b/src/a.py" in normalized
    assert "-literal a/baseline/value" in normalized
    assert "+literal b/workspace/value" in normalized


def test_diff_normalization_strips_one_outer_component_and_not_hunk_content():
    raw = (
        "diff --git a/baseline/baseline_secret.py b/workspace/workspace_secret.py\n"
        "--- a/baseline/baseline_secret.py\n"
        "+++ b/workspace/workspace_secret.py\n"
        "@@ -1 +1 @@\n"
        "--- a/baseline/content-must-not-change\n"
        "+++ b/workspace/content-must-not-change\n"
        "diff --git a/workspace/added.py b/workspace/added.py\n"
        "--- /dev/null\n"
        "+++ b/workspace/added.py\n"
        "diff --git a/baseline/deleted.py b/baseline/deleted.py\n"
        "--- a/baseline/deleted.py\n"
        "+++ /dev/null\n"
        "diff --git a/baseline/workspace/secret.py "
        "b/workspace/workspace/secret.py\n"
        "--- a/baseline/workspace/secret.py\n"
        "+++ b/workspace/workspace/secret.py\n"
        "diff --git a/workspace/baseline/added.py "
        "b/workspace/baseline/added.py\n"
        "--- /dev/null\n"
        "+++ b/workspace/baseline/added.py\n"
    )

    normalized = _normalize_no_index_patch(raw)

    assert "a/baseline_secret.py b/workspace_secret.py" in normalized
    assert "--- a/baseline_secret.py" in normalized
    assert "+++ b/workspace_secret.py" in normalized
    assert "--- a/baseline/content-must-not-change" in normalized
    assert "+++ b/workspace/content-must-not-change" in normalized
    assert "diff --git a/added.py b/added.py" in normalized
    assert "diff --git a/deleted.py b/deleted.py" in normalized
    assert "diff --git a/workspace/secret.py b/workspace/secret.py" in normalized
    assert "diff --git a/baseline/added.py b/baseline/added.py" in normalized
