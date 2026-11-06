# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run the guard from Claude Code and Codex lifecycle hooks.

Two hooks are installed per agent:

- **SessionStart** records the commit the session started from and starts
  building its graph in the background. It prints nothing, because both agents
  add a SessionStart hook's output to the model's context.
- **Stop** runs when the agent is about to finish. When the session's changes
  reach tests, or touch code that has notes, it hands the report back to the
  agent, which then continues. It reports each distinct change once, and never
  on a turn that a Stop hook already continued, so it cannot loop.

A hook never fails the agent's turn: on any error it logs and lets the agent
stop.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import traceback
import urllib.request
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from filelock import FileLock, Timeout

from ..paths import user_state_dir
from .report import render_for_agent, run_guard
from .snapshot import (
    SnapshotError,
    build_in_background,
    cached_snapshot,
    failed_snapshot,
    guard_dir,
    has_python,
    resolve_commit,
)

AGENTS = ("claude", "codex")
HOOK_MARKER = "codenib guard hook"
STOP_TIMEOUT = 300  # seconds the agent allows the Stop hook
GRAPH_WAIT = 240  # seconds the Stop hook waits for a graph build in progress
SESSIONS_KEPT = 50
EVENTS_URL = "https://demo.codenib.ai/api/experience-events"


# ---------------------------------------------------------------- install


def hook_command(agent: str, event: str, python: Optional[str] = None) -> str:
    """The hook's command line, for this CodeNib installation.

    The ``codenib`` script next to the interpreter is preferred: agents run
    hooks from the repository, and ``python -m`` would put the repository first
    on the import path.
    """

    interpreter = Path(python or sys.executable)
    script = interpreter.with_name("codenib")
    if script.is_file() and os.access(script, os.X_OK):
        program = shlex.quote(str(script))
    else:
        program = f"{shlex.quote(str(interpreter))} -m codenib"
    return f"{program} guard hook --agent {agent} --event {event}"


def settings_path(repo: Path, agent: str, scope: str) -> Path:
    if agent == "claude":
        if scope == "user":
            return Path.home() / ".claude" / "settings.json"
        name = "settings.json" if scope == "project" else "settings.local.json"
        return repo / ".claude" / name
    if scope == "user":
        return Path.home() / ".codex" / "hooks.json"
    return repo / ".codex" / "hooks.json"


def _load_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def _is_guard_handler(handler: object) -> bool:
    return isinstance(handler, dict) and HOOK_MARKER in str(handler.get("command", ""))


def _without_guard(groups: object) -> List[object]:
    kept = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        handlers = [h for h in group.get("hooks", []) if not _is_guard_handler(h)]
        if handlers:
            kept.append({**group, "hooks": handlers})
    return kept


def install(
    repo: Path, agent: str, *, scope: str = "local", python: Optional[str] = None
) -> Path:
    """Add the guard's SessionStart and Stop hooks to one agent's settings."""

    if agent == "codex" and scope == "local":
        scope = "project"  # Codex has no untracked project settings file
    path = settings_path(repo, agent, scope)
    data = _load_json(path)
    hooks = data.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    for event, name, timeout in (
        ("SessionStart", "session-start", 10),
        ("Stop", "stop", STOP_TIMEOUT),
    ):
        groups = _without_guard(hooks.get(event))
        handler = {
            "type": "command",
            "command": hook_command(agent, name, python),
            "timeout": timeout,
        }
        if agent == "codex" and event == "Stop":
            handler["statusMessage"] = "CodeNib guard: checking tests at risk"
        groups.append({"hooks": [handler]})
        hooks[event] = groups
    data["hooks"] = hooks
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def uninstall(repo: Path, agent: str, *, scope: str = "local") -> Optional[Path]:
    if agent == "codex" and scope == "local":
        scope = "project"
    path = settings_path(repo, agent, scope)
    if not path.exists():
        return None
    data = _load_json(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return None
    for event in list(hooks):
        groups = _without_guard(hooks[event])
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- state


def _state_path(repo: Path) -> Path:
    return guard_dir(repo) / "sessions.json"


def _log_path(repo: Path) -> Path:
    return guard_dir(repo) / "hook.log"


def _log(repo: Path, message: str) -> None:
    path = _log_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")


def _update_session(repo: Path, session: str, **values) -> Dict[str, object]:
    path = _state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock", timeout=10):
        try:
            sessions = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sessions = {}
        entry = dict(sessions.get(session) or {})
        if "base" in values:
            entry.setdefault("base", values["base"])  # a resumed session keeps its base
        if "reported" in values:
            entry["reported"] = [*entry.get("reported", []), values["reported"]][-20:]
        entry["seen"] = time.time()
        sessions[session] = entry
        newest = sorted(sessions.items(), key=lambda kv: kv[1].get("seen", 0))[
            -SESSIONS_KEPT:
        ]
        path.write_text(json.dumps(dict(newest), indent=1), encoding="utf-8")
        return entry


# ---------------------------------------------------------------- telemetry


def _telemetry_path() -> Path:
    return user_state_dir() / "guard-telemetry.json"


def set_first_run_sharing(enabled: bool) -> None:
    path = _telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_json(path) if path.exists() else {}
    data["share_first_run"] = bool(enabled)
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def _report_first_run() -> None:
    """Send one anonymous ``guard_first_run`` event, only if the user opted in.

    The event has the fixed shape of the site's funnel events: its name, the
    ``cli`` surface and a random identifier, and nothing about the repository,
    the agent or the machine. It is sent at most once per user.
    """

    path = _telemetry_path()
    try:
        data = _load_json(path)
    except (OSError, ValueError):
        return
    if not data.get("share_first_run") or data.get("first_run_sent"):
        return
    data["first_run_sent"] = True
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    body = json.dumps(
        {"event": "guard_first_run", "surface": "cli", "visit": uuid.uuid4().hex}
    ).encode("utf-8")
    request = urllib.request.Request(
        os.environ.get("CODENIB_EVENTS_URL", EVENTS_URL),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=3).close()
    except OSError:
        pass


# ---------------------------------------------------------------- hook


def _repo_root(cwd: str) -> Optional[Path]:
    import subprocess

    completed = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(completed.stdout.strip()) if completed.returncode == 0 else None


def _output(agent: str, text: str) -> str:
    if agent == "claude":
        return json.dumps(
            {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}}
        )
    return json.dumps({"decision": "block", "reason": text})


