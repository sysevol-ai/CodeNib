#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Check CodeNib display branding without breaking legacy identities."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

LEGACY_BRAND = "Code" + "Miner"
SPDX_MARKER = "SPDX-FileCopyrightText:"

_ALLOWED_LEGACY_PATTERNS = (
    re.compile(rf"sysevol-ai/{LEGACY_BRAND}\b"),
    re.compile(rf"{LEGACY_BRAND}(?:AgentOptions|BaseDataset|SynthesisDataset)\b"),
    re.compile(rf"{LEGACY_BRAND}(?:-| )(?:[Bb]ase|[Ss]ynthesis)\b"),
    re.compile(rf"[/\\]{LEGACY_BRAND}\b"),
)

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
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / path.decode() for path in result.stdout.split(b"\0") if path]


def _unapproved_branding(root: Path) -> list[str]:
    failures: list[str] = []
    for path in _candidate_files(root):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        relative = path.relative_to(root)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if LEGACY_BRAND not in line:
                continue
            if SPDX_MARKER in line:
                continue

            remainder = line
            for pattern in _ALLOWED_LEGACY_PATTERNS:
                remainder = pattern.sub("", remainder)
            if LEGACY_BRAND in remainder:
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
        description="Check CodeNib display branding and canonical asset mirrors."
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
    failures += _unapproved_branding(root) + _asset_drift(root)
    if failures:
        print(
            "CodeNib branding check failed. Preserve legacy names only for "
            "documented compatibility, repository, path, or dataset identities:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print("CodeNib branding check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
