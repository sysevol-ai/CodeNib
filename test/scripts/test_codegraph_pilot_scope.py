# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Reproduce the missing MCP spill handoff without SDK or model calls."""

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from scripts import codegraph_pilot_scope as scope
from scripts.codegraph_pilot_mcp_proxy import WireReceipts


@pytest.fixture
def setup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = tmp_path / "sdk-projects" / "repo"
    results = project / "session-1" / "tool-results"
    results.mkdir(parents=True)
    wire_root = tmp_path / "private-wire"
    writer = WireReceipts(wire_root)
    calls = []

    def command(argv, *, input, **kwargs):
        calls.append((argv, input))
        if scope._VERIFY in argv:
            return subprocess.CompletedProcess(argv, 0, b"verified\n", b"")
        assert scope._MIRROR in argv
        assert input and scope._sha(input) == argv[-1]
        return subprocess.CompletedProcess(argv, 0, (argv[-1] + "\n").encode(), b"")

    monkeypatch.setattr(
        scope,
        "subprocess",
        SimpleNamespace(run=command, SubprocessError=subprocess.SubprocessError),
    )
    events = []
    output = scope.SessionOutputs(
        workspace, "owned-container", wire_root, events.append
    )
    data = {
        "cwd": str(workspace),
        "tool_name": "mcp__study__explore_context",
        "tool_input": {"query": "symbols"},
        "tool_use_id": "toolu_test",
        "session_id": "session-1",
        "transcript_path": str(project / "session-1.jsonl"),
    }
    return SimpleNamespace(
        workspace=workspace,
        project=project,
        results=results,
        writer=writer,
        wire_root=wire_root,
        calls=calls,
        output=output,
        data=data,
        events=events,
    )


def exchange(setup, text, *, request_id=1, arguments=None):
    arguments = setup.data["tool_input"] if arguments is None else arguments
    request = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "explore_context", "arguments": arguments},
            }
        ).encode()
        + b"\n"
    )
    reply = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        ).encode()
        + b"\n"
    )
    setup.writer.request(request)
    setup.writer.reply(reply)
    return request, reply


def spill(setup, text, *, path=None):
    path = path or setup.results / "mcp-study-explore_context-123.txt"
    path.write_bytes(text.encode("utf-8"))
    chars = len(text.encode("utf-16-le")) // 2
    response = (
        f"Error: result ({chars:,} characters) exceeds maximum allowed tokens. "
        f"Output has been saved to {path}.\nPreview follows"
    )
    return {**setup.data, "tool_response": response}, path


def register(setup, text="source café 🔎"):
    setup.output.before(setup.data, "toolu_test")
    exchange(setup, text)
    post, path = spill(setup, text)
    receipt = setup.output.register(post, "toolu_test", "mcp__study__")
    return receipt, path


def test_original_wire_bytes_authorize_only_the_same_session_payload(setup):
    receipt, path = register(setup)

    assert receipt["producer"] == "authenticated_mcp_wire"
    assert receipt["sha256"] == scope._sha(path.read_bytes())
    assert receipt["request_id"] == 1
    assert receipt["container_id"] == "owned-container"
    assert receipt["delivery"] == "available to native tools; no model-read credit"
    assert setup.output.readable(path)
    assert len(setup.calls) == 1
    assert setup.calls[0][1] == path.read_bytes()


@pytest.mark.parametrize(
    "bad",
    [
        "foreign_session",
        "wrong_callback",
        "mismatch",
        "missing_wire",
        "wrong_arguments",
        "old_wire",
        "wrong_count",
    ],
)
def test_untrusted_mcp_paths_do_not_authorize_a_copy(setup, tmp_path, bad):
    text = "original source"
    if bad == "old_wire":
        exchange(setup, text)
    setup.output.before(setup.data, "toolu_test")
    if bad not in {"missing_wire", "old_wire"}:
        exchange(
            setup,
            text,
            arguments={"query": "different"} if bad == "wrong_arguments" else None,
        )
    path = None
    if bad == "foreign_session":
        path = tmp_path / "other-session.txt"
    post, path = spill(
        setup, "different source" if bad == "mismatch" else text, path=path
    )
    if bad == "wrong_callback":
        post["session_id"] = "session-2"
    elif bad == "wrong_count":
        post["tool_response"] = post["tool_response"].replace(
            "15 characters", "16 characters"
        )

    with pytest.raises(ValueError):
        setup.output.register(post, "toolu_test", "mcp__study__")

    assert setup.calls == []
    assert setup.output.outputs == {}
    assert not setup.output.readable(path)


