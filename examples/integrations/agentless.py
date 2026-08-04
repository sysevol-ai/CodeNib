#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Inspect classic Agentless context without installing Agentless."""

from __future__ import annotations

import argparse
from pathlib import Path

from codenib.integrations.agentless import AgentlessRepositoryProvider


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
        help="Repository-relative Python file to render as a skeleton.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    provider = AgentlessRepositoryProvider.from_manifest(args.manifest)
    if args.file:
        print(provider.skeleton_context(args.file))
    else:
        print(provider.project_structure())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
