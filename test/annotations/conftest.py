# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A git repository and a stand-in for ``claude -p`` that prints its JSON."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

FAKE_AGENT = textwrap.dedent("""
    import json, os, re, sys
    from pathlib import Path

    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_AGENT_MODE", "ok")
    counter = Path(os.environ["FAKE_AGENT_COUNTER"])
    calls = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(calls))
    Path(os.environ["FAKE_AGENT_LAST_PROMPT"]).write_text(prompt)
    ids = re.findall(r"^## Annotation ([0-9A-Z]{26})", prompt, flags=re.M)
    if mode == "error":
        print(json.dumps({"type": "result", "subtype": "error_during_execution",
                          "is_error": True, "result": "model overloaded"}))
        sys.exit(1)
    if mode == "empty-first" and calls == 1:
        ids = []
    if os.environ.get("CODENIB_TASK_EDIT") == "1":
        path = Path("calc.py")
        path.write_text(path.read_text().replace("a - b", "a + b"))
    results = []
    for index, annotation_id in enumerate(ids):
        key = "A-" + annotation_id if mode == "mangle" else annotation_id
        results.append({"annotation": key,
                        "answer": f"Answer {index} from calc.py:1-2",
                        "files": [{"path": "calc.py", "start_line": 1, "end_line": 2},
                                  {"path": "missing.py"}, {"path": "/etc/passwd"},
                                  {"path": "../repo/calc.py"}]})
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "done", "structured_output": {"results": results},
                      "usage": {"input_tokens": 100, "cache_read_input_tokens": 50,
                                "output_tokens": 20},
                      "total_cost_usd": 0.01, "num_turns": 3, "duration_ms": 1200}))
    """)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "maintenance.auto=false", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "README.md").write_text("# calc\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return repo


@pytest.fixture
def fake_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT)
    monkeypatch.setenv("FAKE_AGENT_COUNTER", str(tmp_path / "calls"))
    monkeypatch.setenv("FAKE_AGENT_LAST_PROMPT", str(tmp_path / "prompt.txt"))

    from codenib.annotations.agents import CommandAgent

    agent = CommandAgent([sys.executable, str(script)], name="fake")
    agent.prompt_path = tmp_path / "prompt.txt"  # type: ignore[attr-defined]
    agent.calls_path = tmp_path / "calls"  # type: ignore[attr-defined]
    return agent
