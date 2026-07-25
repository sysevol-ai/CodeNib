#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reject former product identifiers outside immutable external identities."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

_FORMER_CAMEL = "Code" + "Miner"
_FORMER_LOWER = "code" + "miner"
_FORMER_UPPER = "CODE" + "MINER"
_FORMER_IDENTIFIERS = (_FORMER_CAMEL, _FORMER_LOWER, _FORMER_UPPER)

# These datasets have not been republished under new owners. They are immutable
# data addresses, not supported package, command, environment, or state aliases.
_EXTERNAL_IDENTITY_PATTERNS = (
    re.compile(rf"fishmingyu/{_FORMER_LOWER}-base-dataset" r"(?=$|[^A-Za-z0-9_.-])"),
    re.compile(rf"sysevol-ai/{_FORMER_LOWER}-synthesis" r"(?=$|[^A-Za-z0-9_.-])"),
)

# This document records the breaking migration itself. Fixing its occurrence
# counts makes the exception explicit and prevents it from becoming a dumping
# ground for new legacy references.
_MIGRATION_RECORD = Path("docs/codenib_namespace_migration.md")
_MIGRATION_COUNTS = {
    _FORMER_CAMEL: 5,
    _FORMER_LOWER: 14,
    _FORMER_UPPER: 3,
}

_ASSET_MIRRORS = {
    "assets/codenib_icon.svg": (
        "landing/assets/codenib-icon.svg",
        "web/public/codenib-icon.svg",
    ),
    "assets/codenib_logo.svg": ("landing/assets/codenib-logo.svg",),
}


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _candidate_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / path.decode() for path in result.stdout.split(b"\0") if path]


def _migration_record_failure(relative: Path, text: str) -> str | None:
    counts = {identifier: text.count(identifier) for identifier in _FORMER_IDENTIFIERS}
    if counts == _MIGRATION_COUNTS:
        return None
    return (
        f"{relative}: former-identifier counts changed: "
        f"expected {_MIGRATION_COUNTS}, found {counts}"
    )


def _unapproved_namespace(root: Path) -> list[str]:
    failures: list[str] = []
    for path in _candidate_files(root):
        relative = path.relative_to(root)
        relative_text = relative.as_posix()
        if any(identifier in relative_text for identifier in _FORMER_IDENTIFIERS):
            failures.append(f"{relative}: former identifier in tracked path")

        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if relative == _MIGRATION_RECORD:
            failure = _migration_record_failure(relative, text)
            if failure:
                failures.append(failure)
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            remainder = line
            for pattern in _EXTERNAL_IDENTITY_PATTERNS:
                remainder = pattern.sub("", remainder)
            if any(identifier in remainder for identifier in _FORMER_IDENTIFIERS):
                failures.append(f"{relative}:{line_number}: {line.strip()}")
    return failures


def _asset_drift(root: Path) -> list[str]:
    failures: list[str] = []
    for canonical_name, mirror_names in _ASSET_MIRRORS.items():
        canonical = root / canonical_name
        if not canonical.is_file():
            failures.append(f"missing canonical brand asset: {canonical_name}")
            continue

        expected = canonical.read_bytes()
        for mirror_name in mirror_names:
            mirror = root / mirror_name
            if not mirror.is_file():
                failures.append(f"missing brand asset mirror: {mirror_name}")
            elif mirror.read_bytes() != expected:
                failures.append(f"brand asset drift: {mirror_name} != {canonical_name}")
    return failures


def _sync_assets(root: Path) -> list[str]:
    failures: list[str] = []
    for canonical_name, mirror_names in _ASSET_MIRRORS.items():
        canonical = root / canonical_name
        if not canonical.is_file():
            failures.append(f"missing canonical brand asset: {canonical_name}")
            continue
        for mirror_name in mirror_names:
            mirror = root / mirror_name
            mirror.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(canonical, mirror)
    return failures


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the CodeNib namespace and canonical asset mirrors."
    )
    parser.add_argument(
        "--sync-assets",
        action="store_true",
        help="copy canonical SVG assets to their landing and web mirrors",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = _repo_root()
    failures = _sync_assets(root) if args.sync_assets else []
    failures += _unapproved_namespace(root) + _asset_drift(root)
    if failures:
        print(
            "CodeNib namespace check failed. Former names are allowed only in "
            "the frozen migration record and exact external resource IDs:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print("CodeNib namespace check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
