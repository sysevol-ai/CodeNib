#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run the CodexLocAgent baseline (Sub-issue #150) over a benchmark dataset.

Setup:
    # OpenAI's official Codex SDK ships from github.com/openai/codex
    # (not yet on PyPI as of 2026-05). Install from git, pinned to a
    # verified commit. --no-deps + manual cli-bin pin is required because
    # the SDK's pyproject pins to a cli-bin version that isn't published
    # yet (pre-release version skew); same-minor cli-bin is compatible.
    pip install --pre --no-deps \\
        "openai-codex @ git+https://github.com/openai/codex.git@\\
e389e01f8347a4861ed139b01956fe64aa0c0fda#subdirectory=sdk/python"
    pip install --pre "openai-codex-cli-bin==0.131.0a8"
    codex login   # writes ~/.codex/auth.json
    # or: export CODEX_API_KEY=...

Usage:
    # Full codeminer-base corpus (100 instances), append-only JSONL
    python examples/codex_loc_agent.py \\
        --dataset codeminer_base \\
        --result-path .ttmp/baselines/codex_codeminer_base.jsonl

    # Filter to a single instance (smoke):
    python examples/codex_loc_agent.py \\
        --dataset codeminer_base \\
        --filter-instance "^fmtlib__fmt-2317$" \\
        --result-path .ttmp/baselines/codex_smoke.jsonl

    # Resume after a crash -- skip instance_ids already in the result file:
    python examples/codex_loc_agent.py \\
        --dataset codeminer_base \\
        --result-path .ttmp/baselines/codex_codeminer_base.jsonl \\
        --resume

Output: JSONL, one record per instance with retrieve_rerank-compatible
``metric_k_files`` / ``metric_k_node_ids`` plus the full agent ``locations``
and ``usage``.
"""

import argparse
import asyncio

from codeminer.clients.codex_agent import CodexLocAgent
from codeminer.eval.loc_agent_runner import add_common_args, run_agent_baseline
from codeminer.log_utils import get_logger

logger = get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run CodexLocAgent baseline on a benchmark dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_args(parser)
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Codex model name; defaults to the codex CLI's configured model.",
    )
    parser.add_argument(
        "--reasoning-effort",
        type=str,
        default="medium",
        choices=["minimal", "low", "medium", "high"],
        help="model_reasoning_effort passed to openai_codex thread config.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logger.info(
        "CodexLocAgent baseline | dataset=%s model=%s reasoning_effort=%s",
        args.dataset,
        args.model or "(cli-default)",
        args.reasoning_effort,
    )

    def factory():
        return CodexLocAgent(
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )

    rc = asyncio.run(
        run_agent_baseline(
            args,
            agent_factory=factory,
            agent_name="codex",
            model_label=args.model,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
