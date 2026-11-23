# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The prompt for one task: each annotation with what surrounds it.

For each annotation the agent gets the page and section it sits on, the words
the person selected and the passage around them, the code that section cites,
the thread so far, and the standing notes on those pages. It answers with one
result per annotation, checked against ``RESULT_SCHEMA``.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .model import Annotation, AnnotationStore

PageLookup = Callable[[str], Optional[Dict[str, Any]]]

RESULT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "annotation": {
                        "type": "string",
                        "description": "The exact 26-character annotation id",
                    },
                    "answer": {
                        "type": "string",
                        "description": "Markdown for the Wiki reader",
                    },
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "start_line": {"type": "integer"},
                                "end_line": {"type": "integer"},
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["annotation", "answer"],
            },
        }
    },
    "required": ["results"],
}

SYSTEM_PROMPT = """\
You are the coding agent of a person reading the CodeNib Wiki of the \
repository in your working directory. They left annotations on Wiki pages. \
Handle each annotation on its own and return exactly one result per \
annotation, with its exact 26-character id in the annotation field.

- A question: answer it from the code. Read the files you need. Do not edit \
any file.
- A change: make the requested change in the working tree with the file \
tools. Keep it as small as the request allows and never touch .codenib/. In \
the answer, say what you changed and why in two to five sentences.

Write each answer in Markdown for a reader of the Wiki: lead with the answer, \
use short paragraphs, put code identifiers in backticks, no preamble. Name \
the code you rely on as path:start-end in the text and list it in files \
(paths relative to the repository root).

Standing notes come from the people who maintain this code. Never contradict \
them; if a request conflicts with one, say so instead of doing it."""

