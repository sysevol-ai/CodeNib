# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from codenib.web.static_server import build_server


def test_static_server_uses_same_origin_api_and_falls_back_to_spa(tmp_path):
    (tmp_path / "index.html").write_text("<title>CodeNib Wiki</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('ready')")
    server = build_server(
        tmp_path,
        api_base="http://127.0.0.1:8123/",
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/runtime-config.js") as response:
            config = response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
        with urllib.request.urlopen(f"{base}/owner/repo") as response:
            page = response.read().decode()
        with urllib.request.urlopen(f"{base}/assets/app.js") as response:
            asset = response.read().decode()
            assert "immutable" in response.headers["Cache-Control"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert config == 'window.__CODENIB_API_BASE__ = "";\n'
    assert "CodeNib Wiki" in page
    assert "ready" in asset


def test_static_server_proxies_api_for_remote_browser(tmp_path):
    class APIHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler contract
            self._reply({"method": "GET", "path": self.path})

        def do_POST(self):  # noqa: N802 - stdlib handler contract
            length = int(self.headers.get("Content-Length") or 0)
            self._reply(
                {
                    "method": "POST",
                    "path": self.path,
                    "body": self.rfile.read(length).decode(),
                }
            )

        def _reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Upstream", "codenib-api")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    (tmp_path / "index.html").write_text("<title>CodeNib Wiki</title>")
    backend = ThreadingHTTPServer(("127.0.0.1", 0), APIHandler)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    server = build_server(
        tmp_path,
        api_base=f"http://127.0.0.1:{backend.server_port}",
        host="0.0.0.0",
        port=0,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/api/repos?limit=2") as response:
            get_payload = json.load(response)
            assert response.headers["X-Upstream"] == "codenib-api"
        request = urllib.request.Request(
            f"{base}/api/chat",
            data=b'{"question":"where?"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            post_payload = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        backend.shutdown()
        backend.server_close()
        backend_thread.join(timeout=2)

    assert get_payload == {"method": "GET", "path": "/api/repos?limit=2"}
    assert post_payload == {
        "method": "POST",
        "path": "/api/chat",
        "body": '{"question":"where?"}',
    }
