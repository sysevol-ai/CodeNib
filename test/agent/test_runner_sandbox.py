# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codenib.agent import CodeNibAgentOptions, query
from codenib.agent.runner import AgentRunner
from codenib.agent.skills.registry import SkillRegistry
from codenib.agent.tools.defaults import DEFAULT_TOOL_IDS
from codenib.agent.tools.sandbox import _BOUNDED_LINES_HELPER
from codenib.compiler.manifest import RepoManifest
from codenib.compiler.params import SessionContext
from codenib.llm.litellm_chat import LiteLLMChat
from codenib.sandbox import ExecResult, SandboxCapabilities, SandboxMetadata


class FakeSandbox:
    def __init__(self):
        self.requests = []
        self.read_paths = []
        self.sandbox_id = "sbx-test"
        self.capabilities = SandboxCapabilities(
            provider="fake",
            isolation="test",
            non_root_user=True,
            network_isolation=True,
            read_only_rootfs=True,
            resource_limits=True,
            process_tree_cleanup=True,
            disk_quota=False,
        )
        self.metadata = SandboxMetadata(
            sandbox_id=self.sandbox_id,
            task_id="issue-1",
            provider="fake",
            image="test@sha256:" + "a" * 64,
            image_id="sha256:" + "b" * 64,
            platform="linux/amd64",
            source_revision="c" * 40,
            network="none",
            rootless_runtime=True,
            source_fingerprint="sha256-v2:" + "e" * 64,
        )

    def execute(self, request):
        self.requests.append(request)
        if "rg" in request.argv and "--files" in request.argv:
            data = b"src/app.py\n"
            stdout = json.dumps(
                {
                    "data": base64.b64encode(data).decode("ascii"),
                    "returncode": 0,
                    "root_is_file": False,
                    "truncated": False,
                }
            )
        elif "rg" in request.argv:
            data = b"src/app.py:1:def app():\n"
            stdout = json.dumps(
                {
                    "data": base64.b64encode(data).decode("ascii"),
                    "returncode": 0,
                    "root_is_file": False,
                    "truncated": False,
                }
            )
        else:
            stdout = "sandbox-only\n"
        return ExecResult(
            command_id=f"cmd-{len(self.requests)}",
            argv=request.argv,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_ms=1,
        )

    def read_file(self, path, *, max_bytes=None):
        self.read_paths.append((str(path), max_bytes))
        return b"def app():\n    return 1\n"

    def write_file(self, *args, **kwargs):
        raise AssertionError("not used")

    def get_diff(self, *args, **kwargs):
        raise AssertionError("not used")

    def collect_artifacts(self, *args, **kwargs):
        raise AssertionError("not used")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_registry():
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


def _llm_with_tool_call(name, arguments):
    llm = MagicMock(spec=LiteLLMChat)
    call = SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )
    llm._call_raw.side_effect = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant", content=None, tool_calls=[call]
                    )
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant", content="done", tool_calls=None
                    )
                )
            ]
        ),
    ]
    return llm


def test_sandbox_mode_replaces_all_default_tool_executors():
    sandbox = FakeSandbox()
    runner = AgentRunner(
        MagicMock(spec=LiteLLMChat),
        SkillRegistry(),
        sandbox=sandbox,
    )

    assert set(runner.tool_registry.list_tools()) == set(DEFAULT_TOOL_IDS)
    assert "isolated sandbox" in runner.tool_registry.get("bash").tool_doc
    assert "isolated sandbox" in runner.tool_registry.get("read").tool_doc


def test_runner_bash_has_no_host_executor_bypass(monkeypatch):
    sandbox = FakeSandbox()
    monkeypatch.setattr(
        "codenib.agent.tools.defaults._bash",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("host bash must not run")
        ),
    )
    runner = AgentRunner(
        _llm_with_tool_call("bash", {"command": "echo hello"}),
        SkillRegistry(),
        sandbox=sandbox,
    )
    result = runner.run("run command")

    assert result.tool_calls[0].error is None
    assert "sandbox-only" in result.tool_calls[0].result
    assert sandbox.requests[0].argv == ("/bin/sh", "-lc", "echo hello")
    run_start = result.trace.events[0].to_dict()
    assert run_start["data"]["sandbox"]["sandbox_id"] == "sbx-test"


