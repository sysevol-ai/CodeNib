#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Developer-only entry point for the H1 hybrid-index experiment."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.experimental.hybrid_index import IndexRepository  # noqa: E402


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _publish(args: argparse.Namespace) -> int:
    result = IndexRepository.open(args.store).publish_bm25(
        args.artifact,
        ref_name=args.ref,
        expected_revision=args.expected_revision,
    )
    print(
        f"published {result.repository}:{result.ref_name}@{result.ref_revision} "
        f"{result.snapshot_id}"
    )
    return 0


def _export(args: argparse.Namespace) -> int:
    repository = IndexRepository.open(args.store)
    closure = repository.resolve_ref(args.repository, args.ref)
    artifact = repository.materialize_snapshot(
        closure.snapshot.snapshot_id,
        args.output,
    )
    print(f"exported {closure.snapshot.snapshot_id} to {artifact.root}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="source-checkout-only hybrid index persistence experiment"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    publish = commands.add_parser(
        "publish-bm25",
        help="publish one verified portable BM25 artifact",
    )
    publish.add_argument("artifact")
    publish.add_argument("--store", required=True)
    publish.add_argument("--ref", default="main")
    publish.add_argument(
        "--expected-revision",
        type=_nonnegative_int,
        default=0,
    )
    publish.set_defaults(handler=_publish)

    export = commands.add_parser(
        "export-ref",
        help="materialize one ref as a portable context artifact",
    )
    export.add_argument("repository")
    export.add_argument("output")
    export.add_argument("--store", required=True)
    export.add_argument("--ref", default="main")
    export.set_defaults(handler=_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
