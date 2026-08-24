#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build a deterministic multimodal repository knowledge bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from codenib.wiki import build_multimodal_repository_knowledge  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="Repository root to scan")
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Path to write the multimodal knowledge bundle JSON",
    )
    parser.add_argument(
        "--commit",
        default=None,
        help="Optional commit identity to record in the media manifest",
    )
    parser.add_argument(
        "--exclude-root",
        action="append",
        default=[],
        help="Path to exclude from repository discovery; may be repeated",
    )
    parser.add_argument(
        "--max-artifacts",
        type=int,
        default=4096,
        help="Maximum media artifacts to include",
    )
    parser.add_argument(
        "--max-source-candidates",
        type=int,
        default=8192,
        help="Maximum source-symbol candidates to consider for grounding",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser()
    if not repo.exists():
        parser.error(f"repository root does not exist: {repo}")
    if not repo.is_dir():
        parser.error(f"repository root is not a directory: {repo}")
    bundle = build_multimodal_repository_knowledge(
        repo,
        commit=args.commit,
        exclude_roots=tuple(args.exclude_root),
        max_artifacts=args.max_artifacts,
        max_source_candidates=args.max_source_candidates,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    counts = {
        "media_artifacts": bundle["media_manifest"]["artifact_count"],
        "visual_fact_packs": bundle["visual_facts_manifest"]["fact_count"],
        "source_candidates": bundle["source_candidate_count"],
        "visual_code_bindings": bundle["grounding_manifest"]["binding_count"],
        "knowledge_entries": bundle["knowledge_view"]["entry_count"],
    }
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
