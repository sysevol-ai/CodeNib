#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Query LocAgent-compatible tools without installing LocAgent."""

from __future__ import annotations

import argparse
from pathlib import Path

from codenib.integrations.locagent import LocAgentToolProvider


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Graph-enabled CodeNib repo_manifest.json",
    )
    parser.add_argument(
        "--entity",
        help="Return this file or file_path:QualifiedName entity.",
    )
    parser.add_argument(
        "--search",
        help="Search this keyword instead of reading an entity or tree.",
    )
    parser.add_argument(
        "--tree-root",
        default="/",
        help="Entity or directory from which graph traversal starts.",
    )
    parser.add_argument("--max-depth", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    provider = LocAgentToolProvider.from_manifest(args.manifest)
    if args.entity:
        output = provider.get_entity_contents([args.entity])
    elif args.search:
        output = provider.search_code_snippets(
            search_terms=[args.search],
            file_path_or_pattern=None,
        )
    else:
        output = provider.explore_tree_structure(
            [args.tree_root],
            traversal_depth=args.max_depth,
        )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
