# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Select repository-provided visuals for source-grounded Wiki pages.

The selector deliberately stays offline: it resolves image references from a
repository README back to authenticated files in that same repository.  It
never downloads a remote image, and it ignores badges and decorative assets.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from .._contained_source import read_repository_file

_MAX_README_BYTES = 2 * 1024 * 1024
_MAX_VISUAL_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_REFERENCES = 128
_MAX_REFERENCE_BYTES = 8 * 1024
_MAX_PATH_BYTES = 4 * 1024
_MAX_PATH_COMPONENTS = 256
_MIN_VISUAL_SCORE = 35
_README_NAMES = (
    "README.md",
    "README.mdx",
    "README.markdown",
    "readme.md",
)
_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?:<(?P<bracket>[^>]+)>|"
    r"(?P<plain>[^)\s]+))(?:\s+['\"][^)]*['\"])?\s*\)",
)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")
_SPACE_RE = re.compile(r"\s+")
_SAFE_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")

_EXCLUDED_TERMS = frozenset(
    {
        "avatar",
        "badge",
        "build status",
        "coverage",
        "favicon",
        "icon",
        "license",
        "logo",
        "pypi",
        "shield",
        "sponsor",
        "star history",
    }
)
_ARCHITECTURE_TERMS = frozenset(
    {
        "architecture",
        "architectural",
        "component diagram",
        "system design",
        "system overview",
    }
)
_FLOW_TERMS = frozenset(
    {
        "data flow",
        "dataflow",
        "lifecycle",
        "pipeline",
        "sequence",
        "workflow",
    }
)
_PRODUCT_TERMS = frozenset(
    {
        "demo",
        "interface",
        "preview",
        "screen",
        "screenshot",
        "user interface",
        "wiki",
    }
)


@dataclass(frozen=True, slots=True)
class RepositoryVisual:
    """One validated repository-provided visual selected for an Overview page."""

    path: str
    source_document: str
    alt_text: str
    heading: str
    role: str
    mime_type: str
    score: int
    content_sha256: str
    payload: bytes = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _ImageReference:
    target: str
    alt_text: str
    line: int
    heading: str


@dataclass(frozen=True, slots=True)
class _RankedReference:
    path: str
    alt_text: str
    line: int
    heading: str
    role: str
    score: int


class _HTMLImageParser(HTMLParser):
    def __init__(self, headings: Mapping[int, str], *, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self._headings = headings
        self._limit = limit
        self.images: list[_ImageReference] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "img" or len(self.images) >= self._limit:
            return
        values = {name.casefold(): value or "" for name, value in attrs}
        target = values.get("src", "").strip()
        if not target:
            return
        line = self.getpos()[0]
        self.images.append(
            _ImageReference(
                target=target,
                alt_text=values.get("alt", ""),
                line=line,
                heading=_heading_at(self._headings, line),
            )
        )


def _normalized_text(value: str, *, limit: int = 160) -> str:
    text = _SPACE_RE.sub(" ", html.unescape(value or "")).strip()
    return text[:limit].rstrip()


def _headings(text: str) -> dict[int, str]:
    result: dict[int, str] = {}
    current = ""
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _HEADING_RE.match(line)
        if match:
            current = _normalized_text(match.group("title"))
        result[line_number] = current
    return result


def _heading_at(headings: Mapping[int, str], line: int) -> str:
    return headings.get(max(1, line), "")


def _image_references(text: str) -> list[_ImageReference]:
    headings = _headings(text)
    references: list[_ImageReference] = []
    line = 1
    cursor = 0
    for match in _MARKDOWN_IMAGE_RE.finditer(text):
        if len(references) >= _MAX_IMAGE_REFERENCES:
            break
        line += text.count("\n", cursor, match.start())
        cursor = match.start()
        references.append(
            _ImageReference(
                target=(match.group("bracket") or match.group("plain") or ""),
                alt_text=match.group("alt") or "",
                line=line,
                heading=_heading_at(headings, line),
            )
        )

    remaining = _MAX_IMAGE_REFERENCES - len(references)
    parser = _HTMLImageParser(headings, limit=remaining)
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError):
        # A malformed HTML fragment must not prevent Markdown references from
        # participating in selection.
        pass
    references.extend(parser.images)
    return references


