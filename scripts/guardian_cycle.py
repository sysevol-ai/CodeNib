#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0
"""Run one Repository Guardian cycle over a repo and emit a dated report.

Phase 1 skeleton: sync -> index -> observe (churn [+ tests]) -> investigate ->
report. Non-modifying: writes only the report, never the target repo.

Usage:
    python scripts/guardian_cycle.py .
    python scripts/guardian_cycle.py /path/to/repo --output-dir ./guardian_out
    python scripts/guardian_cycle.py . --run-tests --top-n 5 --since "30 days ago"
    python scripts/guardian_cycle.py . --no-investigate   # churn-only, no retrieval
"""

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout, format="%(levelname)-8s %(message)s"
)

from codeminer.guardian import GuardianConfig, render_markdown, run_cycle


def _normalize_languages(value: str) -> list[str]:
    import re

    langs = [t.strip().lower() for t in re.split(r"[,/]", value or "") if t.strip()]
    return langs or ["python"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Repository Guardian cycle")
    parser.add_argument("repo_dir", help="Absolute or relative path to the repo")
    parser.add_argument(
        "--output-dir",
        default="guardian_out",
        help="Directory for the report (default: guardian_out)",
    )
    parser.add_argument(
        "--language",
        default="python",
        help="Comma/slash-separated languages to index (default: python)",
    )
    parser.add_argument(
        "--top-n", type=int, default=10, help="Max churn hotspots (default: 10)"
    )
    parser.add_argument(
        "--since",
        default="90 days ago",
        help='Churn window as a git --since expression (default: "90 days ago")',
    )
    parser.add_argument(
        "--retrieval-top-k",
        type=int,
        default=5,
        help="Evidence locations per hotspot (default: 5)",
    )
    parser.add_argument(
        "--run-tests",
        action="store_true",
        help="Also run the target's pytest suite and report failures",
    )
    parser.add_argument(
        "--no-investigate",
        dest="investigate",
        action="store_false",
        help="Skip retrieval evidence (churn-only, no embeddings)",
    )
    parser.add_argument(
        "--index-cache-dir",
        default=None,
        help="Index cache dir (default: <repo>/.codeminer_cache)",
    )
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.isdir(repo_dir):
        print(f"ERROR: Directory not found: {repo_dir}")
        sys.exit(1)

    languages = _normalize_languages(args.language)
    # Investigation needs the embedding index; build it only when investigating.
    index_types = ("bm25", "vector") if args.investigate else ("bm25",)

    config = GuardianConfig(
        repo_path=repo_dir,
        languages=languages,
        index_cache_dir=args.index_cache_dir,
        index_types=index_types,
        top_n=args.top_n,
        since=args.since,
        run_tests=args.run_tests,
        investigate=args.investigate,
        retrieval_top_k=args.retrieval_top_k,
    )

    print(f"Running Guardian cycle on {repo_dir} (languages: {', '.join(languages)})")
    report = run_cycle(config)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    md_path = out_dir / f"guardian_report_{stamp}.md"
    json_path = out_dir / f"guardian_report_{stamp}.json"

    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n{len(report.findings)} finding(s).")
    print(f"Report:  {md_path}")
    print(f"JSON:    {json_path}")


if __name__ == "__main__":
    main()
