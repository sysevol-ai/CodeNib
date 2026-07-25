#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run the ClaudeLocAgent baseline (Sub-issue #150) over a benchmark dataset.

Setup:
    pip install claude-agent-sdk>=0.2.82
    export ANTHROPIC_API_KEY=...   # or use a logged-in `claude` CLI

Usage:
    # Full codenib-base corpus (100 instances), append-only JSONL
    python examples/claude_loc_agent.py \\
        --dataset codenib_base \\
        --result-path .ttmp/baselines/claude_codenib_base.jsonl

    # Filter to a single instance (smoke):
    python examples/claude_loc_agent.py \\
        --dataset codenib_base \\
        --filter-instance "^fmtlib__fmt-2317$" \\
        --result-path .ttmp/baselines/claude_smoke.jsonl

    # Resume after a crash -- skip instance_ids already in the result file:
    python examples/claude_loc_agent.py \\
        --dataset codenib_base \\
        --result-path .ttmp/baselines/claude_codenib_base.jsonl \\
        --resume

Output: JSONL, one record per instance with retrieve_rerank-compatible
``metric_k_files`` / ``metric_k_node_ids`` plus the full agent ``locations``
and ``usage``.
"""

import argparse
import asyncio

from codenib.clients.claude_agent import ClaudeLocAgent
from codenib.eval.agent_runner.loc_baseline import add_common_args, run_agent_baseline
from codenib.log_utils import get_logger

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run ClaudeLocAgent baseline on a benchmark dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_args(parser)
    parser.add_argument(
        "--model",
        type=str,
        default="sonnet",
        help="Claude model alias passed to claude_agent_sdk (e.g. sonnet, opus).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=100,
        help="Hard cap on agent turns (claude_agent_sdk max_turns).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logger.info(
        "ClaudeLocAgent baseline | dataset=%s model=%s max_turns=%d",
        args.dataset,
        args.model,
        args.max_turns,
    )

    def factory():
        return ClaudeLocAgent(model=args.model, max_turns=args.max_turns)

    rc = asyncio.run(
        run_agent_baseline(
            args,
            agent_factory=factory,
            agent_name="claude",
            model_label=args.model,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
