# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.sandbox import (
    ExecRequest,
    ExecResult,
    NetworkMode,
    SandboxLimits,
    SandboxPolicy,
    SandboxProvider,
    SandboxSpec,
)
from codenib.sandbox.types import normalize_workspace_path

PINNED_IMAGE = "registry.example/codenib-agent@sha256:" + "a" * 64


def test_sandbox_limits_are_safe_and_positive_by_default():
    limits = SandboxLimits()

    assert limits.cpus > 0
    assert limits.memory_bytes > 0
    assert limits.pids > 0
    assert limits.output_bytes <= limits.audit_log_bytes


@pytest.mark.parametrize(
    "overrides",
    [
        {"cpus": 0},
        {"cpus": float("nan")},
        {"cpus": float("inf")},
        {"memory_bytes": 0},
        {"memory_bytes": 1.5},
        {"memory_bytes": True},
        {"pids": -1},
        {"command_timeout_seconds": 0},
        {"command_timeout_seconds": float("nan")},
        {"command_timeout_seconds": float("inf")},
        {"output_bytes": 2, "audit_log_bytes": 1},
    ],
)
def test_sandbox_limits_reject_invalid_values(overrides):
    with pytest.raises(ValueError):
        SandboxLimits(**overrides)


def test_policy_defaults_fail_closed():
    policy = SandboxPolicy()

    assert policy.network is NetworkMode.NONE
    assert policy.read_only_rootfs is True
    assert policy.require_rootless_runtime is True
    assert policy.require_source_revision is True
    assert policy.allow_unpinned_image is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("read_only_rootfs", 0),
        ("require_rootless_runtime", "false"),
        ("require_source_revision", 1),
        ("allow_unpinned_image", "true"),
    ],
)
def test_policy_rejects_non_boolean_security_flags(field, value):
    with pytest.raises(TypeError, match="must be booleans"):
        SandboxPolicy(**{field: value})


def test_spec_requires_digest_pin_by_default(tmp_path):
    with pytest.raises(ValueError, match="pinned"):
        SandboxSpec(
            source_dir=tmp_path,
            image="python:3.12-slim",
            platform="linux/amd64",
        )


def test_spec_requires_exact_source_revision_by_default(tmp_path):
    with pytest.raises(ValueError, match="source_revision"):
        SandboxSpec(
            source_dir=tmp_path,
            image=PINNED_IMAGE,
            platform="linux/amd64",
        )


def test_spec_accepts_exact_revision_and_digest(tmp_path):
    spec = SandboxSpec(
        source_dir=tmp_path,
        image=PINNED_IMAGE,
        platform="linux/amd64",
        source_revision="b" * 40,
        task_id="issue-123",
    )

    assert spec.source_dir == tmp_path
    assert spec.source_revision == "b" * 40


def test_trusted_fixture_can_explicitly_disable_revision_requirement(tmp_path):
    spec = SandboxSpec(
        source_dir=tmp_path,
        image=PINNED_IMAGE,
        platform="linux/amd64",
        policy=SandboxPolicy(require_source_revision=False),
    )

    assert spec.source_revision is None


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", "a/../../b", "C:\\x"])
def test_workspace_paths_reject_escape(path):
    with pytest.raises(ValueError):
        normalize_workspace_path(path)


def test_exec_request_is_argv_based_and_rejects_sensitive_environment():
    request = ExecRequest(argv=("/bin/sh", "-lc", "echo hi"), cwd="src")
    assert request.argv == ("/bin/sh", "-lc", "echo hi")
    assert str(request.cwd) == "src"

    with pytest.raises(ValueError, match="sensitive"):
        ExecRequest(argv=("env",), environment={"GITHUB_TOKEN": "secret"})
    with pytest.raises(ValueError, match="sensitive"):
        ExecRequest(argv=("env",), environment={"SSH_AUTH_SOCK": "/tmp/agent"})

    immutable = ExecRequest(argv=("env",), environment={"SAFE": "value"})
    with pytest.raises(TypeError):
        immutable.environment["GITHUB_TOKEN"] = "bypass"  # type: ignore[index]


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), True, 0])
def test_exec_request_rejects_nonfinite_or_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="finite positive"):
        ExecRequest(argv=("true",), timeout_seconds=timeout)


def test_exec_result_separates_exit_timeout_and_output_limit():
    ok = ExecResult(
        command_id="cmd",
        argv=("true",),
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
    )
    timed_out = ExecResult(
        command_id="cmd",
        argv=("sleep", "10"),
        exit_code=None,
        stdout="",
        stderr="",
        duration_ms=10,
        timed_out=True,
    )
    limited = ExecResult(
        command_id="cmd",
        argv=("yes",),
        exit_code=None,
        stdout="x",
        stderr="",
        duration_ms=2,
        output_limited=True,
    )

    assert ok.succeeded is True
    assert timed_out.succeeded is False
    assert limited.succeeded is False
    assert limited.to_dict()["output_limited"] is True


def test_provider_protocol_accepts_structural_fake():
    class FakeProvider:
        capabilities = object()

        def create(self, spec):
            return spec

    assert isinstance(FakeProvider(), SandboxProvider)
