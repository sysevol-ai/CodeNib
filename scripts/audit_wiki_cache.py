#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Report current AgentWiki cache coverage without generating any pages."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.log_utils import set_console_log_level  # noqa: E402
from codenib.web.config import load_config  # noqa: E402
from codenib.web.native_authority import authorize_local_manifest_vector  # noqa: E402
from codenib.web.repo_registry import RepoRegistry  # noqa: E402
from codenib.wiki.cache_audit import audit_wiki_cache  # noqa: E402
from codenib.wiki.sqlite_store import SQLiteWikiStore  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        help="QA config path; defaults to CODENIB_DEMO_CONFIG or qa_config.yaml",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Audit one repository id; repeat to select more than one",
    )
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    parser.add_argument(
        "--require-overviews",
        action="store_true",
        help="Exit non-zero when an outline or Overview page is not cached",
    )
    parser.add_argument(
        "--fail-on-fallback",
        action="store_true",
        help="Exit non-zero when a current page uses a diagnostic fallback",
    )
    parser.add_argument(
        "--fail-on-quality-invalid",
        action="store_true",
        help="Exit non-zero when a current cached page fails its quality gate",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    set_console_log_level("WARNING")
    config = load_config(args.config)
    registry = RepoRegistry(
        config,
        native_index_authorization_resolver=authorize_local_manifest_vector,
        allow_missing_native_index_authorization=True,
    )
    registry.load_all()
    cache_dir = os.path.join(os.path.abspath(config.data_dir), "wiki_cache")
    database_path = Path(cache_dir) / "wiki.sqlite3"
    store = SQLiteWikiStore(database_path) if database_path.exists() else None
    report = audit_wiki_cache(
        registry,
        model=config.wiki_generation_model,
        repo_ids=args.repos,
        store=store,
    )
    print(
        json.dumps(
            report,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
            sort_keys=True,
        )
    )
    failed = bool(
        (
            args.require_overviews
            and (report["missing_outlines"] or report["missing_overviews"])
        )
        or (args.fail_on_fallback and report["fallback_pages"])
        or (args.fail_on_quality_invalid and report["quality_invalid_pages"])
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
