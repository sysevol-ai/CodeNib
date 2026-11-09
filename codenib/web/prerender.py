# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Readable HTML for Wiki pages, for search engines and for the first paint.

The Wiki is a single-page app: without this, every route answers with the
same empty shell, so a search engine indexes one page titled "CodeNib Wiki".
Static exports write one document per page from these helpers, and the hosted
service answers search crawlers with the same markup.

Everything here is derived from a page that already passed publication: no
model call, no source read. All text is escaped; Markdown becomes a small,
fixed set of elements, and links leave the page only to absolute http(s) URLs
or to other pages of the same Wiki.
"""

from __future__ import annotations

import re
from html import escape
from typing import Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qs, quote, urlsplit

from ..wiki.fences import _fence_spans
from ..wiki.lead import overview_lead

_PAGE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+(.*)$")
_NUMBERED = re.compile(r"^[ \t]*\d+[.)][ \t]+(.*)$")
_TABLE_RULE = re.compile(r"^\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?$")
_CITATION = re.compile(r"\s?\[(?:[ER]\d+)\](?:\(#evidence-[ER]\d+\))?")
_INLINE = re.compile(
    r"(?P<code>`+)(?P<code_text>.+?)(?P=code)"
    r"|!\[(?P<image>[^\]\n]*)\]\([^)\s]*\)"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<url>[^)\s]+)\)"
    r"|\*\*(?P<strong>[^*\n]+)\*\*"
)
_DESCRIPTION_CHARS = 200
_SHELL_TITLE = re.compile(r"<title>.*?</title>", re.S | re.I)
_SHELL_META = re.compile(
    r"[ \t]*<meta\s+(?:name|property)=\"(?:description|og:[a-z:]+|twitter:[a-z:]+)\""
    r"[^>]*>\s*",
    re.S | re.I,
)
_SHELL_ROOT = re.compile(r"<div id=\"root\">\s*</div>", re.I)

PageHref = Callable[[str], Optional[str]]


def attr(value: str) -> str:
    """An HTML attribute value, escaped and quoted."""

    return '"' + escape(value, quote=True) + '"'


def page_route(repo_id: str, page_id: str) -> str:
    """The app route of one Wiki page, relative to the site's mount path.

    The Overview keeps the repository route; other pages use a path rather
    than ``?p=`` so that a static host can serve each one its own document.
    """

    repo = quote(repo_id, safe="")
    if page_id == "overview":
        return f"{repo}/"
    return f"{repo}/page/{quote(page_id, safe='')}/"


def plain_text(markdown: str) -> str:
    """Inline Markdown as plain words: no markers, links, code ticks or HTML."""

    text = _CITATION.sub("", markdown or "")
    text = re.sub(r"!\[[^\]\n]*\]\([^)\s]*\)", "", text)
    text = re.sub(r"\[([^\]\n]+)\]\([^)\s]+\)", r"\1", text)
    text = re.sub(r"`+([^`]*)`+", r"\1", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    text = re.sub(r"<[^>\n]*>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def page_description(markdown: str, fallback: str) -> str:
    """One or two sentences for search results, from the page's own lead."""

    lead = plain_text(overview_lead(markdown) or "")
    text = lead or plain_text(fallback)
    if len(text) <= _DESCRIPTION_CHARS:
        return text
    cut = text.rfind(". ", 0, _DESCRIPTION_CHARS)
    if cut > 60:
        return text[: cut + 1]
    return text[: _DESCRIPTION_CHARS - 1].rstrip() + "…"


def _safe_href(url: str, page_href: PageHref) -> Optional[str]:
    parts = urlsplit(url)
    if parts.scheme in {"http", "https"} and parts.netloc:
        return url
    if parts.scheme or parts.netloc or not parts.query:
        return None
    target = (parse_qs(parts.query).get("p") or [""])[0]
    if not _PAGE_ID.fullmatch(target):
        return None
    href = page_href(target)
    if (
        href
        and parts.fragment
        and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", parts.fragment)
    ):
        href += f"#{parts.fragment}"
    return href


