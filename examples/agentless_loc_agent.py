#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run classic Agentless localization with the common benchmark runner."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from codenib.clients.agentless_agent import AgentlessAgent
from codenib.eval.agent_runner.loc_baseline import add_common_args, run_agent_baseline
from codenib.log_utils import get_logger

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Agentless over CodeNib views on a benchmark dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_args(parser)
    parser.add_argument(
        "--model",
        required=True,
        help="OpenAI-compatible model name used by the Agentless policy.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "Fixed manifest for a single-snapshot run. Multi-repository runs "
            "resolve each manifest from CODENIB_HOME."
        ),
    )
    parser.add_argument("--top-n-files", type=int, default=3)
    parser.add_argument("--context-window", type=int, default=10)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--max-completion-tokens", type=int, default=300)
    parser.add_argument(
        "--base-url",
        default=None,
        help="Optional OpenAI-compatible endpoint, including a LiteLLM proxy.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger.info(
        "Agentless baseline | dataset=%s model=%s top_n_files=%d",
        args.dataset,
        args.model,
        args.top_n_files,
    )

    def factory() -> AgentlessAgent:
        return AgentlessAgent(
            model=args.model,
            manifest_path=args.manifest,
            top_n_files=args.top_n_files,
            context_window=args.context_window,
            max_retries=args.max_retries,
            max_completion_tokens=args.max_completion_tokens,
            base_url=args.base_url,
        )

    rc = asyncio.run(
        run_agent_baseline(
            args,
            agent_factory=factory,
            agent_name="agentless-codenib",
            model_label=args.model,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
