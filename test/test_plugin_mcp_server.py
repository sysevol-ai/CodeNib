# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Each agent plugin's command starts a server that answers a real question.

The other plugin tests replace ``codenib`` with a stand-in. These run the
command an agent runs against this checkout's CodeNib and speak MCP to it:
initialize, list the tools, call one.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

pytest.importorskip("mcp")

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_LAUNCHER = ROOT / "plugins" / "codenib" / "scripts" / "mcp.sh"
DSH_PATCH = ROOT / "plugins" / "dsh" / "cordis.patch.yml"

SOURCE = '''\
def unsign(value, key):
    """Return the payload when its signature matches."""
    payload, signature = value.rsplit(".", 1)
    if verify_signature(payload, signature, key):
        return payload
    raise ValueError("signature does not match")


def verify_signature(payload, signature, key):
    return signature == f"{key}:{len(payload)}"
'''


def _dsh_command() -> list[str]:
    (layer,) = yaml.safe_load(DSH_PATCH.read_text())
    config = layer["insert"][0]["config"]
    return [config["command"], *config["args"]]


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "signer.py").write_text(SOURCE)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    return repo


@pytest.fixture
def user_env(tmp_path):
    """A user's shell: this checkout's ``codenib`` on the PATH, an empty home."""
    tools, home = tmp_path / "tools", tmp_path / "home"
    tools.mkdir()
    home.mkdir()
    shim = tools / "codenib"
    shim.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} -c "
        "'import sys; from codenib.cli import main; sys.exit(main())' \"$@\"\n"
    )
    shim.chmod(0o755)
    return {
        "PATH": f"{tools}:/usr/bin:/bin",
        "HOME": str(home),
        "PYTHONPATH": str(ROOT),
    }


class _Server:
    def __init__(self, command: list[str], cwd: Path, env: dict[str, str]):
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.stdout: queue.Queue[str | None] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        for line in self.process.stdout:
            self.stdout.put(line)
        self.stdout.put(None)

    def _read_stderr(self) -> None:
        self.stderr.extend(self.process.stderr)

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.process.stdin.flush()

    def request(self, request_id: int, method: str, params: dict | None = None):
        self.send({"id": request_id, "method": method, "params": params or {}})
        while True:
            try:
                line = self.stdout.get(timeout=180)
            except queue.Empty:
                line = None
            assert line is not None, "server stopped:\n" + "".join(self.stderr[-30:])
            # Anything on stdout that is not the protocol breaks an agent.
            message = json.loads(line)
            if message.get("id") == request_id:
                assert "error" not in message, message
                return message["result"]

    def close(self) -> None:
        self.process.kill()
        self.process.wait()


@pytest.mark.parametrize("host", ["claude-code", "dsh"])
def test_the_plugin_command_serves_source_for_a_question(host, checkout, user_env):
    if host == "claude-code":
        # Claude Code and Grok Build pass the project; they start it anywhere.
        command, cwd = [str(CLAUDE_LAUNCHER), str(checkout)], checkout.parent
    else:
        command, cwd = _dsh_command(), checkout / "pkg"

    server = _Server(command, cwd, {**os.environ, **user_env})
    try:
        hello = server.request(
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "plugin-test", "version": "0"},
            },
        )
        assert hello["serverInfo"]["name"] == "codenib"
        server.send({"method": "notifications/initialized"})

        tools = server.request(2, "tools/list")["tools"]
        assert [tool["name"] for tool in tools] == ["explore_context"]

        answer = server.request(
            3,
            "tools/call",
            {
                "name": "explore_context",
                "arguments": {"query": "where is a signature verified"},
            },
        )
        assert not answer.get("isError"), answer
        text = "".join(part.get("text", "") for part in answer["content"])
        assert "pkg/signer.py" in text and "verify_signature" in text
    finally:
        server.close()
    assert not (checkout / ".codenib").exists()