@pytest.mark.parametrize("link", ["file", "parent", "hardlink"])
def test_links_cannot_expand_the_session_result_boundary(setup, tmp_path, link):
    setup.output.before(setup.data, "toolu_test")
    exchange(setup, "original")
    post, path = spill(setup, "original")
    if link == "file":
        other = tmp_path / "outside.txt"
        other.write_text("original")
        path.unlink()
        path.symlink_to(other)
    elif link == "parent":
        moved = tmp_path / "moved-results"
        setup.results.rename(moved)
        setup.results.symlink_to(moved, target_is_directory=True)
    else:
        os.link(path, tmp_path / "hardlink.txt")

    with pytest.raises((ValueError, OSError)):
        setup.output.register(post, "toolu_test", "mcp__study__")

    assert setup.calls == []


def test_file_swap_during_authentication_does_not_register_captured_bytes(
    setup, monkeypatch
):
    setup.output.before(setup.data, "toolu_test")
    exchange(setup, "original")
    post, path = spill(setup, "original")
    real_os = scope.os
    changed = []

    class GuardedOS:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def read(self, descriptor, size):
            data = real_os.read(descriptor, size)
            if data and not changed:
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(data)
                replacement.replace(path)
                changed.append(True)
            return data

    monkeypatch.setattr(scope, "os", GuardedOS())

    with pytest.raises(ValueError, match="changed while reading"):
        setup.output.register(post, "toolu_test", "mcp__study__")

    assert changed and setup.calls == [] and setup.output.outputs == {}


def test_tampered_registered_host_file_is_not_readable(setup):
    _, path = register(setup)
    path.write_text("changed")

    assert not setup.output.readable(path)


def test_failed_mirror_never_commits_host_read_permission(setup, monkeypatch):
    setup.output.before(setup.data, "toolu_test")
    exchange(setup, "original")
    post, path = spill(setup, "original")

    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(scope.subprocess, "run", failed)
    with pytest.raises(subprocess.CalledProcessError):
        setup.output.register(post, "toolu_test", "mcp__study__")
    assert not setup.output.readable(path)
    assert setup.output.claimed_wire == set()


def test_hook_binding_failure_returns_an_explicit_denial(setup):
    before, _ = scope.scoped_hooks(
        setup.workspace, "owned-container", setup.events.append, setup.wire_root
    )
    invalid = {**setup.data, "transcript_path": "/tmp/other.jsonl"}

    result = asyncio.run(before(invalid, "toolu_test", None))

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert setup.events[-1]["event"] == "pre_rejected"


