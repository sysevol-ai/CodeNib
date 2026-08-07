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


def test_start_runs_the_validated_command(monkeypatch):
    calls = []
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
