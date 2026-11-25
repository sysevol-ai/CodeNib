# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The repository's own Markdown documents as Wiki pages.

A local Wiki shows the README and the files under ``docs/`` (or ``doc/``,
``documentation/``) beside the pages it builds or writes: they are what the
people behind the code wrote for readers, and they are there from the first
second, before any page is generated. Each document is one page, read from
the checkout, cleaned of what a Wiki page cannot show (raw HTML, relative
images), with links between documents turned into page links.

Page ids are ``doc--<path>``, so they never collide with generated pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional

import yaml

PREFIX = "doc--"
DOC_DIRS = ("docs", "doc", "documentation")
README_NAMES = ("README.md", "readme.md", "Readme.md", "README.markdown")
MAX_DOCS = 60
MAX_BYTES = 300_000
_SKIP_PARTS = {"node_modules", "vendor", "third_party", "site", "_build", "build"}
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_ADMONITION = re.compile(r'^(\s*)(!!!|\?\?\?\+?)\s+(\w+)(?:\s+"([^"]*)")?\s*$')


@dataclass(frozen=True)
class RepoDoc:
    id: str
    title: str
    path: str  # repository-relative, POSIX

    def to_json(self) -> Dict[str, str]:
        return {"id": self.id, "title": self.title, "path": self.path}


def doc_id(path: str) -> str:
    stem = re.sub(r"\.(md|markdown)$", "", path, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return PREFIX + (slug or "readme")


def is_doc_id(page_id: str) -> bool:
    return page_id.startswith(PREFIX)


def _visible(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    return not any(part.startswith(".") or part in _SKIP_PARTS for part in parts)


def _title(path: Path, text: str) -> str:
    fenced = False
    for line in text.splitlines()[:80]:
        if _FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else _HEADING.match(line)
        if match:
            title = _TAG.sub("", match.group(1)).strip().strip("*_` ")
            if title:
                return title[:120]
    if path.name.lower().startswith("readme"):
        return "README"
    name = path.stem if path.stem.lower() != "index" else path.parent.name or "Home"
    return re.sub(r"[-_]+", " ", name).strip().capitalize() or path.name


def _mkdocs_nav(root: Path) -> Dict[str, Optional[str]]:
    """Paths in the order ``mkdocs.yml``'s nav lists them, with their labels."""

    config = root / "mkdocs.yml"
    if not config.is_file():
        return {}
    try:
        data = yaml.load(config.read_text(encoding="utf-8"), Loader=_LenientLoader)
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    docs_dir = str(data.get("docs_dir") or "docs").strip("/")
    order: Dict[str, Optional[str]] = {}

    def add(target: str, label: Optional[str]) -> None:
        if target.lower().endswith((".md", ".markdown")) and "://" not in target:
            order.setdefault(f"{docs_dir}/{target.lstrip('./')}", label)

    def walk(node: Any) -> None:
        if isinstance(node, str):
            add(node, None)
        elif isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    add(value, str(key))
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data.get("nav"))
    return order


class _LenientLoader(yaml.SafeLoader):
    """Safe loading that ignores mkdocs' Python tags (``!!python/name:...``)."""


_LenientLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def find_docs(repo_dir: Path | str, *, limit: int = MAX_DOCS) -> List[RepoDoc]:
    """The README first, then the documents in nav order, else by path."""

    root = Path(repo_dir)
    found: List[Path] = []
    readme = next((root / n for n in README_NAMES if (root / n).is_file()), None)
    if readme is not None:
        found.append(readme)
    for name in DOC_DIRS:
        directory = root / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if (
                path.is_file()
                and path.suffix.lower() in (".md", ".markdown")
                and _visible(path, root)
                and path.stat().st_size <= MAX_BYTES
            ):
                found.append(path)
    nav = _mkdocs_nav(root)
    order = {path: index for index, path in enumerate(nav)}

    def rank(path: Path) -> tuple:
        rel = path.relative_to(root).as_posix()
        if path == readme:
            return (0, 0, "")
        if rel in order:
            return (1, order[rel], rel)
        is_index = (
            path.stem.lower() in ("index", "readme") and path.parent.name in DOC_DIRS
        )
        return (2, 0 if is_index else 1, rel)

    docs: List[RepoDoc] = []
    seen = set()
    for path in sorted(found, key=rank)[:limit]:
        rel = path.relative_to(root).as_posix()
        page_id = doc_id(rel)
        if page_id in seen:
            continue
        seen.add(page_id)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = nav.get(rel) or _title(path, text)
        docs.append(RepoDoc(id=page_id, title=title[:120], path=rel))
    return docs


def _resolve(base: str, target: str) -> Optional[str]:
    """A link target relative to ``base``, as a repository path."""

    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    parts: List[str] = []
    for part in (PurePosixPath(base).parent / target).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)


def clean_markdown(text: str, path: str, docs_by_path: Dict[str, RepoDoc]) -> str:
    """Markdown a Wiki page can show, with document links as page links."""

    out: List[str] = []
    fenced = False
    in_admonition: Optional[str] = None
    for line in text.replace("\r\n", "\n").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        admonition = _ADMONITION.match(line)
        if admonition:
            label = admonition.group(4) or admonition.group(3).capitalize()
            out.append(f"> **{label}**")
            in_admonition = admonition.group(1) + "    "
            continue
        if in_admonition is not None:
            if line.startswith(in_admonition) or not line.strip():
                out.append(("> " + line[len(in_admonition) :]).rstrip())
                continue
            in_admonition = None

        def link(match: re.Match) -> str:
            image, label, target = match.groups()
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, flags=re.IGNORECASE):
                return "" if image and not target.startswith("http") else match.group(0)
            if image:
                return ""  # relative images are not served
            resolved = _resolve(path, target)
            if resolved is None:
                return label
            if not resolved.lower().endswith((".md", ".markdown")):
                for suffix in ("index.md", "README.md"):
                    candidate = f"{resolved.rstrip('/')}/{suffix}"
                    if candidate in docs_by_path:
                        resolved = candidate
                        break
            doc = docs_by_path.get(resolved)
            return f"[{label}](?p={doc.id})" if doc else label

        line = _LINK.sub(link, line)
        line = _TAG.sub("", line).rstrip()
        out.append(line)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def doc_page(
    repo_dir: Path | str, page_id: str, docs: Optional[Iterable[RepoDoc]] = None
) -> Optional[Dict[str, Any]]:
    """The page for one document, or None when there is no such document."""

    root = Path(repo_dir)
    docs = list(docs) if docs is not None else find_docs(root)
    doc = next((d for d in docs if d.id == page_id), None)
    if doc is None:
        return None
    try:
        text = (root / doc.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    markdown = clean_markdown(text, doc.path, {d.path: d for d in docs})
    if not markdown.startswith("# "):
        markdown = f"# {doc.title}\n\n{markdown}"
    lines = max(1, text.count("\n") + 1)
    return {
        "id": doc.id,
        "title": doc.title,
        "markdown": markdown,
        "citations": [
            {
                "file": doc.path,
                "start_line": 1,
                "end_line": lines,
                "node_name": doc.path,
                "type": "document",
                "score": None,
                "content": None,
            }
        ],
        "diagram": "",
        "media_slots": [],
        "generation": {
            "mode": "document",
            "model": None,
            "repaired": False,
            "path": doc.path,
        },
    }
