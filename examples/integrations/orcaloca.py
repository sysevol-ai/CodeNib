#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query an OrcaLoca-compatible provider without installing OrcaLoca.

Build a graph-enabled manifest first:

    codenib index /path/to/repository --preset graph

Then inspect the repository tree:

    python examples/integrations/orcaloca.py \
        --manifest /path/to/repo_manifest.json

Or query one class:

    python examples/integrations/orcaloca.py \
        --manifest /path/to/repo_manifest.json \
        --class-name SearchManager
"""

from __future__ import annotations

import argparse
from pathlib import Path

from codenib.integrations.orcaloca import OrcaLocaSearchProvider


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Graph-enabled CodeNib repo_manifest.json",
    )
    parser.add_argument(
        "--class-name",
        help="Query this class instead of printing the repository tree",
    )
    parser.add_argument(
        "--file-path",
        help="Optional repository-relative file constraint for --class-name",
    )
    parser.add_argument(
        "--tree-root",
        help="Optional repository-relative directory for the tree query",
    )
    parser.add_argument("--max-depth", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    provider = OrcaLocaSearchProvider.from_manifest(args.manifest)
    if args.class_name:
        output = provider.search_class(args.class_name, file_path=args.file_path)
    else:
        output = provider.search_file_tree(
            name=args.tree_root,
            max_depth=args.max_depth,
        )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
