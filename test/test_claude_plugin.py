# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""The Claude Code plugin is well formed and starts the right server."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from codenib import cli

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codenib"
LAUNCHER = PLUGIN / "scripts" / "mcp.sh"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"{path} has no frontmatter"
    return yaml.safe_load(match.group(1))


def test_marketplace_lists_the_plugin_under_its_own_name():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())

    (entry,) = marketplace["plugins"]
    assert entry["name"] == manifest["name"] == "codenib"
    assert (ROOT / entry["source"]).resolve() == PLUGIN
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert (PLUGIN / ".claude-plugin" / "icon.png").read_bytes()[:8] == (
        b"\x89PNG\r\n\x1a\n"
    )


def test_the_mcp_server_runs_the_bundled_launcher_for_the_project():
    servers = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]

    assert servers == {
        "codenib": {
            "command": "${CLAUDE_PLUGIN_ROOT}/scripts/mcp.sh",
            # Grok Build reads this file too but may not set the project
            # variable; it starts the server in the project, so "." stands in.
            "args": ["${CLAUDE_PROJECT_DIR:-.}"],
        }
    }
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(LAUNCHER)]).returncode == 0


def test_skills_describe_when_to_use_them_and_side_effects_need_the_user():
    skills = {
        path.parent.name: _frontmatter(path)
        for path in PLUGIN.glob("skills/*/SKILL.md")
    }

    assert set(skills) == {"explain", "impact", "setup", "wiki"}
    for name, front in skills.items():
        assert len(front["description"]) > 40, name
    # Installing software or starting servers happens only when asked.
    assert skills["setup"].get("disable-model-invocation") is True
    assert skills["wiki"].get("disable-model-invocation") is True
    assert "disable-model-invocation" not in skills["explain"]


def _subcommands(parser) -> dict:
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return {name: _subcommands(sub) for name, sub in action.choices.items()}
    return {}


def test_every_codenib_command_the_plugin_mentions_exists():
    tree = _subcommands(cli.build_parser())
    texts = [
        path.read_text() for path in PLUGIN.rglob("*") if path.suffix in {".md", ".sh"}
    ]
    mentioned = set()
    for text in texts:
        mentioned.update(
            re.findall(r"codenib ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", text)
        )
    assert mentioned
    for command, sub in mentioned:
        if command == "pages" and "pages" not in tree:
            continue  # The Wiki skill checks for `codenib pages` before using it.
        assert command in tree, command
        if tree[command] and sub:
            assert sub in tree[command], f"codenib {command} {sub}"


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


def _run(
    project: Path | str, tools: Path, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    env = {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(tools)}
    return subprocess.run(
        [str(LAUNCHER), str(project)],
        env=env,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_launcher_indexes_then_serves_the_checkout_root(checkout, tmp_path):
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
    # Index output goes to stderr; only the server may write to stdout.
    assert result.stdout.count("chatter") == 1
    assert "chatter" in result.stderr


def test_launcher_falls_back_to_uvx(checkout, tmp_path):
    tools, log = tmp_path / "tools", tmp_path / "calls.log"
    tools.mkdir()
    _fake_tool(tools, "uvx", log)

    assert _run(checkout, tools).returncode == 0
    assert (
        log.read_text()
        .splitlines()[0]
        .startswith("uvx --quiet --from codenib[graph,mcp]>=0.2.5 codenib index ")
    )


def test_launcher_passes_over_a_codenib_too_old_to_serve_it(checkout, tmp_path):
    tools, log = tmp_path / "tools", tmp_path / "calls.log"
    tools.mkdir()
    _fake_tool(tools, "codenib", log, current=False)

    stuck = _run(checkout, tools)
    assert stuck.returncode == 1 and "older than 0.2.5" in stuck.stderr
    assert not log.exists()

    _fake_tool(tools, "uvx", log)
    assert _run(checkout, tools).returncode == 0
    assert all(line.startswith("uvx ") for line in log.read_text().splitlines())


def test_launcher_explains_what_is_missing(checkout, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = _run(checkout, empty)
    assert missing.returncode == 1 and "/codenib:setup" in missing.stderr

    tools, log = tmp_path / "tools", tmp_path / "calls.log"
    tools.mkdir()
    _fake_tool(tools, "codenib", log)
    outside = _run(tmp_path / "empty", tools)
    assert outside.returncode == 1 and "not inside one" in outside.stderr
    assert not log.exists()


def test_launcher_uses_the_working_directory_without_a_project(checkout, tmp_path):
    # A host that leaves the variable unexpanded passes its literal text.
    tools, log = tmp_path / "tools", tmp_path / "calls.log"
    tools.mkdir()
    _fake_tool(tools, "codenib", log)

    result = _run("${CLAUDE_PROJECT_DIR}", tools, cwd=checkout / "pkg")

    assert result.returncode == 0, result.stderr
    root = os.path.realpath(checkout)
    assert log.read_text().splitlines()[0] == f"codenib index {root} --preset fast"
