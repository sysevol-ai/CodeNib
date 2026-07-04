#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run an edit command with CodeMiner edit-audit artifacts.

Usage:

    python scripts/agent_compile/run_edit_audit.py \\
        --repo /path/to/repo \\
        --artifact-dir /tmp/audit \\
        --verify "python -m pytest test/foo.py -q" \\
        -- bash -lc "python fix_script.py"
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.agent_compile.lib.edit_audit import (  # noqa: E402
    run_edit_command_with_audit,
)


def _metadata(values: Optional[Sequence[str]]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"metadata must be key=value, got {value!r}")
        key, raw = value.split("=", 1)
        metadata[key] = raw
    return metadata


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--edit-cwd", type=Path)
    parser.add_argument("--edit-timeout-s", type=float)
    parser.add_argument("--verify", help="Smallest local verification command")
    parser.add_argument("--verify-cwd", type=Path)
    parser.add_argument("--verify-timeout-s", type=float)
    parser.add_argument("--metadata", action="append", default=[])
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the full audit JSON instead of just its path",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Edit command after '--', e.g. -- bash -lc 'python fix.py'",
    )
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing edit command after '--'")
    verify_command = shlex.split(args.verify) if args.verify else None

    audit = run_edit_command_with_audit(
        args.repo,
        command,
        artifact_dir=args.artifact_dir,
        edit_cwd=args.edit_cwd,
        edit_timeout_s=args.edit_timeout_s,
        verification_command=verify_command,
        verification_cwd=args.verify_cwd,
        verification_timeout_s=args.verify_timeout_s,
        metadata=_metadata(args.metadata),
    )
    if args.print_json:
        print(json.dumps(audit.to_dict(), indent=2, sort_keys=True))
    else:
        print(audit.audit_json_path)

    edit_exit = audit.edit_command.exit_code if audit.edit_command else 0
    verify_exit = audit.verification.exit_code if audit.verification else 0
    return edit_exit if edit_exit else verify_exit


if __name__ == "__main__":
    raise SystemExit(main())
