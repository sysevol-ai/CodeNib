# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""People write the Wiki: page edits, and answers folded into pages.

A Wiki page edited by a person is saved whole as ``.codenib/wiki/<page>.md``,
with front matter naming the generated text it started from. The edited page
is what readers see from then on, and regenerating the Wiki never overwrites
it; when the generated page has changed since, the page says so.

A repository document (README, ``docs/``) is its own source, so editing its
page edits the file itself.

Folding an agent's answer into a page adds it, under the question it answers,
at the end of the section the question was asked on.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..wiki.repo_docs import find_docs, is_doc_id
from .model import AnnotationError, _atomic_write, repo_lock, utc_now
from .prompt import plain, split_sections

WIKI_DIR = Path(".codenib") / "wiki"
_PAGE_ID = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,150}\Z")
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
MAX_MARKDOWN = 400_000


def markdown_hash(text: str) -> str:
    normalized = "\n".join(
        line.rstrip() for line in text.replace("\r\n", "\n").split("\n")
    )
    return hashlib.sha256(normalized.strip().encode("utf-8")).hexdigest()[:16]


@dataclass
class PageEdit:
    page: str
    markdown: str
    base: str  # hash of the generated markdown the edit started from
    edited: str

    def dump(self) -> str:
        meta = {
            "page": self.page,
            "source": "human",
            "base": self.base,
            "edited": self.edited,
        }
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
        return f"---\n{front}\n---\n{self.markdown.strip()}\n"


def _edit_path(repo: Path, page_id: str) -> Path:
    if not _PAGE_ID.match(page_id or "") or is_doc_id(page_id):
        raise AnnotationError(f"no editable page {page_id!r}")
    return Path(repo) / WIKI_DIR / f"{page_id}.md"


def load_edit(repo: Path, page_id: str) -> Optional[PageEdit]:
    try:
        path = _edit_path(repo, page_id)
        text = path.read_text(encoding="utf-8")
    except (AnnotationError, FileNotFoundError):
        return None
    match = _FRONT_MATTER.match(text.replace("\r\n", "\n"))
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    return PageEdit(
        page=page_id,
        markdown=match.group(2).strip(),
        base=str(meta.get("base") or ""),
        edited=str(meta.get("edited") or ""),
    )


def apply_edit(
    repo: Path, page_id: str, page: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The page as readers see it: a person's edit replaces the generated text."""

    if page is None or is_doc_id(page_id):
        return page
    edit = load_edit(repo, page_id)
    if edit is None:
        return page
    generated = str(page.get("markdown") or "")
    return {
        **page,
        "markdown": edit.markdown,
        "edited": {
            "at": edit.edited,
            "path": str(WIKI_DIR / f"{page_id}.md"),
            # The generated page changed since the person edited it.
            "stale": bool(edit.base) and edit.base != markdown_hash(generated),
        },
    }


def _doc_path(repo: Path, page_id: str) -> Path:
    doc = next((d for d in find_docs(repo) if d.id == page_id), None)
    if doc is None:
        raise AnnotationError(f"no document {page_id!r}")
    return Path(repo) / doc.path


def page_source(
    repo: Path, page_id: str, page: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """What an editor opens: the markdown, where it saves, and its version."""

    if is_doc_id(page_id):
        path = _doc_path(repo, page_id)
        text = path.read_text(encoding="utf-8", errors="replace")
        return {
            "markdown": text,
            "base": markdown_hash(text),
            "kind": "document",
            "path": str(path.relative_to(repo)),
            "edited": False,
        }
    if page is None:
        raise AnnotationError(f"no page {page_id!r}")
    shown = apply_edit(repo, page_id, page) or page
    markdown = str(shown.get("markdown") or "")
    return {
        "markdown": markdown,
        "base": markdown_hash(markdown),
        "kind": "page",
        "path": str(WIKI_DIR / f"{page_id}.md"),
        "edited": "edited" in shown,
    }


def save_page(
    repo: Path,
    page_id: str,
    page: Optional[Dict[str, Any]],
    markdown: str,
    base: str,
) -> Dict[str, Any]:
    """Save a person's text, refusing it when the page changed under them."""

    if not markdown.strip():
        raise AnnotationError("a page needs some text")
    if len(markdown) > MAX_MARKDOWN:
        raise AnnotationError("the page is too long to save")
    with repo_lock(Path(repo)):
        current = page_source(repo, page_id, page)
        if base and base != current["base"]:
            raise AnnotationError(
                "the page changed since you opened it; reload it and edit again"
            )
        if current["kind"] == "document":
            target = Path(repo) / current["path"]
            ending = "" if markdown.endswith("\n") else "\n"
            _atomic_write(target, markdown + ending)
        else:
            generated = str((page or {}).get("markdown") or "")
            _atomic_write(
                _edit_path(repo, page_id),
                PageEdit(
                    page=page_id,
                    markdown=markdown,
                    base=markdown_hash(generated),
                    edited=utc_now(),
                ).dump(),
            )
    return page_source(repo, page_id, page)


def revert_page(repo: Path, page_id: str) -> bool:
    """Drop a person's edit so the generated page shows again."""

    path = _edit_path(repo, page_id)
    with repo_lock(Path(repo)):
        if not path.exists():
            return False
        path.unlink()
        return True


def _answer_block(question: str, answer: str) -> str:
    """The answer as page text: headings demoted, under its question."""

    lines: List[str] = []
    fenced = False
    for line in answer.strip().splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        if not fenced and re.match(r"^#{1,6}\s+", line):
            lines.append("**" + re.sub(r"^#{1,6}\s+", "", line).strip() + "**")
            lines.append("")
            continue
        lines.append(line)
    title = " ".join(question.split())
    return f"**{title}**\n\n" + "\n".join(lines).strip()


def fold_into(
    markdown: str, *, section: str, quote: str, question: str, answer: str
) -> str:
    """Insert an answer at the end of the section it was asked about."""

    block = _answer_block(question, answer)
    sections = split_sections(markdown)
    if not sections:
        return (markdown.rstrip() + "\n\n" + block + "\n").lstrip()
    target = None
    heading = " ".join(section.split()).lower()
    if heading:
        target = next((s for s in sections if s["heading"].lower() == heading), None)
    if target is None and quote:
        wanted = " ".join(quote.split()).lower()
        target = next(
            (s for s in sections if wanted in plain(s["markdown"]).lower()), None
        )
    if target is None:
        target = sections[-1]
    # Rebuild the page from its sections unchanged, with the answer added.
    out: List[str] = []
    for item in sections:
        if item is target:
            out.append(item["markdown"].rstrip("\n") + "\n\n" + block + "\n\n")
        else:
            out.append(item["markdown"])
    return "".join(out).rstrip() + "\n"
