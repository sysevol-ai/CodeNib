#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rebuild the code maps of saved visitor Wikis that have none.

Wikis generated while hosted Python indexing saw no files below the
repository root were saved without a system map. This downloads each Wiki's
pinned commit again, indexes it, and replaces only its graph entries. Pages,
prose and reported cost are untouched, and no model is called.

    CODENIB_DEMO_CONFIG=qa_config.api.yaml \\
        python scripts/backfill_visitor_wiki_graphs.py --dry-run
    ... --attempt <id>    # one Wiki; default: every Wiki missing a map
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.web.config import load_config  # noqa: E402
from codenib.wiki.sqlite_store import SQLiteWikiStore  # noqa: E402
from codenib.wiki.visitor_wiki import VisitorWikis  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="demo config (default: CODENIB_DEMO_CONFIG)")
    parser.add_argument(
        "--attempt", action="append", default=[], help="saved Wiki id (repeatable)"
    )
    parser.add_argument(
        "--reason",
        action="append",
        default=[],
        help="missing-map reasons to repair (default: index_failed, not_indexed)",
    )
    parser.add_argument("--dry-run", action="store_true", help="only list the Wikis")
    args = parser.parse_args(argv)

    path = (
        Path(load_config(args.config).data_dir).absolute()
        / "wiki_cache"
        / "visitor_wiki.sqlite3"
    )
    if not path.is_file():
        parser.error(f"no visitor Wiki store at {path}")
    wikis = VisitorWikis(SQLiteWikiStore(path))
    reasons = tuple(args.reason) or ("index_failed", "not_indexed")
    targets = [
        found
        for found in wikis.missing_graph_views(reasons)
        if not args.attempt or found["id"] in args.attempt
    ]
    failed = 0
    for found in targets:
        label = f"{found['id'][:8]} {found['repository']} ({found['reason']})"
        if args.dry_run:
            print(label)
            continue
        try:
            coverage = wikis.backfill_graph_views(found["id"])
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest
            failed += 1
            print(f"{label}: failed ({type(exc).__name__}: {exc})")
            continue
        if coverage.get("available"):
            print(
                f"{label}: {coverage.get('files')} files, "
                f"{coverage.get('nodes')} symbols, {coverage.get('edges')} edges"
            )
        else:
            failed += 1
            print(f"{label}: still unavailable ({coverage.get('reason')})")
    if not targets:
        print("No saved Wiki is missing a code map for", ", ".join(reasons))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
