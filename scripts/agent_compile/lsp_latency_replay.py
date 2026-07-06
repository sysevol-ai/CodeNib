#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Replay dynamic LSP-route calls against the same static graph backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeminer.eval.agent_runner import write_lsp_route_latency_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cells-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--prebuilt-dir", default="/mnt/data/codeminer")
    args = parser.parse_args(argv)

    markdown = write_lsp_route_latency_report(
        cells_dir=args.cells_dir,
        output_dir=args.output_dir,
        prebuilt_root=args.prebuilt_dir,
    )
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
