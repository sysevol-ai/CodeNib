#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render or check the language capability matrix from the registry."""

from __future__ import annotations

import argparse
import html
import importlib.util
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_LANGUAGES_PATH = _PROJECT_ROOT / "codenib" / "languages.py"

BEGIN_MARKER = "<!-- BEGIN CODENIB_LANGUAGE_CAPABILITIES -->"
END_MARKER = "<!-- END CODENIB_LANGUAGE_CAPABILITIES -->"
LANDING_BEGIN_MARKER = "<!-- BEGIN CODENIB_LANGUAGE_STRIP -->"
LANDING_END_MARKER = "<!-- END CODENIB_LANGUAGE_STRIP -->"
_ICONS = _PROJECT_ROOT / "landing" / "assets" / "languages"
# Icon file and hover color for each language; colors are tuned to read on dark.
_LANDING_LANGUAGES = {
    "Python": ("python", "#ffd845"),
    "Go": ("go", "#00add8"),
    "Rust": ("rust", "#dea584"),
    "C/C++": ("cplusplus", "#659ad2"),
    "C#": ("csharp", "#a97bdc"),
    "Java": ("java", "#f0605f"),
    "Ruby": ("ruby", "#e8534a"),
    "PHP": ("php", "#8f9ed1"),
    "Kotlin": ("kotlin", "#b98cff"),
    "Swift": ("swift", "#f77d4e"),
    "Scala": ("scala", "#e8534a"),
    "Lua": ("lua", "#7d8cff"),
    "JavaScript": ("javascript", "#f0db4f"),
    "TypeScript": ("typescript", "#4a9be0"),
}


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


def _landing_item(name: str, tag: str) -> str:
    icon, color = _LANDING_LANGUAGES[name]
    return (
        f'<li class="lang-item lang-{tag.split()[-1]}" style="--brand:{color}">'
        f'<svg aria-hidden="true"><use href="#lang-{icon}"/></svg>'
        f'<span class="lang-name">{html.escape(name)}</span>'
        f'<span class="lang-tag">{tag}</span></li>'
    )


def _landing_sprite(names: list[str]) -> str:
    symbols = []
    for name in names:
        icon = _LANDING_LANGUAGES[name][0]
        source = (_ICONS / f"{icon}.svg").read_text(encoding="utf-8")
        path = re.search(r'<path d="[^"]+"/>', source).group(0)
        symbols.append(
            f'<symbol id="lang-{icon}" viewBox="0 0 128 128">{path}</symbol>'
        )
    return f'<svg class="lang-sprite" aria-hidden="true">{"".join(symbols)}</svg>'


def render_landing_strip() -> str:
    """Return the homepage's scrolling language strip, sorted from the registry."""

    rows = _language_capability_rows()
    with_graph = [row for row in rows if row.graph_backend]
    tagged = [
        *((r.display_name, "live graph") for r in with_graph if r.incremental_backend),
        *((r.display_name, "graph") for r in with_graph if not r.incremental_backend),
        *((r.display_name, "search") for r in rows if not r.graph_backend),
    ]
    items = "".join(_landing_item(name, tag) for name, tag in tagged)
    lines = [
        '<div class="section-head">',
        f'  <h2 id="languages-title">{len(rows)} languages, '
        f'<span class="dim">{len(with_graph)} with a code graph.</span></h2>',
        '  <p class="section-sub">See what CodeNib can do with yours, from a graph '
        "that updates as files change to plain code search.</p>",
        "</div>",
        _landing_sprite([row.display_name for row in rows]),
        '<div class="lang-marquee" role="region" aria-labelledby="languages-title">',
        '  <div class="lang-track">',
        f'    <ul class="lang-list">{items}</ul>',
        f'    <ul class="lang-list" aria-hidden="true">{items}</ul>',
        "  </div>",
        "</div>",
    ]
    return "\n".join(f"      {line}" for line in lines)


def replace_landing_strip(text: str, strip: str) -> str:
    """Replace the generated strip between the homepage's markers."""

    before, begin, tail = text.partition(LANDING_BEGIN_MARKER)
    if not begin:
        raise ValueError(f"missing marker: {LANDING_BEGIN_MARKER}")
    _, end, after = tail.partition(LANDING_END_MARKER)
    if not end:
        raise ValueError(f"missing marker: {LANDING_END_MARKER}")
    return f"{before}{begin}\n{strip}\n      {end}{after}"


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
        "--write-landing", type=Path, help="Update the homepage's language strip"
    )
    parser.add_argument(
        "--check-landing",
        type=Path,
        help="Fail if the homepage's language strip is out of date",
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
            replace_landing_strip(text, render_landing_strip()), encoding="utf-8"
        )
        return 0

    if args.check_landing:
        path = args.check_landing
        actual = path.read_text(encoding="utf-8")
        if actual != replace_landing_strip(actual, render_landing_strip()):
            print(f"{path} language strip is out of date", file=sys.stderr)
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
