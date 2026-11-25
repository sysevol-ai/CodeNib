"""Serve the built window in a browser for checks, without the app process.

The page is served from dist/; requests to /__proxy/<port>/api/... are
forwarded to a real `codenib wiki` service on 127.0.0.1:<port>, with the
session header the page passes. tools/bridge-mock.js stands in for the app's
Tauri bridge and sends its `api` calls here.

    python tools/preview.py 8735
"""

from __future__ import annotations

import http.server
import sys
import urllib.error
import urllib.request
from pathlib import Path

DIST = Path(__file__).resolve().parent.parent / "dist"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def log_message(self, *_args):
        pass

    def _proxy(self) -> bool:
        if not self.path.startswith("/__proxy/"):
            return False
        _, _, port, rest = self.path.split("/", 3)
        if not port.isdigit() or not rest.startswith("api/"):
            self.send_error(400)
            return True
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/{rest}",
            data=body,
            method=self.command,
            headers={
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "X-CodeNib-Session": self.headers.get("X-CodeNib-Session", ""),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                status, payload = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return True

    def do_GET(self):  # noqa: N802
        if self._proxy():
            return
        if not (DIST / self.path.lstrip("/").split("?")[0]).is_file():
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):  # noqa: N802
        self._proxy()

    def do_PATCH(self):  # noqa: N802
        self._proxy()

    def do_PUT(self):  # noqa: N802
        self._proxy()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8735
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