def _wait_for_graph(repo: Path, commit: str) -> bool:
    """Start a build if none runs, and wait up to ``GRAPH_WAIT`` for the graph."""

    if cached_snapshot(repo, commit) is not None:
        return True
    if failed_snapshot(repo, commit) is not None:
        return False
    lock_path = guard_dir(repo) / "snapshots" / f"{commit}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path))
    try:
        lock.acquire(timeout=0)  # free: no build is running for this commit
    except Timeout:
        pass
    else:
        lock.release()
        build_in_background(repo, commit, _log_path(repo))
    deadline = time.monotonic() + GRAPH_WAIT
    while time.monotonic() < deadline:
        time.sleep(2)
        if cached_snapshot(repo, commit) is not None:
            return True
        if failed_snapshot(repo, commit) is not None:
            return False
    return False


def stop(agent: str, payload: Dict[str, object]) -> Tuple[str, str]:
    """Return ``(stdout, reason logged)`` for one Stop event."""

    if payload.get("stop_hook_active"):
        return "", "skipped: a Stop hook already continued this turn"
    repo = _repo_root(str(payload.get("cwd") or os.getcwd()))
    if repo is None:
        return "", "skipped: not a git repository"
    session = str(payload.get("session_id") or "unknown")
    entry = _update_session(repo, session)
    base = str(entry.get("base") or resolve_commit(repo))
    if not has_python(repo, base):
        return "", "skipped: no Python files"
    if not _wait_for_graph(repo, base):
        reason = failed_snapshot(repo, base) or "still building"
        return "", f"skipped: graph of {base[:12]}: {reason}"
    report = run_guard(repo, base)
    if not report.has_findings:
        return "", "no tests or notes at risk"
    if report.diff_sha256 in entry.get("reported", []):
        return "", "already reported this change"
    _update_session(repo, session, reported=report.diff_sha256)
    _report_first_run()
    return _output(agent, render_for_agent(report)), (
        f"reported {len(report.impact.tests)} tests, "
        f"{len(report.notes_on_change)} notes, {len(report.stale_notes)} stale"
    )


def session_start(agent: str, payload: Dict[str, object]) -> str:
    repo = _repo_root(str(payload.get("cwd") or os.getcwd()))
    if repo is None:
        return "skipped: not a git repository"
    commit = resolve_commit(repo)
    session = str(payload.get("session_id") or "unknown")
    entry = _update_session(repo, session, base=commit)
    base = str(entry["base"])
    if not has_python(repo, base):
        return "skipped: no Python files"
    if cached_snapshot(repo, base) is None:
        build_in_background(repo, base, _log_path(repo))
        return f"session base {base[:12]}; building its graph"
    return f"session base {base[:12]}; graph ready"


def run_hook(agent: str, event: str, stdin: str) -> int:
    """Entry point of ``codenib guard hook``. Always exits 0."""

    try:
        payload = json.loads(stdin or "{}")
    except ValueError:
        payload = {}
    repo = _repo_root(str(payload.get("cwd") or os.getcwd()))
    try:
        if event == "stop":
            out, reason = stop(agent, payload)
            if out:
                sys.stdout.write(out + "\n")
        else:
            reason = session_start(agent, payload)
        if repo is not None:
            _log(repo, f"{agent} {event}: {reason}")
    except (SnapshotError, OSError, ValueError, RuntimeError, Timeout) as exc:
        if repo is not None:
            _log(repo, f"{agent} {event}: error {exc}\n{traceback.format_exc()}")
    return 0
