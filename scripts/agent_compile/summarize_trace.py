#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Summarize a persisted synthesis sweep cell trace."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.agent_compile.lib.trace_summary import (  # noqa: E402
    load_cell_replay,
    load_cell_trace_summary,
    render_replay_markdown,
    render_summary_markdown,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cell", type=Path, help="Path to a persisted cell JSON file")
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "replay-json", "replay-markdown"),
        default="json",
        help="Output format",
    )
    parser.add_argument(
        "--require-exact-replay",
        action="store_true",
        help=(
            "Return non-zero unless replay_readiness says the cell is exactly "
            "message-replayable. Output is still written first."
        ),
    )
    args = parser.parse_args(argv)

    if args.format == "replay-json":
        payload = load_cell_replay(args.cell)
        print(json.dumps(payload, indent=2, sort_keys=True))
        readiness = payload.get("replay_readiness") or {}
    elif args.format == "replay-markdown":
        payload = load_cell_replay(args.cell)
        print(render_replay_markdown(payload), end="")
        readiness = payload.get("replay_readiness") or {}
    elif args.format == "markdown":
        summary = load_cell_trace_summary(args.cell)
        print(render_summary_markdown(summary), end="")
        readiness = summary.get("replay_readiness") or {}
    else:
        summary = load_cell_trace_summary(args.cell)
        print(json.dumps(summary, indent=2, sort_keys=True))
        readiness = summary.get("replay_readiness") or {}
    if args.require_exact_replay and not readiness.get("exact_message_replayable"):
        blockers = readiness.get("blocking_gaps") or ["not_exact_message_replayable"]
        print(
            "exact replay gate failed: " + ", ".join(str(item) for item in blockers),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
