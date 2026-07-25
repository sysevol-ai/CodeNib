#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0
"""Apply post-fix to flagged rows in an existing synthesized-queries JSON.

Reads ``--input`` JSON, finds rows with ``judge_verdict='regenerate'``, asks
Claude to fix each one with the judge's complaint as feedback, re-judges, and
writes the modified rows back to ``--input`` in place (or to ``--output``).

Example
-------
    python scripts/postfix_flagged_file.py \\
        --input synthesis_output_per_language/Go.json \\
        --model opus --judge-model opus
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _post_fix import post_fix_flagged  # noqa: E402


async def _main(args: argparse.Namespace) -> None:
    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser() if args.output else input_path
    rows = json.loads(input_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(rows)} rows from {input_path}")

    rows, stats = await post_fix_flagged(
        rows,
        model=args.model,
        judge_model=args.judge_model,
        max_retries=args.max_retries,
    )

    output_path.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n--- Post-fix summary ---")
    print(f"Flagged at start:            {stats['flagged']}")
    print(f"Promoted at triage re-judge: {stats['promoted_at_triage']}")
    print(f"Fixed via 'fix' mode:        {stats['fixed_via_fix']}")
    print(f"Fixed via 'regenerate' mode: {stats['fixed_via_regenerate']}")
    print(f"Still flagged:               {stats['still_flagged']}")
    print(f"Wrote: {output_path}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--input", required=True, type=str)
    p.add_argument(
        "--output",
        default=None,
        type=str,
        help="Where to write fixed rows. Defaults to overwriting --input.",
    )
    p.add_argument("--model", default="opus", help="Model for the fixer.")
    p.add_argument("--judge-model", default="opus", help="Model for re-judging.")
    p.add_argument("--max-retries", type=int, default=3)
    return p


if __name__ == "__main__":
    asyncio.run(_main(build_parser().parse_args()))
