# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Serve the prebuilt Wiki SPA with runtime API configuration."""

from __future__ import annotations

import argparse
import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def runtime_config(api_base: str) -> bytes:
    value = api_base.rstrip("/")
    return f"window.__CODENIB_API_BASE__ = {json.dumps(value)};\n".encode("utf-8")


class WikiStaticHandler(SimpleHTTPRequestHandler):
    """Static-file handler with one SPA fallback and a generated config file."""

    api_base = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        request_path = urlsplit(self.path).path
        if request_path == "/runtime-config.js":
            body = runtime_config(self.api_base)
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        relative = request_path.lstrip("/")
        candidate = Path(self.directory, relative)
        if request_path != "/" and not candidate.is_file():
            self.path = "/index.html"
        super().do_GET()

    def end_headers(self) -> None:
        request_path = urlsplit(self.path).path
        if request_path.startswith("/assets/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif request_path == "/index.html" or request_path == "/":
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        if os.environ.get("CODENIB_WEB_ACCESS_LOG"):
            super().log_message(format, *args)


def build_server(
    directory: Path,
    *,
    api_base: str,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    root = directory.expanduser().resolve()
    if not (root / "index.html").is_file():
        raise FileNotFoundError(f"prebuilt Wiki index is missing under {root}")
    handler_type = type(
        "ConfiguredWikiStaticHandler",
        (WikiStaticHandler,),
        {"api_base": api_base},
    )
    handler = partial(handler_type, directory=str(root))
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args(argv)

    server = build_server(
        args.directory,
        api_base=args.api_base,
        host=args.host,
        port=args.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