_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_EVIDENCE = re.compile(r"\]\(#evidence-E(\d+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MAX_SECTION = 2500
_MAX_BLOCK = 1500


def _squash(text: str) -> str:
    return " ".join(text.split())


def plain(markdown: str) -> str:
    """Markdown as a reader sees it, near enough to find a quote in."""

    text = re.sub(r"^```.*$", "", markdown, flags=re.MULTILINE)
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("`", "").replace("|", " ")
    return _squash(text)


def split_sections(markdown: str) -> List[Dict[str, str]]:
    """The page cut at its headings: ``[{"heading", "markdown"}]``."""

    sections: List[Dict[str, str]] = [{"heading": "", "markdown": ""}]
    fenced = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match and len(match.group(1)) >= 2:
            sections.append({"heading": plain(match.group(2)), "markdown": ""})
        sections[-1]["markdown"] += line + "\n"
    return [s for s in sections if s["markdown"].strip()]


def find_section(
    page: Dict[str, Any], annotation: Annotation
) -> Optional[Dict[str, str]]:
    """The section an annotation sits in: by heading, else by its quote."""

    sections = split_sections(str(page.get("markdown") or ""))
    heading = _squash(str(annotation.on.get("section") or "")).lower()
    if heading:
        for section in sections:
            if section["heading"].lower() == heading:
                return section
    quote = _squash(str(annotation.on.get("quote") or "")).lower()
    if quote:
        for section in sections:
            if quote in plain(section["markdown"]).lower():
                return section
    return sections[0] if sections else None


def cited_code(page: Dict[str, Any], markdown: str) -> List[Dict[str, Any]]:
    """The citations a stretch of a page links to, in order, once each."""

    citations = page.get("citations") or []
    found: List[Dict[str, Any]] = []
    seen = set()
    for match in _EVIDENCE.finditer(markdown):
        index = int(match.group(1)) - 1
        if 0 <= index < len(citations) and index not in seen:
            seen.add(index)
            found.append(citations[index])
    return found


def _cite(citation: Dict[str, Any]) -> str:
    path = citation.get("file") or citation.get("path") or "?"
    start, end = citation.get("start_line"), citation.get("end_line")
    where = (
        f"{path}:{start}-{end}"
        if start and end and end != start
        else (f"{path}:{start}" if start else str(path))
    )
    name = citation.get("node_name")
    return f"{where} ({name})" if name else where


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def standing_notes(
    store: AnnotationStore, pages: Iterable[str], files: Iterable[str]
) -> List[str]:
    """Notes on these pages, and guard notes anchored in the code they cite."""

    pages, files = set(pages), set(files)
    notes = [
        f"- {_squash(a.text)} (note on page {a.page})"
        for a in store.list()
        if a.kind == "note" and a.status != "resolved" and a.page in pages
    ]
    try:
        from ..guard.notes import load_notes

        guard_notes, _ = load_notes(store.repo)
    except Exception:  # noqa: BLE001 - notes are context, never a reason to fail
        guard_notes = []
    for note in guard_notes:
        if any(anchor.path in files for anchor in note.anchors):
            symbols = ", ".join(anchor.symbol for anchor in note.anchors)
            notes.append(f"- [{note.kind}] {_squash(note.text)} (on {symbols})")
    return notes


def build_prompt(
    annotations: Sequence[Annotation],
    store: AnnotationStore,
    page_lookup: PageLookup,
    *,
    repo_name: str = "",
    retry_note: str = "",
) -> str:
    parts: List[str] = []
    if retry_note:
        parts.append(retry_note)
    parts.append(
        f"Annotations on the CodeNib Wiki of {repo_name or 'this repository'}: "
        f"{len(annotations)} in this task."
    )
    pages_seen: Dict[str, Optional[Dict[str, Any]]] = {}
    cited_files: set = set()
    for annotation in annotations:
        page_id = annotation.page
        if page_id and page_id not in pages_seen:
            try:
                pages_seen[page_id] = page_lookup(page_id)
            except Exception:  # noqa: BLE001 - a missing page leaves the quote
                pages_seen[page_id] = None
        page = pages_seen.get(page_id) if page_id else None
        lines = [f"## Annotation {annotation.id} ({annotation.kind})"]
        title = str((page or {}).get("title") or page_id or "")
        on_code = bool(annotation.on.get("path"))
        # An annotation on code is about that code, not a section of the page.
        section = find_section(page, annotation) if page and not on_code else None
        where = f"Page {title!r}" if title else "The Wiki"
        heading = (section or {}).get("heading") or annotation.on.get("section")
        if heading:
            where += f", section {heading!r}"
        lines.append(where + ".")
        if annotation.on.get("path"):
            span = annotation.on.get("start_line")
            end = annotation.on.get("end_line")
            lines.append(
                f"On the code {annotation.on['path']}"
                + (f":{span}-{end}" if span and end else "")
            )
        quote = annotation.on.get("quote")
        if quote and on_code:
            lines.append(
                "They selected this code:\n```\n" + _clip(quote, 1500) + "\n```"
            )
        elif quote:
            lines.append("They selected:\n" + _quote(_clip(quote, 600)))
        block = annotation.on.get("block")
        if block and _squash(block) != _squash(quote or ""):
            lines.append("In this passage:\n" + _quote(_clip(block, _MAX_BLOCK)))
        if section:
            lines.append(
                "The section reads:\n"
                + _quote(_clip(section["markdown"], _MAX_SECTION))
            )
            code = cited_code(page or {}, section["markdown"])
            if code:
                lines.append(
                    "Code this section cites:\n"
                    + "\n".join(f"- {_cite(c)}" for c in code[:12])
                )
                cited_files.update(str(c.get("file") or "") for c in code)
        thread = store.thread(annotation.id)[:-1]
        if thread:
            lines.append(
                "Earlier in this thread:\n"
                + "\n".join(
                    f"- {'agent' if a.author == 'agent' else 'person'} ({a.kind}): "
                    + _clip(_squash(a.text), 800)
                    for a in thread
                )
            )
        ask = (
            "Their question"
            if annotation.kind == "question"
            else "The change they ask for"
        )
        lines.append(f"{ask}:\n{annotation.text.strip()}")
        parts.append("\n\n".join(lines))
    notes = standing_notes(store, pages_seen, cited_files)
    if notes:
        parts.append("## Standing notes\n" + "\n".join(notes))
    ids = ", ".join(a.id for a in annotations)
    parts.append(f"Return one result for each of: {ids}.")
    return "\n\n".join(parts) + "\n"
