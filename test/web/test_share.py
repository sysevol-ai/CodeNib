# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codenib.web import share


class Registry:
    def list_infos(self):
        return [
            SimpleNamespace(
                id="psf__requests",
                repo="psf/requests",
                summary="Session.prepare_request() merges session state.",
                description="HTTP for humans",
            )
        ]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(share, "share_dir", lambda: tmp_path)
    monkeypatch.setenv("CODENIB_PUBLIC_ORIGIN", "https://demo.example")
    (tmp_path / "psf__requests.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "_site.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    app = FastAPI()
    app.state.registry = Registry()
    app.include_router(share.router)
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    ["/psf__requests", "/psf/requests", "/github.com/psf/requests/tree/main"],
)
def test_repository_routes_get_the_repository_card(client, path):
    html = client.get("/api/share-page", params={"path": path}).text
    assert "<title>psf/requests · CodeNib Wiki</title>" in html
    assert (
        'property="og:image" content="https://demo.example/api/share-cards/'
        'psf__requests.png"' in html
    )
    assert "Session.prepare_request() merges session state." in html
    assert 'name="twitter:card" content="summary_large_image"' in html


def test_other_routes_get_the_site_card(client):
    html = client.get("/api/share-page", params={"path": "/browse"}).text
    assert "<title>CodeNib Wiki</title>" in html
    assert "share-cards/_site.png" in html
    assert 'content="https://demo.example/browse"' in html


def test_values_are_escaped_and_chapter_links_kept(client):
    html = client.get(
        "/api/share-page", params={"path": '/"><script>', "p": "redirects"}
    ).text
    assert "<script>" not in html
    page = client.get(
        "/api/share-page", params={"path": "/psf__requests", "p": "redirects"}
    ).text
    assert 'content="https://demo.example/psf__requests?p=redirects"' in page


def test_card_images_are_served_and_names_are_checked(client):
    ok = client.get("/api/share-cards/psf__requests.png")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
    assert client.get("/api/share-cards/missing.png").status_code == 404
    assert client.get("/api/share-cards/..%2Fsecret.png").status_code == 404
