# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Generate the idempotent launcher installed in Pier task containers."""

from __future__ import annotations

from typing import Mapping, Sequence


def guardian_start_script(
    *,
    command: Sequence[str],
    environment: Mapping[str, str],
    repo: str,
    baseline_file: str,
    pid_file: str,
    log_file: str,
) -> str:
    """Return a lock-protected launcher for the Guardian filesystem bridge."""

    process_marker = (
        "scripts.guardian.deepswe.harness.bridge"
        if "scripts.guardian.deepswe.harness.bridge" in command
        else ""
    )
    return f"""#!/usr/bin/env python3
import fcntl
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

COMMAND = {list(command)!r}
ENVIRONMENT = {dict(environment)!r}
REPO = {repo!r}
BASELINE_FILE = Path({baseline_file!r})
PID_FILE = Path({pid_file!r})
LOG_FILE = Path({log_file!r})
LOCK_FILE = PID_FILE.with_suffix(PID_FILE.suffix + ".lock")
PROCESS_MARKER = {process_marker!r}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    if PROCESS_MARKER:
        try:
            command_line = Path(
                f"/proc/{{pid}}/cmdline"
            ).read_bytes().replace(b"\\0", b" ").decode(errors="replace")
        except OSError:
            return False
        if PROCESS_MARKER not in command_line:
            return False
    return True


def _read_pid() -> int:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def main() -> int:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        pid = _read_pid()
        if pid and _alive(pid):
            print(f"Guardian already started (pid={{pid}})")
            return 0
        if PID_FILE.exists():
            PID_FILE.unlink()

        try:
            baseline = BASELINE_FILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(
                f"Guardian cannot start without the setup-time baseline "
                f"{{BASELINE_FILE}}: {{exc}}",
                file=sys.stderr,
            )
            return 2
        if not baseline:
            print(
                f"Guardian cannot start: setup-time baseline "
                f"{{BASELINE_FILE}} is empty",
                file=sys.stderr,
            )
            return 2

        child_environment = os.environ.copy()
        child_environment.update(ENVIRONMENT)
        child_environment.setdefault(
            "CODEMINER_CODEX_BIN", shutil.which("codex") or ""
        )
        log = LOG_FILE.open("a", encoding="utf-8")
        process = subprocess.Popen(
            COMMAND,
            cwd=REPO,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        log.close()
        time.sleep(0.2)
        return_code = process.poll()
        if return_code is not None:
            print(
                f"Guardian failed to start (exit={{return_code}}); "
                f"inspect {{LOG_FILE}}",
                file=sys.stderr,
            )
            return 1
        temporary = PID_FILE.with_suffix(".tmp")
        temporary.write_text(str(process.pid) + "\\n", encoding="utf-8")
        temporary.replace(PID_FILE)
        print(
            f"Guardian started (pid={{process.pid}}, "
            f"baseline={{baseline[:12]}})"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""
