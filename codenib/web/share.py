# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Link previews: share-card images and the tags link-preview crawlers read.

The demo is a single-page app, so a crawler that does not run JavaScript
sees no title or image. The ingress sends known link-preview agents to
``/api/share-page`` instead; it answers with the Open Graph and Twitter tags
for the requested route and nothing else. People and search engines keep
the app.

Card images are rendered ahead of time by ``web/scripts/render-share-cards.mjs``
into ``<data_dir>/share_cards/<repo_id>.png`` (``_site.png`` for the home
page) and served from here.
"""

from __future__ import annotations

import os
import re
from html import escape
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from .config import load_config

router = APIRouter()

_REPO_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_TOKEN = re.compile(r"^[a-f0-9]{64}$")
_GITHUB = re.compile(
    r"^(?:https?:/+)?(?:www\.)?(?:github\.com/)?"
    r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})(?:/.*)?$",
    re.IGNORECASE,
)
SITE_TITLE = "CodeNib Wiki"
SITE_DESCRIPTION = (
    "See how a repository is wired: its parts, the calls the code index "
    "recorded between them, and the path a request takes. Follow any line "
    "to the source."
)


def public_origin() -> str:
    return os.environ.get("CODENIB_PUBLIC_ORIGIN", "https://demo.codenib.ai").rstrip(
        "/"
    )


def share_dir() -> Path:
    return Path(load_config().data_dir).absolute() / "share_cards"


def _card_url(name: str) -> str | None:
    if (share_dir() / f"{name}.png").is_file():
        return f"{public_origin()}/api/share-cards/{quote(name)}.png"
    return None


@router.get("/api/share-cards/{name}.png")
def share_card(name: str):
    if not _REPO_ID.match(name):
        raise HTTPException(404, "No share card for this page.")
    path = share_dir() / f"{name}.png"
    if not path.is_file():
        raise HTTPException(404, "No share card for this page.")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _repo_for(path: str, registry) -> tuple[str, object] | None:
    """The prepared repository a route names: /id, /owner/repo or a GitHub URL."""
    infos = {info.id: info for info in (registry.list_infos() if registry else [])}
    head = path.strip("/").split("/")[0] if path.strip("/") else ""
    if head in infos:
        return head, infos[head]
    match = _GITHUB.match(path.strip("/"))
    if match:
        slug = f"{match.group(1)}/{match.group(2)}".lower()
        for repo_id, info in infos.items():
            if (getattr(info, "repo", "") or "").lower() == slug:
                return repo_id, info
    return None


def _page(title: str, description: str, url: str, image: str | None) -> str:
    tags = [
        ("property", "og:type", "website"),
        ("property", "og:site_name", SITE_TITLE),
        ("property", "og:title", title),
        ("property", "og:description", description),
        ("property", "og:url", url),
        ("name", "twitter:card", "summary_large_image" if image else "summary"),
        ("name", "twitter:title", title),
        ("name", "twitter:description", description),
        ("name", "description", description),
    ]
    if image:
        tags += [
            ("property", "og:image", image),
            ("property", "og:image:width", "1200"),
            ("property", "og:image:height", "630"),
            ("name", "twitter:image", image),
        ]
    meta = "\n".join(
        f"<meta {kind}={_attr(key)} content={_attr(value)}>"
        for kind, key, value in tags
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        f"<title>{escape(title)}</title>\n{meta}\n"
        f'<link rel="canonical" href={_attr(url)}>\n</head>\n'
        f"<body><p><a href={_attr(url)}>{escape(title)}</a></p></body></html>\n"
    )


def _attr(value: str) -> str:
    """An HTML attribute value, escaped and quoted."""
    return '"' + escape(value, quote=True) + '"'


@router.get("/api/share-page", response_class=HTMLResponse)
def share_page(request: Request, path: str = "/", p: str = ""):
    path = "/" + unquote(path).lstrip("/")[:512]
    url = public_origin() + quote(path, safe="/:@-._~")
    if p and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", p):
        url += f"?p={p}"
    title, description, image = SITE_TITLE, SITE_DESCRIPTION, _card_url("_site")

    registry = getattr(request.app.state, "registry", None)
    found = _repo_for(path, registry)
    parts = path.strip("/").split("/")
    if found:
        repo_id, info = found
        name = getattr(info, "repo", "") or repo_id
        title = f"{name} · {SITE_TITLE}"
        description = (
            getattr(info, "summary", "")
            or getattr(info, "description", "")
            or f"The system map and source-linked Wiki of {name}."
        )
        image = _card_url(repo_id) or image
    elif len(parts) == 2 and parts[0] == "wiki" and _TOKEN.match(parts[1]):
        from .visitor_wikis import _manager

        try:
            state = _manager(request).status(parts[1])
        except Exception:  # noqa: BLE001 - an unknown Wiki keeps the site card
            state = None
        if state and state.get("repository"):
            title = f"{state['repository']} · {SITE_TITLE}"
            description = (
                f"A source-linked Wiki of {state['repository']}: its architecture "
                "and the code behind each explanation."
            )
    return HTMLResponse(
        _page(title, description[:300], url, image),
        headers={"Cache-Control": "public, max-age=600"},
    )