def test_sandbox_read_grep_and_glob_are_session_backed():
    sandbox = FakeSandbox()
    runner = AgentRunner(
        MagicMock(spec=LiteLLMChat),
        SkillRegistry(),
        sandbox=sandbox,
    )

    read = runner.tool_registry.get("read").executor_fn("src/app.py")
    grep = runner.tool_registry.get("grep").executor_fn("def app", path=".")
    glob = runner.tool_registry.get("glob").executor_fn("*.py", path=".")

    assert "def app" in read
    assert sandbox.read_paths[0][0] == "src/app.py"
    assert "src/app.py:1: def app" in grep
    assert glob == "src/app.py"
    assert [request.argv[0] for request in sandbox.requests] == [
        "python3",
        "python3",
    ]
    assert all("rg" in request.argv for request in sandbox.requests)


def test_sandbox_grep_uses_bounded_parity_flags_and_type_alias():
    sandbox = FakeSandbox()
    runner = AgentRunner(
        MagicMock(spec=LiteLLMChat),
        SkillRegistry(),
        sandbox=sandbox,
    )

    runner.tool_registry.get("grep").executor_fn(
        "needle", type="python", output_mode="count"
    )
    argv = sandbox.requests[0].argv

    assert argv[:4] == ("python3", "-I", "-S", "-c")
    assert "--hidden" in argv
    assert "--no-ignore" in argv
    assert "--text" in argv
    assert "--count" in argv
    assert "--count-matches" not in argv
    assert "codenib:*.py" in argv
    assert "codenib:*.pyi" in argv


def test_sandbox_grep_rejects_multiline_without_running_command():
    sandbox = FakeSandbox()
    runner = AgentRunner(
        MagicMock(spec=LiteLLMChat),
        SkillRegistry(),
        sandbox=sandbox,
    )

    result = runner.tool_registry.get("grep").executor_fn("start.*end", multiline=True)

    assert result == "Error: multiline grep is not supported in sandbox mode"
    assert sandbox.requests == []


def test_search_helper_stops_at_row_limit():
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _BOUNDED_LINES_HELPER,
            "3",
            "40960",
            str(Path.cwd()),
            ".",
            sys.executable,
            "-c",
            "for i in range(100): print(i)",
            ".",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert base64.b64decode(payload["data"]).splitlines() == [b"0", b"1", b"2"]
    assert payload["truncated"] is True


def test_sandbox_tools_negotiate_tighter_session_limits():
    sandbox = FakeSandbox()
    object.__setattr__(
        sandbox.metadata,
        "policy",
        MappingProxyType(
            {
                "limits": MappingProxyType(
                    {
                        "artifact_bytes": 128,
                        "command_timeout_seconds": 0.5,
                        "output_bytes": 1024,
                    }
                )
            }
        ),
    )
    runner = AgentRunner(
        MagicMock(spec=LiteLLMChat),
        SkillRegistry(),
        sandbox=sandbox,
    )

    runner.tool_registry.get("read").executor_fn("src/app.py")
    runner.tool_registry.get("grep").executor_fn("needle")
    runner.tool_registry.get("bash").executor_fn("true")

    assert sandbox.read_paths[0][1] == 128
    assert int(sandbox.requests[0].argv[6]) <= 576
    assert sandbox.requests[1].timeout_seconds == 0.5


def test_without_sandbox_keeps_existing_local_default_tools():
    runner = AgentRunner(MagicMock(spec=LiteLLMChat), SkillRegistry())
    assert "no path jail" in runner.tool_registry.get("bash").tool_doc


def test_sandbox_can_be_combined_with_default_tool_off_switch():
    runner = AgentRunner(
        MagicMock(spec=LiteLLMChat),
        SkillRegistry(),
        sandbox=FakeSandbox(),
        include_default_tools=False,
    )
    assert runner.tool_registry.list_tools() == {}


