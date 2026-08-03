# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rootless-Docker sandbox backend for untrusted repository commands.

The backend deliberately uses one ``docker run --rm`` container per command.
Only a server-created named volume survives between commands. This makes a
timeout or output-limit kill remove the entire process tree instead of leaving
background children in a persistent container.

The repository baseline is held in a second volume that is never mounted into
model-controlled commands. Diff and file helpers are fixed provider calls; they
do not trust a workspace ``.git`` directory or run host-side repository code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Collection, Dict, Mapping, Sequence, TypedDict

from ..repository_filters import DEFAULT_IGNORED_DIRS, REPOSITORY_FILTER_POLICY_VERSION
from ..source_fingerprint import SOURCE_FINGERPRINT_VERSION
from .protocol import (
    SandboxClosedError,
    SandboxError,
    SandboxPolicyError,
    SandboxUnavailableError,
)
from .types import (
    ArtifactBundle,
    ArtifactMember,
    DiffResult,
    ExecRequest,
    ExecResult,
    NetworkMode,
    SandboxCapabilities,
    SandboxMetadata,
    SandboxSpec,
    normalize_workspace_path,
)

_CONTAINER_USER = "65532:65532"
_CONTROL_USER = "0:0"
_MAX_ARTIFACT_FILES = 1000
_SECRET_IMAGE_ENV_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|API_KEY|AUTH|COOKIE|"
    r"AWS_|AZURE_|GOOGLE_APPLICATION_CREDENTIALS|GITHUB_|OPENAI_|ANTHROPIC_|"
    r"SSH_|KUBE|DOCKER_)",
    re.IGNORECASE,
)

_COPY_TRUSTED_SOURCE_SCRIPT = r"""
set -eu
tar -C /source --exclude=.git --exclude='*/.git' -cf - . \
  | tar -C /workspace --no-same-owner -xf -
chown -R 65532:65532 /workspace
""".strip()

_ARCHIVE_COMMIT_SCRIPT = r"""
set -eu
export GIT_NO_REPLACE_OBJECTS=1
export GIT_NO_LAZY_FETCH=1
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=/bin/false
export SSH_ASKPASS=/bin/false
export GIT_ALLOW_PROTOCOL=
git --git-dir=/repository.git -c core.hooksPath=/dev/null \
  -c core.attributesFile=/dev/null \
  archive --format=tar --output=/tmp/source.tar "$1"
tar -C /workspace --no-same-owner -xf /tmp/source.tar
chown -R 65532:65532 /workspace
""".strip()

_COPY_VOLUME_SCRIPT = r"""
set -eu
tar -C /baseline -cf - . | tar -C /workspace --no-same-owner -xf -
chown -R 65532:65532 /workspace
""".strip()

_CGROUP_PROBE_SCRIPT = r"""
set -eu
printf 'memory='; cat /sys/fs/cgroup/memory.max
printf 'pids='; cat /sys/fs/cgroup/pids.max
printf 'cpu='; cat /sys/fs/cgroup/cpu.max
""".strip()

_FINGERPRINT_SOURCE_SCRIPT = r"""
import hashlib, json, os, pathlib, stat, sys

root = pathlib.Path('/workspace')
ignored = set(json.loads(sys.argv[1]))
fingerprint_version = int(sys.argv[2])
filter_version = int(sys.argv[3])
hasher = hashlib.sha256()
hasher.update(
    (
        f'codenib-source-fingerprint:{fingerprint_version}:'
        f'{filter_version}\0'
    ).encode('ascii')
)
file_count = 0
for current_root, directories, files in os.walk(root):
    directories[:] = sorted(
        name for name in directories
        if name not in ignored and not name.startswith('.codenib')
    )
    current = pathlib.Path(current_root)
    for filename in sorted(files):
        path = current / filename
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise SystemExit('source fingerprint does not permit symlinks')
        if not stat.S_ISREG(mode):
            continue
        relative = path.relative_to(root).as_posix().encode(
            'utf-8', errors='surrogateescape'
        )
        hasher.update(b'F\0')
        hasher.update(relative)
        hasher.update(b'\0')
        with path.open('rb') as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
        hasher.update(b'\0')
        file_count += 1
print(json.dumps({
    'file_count': file_count,
    'source_fingerprint': 'sha256:' + hasher.hexdigest(),
}, sort_keys=True, separators=(',', ':')))
""".strip()

_READ_FILE_HELPER = r"""
import os, pathlib, stat, sys
root = pathlib.Path('/workspace')
relative = pathlib.PurePosixPath(sys.argv[1])
limit = int(sys.argv[2])
current = root
for part in relative.parts:
    current = current / part
    if current.is_symlink():
        raise SystemExit('symlink paths are not allowed')
resolved = current.resolve(strict=True)
if resolved != root and root not in resolved.parents:
    raise SystemExit('path escaped workspace')
mode = resolved.stat().st_mode
if not stat.S_ISREG(mode):
    raise SystemExit('path is not a regular file')
with resolved.open('rb') as handle:
    data = handle.read(limit + 1)
if len(data) > limit:
    raise SystemExit('file exceeds byte limit')
sys.stdout.buffer.write(data)
""".strip()

_WRITE_FILE_HELPER = r"""
import os, pathlib, stat, sys, tempfile
root = pathlib.Path('/workspace')
relative = pathlib.PurePosixPath(sys.argv[1])
create_parents = sys.argv[2] == '1'
current = root
parts = relative.parts
for part in parts[:-1]:
    current = current / part
    if current.exists() and current.is_symlink():
        raise SystemExit('symlink paths are not allowed')
    if not current.exists():
        if not create_parents:
            raise SystemExit('parent directory does not exist')
        current.mkdir(mode=0o755)
parent = current.resolve(strict=True)
if parent != root and root not in parent.parents:
    raise SystemExit('path escaped workspace')
target = parent / parts[-1]
if target.exists() and (target.is_symlink() or not target.is_file()):
    raise SystemExit('target must be a regular file')
data = sys.stdin.buffer.read()
fd, temporary = tempfile.mkstemp(prefix='.codenib-write-', dir=parent)
try:
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
""".strip()

_LIST_ARTIFACTS_HELPER = r"""
import json, os, pathlib, stat, sys
root = pathlib.Path('/workspace')
limit = int(sys.argv[1])
seen = set()
rows = []

def add_path(path):
    relative = path.relative_to(root).as_posix()
    if relative in seen:
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit('artifact symlinks are not allowed')
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise SystemExit('artifact hardlinks are not allowed')
        seen.add(relative)
        rows.append({'path': relative, 'size': info.st_size})
        if len(rows) > limit:
            raise SystemExit('artifact file-count limit exceeded')
        return
    if not stat.S_ISDIR(info.st_mode):
        raise SystemExit('artifact contains a special file')
    for entry in sorted(os.scandir(path), key=lambda item: item.name):
        if entry.is_symlink():
            raise SystemExit('artifact symlinks are not allowed')
        add_path(pathlib.Path(entry.path))

for raw in sys.argv[2:]:
    current = root
    for part in pathlib.PurePosixPath(raw).parts:
        current = current / part
        if current.is_symlink():
            raise SystemExit('artifact symlinks are not allowed')
    resolved = current.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise SystemExit('artifact path escaped workspace')
    add_path(resolved)
print(json.dumps(rows, sort_keys=True, separators=(',', ':')))
""".strip()


