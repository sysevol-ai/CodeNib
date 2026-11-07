#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render or check the language capability matrix from the registry."""

from __future__ import annotations

import argparse
import html
import importlib.util
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_LANGUAGES_PATH = _PROJECT_ROOT / "codenib" / "languages.py"

BEGIN_MARKER = "<!-- BEGIN CODENIB_LANGUAGE_CAPABILITIES -->"
END_MARKER = "<!-- END CODENIB_LANGUAGE_CAPABILITIES -->"
LANDING_BEGIN_MARKER = "<!-- BEGIN CODENIB_LANGUAGE_TIERS -->"
LANDING_END_MARKER = "<!-- END CODENIB_LANGUAGE_TIERS -->"


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


def _landing_tier(title: str, detail: str, names: list[str]) -> list[str]:
    items = "".join(f"<li>{html.escape(name)}</li>" for name in names)
    return [
        '  <div class="lang-tier">',
        f"    <div><h3>{title}</h3><p>{detail}</p></div>",
        f'    <ul class="lang-names">{items}</ul>',
        "  </div>",
    ]


def render_landing_tiers() -> str:
    """Return the homepage's language tiers, sorted from the registry."""

    rows = _language_capability_rows()
    with_graph = [row for row in rows if row.graph_backend]
    updated = [row.display_name for row in with_graph if row.incremental_backend]
    rebuilt = [row.display_name for row in with_graph if not row.incremental_backend]
    search = [row.display_name for row in rows if not row.graph_backend]
    lines = [
        '<div class="section-head">',
        f'  <h2 id="languages-title">{len(rows)} languages, '
        f'<span class="dim">{len(with_graph)} with a code graph.</span></h2>',
        '  <p class="section-sub">Find your language to see what CodeNib can do '
        "with it.</p>",
        "</div>",
        '<div class="lang-tiers">',
        *_landing_tier(
            "Code graph, updated as files change",
            "Indexed with SCIP or clangd, then patched through a language server.",
            updated,
        ),
        *_landing_tier(
            "Code graph, rebuilt when you re-index",
            "Indexed with SCIP. Picking up a change takes a fresh index.",
            rebuilt,
        ),
        *_landing_tier(
            "Code search without a graph",
            "Search and source reads work. Definitions and callers need a graph.",
            search,
        ),
        "</div>",
    ]
    return "\n".join(f"      {line}" for line in lines)


def replace_landing_tiers(text: str, tiers: str) -> str:
    """Replace the generated tiers between the homepage's markers."""

    before, begin, tail = text.partition(LANDING_BEGIN_MARKER)
    if not begin:
        raise ValueError(f"missing marker: {LANDING_BEGIN_MARKER}")
    _, end, after = tail.partition(LANDING_END_MARKER)
    if not end:
        raise ValueError(f"missing marker: {LANDING_END_MARKER}")
    return f"{before}{begin}\n{tiers}\n      {end}{after}"


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
    parser.add_argument(
        "--write-landing", type=Path, help="Update the homepage's language tiers"
    )
    parser.add_argument(
        "--check-landing",
        type=Path,
        help="Fail if the homepage's language tiers are out of date",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    block = render_block()

    if args.write and args.check:
        raise SystemExit("--write and --check are mutually exclusive")

    if args.write_landing:
        path = args.write_landing
        text = path.read_text(encoding="utf-8")
        path.write_text(
            replace_landing_tiers(text, render_landing_tiers()), encoding="utf-8"
        )
        return 0

    if args.check_landing:
        path = args.check_landing
        actual = path.read_text(encoding="utf-8")
        if actual != replace_landing_tiers(actual, render_landing_tiers()):
            print(f"{path} language tiers are out of date", file=sys.stderr)
            print(
                f"Run: python {Path(__file__).as_posix()} --write-landing {path}",
                file=sys.stderr,
            )
            return 1
        return 0

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