def test_sandbox_prompt_hides_host_path_and_uses_workspace_root(tmp_path):
    host_path = str(tmp_path / "secret-worker-layout" / "repo")
    runner = AgentRunner(
        MagicMock(spec=LiteLLMChat),
        SkillRegistry(),
        sandbox=FakeSandbox(),
        session_ctx=SessionContext(repo_path=host_path, primary_language="python"),
    )

    assert host_path not in runner.system_prompt
    assert "repo_path: ." in runner.system_prompt
    assert "workspace_root: /workspace" in runner.system_prompt
    assert "git_metadata: unavailable" in runner.system_prompt
    assert "indexes may become stale" in runner.system_prompt


def test_sandbox_manifest_revision_must_match():
    manifest = RepoManifest(
        commit="d" * 40,
        source_fingerprint="sha256-v2:" + "e" * 64,
    )
    with pytest.raises(ValueError, match="does not match"):
        AgentRunner(
            MagicMock(spec=LiteLLMChat),
            SkillRegistry(),
            sandbox=FakeSandbox(),
            manifest=manifest,
        )


def test_sandbox_manifest_requires_revision():
    with pytest.raises(ValueError, match="revision-bearing"):
        AgentRunner(
            MagicMock(spec=LiteLLMChat),
            SkillRegistry(),
            sandbox=FakeSandbox(),
            manifest=RepoManifest(),
        )


def test_sandbox_manifest_fingerprint_must_match():
    manifest = RepoManifest(
        commit="c" * 40,
        source_fingerprint="sha256-v2:" + "f" * 64,
    )
    with pytest.raises(ValueError, match="fingerprint does not match"):
        AgentRunner(
            MagicMock(spec=LiteLLMChat),
            SkillRegistry(),
            sandbox=FakeSandbox(),
            manifest=manifest,
        )


def test_sandbox_matching_legacy_fingerprint_cannot_authorize_runner():
    sandbox = FakeSandbox()
    legacy = "sha256:" + "e" * 64
    sandbox.metadata = replace(sandbox.metadata, source_fingerprint=legacy)
    manifest = RepoManifest(commit="c" * 40, source_fingerprint=legacy)

    with pytest.raises(ValueError, match="secure source fingerprint v2"):
        AgentRunner(
            MagicMock(spec=LiteLLMChat),
            SkillRegistry(),
            sandbox=sandbox,
            manifest=manifest,
        )


def test_query_matching_legacy_fingerprint_cannot_authorize_context_loading():
    sandbox = FakeSandbox()
    legacy = "sha256:" + "e" * 64
    sandbox.metadata = replace(sandbox.metadata, source_fingerprint=legacy)
    manifest = RepoManifest(commit="c" * 40, source_fingerprint=legacy)

    with pytest.raises(ValueError, match="secure source fingerprint v2"):
        query(
            "noop",
            options=CodeNibAgentOptions(
                manifest=manifest,
                llm=MagicMock(spec=LiteLLMChat),
                sandbox=sandbox,
            ),
        )


def test_sandbox_without_manifest_rejects_unbound_skills():
    registry = MagicMock(spec=SkillRegistry)
    registry.list_skills.return_value = {"bm25_search": object()}

    with pytest.raises(ValueError, match="source snapshot is unbound"):
        AgentRunner(
            MagicMock(spec=LiteLLMChat),
            registry,
            sandbox=FakeSandbox(),
        )


def test_query_rejects_host_inline_index_build_with_sandbox(tmp_path):
    with pytest.raises(ValueError, match=r"query\(\) sandbox.*requires"):
        query(
            "noop",
            options=CodeNibAgentOptions(
                repo_path=str(tmp_path),
                llm=MagicMock(spec=LiteLLMChat),
                sandbox=FakeSandbox(),
            ),
        )


def test_query_rejects_unversioned_contexts_with_sandbox():
    with pytest.raises(ValueError, match="contexts have no source provenance"):
        query(
            "noop",
            options=CodeNibAgentOptions(
                contexts={},
                llm=MagicMock(spec=LiteLLMChat),
                sandbox=FakeSandbox(),
            ),
        )
