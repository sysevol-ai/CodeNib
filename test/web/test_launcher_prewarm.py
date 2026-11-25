# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Writing a Wiki's pages in the background, Overview first."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from codenib.web.launcher import _page_order, prewarm_pages


def test_overview_first_then_top_level_then_children() -> None:
    tree = [
        {"id": "client", "children": [{"id": "send"}, {"id": "stream"}]},
        {"id": "overview", "children": []},
        {"id": "auth"},
    ]
    assert _page_order(tree) == ["overview", "client", "auth", "send", "stream"]
    assert _page_order([]) == []


def test_prewarm_requests_every_page_and_counts_failures() -> None:
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            seen.append(self.path)
            if self.path == "/api/repos/demo/wiki":
                body = json.dumps(
                    {
                        "pages": [
                            {"id": "overview"},
                            {"id": "a", "children": [{"id": "b"}]},
                        ]
                    }
                ).encode()
                self.send_response(200)
            elif self.path.startswith("/api/repos/demo/wiki/b"):
                body, _ = b"{}", self.send_response(500)
            else:
                body = b"{}"
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        written, total = prewarm_pages(
            f"http://127.0.0.1:{server.server_address[1]}", "demo", workers=2
        )
    finally:
        server.shutdown()
    assert (written, total) == (2, 3)
    assert seen[0] == "/api/repos/demo/wiki"
    assert seen[1] == "/api/repos/demo/wiki/overview?materialize_media=false"
    assert sorted(seen[2:]) == [
        "/api/repos/demo/wiki/a?materialize_media=false",
        "/api/repos/demo/wiki/b?materialize_media=false",
    ]


def test_prewarm_writes_four_pages_at_once_by_default(monkeypatch) -> None:
    from codenib.web.launcher import prewarm_workers

    monkeypatch.delenv("CODENIB_PREWARM_WORKERS", raising=False)
    assert prewarm_workers() == 4
    monkeypatch.setenv("CODENIB_PREWARM_WORKERS", "6")
    assert prewarm_workers() == 6
    monkeypatch.setenv("CODENIB_PREWARM_WORKERS", "99")
    assert prewarm_workers() == 8
    monkeypatch.setenv("CODENIB_PREWARM_WORKERS", "x")
    assert prewarm_workers() == 4
