# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The container a hosted visitor's Claude Code runs in."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import urllib.error
from pathlib import Path

import pytest

from codenib.annotations import sandbox
from codenib.annotations.model import AnnotationError
from codenib.annotations.prompt import RESULT_SCHEMA
from codenib.annotations.sandbox import KeyRefused, SandboxedClaude, provider_of

OPENROUTER = "sk-or-v1-" + "1" * 40
ANTHROPIC = "sk-ant-api03-" + "2" * 40


def test_the_key_names_its_provider() -> None:
    assert provider_of(OPENROUTER) == "openrouter"
    assert provider_of(ANTHROPIC) == "anthropic"
    for bad in ("", "short", "sk-proj-" + "3" * 40, "sk-or-v1 has spaces in it ok"):
        with pytest.raises(KeyRefused):
            provider_of(bad)


def _fake_urlopen(status: int, body: dict):
    def urlopen(request, timeout):
        if status != 200:
            raise urllib.error.HTTPError(
                request.full_url, status, "no", {}, io.BytesIO(b"{}")
            )
        return io.BytesIO(json.dumps(body).encode())

    return urlopen


@pytest.mark.parametrize(
    "status, body, refused",
    [
        (200, {"data": {"is_management_key": False, "limit_remaining": 3}}, False),
        (200, {"data": {"is_management_key": False, "limit_remaining": None}}, False),
        (200, {"data": {"is_management_key": True}}, True),
        (200, {"data": {"is_management_key": False, "limit_remaining": 0.01}}, True),
        (401, {}, True),
    ],
)
def test_an_openrouter_key_is_checked_before_a_run(
    monkeypatch, status, body, refused
) -> None:
    monkeypatch.setattr(sandbox.urllib.request, "urlopen", _fake_urlopen(status, body))
    if refused:
        with pytest.raises(KeyRefused):
            sandbox.check_key(OPENROUTER)
    else:
        assert sandbox.check_key(OPENROUTER) == "openrouter"


def test_a_provider_outage_is_not_a_refused_key(monkeypatch) -> None:
    monkeypatch.setattr(sandbox.urllib.request, "urlopen", _fake_urlopen(503, {}))
    with pytest.raises(AnnotationError) as caught:
        sandbox.check_key(ANTHROPIC)
    assert not isinstance(caught.value, KeyRefused)


def test_the_container_is_locked_down_and_the_key_stays_out_of_argv(
    tmp_path: Path,
) -> None:
    exe = tmp_path / "claude"
    exe.write_text("")
    agent = SandboxedClaude(OPENROUTER, executable=exe)
    question = agent.argv(
        cwd=tmp_path, system="s", schema=RESULT_SCHEMA, edit=False, name="n"
    )
    joined = " ".join(question)
    assert OPENROUTER not in joined
    for flag in (
        "--read-only",
        "--cap-drop ALL",
        "no-new-privileges",
        "--pids-limit",
        "--bare",
    ):
        assert flag in joined
    assert f"{tmp_path.resolve()}:/work:ro" in question
    assert "--tools" in question and question[question.index("--tools") + 1] == (
        "Read,Grep,Glob"
    )
    assert any("apiKeyHelper" in part for part in question)
    change = agent.argv(
        cwd=tmp_path, system="s", schema=RESULT_SCHEMA, edit=True, name="n"
    )
    assert f"{tmp_path.resolve()}:/work:rw" in change
    assert "acceptEdits" in change
    direct = SandboxedClaude(ANTHROPIC, executable=exe).argv(
        cwd=tmp_path, system="s", schema=RESULT_SCHEMA, edit=False, name="n"
    )
    assert "ANTHROPIC_API_KEY" in direct and ANTHROPIC not in " ".join(direct)
    assert not any("apiKeyHelper" in part for part in direct)


FAKE_CLAUDE = """#!/bin/sh
# Stands in for Claude Code inside the container: answers, edits when asked.
cat > /dev/null
if [ -n "$ANTHROPIC_BASE_URL" ] && [ -n "$CODENIB_PROVIDER_KEY" ]; then ok=key; else ok=nokey; fi  # noqa: B950
if touch /work/probe 2>/dev/null; then rm -f /work/probe; w=writable; else w=readonly; fi
if [ -e "HOST_SECRET" ]; then s=seen; else s=unseen; fi
case "$*" in *acceptEdits*) printf 'x = 2\\n' > /work/calc.py ;; esac
printf '{"type":"result","subtype":"success","structured_output":{"results":[{"annotation":"A","answer":"%s %s %s","files":[{"path":"/work/calc.py"}]}]},"total_cost_usd":0}' "$ok" "$w" "$s"  # noqa: B950
"""


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", sandbox.DEFAULT_IMAGE],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.integration
@pytest.mark.skipif(not _docker_ready(), reason="needs docker and the base image")
def test_a_run_in_the_real_container(tmp_path: Path) -> None:
    # A file on the host, outside the mounted folder: the run must not see it.
    secret = tmp_path / "secret.txt"
    secret.write_text("host only\n")
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE.replace("HOST_SECRET", str(secret)))
    exe.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    (work / "calc.py").write_text("x = 1\n")
    agent = SandboxedClaude(OPENROUTER, executable=exe)

    asked = agent.run(
        "prompt",
        cwd=work,
        system="s",
        schema=RESULT_SCHEMA,
        edit=False,
        log_dir=tmp_path / "log",
    )
    assert asked.error == ""
    result = asked.structured["results"][0]
    assert result["answer"] == "key readonly unseen"
    assert result["files"] == [{"path": "calc.py"}]
    assert (work / "calc.py").read_text() == "x = 1\n"

    changed = agent.run(
        "prompt",
        cwd=work,
        system="s",
        schema=RESULT_SCHEMA,
        edit=True,
        log_dir=tmp_path / "log",
    )
    assert changed.structured["results"][0]["answer"] == "key writable unseen"
    assert (work / "calc.py").read_text() == "x = 2\n"
    logs = "".join(p.read_text() for p in (tmp_path / "log").iterdir())
    assert OPENROUTER not in logs
