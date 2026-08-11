#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run RepoNavigator's published jump contract over a CodeNib manifest."""

from __future__ import annotations

import argparse
from pathlib import Path

from codenib.integrations.reponavigator import RepoNavigatorRepositoryProvider


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Graph-enabled CodeNib repo_manifest.json",
    )
    parser.add_argument(
        "--file-path",
        required=True,
        help="Repository-relative file containing the symbol occurrence.",
    )
    parser.add_argument(
        "--symbol",
        required=True,
        help="Exact symbol text to resolve.",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=0,
        help="Zero-based occurrence index within the file (default: 0).",
    )
    parser.add_argument(
        "--allow-graph-fallback",
        action="store_true",
        help=(
            "Allow degraded symbol-graph resolution when no SCIP occurrence "
            "index or language-server provider is available."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    provider = RepoNavigatorRepositoryProvider.from_manifest(
        args.manifest,
        allow_graph_fallback=args.allow_graph_fallback,
    )
    print(
        provider.jump(
            file_path=args.file_path,
            symbol=args.symbol,
            index=args.index,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
