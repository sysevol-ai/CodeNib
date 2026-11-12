# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""The DeepSeek Harness bundle is well formed and starts the right server."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "plugins" / "dsh"
CLAUDE_LAUNCHER = ROOT / "plugins" / "codenib" / "scripts" / "mcp.sh"


def _row() -> dict:
    (layer,) = yaml.safe_load((BUNDLE / "cordis.patch.yml").read_text())
    (row,) = layer["insert"]
    return row


def _script() -> str:
    args = _row()["config"]["args"]
    assert args[0] == "-c"
    return args[1]


def test_the_bundle_declares_its_patch_and_ships_it():
    manifest = json.loads((BUNDLE / "package.json").read_text())

    bundle = manifest["dsh"]["bundle"]
    assert (BUNDLE / bundle["patch"]).is_file()
    assert set(manifest["files"]) >= {Path(bundle["patch"]).name, "README.md"}
    # The server starts through sh, so Windows hosts skip the bundle.
    assert bundle["platforms"] == ["darwin", "linux"]
    assert "dsh-plugin" in manifest["keywords"]
    # No engines.dsh: a range rejects DSH's prerelease builds.
    assert "engines" not in manifest


def test_the_patch_adds_one_stdio_mcp_server():
    row = _row()

    assert row["name"] == "@deepseek-ai/dsh-mcp-client"
    config = row["config"]
    assert config["serverName"] == "codenib"
    assert config["transport"] == "stdio"
    assert config["command"] == "sh"
    assert subprocess.run(["sh", "-n", "-c", _script()]).returncode == 0


def test_it_runs_the_same_commands_as_the_claude_code_launcher():
    launcher = CLAUDE_LAUNCHER.read_text()
    for step in (
        'index "$root" --preset fast >&2',
        'mcp "$root" --defer-graph --tool-surface explore',
        "codenib[graph,mcp]",
    ):
        assert step in launcher and step in _script(), step


def _fake_tool(directory: Path, name: str, log: Path, current: bool = True) -> None:
    """A stand-in CLI that logs its calls; ``current`` decides what its help lists."""
    option = "--defer-graph" if current else "--tool-surface"
    script = directory / name
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$*" == *"mcp --help" ]]; then echo "usage: [{option}]"; exit 0; fi\n'
        f'echo "{name} $*" >> {shlex.quote(str(log))}\n'
        'echo "chatter that must not reach the protocol stream"\n'
    )
    script.chmod(0o755)


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _run(cwd: Path, tools: Path) -> subprocess.CompletedProcess:
    env = {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(tools)}
    return subprocess.run(
        ["sh", "-c", _script()], env=env, capture_output=True, text=True, cwd=cwd
    )


def test_it_indexes_then_serves_the_checkout_dsh_runs_in(checkout, tmp_path):
    tools, log = tmp_path / "tools", tmp_path / "calls.log"
    tools.mkdir()
    _fake_tool(tools, "codenib", log)

    result = _run(checkout / "pkg", tools)

    assert result.returncode == 0, result.stderr
    root = os.path.realpath(checkout)
    assert log.read_text().splitlines() == [
        f"codenib index {root} --preset fast",
        f"codenib mcp {root} --defer-graph --tool-surface explore",
    ]
    assert result.stdout.count("chatter") == 1


def test_it_falls_back_to_uvx_and_explains_what_is_missing(checkout, tmp_path):
    tools, log = tmp_path / "tools", tmp_path / "calls.log"
    tools.mkdir()
    _fake_tool(tools, "uvx", log)
    assert _run(checkout, tools).returncode == 0
    assert log.read_text().startswith(
        "uvx --quiet --from codenib[graph,mcp]>=0.2.5 codenib index "
    )

    empty = tmp_path / "empty"
    empty.mkdir()
    missing = _run(checkout, empty)
    assert missing.returncode == 1 and "pip install" in missing.stderr
    outside = _run(empty, tools)
    assert outside.returncode == 1 and "Git checkout" in outside.stderr


def test_it_passes_over_a_codenib_too_old_to_serve_it(checkout, tmp_path):
    tools, log = tmp_path / "tools", tmp_path / "calls.log"
    tools.mkdir()
    _fake_tool(tools, "codenib", log, current=False)

    stuck = _run(checkout, tools)
    assert stuck.returncode == 1 and "older than 0.2.5" in stuck.stderr
    assert not log.exists()

    _fake_tool(tools, "uvx", log)
    assert _run(checkout, tools).returncode == 0
    assert all(line.startswith("uvx ") for line in log.read_text().splitlines())


def test_the_documented_install_names_a_profile():
    # `dsh plugin` refuses to run without --profile <name>.
    for page in ("README.md", "docs/codegraph.md", "plugins/dsh/README.md"):
        commands = [
            line
            for line in (ROOT / page).read_text().splitlines()
            if line.startswith("dsh plugin ")
        ]
        assert commands, page
        for command in commands:
            assert command.startswith("dsh plugin --profile "), (page, command)
