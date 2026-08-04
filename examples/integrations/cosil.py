#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Inspect CoSIL-compatible repository context without installing CoSIL."""

from __future__ import annotations

import argparse
from pathlib import Path

from codenib.integrations.cosil import CoSILRepositoryProvider


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Graph-enabled CodeNib repo_manifest.json",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        help="Candidate Python file whose CoSIL structure should be shown.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    provider = CoSILRepositoryProvider.from_manifest(args.manifest)
    if args.file:
        print(provider.candidate_structure(args.file))
    else:
        print(provider.project_structure())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
