# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
import sys

import pytest

from scripts import start_vllm_server as module


def test_command_uses_active_interpreter_and_does_not_trust_by_default():
    command = module._build_vllm_command("vendor/standard-model")

    assert command[:3] == [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
    ]
    assert "--trust-remote-code" not in command
    assert "--revision" not in command
    assert "--code-revision" not in command


@pytest.mark.parametrize(
    ("revision", "code_revision", "missing_option"),
    [
        (None, "b" * 40, "--revision"),
        ("main", "b" * 40, "--revision"),
        ("a" * 40, None, "--code-revision"),
        ("a" * 40, "main", "--code-revision"),
    ],
)
def test_trusted_hub_code_requires_immutable_revisions(
    revision, code_revision, missing_option
):
    with pytest.raises(ValueError, match=missing_option):
        module._build_vllm_command(
            "vendor/custom-model",
            revision=revision,
            code_revision=code_revision,
            trust_remote_code=True,
        )


def test_trusted_hub_code_forwards_pinned_revisions():
    revision = "a" * 40
    code_revision = "b" * 40

    command = module._build_vllm_command(
        "vendor/custom-model",
        revision=revision,
        code_revision=code_revision,
        tokenizer_revision="tokenizer-v1",
        trust_remote_code=True,
    )

    assert command[command.index("--revision") + 1] == revision
    assert command[command.index("--code-revision") + 1] == code_revision
    assert command[command.index("--tokenizer-revision") + 1] == "tokenizer-v1"
    assert command[-1] == "--trust-remote-code"


def test_local_model_can_explicitly_trust_code_without_hub_revisions(tmp_path):
    model = tmp_path / "local model"
    model.mkdir()

    command = module._build_vllm_command(
        str(model),
        trust_remote_code=True,
    )

    assert command[command.index("--model") + 1] == str(model)
    assert command[-1] == "--trust-remote-code"


@pytest.mark.parametrize("version", ["0.10.2", "0.11.0", "0.11.1rc1"])
def test_unsafe_vllm_versions_are_rejected(monkeypatch, version):
    monkeypatch.setattr(module, "package_version", lambda _package: version)

    with pytest.raises(RuntimeError, match="vLLM>=0.11.1"):
        module._require_safe_vllm_version()


@pytest.mark.parametrize("version", ["0.11.1", "0.11.1.post1", "0.12.0.dev1"])
def test_patched_vllm_versions_are_accepted(monkeypatch, version):
    monkeypatch.setattr(module, "package_version", lambda _package: version)

    assert module._require_safe_vllm_version() == version


def test_missing_vllm_has_actionable_error(monkeypatch):
    def missing(_package):
        raise module.PackageNotFoundError

    monkeypatch.setattr(module, "package_version", missing)

    with pytest.raises(RuntimeError, match="vLLM>=0.11.1 is required"):
        module._require_safe_vllm_version()


def test_start_runs_the_validated_command(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "_require_safe_vllm_version", lambda: "0.11.1")
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    module.start_vllm_server(
        "vendor/standard-model",
        revision="release-v1",
    )

    [(args, kwargs)] = calls
    [command] = args
    assert command[command.index("--revision") + 1] == "release-v1"
    assert "--trust-remote-code" not in command
    assert kwargs == {"check": True}
