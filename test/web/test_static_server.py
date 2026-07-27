# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
import urllib.request

from codenib.web.static_server import build_server


def test_static_server_injects_api_base_and_falls_back_to_spa(tmp_path):
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

    assert config == ('window.__CODENIB_API_BASE__ = "http://127.0.0.1:8123";\n')
    assert "CodeNib Wiki" in page
    assert "ready" in asset
