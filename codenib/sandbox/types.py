# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stable value types for isolated repository execution."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Dict, Mapping, Sequence, Tuple

from ..repository_source_selection import RepositorySourceSelection

_IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
_IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
_PLATFORM_RE = re.compile(r"^[a-z0-9]+/[a-z0-9_]+(?:/[a-z0-9_]+)?$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|API_KEY|AUTH|COOKIE)",
    re.IGNORECASE,
)
_BLOCKED_ENV_NAMES = frozenset(
    {
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "GIT_ASKPASS",
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "SSH_AUTH_SOCK",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class NetworkMode(str, Enum):
    """Container egress policy.

    ``BRIDGE`` is intentionally explicit. Docker cannot implement a reliable
    hostname allowlist by itself, so public issue-bot jobs should remain on
    ``NONE`` and use a separate, policy-enforcing bootstrap service when they
    need dependencies.
    """

    NONE = "none"
    BRIDGE = "bridge"


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Hard resource and output bounds applied to every command."""

    cpus: float = 2.0
    memory_bytes: int = 2 * 1024**3
    pids: int = 256
    command_timeout_seconds: float = 300.0
    output_bytes: int = 64 * 1024
    audit_log_bytes: int = 8 * 1024**2
    stdin_bytes: int = 1024**2
    tmpfs_bytes: int = 256 * 1024**2
    artifact_bytes: int = 64 * 1024**2

    def __post_init__(self) -> None:
        finite_numbers = {
            "cpus": self.cpus,
            "command_timeout_seconds": self.command_timeout_seconds,
        }
        invalid_numbers = [
            name
            for name, value in finite_numbers.items()
            if isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ]
        integer_limits = {
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "output_bytes": self.output_bytes,
            "audit_log_bytes": self.audit_log_bytes,
            "stdin_bytes": self.stdin_bytes,
            "tmpfs_bytes": self.tmpfs_bytes,
            "artifact_bytes": self.artifact_bytes,
        }
        invalid_integers = [
            name
            for name, value in integer_limits.items()
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ]
        invalid = sorted(invalid_numbers + invalid_integers)
        if invalid:
            raise ValueError(
                "sandbox limits must be finite positive numbers with byte, PID, "
                f"and count limits expressed as integers: {invalid}"
            )
        if self.output_bytes > self.audit_log_bytes:
            raise ValueError("output_bytes cannot exceed audit_log_bytes")


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Security policy for one sandbox session.

    The safe defaults are fail-closed: no network, a read-only container root,
    no Linux capabilities, no privilege escalation, and a rootless daemon
    requirement. ``require_rootless_runtime=False`` is intended only for
    explicitly trusted repositories or a separately isolated worker VM.
    """

    network: NetworkMode = NetworkMode.NONE
    read_only_rootfs: bool = True
    require_rootless_runtime: bool = True
    require_source_revision: bool = True
    allow_unpinned_image: bool = False
    seccomp_profile: str | None = None
    runtime: str | None = None
    limits: SandboxLimits = field(default_factory=SandboxLimits)

    def __post_init__(self) -> None:
        boolean_fields = {
            "read_only_rootfs": self.read_only_rootfs,
            "require_rootless_runtime": self.require_rootless_runtime,
            "require_source_revision": self.require_source_revision,
            "allow_unpinned_image": self.allow_unpinned_image,
        }
        invalid_booleans = [
            name for name, value in boolean_fields.items() if type(value) is not bool
        ]
        if invalid_booleans:
            raise TypeError(
                f"sandbox policy flags must be booleans: {sorted(invalid_booleans)}"
            )
        try:
            object.__setattr__(self, "network", NetworkMode(self.network))
        except ValueError as exc:
            raise ValueError(
                f"unsupported sandbox network mode: {self.network}"
            ) from exc
        if self.seccomp_profile is not None:
            profile = Path(self.seccomp_profile)
            if not profile.is_absolute():
                raise ValueError("seccomp_profile must be an absolute path")
            if not profile.is_file():
                raise ValueError(f"seccomp_profile does not exist: {profile}")
        if self.runtime is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.runtime
        ):
            raise ValueError("runtime contains unsupported characters")


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """Immutable request for a repository sandbox.

    ``source_revision`` is optional at the library boundary for generated or
    non-Git fixtures. GitHub automation should always provide the exact
    40-character commit and let the provider verify it before copying files.
    """

    source_dir: Path
    image: str
    platform: str
    source_revision: str | None = None
    task_id: str | None = None
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    source_selection: RepositorySourceSelection = field(
        default_factory=RepositorySourceSelection
    )

    def __post_init__(self) -> None:
        source_dir = Path(self.source_dir).expanduser()
        object.__setattr__(self, "source_dir", source_dir)
        if type(self.source_selection) is not RepositorySourceSelection:
            raise TypeError("source_selection must be a RepositorySourceSelection")
        object.__setattr__(
            self,
            "source_selection",
            RepositorySourceSelection(self.source_selection.exclude_subtrees),
        )
        if not self.image or not _IMAGE_REF_RE.fullmatch(self.image):
            raise ValueError("image must be a non-empty Docker image reference")
        if not self.policy.allow_unpinned_image and not _IMAGE_DIGEST_RE.search(
            self.image
        ):
            raise ValueError(
                "image must be pinned by sha256 digest; set "
                "allow_unpinned_image only for trusted development"
            )
        if not _PLATFORM_RE.fullmatch(self.platform):
            raise ValueError("platform must look like 'linux/amd64'")
        if self.source_revision is not None and not _REVISION_RE.fullmatch(
            self.source_revision
        ):
            raise ValueError("source_revision must be a full lowercase Git SHA")
        if self.policy.require_source_revision and self.source_revision is None:
            raise ValueError(
                "source_revision is required by the sandbox policy; disable the "
                "requirement only for trusted generated fixtures"
            )
        if self.task_id is not None and not _TASK_ID_RE.fullmatch(self.task_id):
            raise ValueError("task_id contains unsupported characters")


def normalize_workspace_path(path: str | PurePosixPath) -> PurePosixPath:
    """Return a safe, repository-relative POSIX path.

    The value is validated lexically here and resolved against the copied
    workspace by the provider before any host-side file access.
    """

    raw = str(path)
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("workspace path must be a non-empty POSIX path")
    value = PurePosixPath(raw)
    if value.is_absolute() or any(part in {"", ".."} for part in value.parts):
        raise ValueError("workspace path must stay below the repository root")
    if value == PurePosixPath("."):
        return value
    return PurePosixPath(*[part for part in value.parts if part != "."])


def validate_environment(environment: Mapping[str, str]) -> Dict[str, str]:
    """Validate non-sensitive environment values for a container command."""

    clean: Dict[str, str] = {}
    for name, value in environment.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if name in _BLOCKED_ENV_NAMES or _SECRET_ENV_RE.search(name):
            raise ValueError(f"sensitive environment variable is not allowed: {name}")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"invalid value for environment variable: {name}")
        clean[name] = value
    return clean