def _normalize_relative(path: str) -> str | None:
    if (
        not path
        or "\\" in path
        or "\x00" in path
        or len(path.encode("utf-8")) > _MAX_PATH_BYTES
    ):
        return None
    decoded = unquote(path)
    if len(decoded.encode("utf-8")) > _MAX_PATH_BYTES:
        return None
    if decoded.startswith("/") or re.match(r"^[A-Za-z]:", decoded):
        return None
    parts = PurePosixPath(decoded).parts
    if (
        not parts
        or len(parts) > _MAX_PATH_COMPONENTS
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    normalized = PurePosixPath(*parts).as_posix()
    if PurePosixPath(normalized).suffix.casefold() not in _MIME_TYPES:
        return None
    return normalized


def _repository_slug(value: str | None) -> str | None:
    candidate = str(value or "").strip().strip("/")
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    if not _SAFE_REPOSITORY_RE.fullmatch(candidate):
        return None
    return candidate.casefold()


def _same_repository_url_paths(
    target: str,
    *,
    repository: str | None,
    inventory: frozenset[str] | None,
) -> list[str]:
    slug = _repository_slug(repository)
    if slug is None:
        return []
    parsed = urlsplit(target)
    host = parsed.hostname.casefold() if parsed.hostname else ""
    raw_parts = [unquote(part) for part in parsed.path.split("/") if part]
    owner, name = slug.split("/", 1)
    if len(raw_parts) < 4 or [part.casefold() for part in raw_parts[:2]] != [
        owner,
        name,
    ]:
        return []

    if host == "raw.githubusercontent.com":
        tail = raw_parts[2:]
    elif host in {"github.com", "www.github.com"} and raw_parts[2].casefold() in {
        "blob",
        "raw",
    }:
        tail = raw_parts[3:]
    else:
        return []
    if len(tail) < 2:
        return []

    # A Git ref may contain slashes.  Prefer an authenticated inventory match;
    # otherwise the conventional one-component branch form remains useful for
    # local checkouts without a retained inventory.
    candidates: list[str] = []
    for offset in range(1, len(tail)):
        normalized = _normalize_relative("/".join(tail[offset:]))
        if normalized is None:
            continue
        if inventory is not None and normalized not in inventory:
            continue
        candidates.append(normalized)
    return candidates


def _reference_paths(
    target: str,
    *,
    source_document: str,
    repository: str | None,
    inventory: frozenset[str] | None,
) -> list[str]:
    value = html.unescape(target).strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or len(value.encode("utf-8")) > _MAX_REFERENCE_BYTES
    ):
        return []
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or value.startswith("//"):
        return _same_repository_url_paths(
            value,
            repository=repository,
            inventory=inventory,
        )
    if not parsed.path:
        return []
    parent = PurePosixPath(source_document).parent
    normalized = _normalize_relative((parent / unquote(parsed.path)).as_posix())
    if normalized is None:
        return []
    if inventory is not None and normalized not in inventory:
        return []
    return [normalized]


def _contains_any(text: str, terms: frozenset[str]) -> bool:
    return any(term in text for term in terms)


def _rank_reference(
    reference: _ImageReference,
    path: str,
) -> tuple[int, str] | None:
    searchable = (
        _normalized_text(
            f"{reference.alt_text} {reference.heading} {path}",
            limit=600,
        )
        .casefold()
        .replace("_", "-")
        .replace("-", " ")
    )
    if _contains_any(searchable, _EXCLUDED_TERMS):
        return None

    score = 10
    role = "repository visual"
    if _contains_any(searchable, _ARCHITECTURE_TERMS):
        score += 130
        role = "architecture"
    elif _contains_any(searchable, _FLOW_TERMS):
        score += 95
        role = "workflow"
    elif _contains_any(searchable, _PRODUCT_TERMS):
        score += 65
        role = "product view"
    elif "overview" in searchable or "hero" in searchable:
        score += 55
        role = "overview"
    if reference.line <= 120:
        score += 18
    elif reference.line <= 240:
        score += 8
    if reference.alt_text.strip():
        score += 5
    return (score, role) if score >= _MIN_VISUAL_SCORE else None


def _read_source(
    repo_dir: str | Path,
    relative: str,
    *,
    max_bytes: int,
    source_reader: Any | None,
) -> bytes:
    if source_reader is not None:
        read_bytes = getattr(source_reader, "read_bytes", None)
        if callable(read_bytes):
            return read_bytes(relative, max_bytes=max_bytes)
        read_prefix = getattr(source_reader, "read_prefix", None)
        if callable(read_prefix):
            payload = read_prefix(relative, max_bytes=max_bytes + 1)
            if len(payload) > max_bytes:
                raise ValueError("source file exceeds its bounded read limit")
            return payload
        raise ValueError("source reader does not provide bounded file reads")
    return read_repository_file(repo_dir, relative, max_bytes=max_bytes)


def _readme_paths(inventory: frozenset[str] | None) -> tuple[str, ...]:
    if inventory is None:
        return _README_NAMES
    by_casefold = {
        path.casefold(): path
        for path in inventory
        if isinstance(path, str) and "/" not in path
    }
    return tuple(
        selected
        for candidate in _README_NAMES
        if (selected := by_casefold.get(candidate.casefold())) is not None
    )


def _validated_mime_type(path: str, payload: bytes) -> str | None:
    suffix = PurePosixPath(path).suffix.casefold()
    mime_type = _MIME_TYPES.get(suffix)
    if mime_type == "image/png":
        valid = (
            len(payload) >= 24
            and payload.startswith(b"\x89PNG\r\n\x1a\n")
            and payload[12:16] == b"IHDR"
        )
    elif mime_type == "image/jpeg":
        valid = len(payload) >= 4 and payload.startswith(b"\xff\xd8\xff")
    elif mime_type == "image/gif":
        valid = len(payload) >= 10 and payload[:6] in {b"GIF87a", b"GIF89a"}
    elif mime_type == "image/webp":
        valid = (
            len(payload) >= 12
            and payload.startswith(b"RIFF")
            and payload[8:12] == b"WEBP"
        )
    else:
        valid = False
    return mime_type if valid else None