def _inline(text: str, page_href: PageHref) -> str:
    text = _CITATION.sub("", text)
    result: list[str] = []
    cursor = 0
    for match in _INLINE.finditer(text):
        result.append(escape(text[cursor : match.start()]))
        cursor = match.end()
        if match["code"]:
            result.append(f"<code>{escape(match['code_text'].strip())}</code>")
        elif match["image"] is not None:
            continue  # Images need the app's media service; keep the prose.
        elif match["label"] is not None:
            # A label cannot hold "]", so it holds no further link.
            label = _inline(match["label"], page_href)
            href = _safe_href(match["url"], page_href)
            if href is None:
                result.append(label)
            elif href.startswith(("http://", "https://")):
                result.append(
                    f"<a href={attr(href)} " f'rel="nofollow noopener">{label}</a>'
                )
            else:
                result.append(f"<a href={attr(href)}>{label}</a>")
        else:
            result.append(f"<strong>{escape(match['strong'])}</strong>")
    result.append(escape(text[cursor:]))
    return "".join(result)


def render_markdown(markdown: str, page_href: PageHref) -> str:
    """A Wiki page's Markdown as escaped HTML with a fixed element set.

    Diagram fences and HTML blocks are omitted: the app draws them. Tables
    become their rows' text so that their words remain readable.
    """

    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    fences = {start: end for start, end in _fence_spans(lines)}
    html: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    list_tag = ""

    def flush() -> None:
        nonlocal list_tag
        if paragraph:
            html.append(f"<p>{_inline(' '.join(paragraph), page_href)}</p>")
            paragraph.clear()
        if items:
            body = "".join(f"<li>{item}</li>" for item in items)
            html.append(f"<{list_tag}>{body}</{list_tag}>")
            items.clear()
            list_tag = ""

    index = 0
    while index < len(lines):
        if index in fences:
            flush()
            end = fences[index]
            opener = lines[index].strip()
            language = opener.lstrip("`~").strip().split(" ", 1)[0].lower()
            closed = end - 1 > index and re.match(
                r"^[ \t]*(`{3,}|~{3,})[ \t]*$", lines[end - 1]
            )
            body = lines[index + 1 : end - 1 if closed else end]
            if language not in {"mermaid", "flow", "codenib-flow", "dot", "svg"}:
                code = escape("\n".join(body))
                html.append(f"<pre><code>{code}</code></pre>")
            index = end
            continue
        line = lines[index]
        stripped = line.strip()
        index += 1
        if not stripped:
            flush()
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush()
            level = min(len(heading.group(1)) + 1, 6)
            html.append(f"<h{level}>{_inline(heading.group(2), page_href)}</h{level}>")
            continue
        if stripped.startswith("<"):
            flush()
            continue
        if stripped.startswith("|"):
            flush()
            if not _TABLE_RULE.match(stripped):
                cells = [cell.strip() for cell in stripped.strip("|").split("|")]
                text = " · ".join(cell for cell in cells if cell)
                if text:
                    html.append(f"<p>{_inline(text, page_href)}</p>")
            continue
        if stripped.startswith(">"):
            flush()
            quote_text = stripped.lstrip(">").strip()
            if quote_text:
                html.append(
                    f"<blockquote><p>{_inline(quote_text, page_href)}</p></blockquote>"
                )
            continue
        bullet = _BULLET.match(line)
        numbered = None if bullet else _NUMBERED.match(line)
        if bullet or numbered:
            tag = "ul" if bullet else "ol"
            if paragraph or (items and list_tag != tag):
                flush()
            list_tag = tag
            items.append(_inline((bullet or numbered).group(1), page_href))
            continue
        if items:
            # A continuation line belongs to the last item.
            items[-1] += " " + _inline(stripped, page_href)
            continue
        paragraph.append(stripped)
    flush()
    return "\n".join(html)


def page_body(
    *,
    repository: str,
    title: str,
    markdown: str,
    pages: Sequence[tuple[str, str]],
    page_href: PageHref,
    current: str,
    source: str = "",
) -> str:
    """The readable article and its Wiki navigation, inside one ``<main>``."""

    links = []
    for page_id, label in pages:
        href = page_href(page_id)
        if not href:
            continue
        marker = ' aria-current="page"' if page_id == current else ""
        links.append(f"<li><a href={attr(href)}{marker}>{escape(label)}</a></li>")
    nav = "".join(links)
    # Pages usually open with their own title as a heading; it is the <h1>.
    lines = (markdown or "").lstrip("\n").split("\n", 1)
    first = _HEADING.match(lines[0].strip())
    if first and plain_text(first.group(2)).casefold() == title.strip().casefold():
        markdown = lines[1] if len(lines) > 1 else ""
    parts = [
        '<main class="prerendered-wiki">',
        f'<p class="prerendered-wiki-repo">{escape(repository)} Wiki</p>',
        f"<h1>{escape(title)}</h1>",
        render_markdown(markdown, page_href),
    ]
    if nav:
        parts.append(
            f"<nav aria-label={attr(repository + ' Wiki pages')}>"
            f"<ul>{nav}</ul></nav>"
        )
    if source:
        parts.append(f'<p class="prerendered-wiki-source">{escape(source)}</p>')
    parts.append("</main>")
    return "\n".join(parts)