@dataclass(frozen=True, slots=True)
class ExecRequest:
    """One argv-based command request.

    No host shell is involved. Callers that deliberately need shell syntax can
    request ``('/bin/sh', '-lc', command)``; those three argv elements are
    still passed after the pinned container image.
    """

    argv: Tuple[str, ...] | Sequence[str]
    cwd: PurePosixPath | str = PurePosixPath(".")
    stdin: bytes | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)):
            raise TypeError("argv must be a sequence of argument strings")
        argv = tuple(self.argv)
        if not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise ValueError("argv must contain non-empty, NUL-free strings")
        if not argv[0]:
            raise ValueError("argv[0] must be non-empty")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "cwd", normalize_workspace_path(self.cwd))
        object.__setattr__(
            self,
            "environment",
            MappingProxyType(validate_environment(self.environment)),
        )
        if self.stdin is not None and not isinstance(self.stdin, bytes):
            raise TypeError("stdin must be bytes when provided")
        if self.timeout_seconds is not None:
            timeout = self.timeout_seconds
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or timeout <= 0
            ):
                raise ValueError(
                    "timeout_seconds must be a finite positive number when provided"
                )


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Bounded model-facing output plus audit hashes for one command."""

    command_id: str
    argv: Tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    output_limited: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_sha256: str = ""
    stderr_sha256: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and not self.output_limited and self.exit_code == 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "command_id": self.command_id,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "output_limited": self.output_limited,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
        }


@dataclass(frozen=True, slots=True)
class DiffResult:
    """Canonical Git patch produced from the immutable source snapshot."""

    patch: str
    sha256: str
    bytes: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactMember:
    """One regular file included in an exported artifact bundle."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactBundle:
    """Controller-owned ZIP export of selected workspace files."""

    path: Path
    size: int
    sha256: str
    members: Tuple[ArtifactMember, ...]


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    """Auditable guarantees a provider can truthfully claim."""

    provider: str
    isolation: str
    non_root_user: bool
    network_isolation: bool
    read_only_rootfs: bool
    resource_limits: bool
    process_tree_cleanup: bool
    disk_quota: bool
    rootless_runtime: bool | None = None


@dataclass(frozen=True, slots=True)
class SandboxMetadata:
    """Non-secret identity recorded in agent traces and job artifacts."""

    sandbox_id: str
    task_id: str
    provider: str
    image: str
    image_id: str
    platform: str
    source_revision: str | None
    network: str
    rootless_runtime: bool | None
    source_fingerprint: str = ""
    policy: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "sandbox_id": self.sandbox_id,
            "task_id": self.task_id,
            "provider": self.provider,
            "image": self.image,
            "image_id": self.image_id,
            "platform": self.platform,
            "source_revision": self.source_revision,
            "network": self.network,
            "rootless_runtime": self.rootless_runtime,
            "source_fingerprint": self.source_fingerprint,
            "policy": dict(self.policy),
        }


__all__ = [
    "ArtifactBundle",
    "ArtifactMember",
    "DiffResult",
    "ExecRequest",
    "ExecResult",
    "NetworkMode",
    "SandboxCapabilities",
    "SandboxLimits",
    "SandboxMetadata",
    "SandboxPolicy",
    "SandboxSpec",
    "normalize_workspace_path",
    "validate_environment",
]
