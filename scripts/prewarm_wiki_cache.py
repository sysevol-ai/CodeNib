#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Prewarm bounded Wiki page scopes and report generation quality/latency."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.llm.litellm_chat import LiteLLMChat  # noqa: E402
from codenib.log_utils import set_console_log_level  # noqa: E402
from codenib.web.config import load_config  # noqa: E402
from codenib.web.native_authority import authorize_local_manifest_vector  # noqa: E402
from codenib.web.repo_registry import RepoRegistry  # noqa: E402
from codenib.wiki.agent_wiki import AgentWiki  # noqa: E402
from codenib.wiki.prewarm import prewarm_wiki_cache  # noqa: E402
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
        help="Prewarm one repository id; repeat to select more than one",
    )
    parser.add_argument(
        "--scope",
        choices=("overview", "root", "all"),
        default="overview",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--fail-on-error", action="store_true")
    parser.add_argument("--fail-on-quality-invalid", action="store_true")
    parser.add_argument(
        "--keep-views",
        action="store_true",
        help="Retain loaded retrieval views until this maintenance process exits",
    )
    parser.add_argument(
        "--retry-degraded-now",
        action="store_true",
        help=(
            "Explicitly retry degraded pages now, bypassing their reader-facing "
            "cooldown after an index or model upgrade"
        ),
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
    store = (
        None
        if args.dry_run and not database_path.exists()
        else SQLiteWikiStore(database_path)
    )

    def wiki_factory(bundle):
        llm = LiteLLMChat(
            model=config.wiki_generation_model,
            temperature=0.2,
            max_tokens=4096,
            api_base=config.wiki_generation_api_base,
            api_key=config.wiki_generation_api_key,
            extra_kwargs=config.wiki_generation_options,
        )
        return AgentWiki(
            bundle,
            model=config.wiki_generation_model,
            store=store,
            llm=llm,
            api_base=config.wiki_generation_api_base,
            api_key=config.wiki_generation_api_key,
        )

    report = prewarm_wiki_cache(
        registry,
        wiki_factory=wiki_factory,
        repo_ids=args.repos,
        scope=args.scope,
        workers=args.workers,
        max_pages=args.max_pages,
        dry_run=args.dry_run,
        retry_degraded_now=args.retry_degraded_now,
        release_views=not args.keep_views,
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
            args.fail_on_error
            and any(
                page["status"] in {"error", "release_error"} for page in report["pages"]
            )
        )
        or (
            args.fail_on_quality_invalid
            and any(page.get("quality_valid") is False for page in report["pages"])
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
