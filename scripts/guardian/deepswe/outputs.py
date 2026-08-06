#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Audit, fix, or delete DeepSWE evaluation output trees."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

DEFAULT_OUTPUT_ROOT = Path("/mnt/data/xiangye/deepswe_outputs")


def _is_writable_dir(path: Path) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


def audit(root: Path) -> int:
    problems = 0
    for path in sorted(root.rglob("*")):
        try:
            st = path.stat()
        except OSError as exc:
            problems += 1
            print(f"[bad] cannot stat {path}: {exc}")
            continue
        if path.is_dir() and not _is_writable_dir(path):
            problems += 1
            print(
                f"[bad] directory not writable: {path} mode={oct(st.st_mode & 0o777)}"
            )
    if problems:
        print(f"[audit] {problems} issue(s)")
    else:
        print(f"[audit] ok: {root}")
    return 1 if problems else 0


def chmod_manageable(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        try:
            if path.is_dir():
                path.chmod(0o777)
            else:
                path.chmod(0o666)
        except OSError as exc:
            print(f"[warn] chmod failed for {path}: {exc}", file=sys.stderr)
    try:
        root.chmod(0o777)
    except OSError as exc:
        print(f"[warn] chmod failed for {root}: {exc}", file=sys.stderr)


def delete_tree(root: Path) -> None:
    chmod_manageable(root)
    shutil.rmtree(root)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true")
    group.add_argument("--fix-permissions", action="store_true")
    group.add_argument("--delete", action="store_true")
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm --delete without an interactive prompt.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.root.expanduser().resolve()
    if args.audit:
        return audit(root)
    if args.fix_permissions:
        chmod_manageable(root)
        print(f"[done] permissions relaxed: {root}")
        return 0
    if args.delete:
        if not args.yes:
            answer = input(f"Delete {root}? Type 'yes' to continue: ")
            if answer != "yes":
                print("[abort]")
                return 1
        delete_tree(root)
        print(f"[done] deleted: {root}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
