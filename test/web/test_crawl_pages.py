# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Search crawlers read cached Wiki pages; a crawl never generates one."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import codenib.web.app as web_app

PAGES = {
    "overview": {
        "id": "overview",
        "title": "Overview",
        "markdown": "# Overview\n\nRequests sends HTTP for humans. See [auth](?p=auth).",
    },
    "auth": {
        "id": "auth",
        "title": "Authentication",
        "markdown": "The `Session` signs <requests>.",
    },
}


class Builder:
    def cached_page_tree(self):
        return [
            {"id": "overview", "title": "Overview"},
            {"id": "auth", "title": "Authentication"},
            {"id": "later", "title": "Not generated yet"},
        ]

    def cached_page(self, page_id):
        return PAGES.get(page_id)

    def page(self, page_id):  # pragma: no cover - asserted below
        pytest.fail("a crawl must never generate a page")

    page_tree = page


class Registry:
    def list_infos(self):
        return [
            SimpleNamespace(
                id="psf__requests",
                repo="psf/requests",
                summary="HTTP for humans.",
                description="",
            )
        ]


@pytest.fixture
def client(monkeypatch):
    @contextmanager
    def pinned(repo_id):
        if repo_id != "psf__requests":
            raise web_app.HTTPException(status_code=404)
        yield SimpleNamespace(entry=SimpleNamespace(repo="psf/requests"))

    monkeypatch.setenv("CODENIB_PUBLIC_ORIGIN", "https://demo.example")
    monkeypatch.setattr(web_app, "_pinned_bundle", pinned)
    monkeypatch.setattr(web_app, "_wiki", lambda *_args: Builder())
    monkeypatch.setattr(web_app.app.state, "registry", Registry(), raising=False)
    return TestClient(web_app.app)


@pytest.mark.parametrize(
    "query",
    [
        "path=/psf__requests/page/auth",
        "path=/psf__requests/page/auth/",
        "path=/psf__requests&p=auth",
        "path=/psf/requests&p=auth",
    ],
)
def test_a_cached_page_is_readable_with_its_own_head(client, query):
    response = client.get(f"/api/crawl-page?{query}")

    assert response.status_code == 200
    html = response.text
    assert "<title>Authentication · psf/requests Wiki</title>" in html
    assert (
        '<link rel="canonical" href="https://demo.example/psf__requests/page/auth/">'
        in html
    )
    assert "<code>Session</code> signs &lt;requests&gt;." in html
    assert "<script" not in html
    assert "Not generated yet" not in html


def test_the_overview_links_pages_by_path(client):
    html = client.get("/api/crawl-page?path=/psf__requests/").text

    assert "<title>psf/requests Wiki</title>" in html
    assert '<a href="/psf__requests/page/auth/">auth</a>' in html
    assert '<meta name="description" content="Requests sends HTTP for humans.' in html


@pytest.mark.parametrize(
    "query",
    [
        "path=/psf__requests/page/later",  # outlined, never generated
        "path=/psf__requests/page/missing",
        "path=/psf__requests/ask",
        "path=/unknown__repo",
        "path=/psf__requests&p=../etc",
    ],
)
def test_pages_without_readable_text_are_not_found(client, query):
    response = client.get(f"/api/crawl-page?{query}")
    if query.endswith("p=../etc"):
        # An unsafe page id falls back to the Overview rather than echoing it.
        assert response.status_code == 200 and "../etc" not in response.text
        return
    assert response.status_code == 404


def test_the_home_lists_prepared_wikis(client):
    html = client.get("/api/crawl-page?path=/").text

    assert '<a href="/psf__requests/">psf/requests</a> — HTTP for humans.' in html
    assert '<link rel="canonical" href="https://demo.example/">' in html


def test_sitemap_lists_only_cached_pages_and_robots_points_at_it(client):
    sitemap = client.get("/api/sitemap.xml")
    robots = client.get("/api/robots.txt")

    assert sitemap.headers["content-type"].startswith("application/xml")
    assert "<loc>https://demo.example/</loc>" in sitemap.text
    assert "<loc>https://demo.example/psf__requests/</loc>" in sitemap.text
    assert "<loc>https://demo.example/psf__requests/page/auth/</loc>" in sitemap.text
    assert "later" not in sitemap.text
    assert robots.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://demo.example/sitemap.xml" in robots.text
    assert "Disallow" not in robots.text
