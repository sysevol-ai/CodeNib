# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Stage 1 of the agent wiki pipeline: a high-level *conceptual* outline.

Instead of one page per directory, an LLM reads the repo's README, its salient
files, and its top symbols and proposes a DeepWiki-style **subsystem** table of
contents (e.g. "Request Pipeline", "Interceptors", "Adapters & Transports") —
concepts, not paths. Each proposed page carries ``keywords`` + ``files`` that
stage 2 uses to retrieve the relevant code and write the page.

This module only produces the outline (the page tree); per-page generation is
a separate stage so the outline can be reviewed first.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from ..log_utils import get_logger
from .builder import Symbol, WikiBuilder
from .narrator import _no_thinking_kwargs

logger = get_logger(__name__)

_README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")


def _read_readme(repo_dir: str, limit: int = 3500) -> str:
    for name in _README_NAMES:
        path = os.path.join(repo_dir, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    return fh.read()[:limit]
            except OSError:
                return ""
    return ""


def _top_symbols(symbols: List[Symbol], limit: int = 70) -> List[Symbol]:
    """Rank symbols by API surface (line count), keeping the largest."""
    return sorted(symbols, key=lambda s: s.lines, reverse=True)[:limit]


_OUTLINE_PROMPT = """\
You are documenting the {repo} codebase like a senior engineer writing a \
developer wiki for new contributors.

Propose a HIGH-LEVEL, CONCEPTUAL table of contents — organized by \
SUBSYSTEM / CAPABILITY, never by directory or file. Think titles like \
"Request Pipeline", "Interceptors", "Adapters & Transports", "Configuration", \
"Error Handling" — the mental model someone needs to understand the system.

Repository: {repo}   (languages: {languages})

README (excerpt):
{readme}

Salient files:
{files}

Top symbols (name — file — kind):
{symbols}

Return ONLY JSON, no prose, of exactly this shape. Aim for 8-14 top-level pages \
that together cover the WHOLE system, and give the major subsystems 1-3 children \
so the tree is genuinely TWO LEVELS deep — not a flat list. Start with "Overview".
{{"pages":[{{"id":"kebab-case-id","title":"Concept Title","summary":"1-2 \
sentences on what this page explains","keywords":["specific","symbol or \
feature","search","terms"],"files":["likely/relevant/path.ext"],"children":[\
{{"id":"child-id","title":"Sub-topic","summary":"...","keywords":["..."],\
"files":["..."],"children":[]}}]}}]}}

Rules:
- Structure it like a senior engineer's onboarding docs: top-level = major areas; \
children = specific capabilities or flows inside them (e.g. an "I/O" page with \
"FITS", "ASCII", "Registry" children; a "Table System" page with "Operations").
- Cover not only domain subsystems but the cross-cutting pages a new contributor \
needs, WHEN APPLICABLE: Getting Started / Installation, Project Structure, \
Configuration, Testing, Extensibility / Plugins.
- Titles are CONCEPTS, not paths (no "lib/core", no file names as titles).
- keywords drive a code search, so make them specific symbol/feature terms.
- Cover the whole system, not just a few files; prefer depth over a flat list.
"""


def _format_symbols(symbols: List[Symbol]) -> str:
    return "\n".join(f"- {s.name} — {s.file} — {s.type}" for s in symbols)


def generate_outline(bundle: Any, model: str, max_tokens: int = 4096) -> Dict[str, Any]:
    """Produce a conceptual wiki page tree for *bundle* using *model*.

    Returns ``{"pages": [...]}``. On a model/parse failure returns
    ``{"pages": [], "raw": <text>, "error": <msg>}`` so callers can inspect.
    """
    wb = WikiBuilder(bundle)
    symbols = list(wb._symbols())
    files = wb._salient_files(limit=40)
    readme = _read_readme(getattr(bundle.entry, "repo_dir", "") or "")
    languages = ", ".join(getattr(bundle.manifest, "languages", []) or [])

    prompt = _OUTLINE_PROMPT.format(
        repo=getattr(bundle.entry, "repo", "this repository"),
        languages=languages or "unknown",
        readme=readme or "(no README found)",
        files="\n".join(f"- {f}" for f in files) or "(none)",
        symbols=_format_symbols(_top_symbols(symbols)) or "(none)",
    )

    try:
        import litellm

        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.2,
            **_no_thinking_kwargs(model),
        )
        text = resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - surface a clean failure to caller
        logger.warning("outline generation failed (%s): %s", model, exc)
        return {"pages": [], "error": str(exc)}

    return _parse_outline(text)


def _parse_outline(text: str) -> Dict[str, Any]:
    """Extract the JSON object from a model response (tolerant of code fences)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {"pages": [], "raw": text}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {"pages": [], "raw": text, "error": f"json: {exc}"}
    if not isinstance(data.get("pages"), list):
        return {"pages": [], "raw": text}
    return data
