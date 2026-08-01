#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run LocAgent's policy over CodeNib views with the common benchmark runner.

The pinned LocAgent checkout supplies the prompts and function schemas.
CodeNib supplies all repository tools from an existing graph-enabled manifest.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from codenib.clients.locagent_agent import LocAgentAgent
from codenib.eval.agent_runner.loc_baseline import add_common_args, run_agent_baseline
from codenib.log_utils import get_logger

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run LocAgent over CodeNib views on a benchmark dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_args(parser)
    parser.add_argument(
        "--model",
        required=True,
        help="OpenAI-compatible model name used by the LocAgent policy.",
    )
    parser.add_argument(
        "--locagent-checkout",
        type=Path,
        default=None,
        help="Pinned LocAgent checkout (or set LOCAGENT_CHECKOUT).",
    )
    parser.add_argument(
        "--locagent-python",
        type=Path,
        default=None,
        help="Python executable with the pinned LocAgent policy dependencies.",
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
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--max-completion-tokens", type=int, default=4096)
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="Optional OpenAI-compatible reasoning effort.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Optional OpenAI-compatible endpoint, including a LiteLLM proxy.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger.info(
        "LocAgent baseline | dataset=%s model=%s max_iterations=%d",
        args.dataset,
        args.model,
        args.max_iterations,
    )

    def factory() -> LocAgentAgent:
        return LocAgentAgent(
            model=args.model,
            locagent_checkout=args.locagent_checkout,
            locagent_python=args.locagent_python,
            manifest_path=args.manifest,
            max_iterations=args.max_iterations,
            max_completion_tokens=args.max_completion_tokens,
            reasoning_effort=args.reasoning_effort,
            base_url=args.base_url,
        )

    rc = asyncio.run(
        run_agent_baseline(
            args,
            agent_factory=factory,
            agent_name="locagent-codenib",
            model_label=args.model,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