def test_native_bash_metadata_uses_effective_hook_input_and_keeps_read_scoped(setup):
    before, after = scope.scoped_hooks(
        setup.workspace, "owned-container", setup.events.append, setup.wire_root
    )
    data = {
        **setup.data,
        "tool_name": "Bash",
        "tool_input": {"command": "printf output"},
    }
    allowed = asyncio.run(before(data, "toolu_test", None))
    effective = allowed["hookSpecificOutput"]["updatedInput"]
    assert effective["command"].startswith("docker exec -i -w ")
    path = setup.results / "native-output.txt"
    path.write_bytes(b"output")
    post = {
        **data,
        "tool_input": effective,
        "tool_response": {"persistedOutputPath": str(path), "persistedOutputSize": 6},
    }
    asyncio.run(after(post, "toolu_test", None))
    assert setup.events[-1]["event"] == "sdk_output_registered"
    read = {
        **setup.data,
        "tool_use_id": "read-1",
        "tool_name": "Read",
        "tool_input": {"file_path": str(path)},
    }
    assert (
        asyncio.run(before(read, "read-1", None))["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "allow"
    )
    edit = {**read, "tool_use_id": "edit-1", "tool_name": "Edit"}
    assert (
        asyncio.run(before(edit, "edit-1", None))["hookSpecificOutput"][
            "permissionDecision"
        ]
        == "deny"
    )


def test_wire_receipts_keep_string_and_integer_ids_distinct_and_exact(setup):
    first, reply = exchange(setup, "one", request_id=1)
    other, other_reply = exchange(setup, "two", request_id="1")
    import base64

    records = [
        json.loads(p.read_text()) for p in sorted(setup.writer.root.glob("*.json"))
    ]
    assert [base64.b64decode(r["request_base64"]) for r in records] == [first, other]
    assert [base64.b64decode(r["reply_base64"]) for r in records] == [
        reply,
        other_reply,
    ]
    assert setup.writer.pending == {}


@pytest.mark.parametrize("changed", [False, True])
def test_cli_block_file_written_after_post_hook_is_verified_before_next_tool(
    setup, changed
):
    setup.output.before(setup.data, "toolu_test")
    exchange(setup, "original")
    blocks = [{"type": "text", "text": "original"}]
    post = {**setup.data, "tool_response": blocks}
    assert setup.output.register(post, "toolu_test", "mcp__study__") is None
    assert setup.calls == []
    path = setup.results / "toolu_test.json"
    path.write_text(
        json.dumps(
            [{"type": "text", "text": "changed"}] if changed else blocks, indent=2
        )
    )

    setup.output.flush_completed()

    assert setup.output.readable(path) is (not changed)
    if changed:
        assert setup.calls == []
        assert setup.events[-1]["event"] == "sdk_output_registration_rejected"
    else:
        assert setup.calls[0][1] == path.read_bytes()
        receipt = setup.events[-1]
        assert receipt["original_text_sha256"] == scope._sha(b"original")
        assert receipt["representation"] == "mcp_content_blocks_json"


def test_reconnection_keeps_new_receipts_distinct_from_prior_request_ids(setup):
    exchange(setup, "old response", request_id=1)
    setup.output.before(setup.data, "toolu_test")
    old = setup.writer
    setup.writer = WireReceipts(setup.wire_root)
    assert setup.writer.root != old.root
    exchange(setup, "new response", request_id=1)
    post, path = spill(setup, "new response")

    receipt = setup.output.register(post, "toolu_test", "mcp__study__")

    assert receipt["wire_receipt"].startswith(setup.writer.root.name + "/")
    assert setup.output.readable(path)
    assert len(list(setup.wire_root.glob("connection-*/call-*.json"))) == 2


def test_multi_block_file_requires_all_original_blocks(setup):
    setup.output.before(setup.data, "toolu_test")
    blocks = [
        {"type": "text", "text": "first source"},
        {"type": "text", "text": "second source"},
    ]
    request = (
        json.dumps(
            {
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "explore_context",
                    "arguments": setup.data["tool_input"],
                },
            }
        ).encode()
        + b"\n"
    )
    reply = json.dumps({"id": 3, "result": {"content": blocks}}).encode() + b"\n"
    setup.writer.request(request)
    setup.writer.reply(reply)
    assert (
        setup.output.register(
            {**setup.data, "tool_response": blocks}, "toolu_test", "mcp__study__"
        )
        is None
    )
    path = setup.results / "toolu_test.json"
    path.write_text(json.dumps(blocks))

    setup.output.flush_completed()

    assert setup.output.readable(path)
    assert setup.events[-1]["request_id"] == 3


def test_wire_directory_must_remain_private(tmp_path):
    root = tmp_path / "public-wire"
    root.mkdir(mode=0o755)

    with pytest.raises(ValueError, match="private owned directory"):
        WireReceipts(root)


def test_sdk_outputs_of_another_owner_are_rejected(setup, monkeypatch):
    setup.output.before(setup.data, "toolu_test")
    exchange(setup, "original")
    post, _ = spill(setup, "original")
    real_os = scope.os

    class OtherOwner:
        def __getattr__(self, name):
            return getattr(real_os, name)

        @staticmethod
        def geteuid():
            return -1

    monkeypatch.setattr(scope, "os", OtherOwner())
    with pytest.raises(ValueError, match="owned regular file"):
        setup.output.register(post, "toolu_test", "mcp__study__")
    assert setup.calls == []
