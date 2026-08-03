#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render or check the language capability matrix from the registry."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_LANGUAGES_PATH = _PROJECT_ROOT / "codenib" / "languages.py"

BEGIN_MARKER = "<!-- BEGIN CODENIB_LANGUAGE_CAPABILITIES -->"
END_MARKER = "<!-- END CODENIB_LANGUAGE_CAPABILITIES -->"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _language_capability_rows():
    spec = importlib.util.spec_from_file_location(
        "_codenib_languages_for_matrix", _LANGUAGES_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {_LANGUAGES_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.language_capability_rows()


def render_matrix() -> str:
    """Return the Markdown table for the current language registry."""

    lines = [
        "| Language | Chunker | GT | Agent | Graph backend | SCIP cold-start | Incremental "
        "| LSP command | Core decoder | Core parity |",
        "|----------|---------|----|-------|---------------|-----------------|-------------|-------------|--------------|-------------|",
    ]
    for row in _language_capability_rows():
        lines.append(
            "| "
            + " | ".join(
                [
                    row.display_name,
                    _yes_no(row.chunker),
                    _yes_no(row.ground_truth),
                    _yes_no(row.agent),
                    row.graph_backend or "none",
                    row.scip_cold_start,
                    row.incremental_backend or "none",
                    _yes_no(row.lsp),
                    _yes_no(row.core_decoder),
                    row.core_parity,
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def render_block() -> str:
    """Return the full generated Markdown block including markers."""

    return "\n".join([BEGIN_MARKER, render_matrix(), END_MARKER])


def replace_block(text: str, block: str) -> str:
    """Replace the generated block in ``text`` with ``block``."""

    before, begin, tail = text.partition(BEGIN_MARKER)
    if not begin:
        raise ValueError(f"missing marker: {BEGIN_MARKER}")
    _, end, after = tail.partition(END_MARKER)
    if not end:
        raise ValueError(f"missing marker: {END_MARKER}")
    return before.rstrip() + "\n\n" + block + after


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render/check docs/language_capabilities.md from LanguageSpec."
    )
    parser.add_argument(
        "--write", type=Path, help="Update the generated block in a file"
    )
    parser.add_argument(
        "--check",
        type=Path,
        help="Fail if the generated block in a file is out of date",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    block = render_block()

    if args.write and args.check:
        raise SystemExit("--write and --check are mutually exclusive")

    if args.write:
        path = args.write
        path.write_text(
            replace_block(path.read_text(encoding="utf-8"), block), encoding="utf-8"
        )
        return 0

    if args.check:
        path = args.check
        expected = replace_block(path.read_text(encoding="utf-8"), block)
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            print(f"{path} language capability matrix is out of date", file=sys.stderr)
            print(
                f"Run: python {Path(__file__).as_posix()} --write {path}",
                file=sys.stderr,
            )
            return 1
        return 0

    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
