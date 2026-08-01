#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Run OrcaLoca with CodeNib views through the generic baseline runner.

OrcaLoca and LlamaIndex remain external dependencies. Install the pinned
OrcaLoca revision, or pass its checkout with ``--orcaloca-checkout``.
Each evaluated checkout must already have a graph-enabled CodeNib manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from codenib.clients.orcaloca_agent import OrcaLocaAgent
from codenib.eval.agent_runner.loc_baseline import add_common_args, run_agent_baseline
from codenib.log_utils import get_logger

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run OrcaLoca over CodeNib views on a benchmark dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_args(parser)
    parser.add_argument(
        "--model",
        required=True,
        help="OpenAI-compatible model name passed to LlamaIndex.",
    )
    parser.add_argument(
        "--orcaloca-checkout",
        type=Path,
        default=None,
        help="Optional OrcaLoca checkout to prepend to Python's import path.",
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
    parser.add_argument("--config-path", type=Path, default=None)
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument(
        "--base-url",
        default=None,
        help="Optional OpenAI-compatible API base URL passed to LlamaIndex.",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=None,
        help="Register a context window for an OpenAI-compatible model alias.",
    )
    parser.add_argument(
        "--tokenizer-encoding",
        default=None,
        help="Register a tiktoken encoding for an otherwise unknown model alias.",
    )
    return parser.parse_args()


def _add_orcaloca_checkout(checkout: Path | None) -> None:
    if checkout is None:
        return
    root = checkout.expanduser().resolve()
    if not (root / "Orcar" / "search_agent.py").is_file():
        raise FileNotFoundError(f"Not an OrcaLoca checkout: {root}")
    sys.path.insert(0, str(root))


def main() -> None:
    args = parse_args()
    _add_orcaloca_checkout(args.orcaloca_checkout)
    logger.info(
        "OrcaLoca baseline | dataset=%s model=%s max_iterations=%d",
        args.dataset,
        args.model,
        args.max_iterations,
    )

    def factory() -> OrcaLocaAgent:
        return OrcaLocaAgent(
            model=args.model,
            manifest_path=args.manifest,
            max_iterations=args.max_iterations,
            temperature=args.temperature,
            config_path=args.config_path,
            context_window=args.context_window,
            tokenizer_encoding=args.tokenizer_encoding,
            base_url=args.base_url,
        )

    rc = asyncio.run(
        run_agent_baseline(
            args,
            agent_factory=factory,
            agent_name="orcaloca-codenib",
            model_label=args.model,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