def discover_overview_visual(
    repo_dir: str | Path,
    *,
    repository: str | None = None,
    source_reader: Any | None = None,
) -> RepositoryVisual | None:
    """Return the strongest validated README visual for an Overview page."""

    inventory_value = getattr(source_reader, "file_paths", None)
    inventory = frozenset(inventory_value) if inventory_value is not None else None
    source_document: str | None = None
    readme: str | None = None
    for candidate in _readme_paths(inventory):
        try:
            readme = _read_source(
                repo_dir,
                candidate,
                max_bytes=_MAX_README_BYTES,
                source_reader=source_reader,
            ).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        source_document = candidate
        break
    if source_document is None or readme is None:
        return None

    ranked_by_path: dict[str, _RankedReference] = {}
    for reference in _image_references(readme):
        for path in _reference_paths(
            reference.target,
            source_document=source_document,
            repository=repository,
            inventory=inventory,
        ):
            score_role = _rank_reference(reference, path)
            if score_role is None:
                continue
            score, role = score_role
            candidate = _RankedReference(
                path=path,
                alt_text=_normalized_text(reference.alt_text, limit=120),
                line=reference.line,
                heading=_normalized_text(reference.heading, limit=120),
                role=role,
                score=score,
            )
            previous = ranked_by_path.get(path)
            if previous is None or (candidate.score, -candidate.line) > (
                previous.score,
                -previous.line,
            ):
                ranked_by_path[path] = candidate

    ranked = list(ranked_by_path.values())
    ranked.sort(key=lambda item: (-item.score, item.line, item.path))
    for candidate in ranked:
        try:
            payload = _read_source(
                repo_dir,
                candidate.path,
                max_bytes=_MAX_VISUAL_BYTES,
                source_reader=source_reader,
            )
        except (OSError, ValueError):
            continue
        mime_type = _validated_mime_type(candidate.path, payload)
        if mime_type is None:
            continue
        return RepositoryVisual(
            path=candidate.path,
            source_document=source_document,
            alt_text=candidate.alt_text,
            heading=candidate.heading,
            role=candidate.role,
            mime_type=mime_type,
            score=candidate.score,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            payload=payload,
        )
    return None


def repository_visual_slot(
    visual: RepositoryVisual,
    *,
    uri: str,
) -> dict[str, Any]:
    """Render a repository visual into the existing provider-neutral slot."""

    role_titles = {
        "architecture": "Architecture at a glance",
        "workflow": "How the system moves",
        "product view": "The product in context",
        "overview": "Repository overview",
    }
    role_purposes = {
        "architecture": (
            "Use the repository's own architecture view as the map for the "
            "implementation story below."
        ),
        "workflow": (
            "Follow the repository's own workflow view before tracing the "
            "implementation details below."
        ),
        "product view": (
            "Start with the repository's own product view, then connect what "
            "it shows to the source-backed explanation below."
        ),
        "overview": (
            "Start with the repository's own overview, then connect it to the "
            "source-backed explanation below."
        ),
    }
    citations = list(dict.fromkeys((visual.source_document, visual.path)))
    title = visual.alt_text or role_titles.get(visual.role, "Repository visual")
    purpose = role_purposes.get(
        visual.role,
        "A repository-provided visual that frames the source-backed explanation below.",
    )
    return {
        "id": "overview-repository-visual",
        "kind": "image",
        "placement": "lead",
        "title": title,
        "purpose": purpose,
        "source_citations": citations,
        "prompt": "",
        "asset": {
            "slot_id": "overview-repository-visual",
            "kind": "image",
            "uri": uri,
            "mime_type": visual.mime_type,
            "model": "repository",
            "provider": "repository",
            "prompt": "",
            "source_citations": citations,
            "metadata": {
                "origin": "repository",
                "source_path": visual.path,
                "source_document": visual.source_document,
                "heading": visual.heading,
                "role": visual.role,
                "selection_score": visual.score,
                "content_sha256": visual.content_sha256,
            },
        },
        "human_prior": {
            "editable": False,
            "notes": ["Repository-provided asset; edit it at its source path."],
        },
    }


def attach_repository_visual(
    page: Mapping[str, Any],
    visual: RepositoryVisual,
    *,
    uri: str,
) -> dict[str, Any]:
    """Prepend one materialized lead visual without mutating the page."""

    result = dict(page)
    existing = page.get("media_slots") or ()
    slots = [dict(slot) for slot in existing if isinstance(slot, Mapping)]
    slots = [slot for slot in slots if slot.get("id") != "overview-repository-visual"]
    result["media_slots"] = [repository_visual_slot(visual, uri=uri), *slots]
    return result


__all__ = [
    "RepositoryVisual",
    "attach_repository_visual",
    "discover_overview_visual",
    "repository_visual_slot",
]