def _head(
    *,
    title: str,
    description: str,
    canonical: Optional[str],
    site_name: str,
) -> str:
    tags = [
        ("name", "description", description),
        ("property", "og:type", "article"),
        ("property", "og:site_name", site_name),
        ("property", "og:title", title),
        ("property", "og:description", description),
        ("name", "twitter:card", "summary"),
        ("name", "twitter:title", title),
        ("name", "twitter:description", description),
    ]
    if canonical:
        tags.append(("property", "og:url", canonical))
    head = [f"<title>{escape(title)}</title>"]
    head.extend(
        f"<meta {kind}={attr(key)} content={attr(value)}>" for kind, key, value in tags
    )
    if canonical:
        head.append(f'<link rel="canonical" href={attr(canonical)}>')
    return "\n    ".join(head)


def can_prerender(shell: str) -> bool:
    """Whether an app shell has the ``<title>`` and empty root this fills."""

    return bool(_SHELL_TITLE.search(shell) and _SHELL_ROOT.search(shell))


def page_document(
    shell: str,
    *,
    title: str,
    description: str,
    canonical: Optional[str],
    body: str,
    site_name: str = "CodeNib Wiki",
) -> str:
    """The app shell with this page's head and readable body.

    The app replaces the body when its script starts, so readers who run it
    see the ordinary Wiki and everyone else, crawlers included, sees the page.
    """

    if not can_prerender(shell):
        raise ValueError("frontend index.html needs a <title> and an empty root")
    document = _SHELL_META.sub("", shell)
    document = _SHELL_TITLE.sub(
        lambda _match: _head(
            title=title,
            description=description,
            canonical=canonical,
            site_name=site_name,
        ),
        document,
        count=1,
    )
    return _SHELL_ROOT.sub(
        lambda _match: f'<div id="root">\n{body}\n</div>', document, count=1
    )


def standalone_document(
    *,
    title: str,
    description: str,
    canonical: Optional[str],
    body: str,
    site_name: str = "CodeNib Wiki",
) -> str:
    """A complete document without the app, for crawlers of the hosted Wiki."""

    head = _head(
        title=title, description=description, canonical=canonical, site_name=site_name
    )
    return (
        '<!doctype html>\n<html lang="en">\n  <head>\n    <meta charset="utf-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"    {head}\n  </head>\n  <body>\n{body}\n  </body>\n</html>\n"
    )


def sitemap(urls: Iterable[str]) -> bytes:
    entries = "".join(
        f"  <url><loc>{escape(url)}</loc></url>\n" for url in dict.fromkeys(urls)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}</urlset>\n"
    ).encode("utf-8")


def robots(sitemap_url: Optional[str]) -> bytes:
    lines = ["User-agent: *", "Allow: /"]
    if sitemap_url:
        lines += ["", f"Sitemap: {sitemap_url}"]
    return ("\n".join(lines) + "\n").encode("utf-8")


def outline_pages(tree: Iterable[Mapping[str, object]]) -> list[tuple[str, str]]:
    """``(id, title)`` for every page of an outline tree, in reading order."""

    pages: list[tuple[str, str]] = []
    seen: set[str] = set()

    def visit(nodes: Iterable[Mapping[str, object]]) -> None:
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            page_id = str(node.get("id") or "")
            if (
                _PAGE_ID.fullmatch(page_id)
                and page_id not in {".", ".."}
                and page_id not in seen
            ):
                seen.add(page_id)
                pages.append((page_id, str(node.get("title") or page_id)))
            children = node.get("children")
            if isinstance(children, list):
                visit(children)

    visit(tree)
    return pages


__all__ = [
    "attr",
    "can_prerender",
    "outline_pages",
    "page_body",
    "page_description",
    "page_document",
    "page_route",
    "plain_text",
    "render_markdown",
    "robots",
    "sitemap",
    "standalone_document",
]
