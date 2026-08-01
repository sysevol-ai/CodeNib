# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Provider-neutral isolation for repository execution.

Sandboxing is opt-in. Importing this package never contacts a container
runtime; construct :class:`DockerSandboxProvider` explicitly on a dedicated
worker when untrusted code execution is required.
"""

from .docker import DockerSandboxProvider, DockerSandboxSession
from .protocol import (
    SandboxClosedError,
    SandboxError,
    SandboxPolicyError,
    SandboxProvider,
    SandboxSession,
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
    SandboxLimits,
    SandboxMetadata,
    SandboxPolicy,
    SandboxSpec,
)

__all__ = [
    "ArtifactBundle",
    "ArtifactMember",
    "DiffResult",
    "DockerSandboxProvider",
    "DockerSandboxSession",
    "ExecRequest",
    "ExecResult",
    "NetworkMode",
    "SandboxCapabilities",
    "SandboxClosedError",
    "SandboxError",
    "SandboxLimits",
    "SandboxMetadata",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxProvider",
    "SandboxSession",
    "SandboxSpec",
    "SandboxUnavailableError",
]
