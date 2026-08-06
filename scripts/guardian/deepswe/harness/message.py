# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Standalone solver-message action installed into a Pier task container."""

from __future__ import annotations


def guardian_message_script(*, inbox: str, repo: str) -> str:
    """Return an append-only message publisher with no CodeNib dependency."""

    return r"""#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

INBOX = %r
REPO = %r


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", action="append", default=[])
    args = parser.parse_args()
    content = sys.stdin.read().strip()
    if not content or len(content) > 16000:
        raise SystemExit("message must contain 1-16000 characters")
    if len(args.scope) > 32 or any(len(item) > 512 for item in args.scope):
        raise SystemExit("message scope exceeds its limit")
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        head = ""
    path = Path(INBOX)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        sequence = 1
        for line in handle:
            try:
                sequence = max(sequence, int(json.loads(line).get("sequence", 0)) + 1)
            except Exception:
                pass
        value = {
            "id": "msg_%%08d" %% sequence,
            "sequence": sequence,
            "sender": "solver:filesystem",
            "content": content,
            "observed_head": head,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": args.scope,
        }
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(value, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
""" % (inbox, repo)


__all__ = ["guardian_message_script"]