RunCLI = Callable[..., subprocess.CompletedProcess[str]]
PopenFactory = Callable[..., subprocess.Popen[bytes]]


@dataclass
class _StreamState:
    """Shared byte budget for stdout and stderr drain threads."""

    remaining: int

    def __post_init__(self) -> None:
        self.lock = threading.Lock()
        self.exhausted = threading.Event()
        self.failed = threading.Event()
        self.errors: list[str] = []

    def reserve(self, size: int) -> int:
        with self.lock:
            allowed = min(size, self.remaining)
            self.remaining -= allowed
            if allowed < size:
                self.exhausted.set()
            return allowed

    def record_failure(self, exc: BaseException) -> None:
        with self.lock:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            self.failed.set()


@dataclass(frozen=True)
class _StreamRecord:
    path: Path
    sha256: str
    bytes: int


@dataclass(frozen=True)
class _RunCapture:
    result: ExecResult
    stdout_record: _StreamRecord
    stderr_record: _StreamRecord


class _SourceIdentity(TypedDict):
    source_fingerprint: str
    file_count: int


class DockerSandboxProvider:
    """Create digest-pinned, rootless-Docker sandbox sessions.

    Args:
        allowed_images: Exact digest-pinned image references approved by the
            service control plane. The model never selects an arbitrary image.
        docker_binary: Docker-compatible client binary. This backend validates
            Docker semantics and must not be pointed at Podman.
        work_root: Controller-only directory for bounded audit logs.
        retain_audit_logs: Keep audit logs after session close. Production bot
            workers should set this and apply their own retention policy.
    """

    def __init__(
        self,
        *,
        allowed_images: Collection[str],
        docker_binary: str = "docker",
        docker_host: str | None = None,
        git_binary: str = "git",
        work_root: Path | None = None,
        retain_audit_logs: bool = False,
        run_cli: RunCLI = subprocess.run,
        popen_factory: PopenFactory = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        images = frozenset(allowed_images)
        if not images:
            raise ValueError("allowed_images must contain at least one pinned image")
        self._allowed_images = images
        self._docker_binary_request = docker_binary
        self._docker_binary: str | None = None
        self._git_binary_request = git_binary
        self._git_binary: str | None = None
        self._docker_host = docker_host or f"unix:///run/user/{os.getuid()}/docker.sock"
        self._validate_docker_host(self._docker_host)
        # Every client call uses the same explicit endpoint, absolute binary,
        # and minimal environment. Host Docker contexts/config and credentials
        # cannot redirect a preflighted call or reach a registry helper.
        self._docker_env = {
            "DOCKER_CONFIG": "/nonexistent/codenib-docker-config",
            "DOCKER_CONTEXT": "",
            "DOCKER_HOST": self._docker_host,
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        self._work_root = Path(work_root) if work_root is not None else None
        self._retain_audit_logs = retain_audit_logs
        self._run_cli = run_cli
        self._popen_factory = popen_factory
        self._clock = clock
        self._id_factory = id_factory

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            provider="docker",
            isolation="container",
            non_root_user=True,
            network_isolation=True,
            read_only_rootfs=True,
            resource_limits=True,
            process_tree_cleanup=True,
            disk_quota=False,
            rootless_runtime=None,
        )

    def create(self, spec: SandboxSpec) -> "DockerSandboxSession":
        """Preflight runtime/image policy, copy the source, and return a session."""

        if spec.image not in self._allowed_images:
            raise SandboxPolicyError("image is not in the provider allowlist")
        if spec.source_revision is not None:
            self._resolve_git_binary()
        source_request = (
            spec.source_dir
            if spec.source_dir.is_absolute()
            else Path.cwd() / spec.source_dir
        )
        self._validate_host_mount_path(source_request)
        if not source_request.is_dir():
            raise SandboxPolicyError(
                f"source directory does not exist: {spec.source_dir}"
            )
        source = source_request.resolve(strict=True)
        if source == Path(source.anchor):
            raise SandboxPolicyError("filesystem root cannot be a sandbox source")
        if self._work_root is not None:
            audit_root = self._work_root.expanduser().resolve()
            if audit_root == source or source in audit_root.parents:
                raise SandboxPolicyError(
                    "controller audit root must be outside the repository source"
                )
        git_common_dir = self._verify_source_revision(source, spec.source_revision)

        runtime = self._inspect_runtime(spec)
        image = self._inspect_image(spec)
        sandbox_token = self._id_factory().hex
        sandbox_id = f"sbx-{sandbox_token}"
        self._verify_runtime_limits(spec, sandbox_id)
        task_id = spec.task_id or sandbox_id
        baseline_volume = f"codenib-{sandbox_token}-base"
        workspace_volume = f"codenib-{sandbox_token}-work"
        audit_dir = self._create_audit_dir(sandbox_token)
        created_volumes: list[str] = []

        try:
            created_volumes.append(baseline_volume)
            self._create_volume(baseline_volume, sandbox_id, "baseline")
            created_volumes.append(workspace_volume)
            self._create_volume(workspace_volume, sandbox_id, "workspace")
            self._populate_baseline(
                spec,
                sandbox_id=sandbox_id,
                source=source,
                git_common_dir=git_common_dir,
                baseline_volume=baseline_volume,
            )
            source_identity: _SourceIdentity = (
                self._fingerprint_baseline(
                    spec,
                    sandbox_id=sandbox_id,
                    baseline_volume=baseline_volume,
                )
                if spec.source_revision is not None
                else {"source_fingerprint": "", "file_count": 0}
            )
            # Detect a controller checkout changing during the snapshot.
            self._verify_source_revision(source, spec.source_revision)
            self._populate_workspace(
                spec,
                sandbox_id=sandbox_id,
                baseline_volume=baseline_volume,
                workspace_volume=workspace_volume,
            )

            metadata = SandboxMetadata(
                sandbox_id=sandbox_id,
                task_id=task_id,
                provider="docker",
                image=spec.image,
                image_id=str(image["Id"]),
                platform=spec.platform,
                source_revision=spec.source_revision,
                network=spec.policy.network.value,
                rootless_runtime=runtime["rootless"],
                source_fingerprint=source_identity["source_fingerprint"],
                policy=_policy_summary(spec),
            )
            session = DockerSandboxSession(
                provider=self,
                spec=spec,
                metadata=metadata,
                baseline_volume=baseline_volume,
                workspace_volume=workspace_volume,
                audit_dir=audit_dir,
            )
            session._record_audit(
                "sandbox_created",
                image_id=metadata.image_id,
                source_revision=metadata.source_revision,
                source_fingerprint=metadata.source_fingerprint,
                source_file_count=source_identity["file_count"],
                platform=metadata.platform,
                network=metadata.network,
                rootless_runtime=metadata.rootless_runtime,
                policy=metadata.policy,
                capabilities=asdict(session.capabilities),
            )
        except Exception as exc:
            failed_cleanup = [
                volume
                for volume in reversed(created_volumes)
                if not self._remove_volume(volume)
            ]
            if failed_cleanup:
                _write_emergency_audit(
                    audit_dir,
                    event="bootstrap_cleanup_failed",
                    sandbox_id=sandbox_id,
                    volumes=failed_cleanup,
                )
                raise SandboxUnavailableError(
                    "sandbox bootstrap cleanup failed; quarantine and reap the "
                    "worker before accepting another job"
                ) from exc
            self._remove_audit_dir(audit_dir)
            raise
        return session

    def _inspect_runtime(self, spec: SandboxSpec) -> Dict[str, Any]:
        binary = shutil.which(self._docker_binary_request)
        if binary is None:
            raise SandboxUnavailableError(
                f"Docker client is unavailable: {self._docker_binary_request}"
            )
        try:
            resolved_binary = Path(binary).resolve(strict=True)
        except OSError as exc:
            raise SandboxUnavailableError(
                f"Docker client cannot be resolved safely: {binary}"
            ) from exc
        if not resolved_binary.is_file() or not os.access(resolved_binary, os.X_OK):
            raise SandboxUnavailableError(
                f"Docker client is not an executable file: {resolved_binary}"
            )
        self._docker_binary = str(resolved_binary)

        completed = self._call_cli(["info", "--format", "{{json .}}"])
        try:
            info = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SandboxUnavailableError("Docker info returned invalid JSON") from exc
        options_text = json.dumps(info.get("SecurityOptions") or []).lower()
        rootless = "rootless" in options_text
        seccomp = "seccomp" in options_text
        if spec.policy.require_rootless_runtime and not rootless:
            raise SandboxPolicyError("Docker daemon is not running in rootless mode")
        if not seccomp:
            raise SandboxPolicyError("Docker daemon does not report seccomp support")
        if rootless and (
            str(info.get("CgroupVersion")) != "2"
            or str(info.get("CgroupDriver")) != "systemd"
        ):
            raise SandboxPolicyError(
                "rootless Docker needs cgroup v2 + systemd for enforced limits"
            )
        return {"rootless": rootless, "info": info}

    def _inspect_image(self, spec: SandboxSpec) -> Mapping[str, Any]:
        completed = self._call_cli(["image", "inspect", spec.image])
        try:
            images = json.loads(completed.stdout)
            image = images[0]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
            raise SandboxUnavailableError(
                "Docker image inspect returned invalid JSON"
            ) from exc

        expected_os, expected_arch, *expected_variant = spec.platform.split("/")
        actual_variant = str(image.get("Variant") or "")
        if (
            str(image.get("Os")) != expected_os
            or str(image.get("Architecture")) != expected_arch
        ):
            raise SandboxPolicyError(
                "image platform does not match SandboxSpec.platform"
            )
        if expected_variant and actual_variant != expected_variant[0]:
            raise SandboxPolicyError(
                "image variant does not match SandboxSpec.platform"
            )

        requested_digest = spec.image.rsplit("@", 1)[-1] if "@" in spec.image else None
        repo_digests = image.get("RepoDigests") or []
        if requested_digest and not any(
            str(value).endswith(f"@{requested_digest}") for value in repo_digests
        ):
            raise SandboxPolicyError("local image does not match the requested digest")

        config = image.get("Config") or {}
        if config.get("Volumes"):
            raise SandboxPolicyError(
                "sandbox images must not declare Docker VOLUME paths"
            )
        for item in config.get("Env") or []:
            name = str(item).partition("=")[0]
            if _SECRET_IMAGE_ENV_RE.search(name):
                raise SandboxPolicyError(
                    f"sandbox image embeds a sensitive environment name: {name}"
                )
        if not image.get("Id"):
            raise SandboxUnavailableError("Docker image inspect omitted image Id")
        return image

    def _verify_runtime_limits(self, spec: SandboxSpec, sandbox_id: str) -> None:
        """Run a fixed probe and verify cgroup limits were not silently ignored."""

        name = f"{sandbox_id}-limit-probe"
        args = self._base_run_args(
            spec,
            container_name=name,
            user=_CONTAINER_USER,
            network=NetworkMode.NONE,
        )
        args.extend(
            [
                "--workdir",
                "/",
                spec.image,
                "/bin/sh",
                "-c",
                _CGROUP_PROBE_SCRIPT,
            ]
        )
        completed = self._run_control_container(name, args)
        values: Dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value.strip()
        limits = spec.policy.limits
        if values.get("memory") != str(limits.memory_bytes):
            raise SandboxPolicyError(
                "Docker did not enforce the requested memory limit"
            )
        if values.get("pids") != str(limits.pids):
            raise SandboxPolicyError("Docker did not enforce the requested PID limit")
        cpu_parts = values.get("cpu", "").split()
        try:
            quota, period = (float(value) for value in cpu_parts)
        except (TypeError, ValueError) as exc:
            raise SandboxPolicyError(
                "Docker did not expose a finite CPU limit"
            ) from exc
        if period <= 0 or abs((quota / period) - limits.cpus) > 0.001:
            raise SandboxPolicyError("Docker did not enforce the requested CPU limit")

    def _verify_source_revision(
        self, source: Path, revision: str | None
    ) -> Path | None:
        if revision is None:
            return None
        completed = subprocess.run(
            self._git_command(
                [
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    str(source),
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                ]
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=_safe_git_environment(),
        )
        actual = completed.stdout.strip()
        if completed.returncode != 0 or actual != revision:
            raise SandboxPolicyError(
                f"source checkout is not at requested revision {revision}"
            )
        top_level_result = subprocess.run(
            self._git_command(["-C", str(source), "rev-parse", "--show-toplevel"]),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=_safe_git_environment(),
        )
        try:
            top_level = Path(top_level_result.stdout.strip()).resolve(strict=True)
        except OSError as exc:
            raise SandboxPolicyError("source Git top-level is unavailable") from exc
        if top_level_result.returncode != 0 or top_level != source:
            raise SandboxPolicyError(
                "sandbox source_dir must be the repository top-level directory"
            )
        common_dir_result = subprocess.run(
            self._git_command(
                [
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(source),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ]
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_safe_git_environment(),
        )
        common_dir_request = Path(common_dir_result.stdout.strip())
        try:
            self._validate_host_mount_path(common_dir_request)
            common_dir = common_dir_request.resolve(strict=True)
        except OSError as exc:
            raise SandboxPolicyError(
                "source Git object directory is unavailable"
            ) from exc
        if common_dir_result.returncode != 0 or not common_dir.is_dir():
            raise SandboxPolicyError(
                "source checkout does not expose a valid Git object directory"
            )
        tree_modes = subprocess.run(
            self._git_command(
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    str(source),
                    "ls-tree",
                    "-r",
                    "--full-tree",
                    "--format=%(objectmode)",
                    revision,
                ]
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_safe_git_environment(),
        )
        if tree_modes.returncode != 0:
            raise SandboxPolicyError("source Git tree cannot be inspected safely")
        if "160000" in tree_modes.stdout.splitlines():
            raise SandboxPolicyError(
                "Git submodules are unsupported in revision-pinned sandboxes"
            )
        if "120000" in tree_modes.stdout.splitlines():
            raise SandboxPolicyError(
                "Git symlinks are unsupported in revision-pinned sandboxes"
            )
        archive_attributes = subprocess.run(
            self._git_command(
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-C",
                    str(source),
                    "grep",
                    "--quiet",
                    "--extended-regexp",
                    r"export-(ignore|subst)",
                    revision,
                    "--",
                    ".gitattributes",
                    ":(glob)**/.gitattributes",
                ]
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_safe_git_environment(),
        )
        if archive_attributes.returncode == 0:
            raise SandboxPolicyError(
                "export-ignore/export-subst attributes are unsupported in exact "
                "revision snapshots"
            )
        if archive_attributes.returncode != 1:
            raise SandboxPolicyError("source archive attributes cannot be inspected")
        info_attributes = common_dir / "info" / "attributes"
        try:
            info_attributes_stat = info_attributes.lstat()
        except FileNotFoundError:
            info_attributes_stat = None
        except OSError as exc:
            raise SandboxPolicyError(
                "Git info attributes file cannot be inspected"
            ) from exc
        if info_attributes_stat is not None:
            if not stat.S_ISREG(info_attributes_stat.st_mode):
                raise SandboxPolicyError(
                    "Git info attributes must be a regular, non-symlink file"
                )
            try:
                if info_attributes_stat.st_size > 1024**2:
                    raise SandboxPolicyError("Git info attributes file is too large")
                info_text = info_attributes.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError as exc:
                raise SandboxPolicyError(
                    "Git info attributes file cannot be inspected"
                ) from exc
            if re.search(r"export-(?:ignore|subst)", info_text):
                raise SandboxPolicyError(
                    "Git info export attributes are unsupported in exact snapshots"
                )
        self._validate_host_mount_path(common_dir)
        return common_dir

    def _create_volume(self, name: str, sandbox_id: str, kind: str) -> None:
        self._call_cli(
            [
                "volume",
                "create",
                "--label",
                f"ai.codenib.sandbox={sandbox_id}",
                "--label",
                f"ai.codenib.kind={kind}",
                name,
            ]
        )

    def _populate_baseline(
        self,
        spec: SandboxSpec,
        *,
        sandbox_id: str,
        source: Path,
        git_common_dir: Path | None,
        baseline_volume: str,
    ) -> None:
        name = f"{sandbox_id}-init-base"
        args = self._base_run_args(
            spec,
            container_name=name,
            user=_CONTROL_USER,
            network=NetworkMode.NONE,
        )
        args.extend(["--cap-add", "CHOWN"])
        if spec.source_revision is not None:
            if git_common_dir is None:
                raise SandboxPolicyError("verified Git source has no object directory")
            args.extend(
                [
                    "--mount",
                    f"type=bind,src={git_common_dir},dst=/repository.git,readonly",
                ]
            )
        else:
            # Explicitly trusted generated fixtures may opt out of Git revision
            # verification. Public automation must never use this path.
            args.extend(
                [
                    "--mount",
                    f"type=bind,src={source},dst=/source,readonly",
                ]
            )
        args.extend(
            [
                "--mount",
                f"type=volume,src={baseline_volume},dst=/workspace,volume-nocopy",
                "--workdir",
                "/",
                spec.image,
                "/bin/sh",
                "-c",
                (
                    _ARCHIVE_COMMIT_SCRIPT
                    if spec.source_revision is not None
                    else _COPY_TRUSTED_SOURCE_SCRIPT
                ),
            ]
        )
        if spec.source_revision is not None:
            args.extend(["codenib-archive", spec.source_revision])
        self._run_control_container(name, args)

    def _populate_workspace(
        self,
        spec: SandboxSpec,
        *,
        sandbox_id: str,
        baseline_volume: str,
        workspace_volume: str,
    ) -> None:
        name = f"{sandbox_id}-init-work"
        args = self._base_run_args(
            spec,
            container_name=name,
            user=_CONTROL_USER,
            network=NetworkMode.NONE,
        )
        args.extend(
            [
                "--cap-add",
                "CHOWN",
                "--mount",
                f"type=volume,src={baseline_volume},dst=/baseline,readonly,volume-nocopy",
                "--mount",
                f"type=volume,src={workspace_volume},dst=/workspace,volume-nocopy",
                "--workdir",
                "/",
                spec.image,
                "/bin/sh",
                "-c",
                _COPY_VOLUME_SCRIPT,
            ]
        )
        self._run_control_container(name, args)

    def _fingerprint_baseline(
        self,
        spec: SandboxSpec,
        *,
        sandbox_id: str,
        baseline_volume: str,
    ) -> _SourceIdentity:
        name = f"{sandbox_id}-fingerprint"
        args = self._base_run_args(
            spec,
            container_name=name,
            user=_CONTAINER_USER,
            network=NetworkMode.NONE,
        )
        args.extend(
            [
                "--mount",
                f"type=volume,src={baseline_volume},dst=/workspace,readonly,volume-nocopy",
                "--workdir",
                "/",
                spec.image,
                "python3",
                "-I",
                "-S",
                "-c",
                _FINGERPRINT_SOURCE_SCRIPT,
                json.dumps(sorted(DEFAULT_IGNORED_DIRS), separators=(",", ":")),
                str(SOURCE_FINGERPRINT_VERSION),
                str(REPOSITORY_FILTER_POLICY_VERSION),
            ]
        )
        completed = self._run_control_container(name, args)
        try:
            identity = json.loads(completed.stdout)
            fingerprint = str(identity["source_fingerprint"])
            file_count = int(identity["file_count"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxUnavailableError(
                "sandbox source fingerprint helper returned invalid output"
            ) from exc
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) or file_count < 0:
            raise SandboxUnavailableError(
                "sandbox source fingerprint helper returned invalid identity"
            )
        return {"source_fingerprint": fingerprint, "file_count": file_count}

    def _run_control_container(
        self, name: str, args: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        failed = False
        try:
            completed = self._run_cli(
                self._docker_command(args),
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                shell=False,
                env=self._docker_env,
            )
        except subprocess.TimeoutExpired as exc:
            failed = True
            raise SandboxUnavailableError("sandbox bootstrap timed out") from exc
        except OSError as exc:
            failed = True
            raise SandboxUnavailableError(f"sandbox bootstrap failed: {exc}") from exc
        finally:
            cleanup_ok = self._remove_container(name)
            if failed and not cleanup_ok:
                raise SandboxUnavailableError(
                    "sandbox bootstrap cleanup failed; quarantine and reap the worker"
                )
        if not cleanup_ok:
            raise SandboxUnavailableError(
                "sandbox bootstrap cleanup failed; quarantine and reap the worker"
            )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "unknown error").strip()
            raise SandboxUnavailableError(f"sandbox bootstrap failed: {message[:500]}")
        return completed

    def _base_run_args(
        self,
        spec: SandboxSpec,
        *,
        container_name: str,
        user: str,
        network: NetworkMode,
    ) -> list[str]:
        limits = spec.policy.limits
        args = [
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            f"ai.codenib.sandbox={container_name.split('-cmd-')[0]}",
            "--pull",
            "never",
            "--platform",
            spec.platform,
            "--network",
            network.value,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            (
                f"seccomp={spec.policy.seccomp_profile}"
                if spec.policy.seccomp_profile
                else "seccomp=builtin"
            ),
            "--user",
            user,
            "--pids-limit",
            str(limits.pids),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            str(limits.cpus),
            "--ulimit",
            "nofile=1024:1024",
            "--ulimit",
            "core=0:0",
            "--ipc",
            "private",
            "--cgroupns",
            "private",
            "--init",
            "--stop-timeout",
            "2",
            "--no-healthcheck",
            "--log-driver",
            "none",
            "--hostname",
            "codenib-sandbox",
            "--tmpfs",
            ("/tmp:rw,nosuid,nodev,noexec,mode=1777,size=" f"{limits.tmpfs_bytes}"),
            "--tmpfs",
            "/run:rw,nosuid,nodev,noexec,size=16777216",
            "--env",
            "HOME=/tmp/codenib-home",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            "CI=1",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--env",
            "HTTP_PROXY=",
            "--env",
            "HTTPS_PROXY=",
            "--env",
            "ALL_PROXY=",
            "--env",
            "NO_PROXY=",
            "--env",
            "http_proxy=",
            "--env",
            "https_proxy=",
            "--env",
            "all_proxy=",
            "--env",
            "no_proxy=",
            "--entrypoint",
            "",
        ]
        if spec.policy.read_only_rootfs:
            args.append("--read-only")
        if spec.policy.runtime:
            args.extend(["--runtime", spec.policy.runtime])
        return args

    def _run_session_container(
        self,
        session: "DockerSandboxSession",
        request: ExecRequest,
        *,
        mounts: Sequence[str] | None = None,
        workdir: str | None = None,
        audit_limit: int | None = None,
        preview_limit: int | None = None,
    ) -> _RunCapture:
        timeout = (
            request.timeout_seconds
            or session._spec.policy.limits.command_timeout_seconds
        )
        if timeout > session._spec.policy.limits.command_timeout_seconds:
            raise SandboxPolicyError("command timeout exceeds the session policy")
        if (
            request.stdin is not None
            and len(request.stdin) > session._spec.policy.limits.stdin_bytes
        ):
            raise SandboxPolicyError("command stdin exceeds the session policy")

        command_id = self._id_factory().hex
        name = f"{session.sandbox_id}-cmd-{command_id[:12]}"
        args = self._base_run_args(
            session._spec,
            container_name=name,
            user=_CONTAINER_USER,
            network=session._spec.policy.network,
        )
        if request.stdin is not None:
            args.append("--interactive")
        effective_mounts = list(mounts or [])
        if not effective_mounts:
            effective_mounts.append(
                f"type=volume,src={session._workspace_volume},"
                "dst=/workspace,volume-nocopy"
            )
        for mount in effective_mounts:
            args.extend(["--mount", mount])
        cwd = workdir or _container_workspace_path(
            normalize_workspace_path(request.cwd)
        )
        args.extend(["--workdir", cwd])
        for key, value in sorted(request.environment.items()):
            args.extend(["--env", f"{key}={value}"])
        args.extend([session._spec.image, *request.argv])

        audit_limit = audit_limit or session._spec.policy.limits.audit_log_bytes
        preview_limit = preview_limit or session._spec.policy.limits.output_bytes
        capture = self._spawn_bounded(
            session,
            command_id=command_id,
            container_name=name,
            docker_args=args,
            request=request,
            timeout=timeout,
            audit_limit=audit_limit,
            preview_limit=preview_limit,
        )
        return capture

    def _spawn_bounded(
        self,
        session: "DockerSandboxSession",
        *,
        command_id: str,
        container_name: str,
        docker_args: Sequence[str],
        request: ExecRequest,
        timeout: float,
        audit_limit: int,
        preview_limit: int,
    ) -> _RunCapture:
        stdout_path = session._audit_dir / f"{command_id}.stdout"
        stderr_path = session._audit_dir / f"{command_id}.stderr"
        started = self._clock()
        stream_state = _StreamState(audit_limit)
        try:
            session._record_audit(
                "command_started",
                command_id=command_id,
                argv=list(request.argv),
                cwd=str(request.cwd),
                environment_names=sorted(request.environment),
                timeout_seconds=timeout,
            )
        except OSError as exc:
            session._poison("audit_event_failed", phase="command_started")
            raise SandboxUnavailableError(
                "sandbox audit event write failed; quarantine the worker"
            ) from exc

        try:
            process = self._popen_factory(
                self._docker_command(docker_args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                env=self._docker_env,
            )
        except OSError as exc:
            if not self._remove_container(container_name):
                session._poison(
                    "container_cleanup_failed", container_name=container_name
                )
            raise SandboxUnavailableError(f"failed to start Docker: {exc}") from exc

        stdout_holder: list[_StreamRecord] = []
        stderr_holder: list[_StreamRecord] = []
        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout_path, stream_state, stdout_holder),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_path, stream_state, stderr_holder),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        writer = threading.Thread(
            target=_write_stdin,
            args=(process.stdin, request.stdin or b""),
            daemon=True,
        )
        writer.start()
        deadline = started + timeout
        timed_out = False
        output_limited = False
        cleanup_failed = False
        audit_failed = False

        try:
            while process.poll() is None:
                if stream_state.failed.is_set():
                    audit_failed = True
                    cleanup_failed = not self._remove_container(container_name)
                    _kill_process_group(process)
                    break
                if stream_state.exhausted.is_set():
                    output_limited = True
                    cleanup_failed = not self._remove_container(container_name)
                    _kill_process_group(process)
                    break
                if self._clock() >= deadline:
                    timed_out = True
                    cleanup_failed = not self._remove_container(container_name)
                    _kill_process_group(process)
                    break
                time.sleep(0.02)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
                process.wait(timeout=5)
        finally:
            # ``--rm`` handles the normal path; this covers client crashes and
            # timeout races without trusting it to have removed the container.
            cleanup_failed = (
                not self._remove_container(container_name) or cleanup_failed
            )
            writer.join(timeout=1)
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)

        if cleanup_failed:
            session._poison("container_cleanup_failed", container_name=container_name)
            raise SandboxUnavailableError(
                "sandbox container cleanup failed; quarantine and reap the worker"
            )
        audit_failed = audit_failed or stream_state.failed.is_set()
        if audit_failed:
            session._poison("audit_stream_failed", errors=stream_state.errors[:2])
            raise SandboxUnavailableError(
                "sandbox audit output capture failed; quarantine the worker"
            )

        # A short-lived command can exit before the reader threads observe the
        # final buffered chunk. Honour the shared cap even on that race.
        output_limited = output_limited or stream_state.exhausted.is_set()

        stdout_record = (
            stdout_holder[0] if stdout_holder else _empty_stream(stdout_path)
        )
        stderr_record = (
            stderr_holder[0] if stderr_holder else _empty_stream(stderr_path)
        )
        stdout_preview = _read_preview(stdout_record.path, preview_limit)
        stderr_preview = _read_preview(
            stderr_record.path, max(0, preview_limit - len(stdout_preview))
        )
        duration_ms = (self._clock() - started) * 1000
        result = ExecResult(
            command_id=command_id,
            argv=tuple(request.argv),
            exit_code=None if (timed_out or output_limited) else process.returncode,
            stdout=stdout_preview.decode("utf-8", errors="replace"),
            stderr=stderr_preview.decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            timed_out=timed_out,
            output_limited=output_limited,
            stdout_truncated=(
                output_limited or stdout_record.bytes > len(stdout_preview)
            ),
            stderr_truncated=(
                output_limited or stderr_record.bytes > len(stderr_preview)
            ),
            stdout_sha256=stdout_record.sha256,
            stderr_sha256=stderr_record.sha256,
            stdout_bytes=stdout_record.bytes,
            stderr_bytes=stderr_record.bytes,
        )
        try:
            session._record_audit(
                "command_finished",
                **{
                    key: value
                    for key, value in result.to_dict().items()
                    if key not in {"stdout", "stderr"}
                },
            )
        except OSError as exc:
            session._poison("audit_event_failed", phase="command_finished")
            raise SandboxUnavailableError(
                "sandbox audit event write failed; quarantine the worker"
            ) from exc
        return _RunCapture(result, stdout_record, stderr_record)

    def _call_cli(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._run_cli(
                self._docker_command(args),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
                env=self._docker_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError(f"Docker CLI failed: {exc}") from exc
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "unknown error").strip()
            raise SandboxUnavailableError(f"Docker CLI failed: {message[:500]}")
        return completed

    def _remove_container(self, name: str) -> bool:
        removed = False
        for action in (("kill", name), ("rm", "-f", name)):
            try:
                completed = self._run_cli(
                    self._docker_command(action),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    shell=False,
                    env=self._docker_env,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if action[0] == "rm" and completed.returncode == 0:
                removed = True
        if removed:
            return True
        try:
            inspected = self._run_cli(
                self._docker_command(("container", "inspect", name)),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
                env=self._docker_env,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        message = f"{inspected.stdout}\n{inspected.stderr}".lower()
        return inspected.returncode != 0 and (
            "no such container" in message or "no such object" in message
        )

    def _remove_volume(self, name: str) -> bool:
        try:
            completed = self._run_cli(
                self._docker_command(("volume", "rm", "-f", name)),
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
                env=self._docker_env,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def _create_audit_dir(self, token: str) -> Path:
        parent = self._work_root
        if parent is not None:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = parent.resolve(strict=True)
        path = Path(
            tempfile.mkdtemp(prefix=f"codenib-sandbox-{token[:12]}-", dir=parent)
        )
        path.chmod(0o700)
        return path

    def _remove_audit_dir(self, path: Path) -> None:
        if path.name.startswith("codenib-sandbox-") and path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def _validate_host_mount_path(path: Path) -> None:
        if not path.is_absolute():
            raise SandboxPolicyError("host mount paths must be absolute")
        if any(character in str(path) for character in (",", "\n", "\r", "\x00")):
            raise SandboxPolicyError(
                "source path cannot be represented as a Docker mount"
            )
        current = Path(path.anchor)
        for part in path.parts[1:]:
            if part == "..":
                raise SandboxPolicyError("host mount paths cannot contain '..'")
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                # The caller emits the more useful missing-source error. No
                # later component can exist beneath a missing ancestor.
                break
            except OSError as exc:
                raise SandboxPolicyError(
                    f"host mount path cannot be inspected: {current}"
                ) from exc
            if stat.S_ISLNK(mode):
                raise SandboxPolicyError("source path ancestors must not be symlinks")

    @staticmethod
    def _validate_docker_host(host: str) -> None:
        if not isinstance(host, str) or not host.startswith("unix:///"):
            raise ValueError("docker_host must be an absolute Unix-socket endpoint")
        if any(character in host for character in ("\n", "\r", "\x00")):
            raise ValueError("docker_host contains unsupported characters")

    def _docker_command(self, args: Sequence[str]) -> list[str]:
        if self._docker_binary is None:
            raise SandboxUnavailableError("Docker client has not passed preflight")
        return [self._docker_binary, "--host", self._docker_host, *args]

    def _resolve_git_binary(self) -> None:
        requested = Path(self._git_binary_request)
        binary = (
            str(requested)
            if requested.is_absolute()
            else shutil.which(self._git_binary_request)
        )
        if binary is None:
            raise SandboxUnavailableError(
                f"Git client is unavailable: {self._git_binary_request}"
            )
        try:
            resolved = Path(binary).resolve(strict=True)
        except OSError as exc:
            raise SandboxUnavailableError(
                f"Git client cannot be resolved safely: {binary}"
            ) from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise SandboxUnavailableError(
                f"Git client is not an executable file: {resolved}"
            )
        self._git_binary = str(resolved)

    def _git_command(self, args: Sequence[str]) -> list[str]:
        if self._git_binary is None:
            raise SandboxUnavailableError("Git client has not passed preflight")
        return [self._git_binary, *args]


class DockerSandboxSession:
    """A copied repository backed by private baseline/workspace volumes."""

    def __init__(
        self,
        *,
        provider: DockerSandboxProvider,
        spec: SandboxSpec,
        metadata: SandboxMetadata,
        baseline_volume: str,
        workspace_volume: str,
        audit_dir: Path,
    ) -> None:
        self._provider = provider
        self._spec = spec
        self._metadata = metadata
        self._baseline_volume = baseline_volume
        self._workspace_volume = workspace_volume
        self._audit_dir = audit_dir
        self._closed = False
        self._poisoned = False
        self._lock = threading.RLock()

    @property
    def sandbox_id(self) -> str:
        return self._metadata.sandbox_id

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            provider="docker",
            isolation=(
                "rootless-container"
                if self._metadata.rootless_runtime
                else "rootful-container"
            ),
            non_root_user=True,
            network_isolation=self._spec.policy.network is NetworkMode.NONE,
            read_only_rootfs=self._spec.policy.read_only_rootfs,
            resource_limits=True,
            process_tree_cleanup=True,
            disk_quota=False,
            rootless_runtime=self._metadata.rootless_runtime,
        )

    @property
    def metadata(self) -> SandboxMetadata:
        return self._metadata

    @property
    def audit_dir(self) -> Path:
        """Controller-only audit directory; never mounted into the sandbox."""

        return self._audit_dir

    def execute(self, request: ExecRequest) -> ExecResult:
        with self._lock:
            self._ensure_open()
            return self._provider._run_session_container(self, request).result

    def read_file(
        self,
        path: str | PurePosixPath,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        with self._lock:
            self._ensure_open()
            relative = normalize_workspace_path(path)
            limit = _bounded_byte_limit(
                max_bytes,
                default=self._spec.policy.limits.artifact_bytes,
                maximum=self._spec.policy.limits.artifact_bytes,
                label="file",
            )
            capture = self._provider._run_session_container(
                self,
                ExecRequest(
                    argv=(
                        "python3",
                        "-I",
                        "-S",
                        "-c",
                        _READ_FILE_HELPER,
                        str(relative),
                        str(limit),
                    ),
                    timeout_seconds=min(
                        30.0, self._spec.policy.limits.command_timeout_seconds
                    ),
                ),
                mounts=(
                    f"type=volume,src={self._workspace_volume},"
                    "dst=/workspace,readonly,volume-nocopy",
                ),
                workdir="/",
                audit_limit=limit + 1,
                preview_limit=1,
            )
            result = capture.result
            if result.output_limited:
                raise SandboxPolicyError("file exceeds requested byte limit")
            if result.exit_code != 0:
                raise SandboxError(result.stderr.strip() or "sandbox file read failed")
            return capture.stdout_record.path.read_bytes()

    def write_file(
        self,
        path: str | PurePosixPath,
        data: bytes,
        *,
        create_parents: bool = False,
    ) -> None:
        with self._lock:
            self._ensure_open()
            relative = normalize_workspace_path(path)
            if not isinstance(data, bytes):
                raise TypeError("data must be bytes")
            if len(data) > self._spec.policy.limits.stdin_bytes:
                raise SandboxPolicyError("file content exceeds session stdin policy")
            result = self._provider._run_session_container(
                self,
                ExecRequest(
                    argv=(
                        "python3",
                        "-I",
                        "-S",
                        "-c",
                        _WRITE_FILE_HELPER,
                        str(relative),
                        "1" if create_parents else "0",
                    ),
                    stdin=data,
                    timeout_seconds=min(
                        30.0, self._spec.policy.limits.command_timeout_seconds
                    ),
                ),
                workdir="/",
            ).result
            if not result.succeeded:
                raise SandboxError(result.stderr.strip() or "sandbox file write failed")

    def get_diff(self, *, max_bytes: int | None = None) -> DiffResult:
        with self._lock:
            self._ensure_open()
            limit = _bounded_byte_limit(
                max_bytes,
                default=self._spec.policy.limits.audit_log_bytes,
                maximum=self._spec.policy.limits.audit_log_bytes,
                label="diff",
            )
            mounts = [
                f"type=volume,src={self._baseline_volume},dst=/baseline,readonly,volume-nocopy",
                f"type=volume,src={self._workspace_volume},dst=/workspace,readonly,volume-nocopy",
            ]
            capture = self._provider._run_session_container(
                self,
                ExecRequest(
                    argv=(
                        "git",
                        "diff",
                        "--no-index",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--no-color",
                        "--binary",
                        "--no-renames",
                        "--src-prefix=a/",
                        "--dst-prefix=b/",
                        "--",
                        "/baseline",
                        "/workspace",
                    ),
                    timeout_seconds=min(
                        120.0, self._spec.policy.limits.command_timeout_seconds
                    ),
                ),
                mounts=mounts,
                workdir="/",
                audit_limit=limit,
                preview_limit=limit,
            )
            result = capture.result
            if result.output_limited:
                raise SandboxPolicyError(
                    "diff exceeds requested byte limit; partial patches are not returned"
                )
            if result.exit_code not in {0, 1}:
                raise SandboxError(result.stderr.strip() or "sandbox diff failed")
            try:
                raw_patch = capture.stdout_record.path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise SandboxError(
                    "sandbox diff is not valid UTF-8; export the changed files as "
                    "artifacts instead"
                ) from exc
            patch = _normalize_no_index_patch(raw_patch)
            encoded = patch.encode("utf-8")
            return DiffResult(
                patch=patch,
                sha256=hashlib.sha256(encoded).hexdigest(),
                bytes=len(encoded),
                truncated=False,
            )

    def collect_artifacts(
        self,
        paths: Sequence[str | PurePosixPath],
        destination: Path,
        *,
        max_bytes: int | None = None,
    ) -> ArtifactBundle:
        with self._lock:
            self._ensure_open()
            if not paths:
                raise ValueError("at least one artifact path is required")
            normalized = [str(normalize_workspace_path(path)) for path in paths]
            limit = _bounded_byte_limit(
                max_bytes,
                default=self._spec.policy.limits.artifact_bytes,
                maximum=self._spec.policy.limits.artifact_bytes,
                label="artifact",
            )
            listing = self._provider._run_session_container(
                self,
                ExecRequest(
                    argv=(
                        "python3",
                        "-I",
                        "-S",
                        "-c",
                        _LIST_ARTIFACTS_HELPER,
                        str(_MAX_ARTIFACT_FILES),
                        *normalized,
                    ),
                    timeout_seconds=min(
                        30.0, self._spec.policy.limits.command_timeout_seconds
                    ),
                ),
                mounts=(
                    f"type=volume,src={self._workspace_volume},"
                    "dst=/workspace,readonly,volume-nocopy",
                ),
                workdir="/",
                audit_limit=1024**2,
                preview_limit=1024**2,
            ).result
            if not listing.succeeded:
                raise SandboxError(listing.stderr.strip() or "artifact listing failed")
            try:
                rows = json.loads(listing.stdout)
            except json.JSONDecodeError as exc:
                raise SandboxError("artifact helper returned invalid JSON") from exc
            total = sum(int(row["size"]) for row in rows)
            if total > limit:
                raise SandboxPolicyError("artifact files exceed requested byte limit")

            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            members: list[ArtifactMember] = []
            try:
                with destination.open("xb") as raw_output:
                    os.fchmod(raw_output.fileno(), 0o600)
                    with zipfile.ZipFile(
                        raw_output, mode="w", compression=zipfile.ZIP_DEFLATED
                    ) as archive:
                        for row in rows:
                            relative = str(normalize_workspace_path(row["path"]))
                            expected_size = int(row["size"])
                            data = self.read_file(
                                relative,
                                max_bytes=max(1, expected_size),
                            )
                            if len(data) != expected_size:
                                raise SandboxError(
                                    "artifact changed while it was being collected"
                                )
                            digest = hashlib.sha256(data).hexdigest()
                            archive.writestr(relative, data)
                            if raw_output.tell() > limit:
                                raise SandboxPolicyError(
                                    "artifact bundle exceeds requested byte limit"
                                )
                            members.append(
                                ArtifactMember(
                                    path=relative,
                                    size=len(data),
                                    sha256=digest,
                                )
                            )
                    if raw_output.tell() > limit:
                        raise SandboxPolicyError(
                            "artifact bundle exceeds requested byte limit"
                        )
            except Exception:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                raise

            bundle_size = destination.stat().st_size
            if bundle_size > limit:
                destination.unlink()
                raise SandboxPolicyError("artifact bundle exceeds requested byte limit")
            bundle = ArtifactBundle(
                path=destination,
                size=bundle_size,
                sha256=_sha256_file(destination),
                members=tuple(members),
            )
            self._record_audit(
                "artifacts_collected",
                bundle_sha256=bundle.sha256,
                bundle_bytes=bundle.size,
                members=[asdict(member) for member in bundle.members],
            )
            return bundle

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # Poison the session before cleanup starts. Even if Docker or audit
            # I/O fails, callers can never reuse a deleted/stale volume name.
            self._closed = True
            workspace_removed = self._provider._remove_volume(self._workspace_volume)
            baseline_removed = self._provider._remove_volume(self._baseline_volume)
            cleanup_ok = workspace_removed and baseline_removed
            audit_error: OSError | None = None
            try:
                self._record_audit("sandbox_closed", cleanup_ok=cleanup_ok)
            except OSError as exc:
                audit_error = exc
            finally:
                if (
                    cleanup_ok
                    and audit_error is None
                    and not self._provider._retain_audit_logs
                ):
                    self._provider._remove_audit_dir(self._audit_dir)
            if not cleanup_ok:
                raise SandboxError(
                    "sandbox closed, but one or more labeled volumes require reaping"
                )
            if audit_error is not None:
                raise SandboxError(
                    "sandbox closed, but its final audit event could not be written"
                ) from audit_error

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxClosedError(f"sandbox is closed: {self.sandbox_id}")
        if self._poisoned:
            raise SandboxUnavailableError(
                f"sandbox worker requires quarantine/reaping: {self.sandbox_id}"
            )

    def _poison(self, event: str, **data: object) -> None:
        self._poisoned = True
        try:
            self._record_audit(event, **data)
        except OSError:
            pass

    def _record_audit(self, event: str, **data: object) -> None:
        payload = {
            "event": event,
            "sandbox_id": self.sandbox_id,
            "task_id": self._metadata.task_id,
            "timestamp_ns": time.time_ns(),
            **data,
        }
        with (self._audit_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")

    def __enter__(self) -> "DockerSandboxSession":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _container_workspace_path(path: PurePosixPath) -> str:
    if path == PurePosixPath("."):
        return "/workspace"
    return f"/workspace/{path.as_posix()}"


def _bounded_byte_limit(
    value: int | None, *, default: int, maximum: int, label: str
) -> int:
    limit = default if value is None else value
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 < limit <= maximum
    ):
        raise SandboxPolicyError(f"{label} byte limit exceeds session policy")
    return limit


def _drain_stream(
    stream: Any,
    destination: Path,
    state: _StreamState,
    holder: list[_StreamRecord],
) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        with destination.open("wb") as output:
            if stream is not None:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    allowed = state.reserve(len(chunk))
                    if allowed:
                        output.write(chunk[:allowed])
        holder.append(
            _StreamRecord(path=destination, sha256=digest.hexdigest(), bytes=total)
        )
    except Exception as exc:  # noqa: BLE001 - cross-thread failure propagation
        state.record_failure(exc)


def _write_stdin(stream: Any, data: bytes) -> None:
    if stream is None:
        return
    try:
        if data:
            stream.write(data)
            stream.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _empty_stream(path: Path) -> _StreamRecord:
    if not path.exists():
        path.touch(mode=0o600)
    return _StreamRecord(path=path, sha256=hashlib.sha256(b"").hexdigest(), bytes=0)


def _read_preview(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _policy_summary(spec: SandboxSpec) -> Dict[str, object]:
    seccomp: Dict[str, str]
    if spec.policy.seccomp_profile is None:
        seccomp = {"kind": "builtin"}
    else:
        seccomp = {
            "kind": "custom",
            "sha256": _sha256_file(Path(spec.policy.seccomp_profile)),
        }
    return {
        "network": spec.policy.network.value,
        "allow_unpinned_image": spec.policy.allow_unpinned_image,
        "read_only_rootfs": spec.policy.read_only_rootfs,
        "require_rootless_runtime": spec.policy.require_rootless_runtime,
        "require_source_revision": spec.policy.require_source_revision,
        "runtime": spec.policy.runtime,
        "seccomp": seccomp,
        "limits": asdict(spec.policy.limits),
    }


def _write_emergency_audit(path: Path, **payload: object) -> None:
    """Best-effort lifecycle record retained when normal cleanup is unsafe."""

    payload.setdefault("timestamp_ns", time.time_ns())
    try:
        with (path / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    except OSError:
        pass


def _normalize_no_index_patch(patch: str) -> str:
    """Remove only the outer helper directory from Git-controlled headers."""

    lines: list[str] = []
    in_file = False
    saw_old_header = False
    headers_done = False
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            in_file = True
            saw_old_header = False
            headers_done = False
            line = _normalize_diff_git_header(line)
        elif in_file and not headers_done and line.startswith("--- "):
            saw_old_header = True
            line = _normalize_single_path_header(line, "--- ")
        elif (
            in_file and not headers_done and saw_old_header and line.startswith("+++ ")
        ):
            headers_done = True
            line = _normalize_single_path_header(line, "+++ ")
        elif in_file and not headers_done and line.startswith("Binary files "):
            headers_done = True
            line = _normalize_binary_files_header(line)
        elif in_file and line.startswith(("@@ ", "GIT binary patch")):
            headers_done = True

        lines.append(line)
    return "".join(lines)


def _normalize_diff_git_header(line: str) -> str:
    prefix = "diff --git "
    first, remainder = _take_git_path_token(line[len(prefix) :])
    whitespace = remainder[: len(remainder) - len(remainder.lstrip(" \t"))]
    second, tail = _take_git_path_token(remainder[len(whitespace) :])
    if not first or not whitespace or not second:
        return line
    return (
        prefix
        + _strip_diff_mount_token(first)
        + whitespace
        + _strip_diff_mount_token(second)
        + tail
    )


def _normalize_single_path_header(line: str, prefix: str) -> str:
    token, tail = _take_git_path_token(line[len(prefix) :])
    if not token:
        return line
    return prefix + _strip_diff_mount_token(token) + tail


def _normalize_binary_files_header(line: str) -> str:
    prefix = "Binary files "
    first, remainder = _take_git_path_token(line[len(prefix) :])
    if not first or not remainder.startswith(" and "):
        return line
    second, tail = _take_git_path_token(remainder[len(" and ") :])
    if not second:
        return line
    return (
        prefix
        + _strip_diff_mount_token(first)
        + " and "
        + _strip_diff_mount_token(second)
        + tail
    )


def _take_git_path_token(value: str) -> tuple[str, str]:
    if not value:
        return "", value
    if value[0] != '"':
        match = re.match(r"[^\t\r\n ]+", value)
        if match is None:
            return "", value
        return match.group(0), value[match.end() :]
    escaped = False
    for index, character in enumerate(value[1:], start=1):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            return value[: index + 1], value[index + 1 :]
    return "", value


def _strip_diff_mount_token(token: str) -> str:
    quote = '"' if token.startswith('"') else ""
    offset = len(quote)
    for side in ("a", "b"):
        for mount in ("baseline", "workspace"):
            prefix = f"{side}/{mount}/"
            if token.startswith(prefix, offset):
                return token[:offset] + f"{side}/" + token[offset + len(prefix) :]
    return token


def _safe_git_environment() -> Dict[str, str]:
    """Minimal controller environment for read-only Git provenance checks."""

    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_ALLOW_PROTOCOL": "",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }


__all__ = ["DockerSandboxProvider", "DockerSandboxSession"]
