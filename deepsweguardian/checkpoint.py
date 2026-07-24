# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Guardian checkpoint script installed for Codex-in-Pier runs."""

from __future__ import annotations


def guardian_checkpoint_script() -> str:
    """Return the executable script Codex runs before final handoff."""
    return r'''#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _head(repo: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _load_status(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("status.json is not an object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wait for Repository Guardian's report for the current HEAD."
    )
    parser.add_argument("--repo", default=os.getcwd())
    parser.add_argument("--guardian-dir", default="/app/.guardian/out")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    repo = os.path.abspath(args.repo)
    guardian_dir = Path(os.path.expanduser(args.guardian_dir))
    status_path = guardian_dir / "status.json"
    findings_path = guardian_dir / "findings.md"

    try:
        head = _head(repo)
    except Exception as exc:
        print(f"Guardian checkpoint: cannot resolve HEAD: {exc}", file=sys.stderr)
        return 2

    print(f"Guardian checkpoint: waiting for commit {head[:12]}...", flush=True)
    deadline = time.time() + max(1, args.timeout)
    last_status = None
    last_error = ""
    while time.time() < deadline:
        try:
            status = _load_status(status_path)
            last_status = status
        except FileNotFoundError:
            last_error = f"missing {status_path}"
            time.sleep(max(0.1, args.interval))
            continue
        except Exception as exc:
            last_error = f"cannot read {status_path}: {exc}"
            time.sleep(max(0.1, args.interval))
            continue

        error = status.get("error")
        if error:
            print(f"Guardian checkpoint: Guardian failed: {error}", file=sys.stderr)
            return 3

        status_commit = str(status.get("commit") or "")
        running = bool(status.get("running"))
        if status_commit == head and not running:
            if not findings_path.exists():
                print(
                    f"Guardian checkpoint: report ready but {findings_path} is missing",
                    file=sys.stderr,
                )
                return 4

            print("Guardian checkpoint: report ready")
            print(f"model: {status.get('llm_model', '')}")
            print(f"backend: {status.get('llm_backend', '')}")
            print(f"findings: {status.get('findings', 0)}")
            tokens = status.get("llm_tokens")
            if isinstance(tokens, dict):
                print(f"tokens: {tokens.get('total', 0)}")
            else:
                print(f"tokens: {tokens or 0}")
            print("")
            print(findings_path.read_text(encoding="utf-8"), end="")
            return 0

        time.sleep(max(0.1, args.interval))

    print(
        f"Guardian checkpoint: timed out waiting for {head[:12]}",
        file=sys.stderr,
    )
    if last_status is not None:
        print(
            "last status: "
            + json.dumps(
                {
                    "commit": last_status.get("commit"),
                    "running": last_status.get("running"),
                    "llm_backend": last_status.get("llm_backend"),
                    "findings": last_status.get("findings"),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    elif last_error:
        print(f"last status: {last_error}", file=sys.stderr)
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
'''
