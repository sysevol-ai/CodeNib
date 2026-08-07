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
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from .limits import MAX_PROXY_RESPONSE_BYTES, MAX_REQUEST_BODY_BYTES

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_MAX_PROXY_REQUEST_BYTES = MAX_REQUEST_BODY_BYTES
_MAX_PROXY_RESPONSE_BYTES = MAX_PROXY_RESPONSE_BYTES


def runtime_config(api_base: str) -> bytes:
    value = api_base.rstrip("/")
    return f"window.__CODENIB_API_BASE__ = {json.dumps(value)};\n".encode("utf-8")


class WikiStaticHandler(SimpleHTTPRequestHandler):
    """Serve the Wiki SPA and proxy its API requests to the local backend."""

    api_base = ""
    browser_api_base = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        request_path = urlsplit(self.path).path
        if request_path == "/api" or request_path.startswith("/api/"):
            self._proxy_api_request()
            return
        if request_path == "/runtime-config.js":
            body = runtime_config(self.browser_api_base)
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

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        request_path = urlsplit(self.path).path
        if request_path == "/api" or request_path.startswith("/api/"):
            self._proxy_api_request()
            return
        relative = request_path.lstrip("/")
        candidate = Path(self.directory, relative)
        if request_path != "/" and not candidate.is_file():
            self.path = "/index.html"
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        self._proxy_api_request()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler contract
        self._proxy_api_request()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler contract
        self._proxy_api_request()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler contract
        self._proxy_api_request()

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler contract
        self._proxy_api_request()

    def _proxy_api_request(self) -> None:
        request_path = urlsplit(self.path).path
        if request_path != "/api" and not request_path.startswith("/api/"):
            self.send_error(404)
            return

        if self.headers.get("Transfer-Encoding"):
            self._send_proxy_error(
                400,
                "Chunked request bodies are not supported by the Wiki proxy.",
            )
            return
        raw_content_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_content_length) if raw_content_length else 0
        except ValueError:
            self._send_proxy_error(400, "Invalid Content-Length header.")
            return
        if content_length < 0:
            self._send_proxy_error(400, "Content-Length must not be negative.")
            return
        if content_length > _MAX_PROXY_REQUEST_BYTES:
            self._send_proxy_error(413, "Request body exceeds the Wiki proxy limit.")
            return
        body = self.rfile.read(content_length) if content_length else None
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower()
            not in {
                *_HOP_BY_HOP_HEADERS,
                "accept-encoding",
                "content-length",
                "host",
            }
        }
        target = f"{self.api_base.rstrip('/')}{self.path}"
        request = urllib_request.Request(
            target,
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            response = urllib_request.urlopen(request, timeout=90)
        except urllib_error.HTTPError as exc:
            response = exc
        except (OSError, urllib_error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            payload = json.dumps(
                {"detail": f"CodeNib API is unavailable: {reason}"}
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return

        try:
            if self.command == "HEAD":
                payload = b""
            else:
                raw_response_length = response.headers.get("Content-Length")
                try:
                    response_length = (
                        int(raw_response_length) if raw_response_length else None
                    )
                except ValueError:
                    self._send_proxy_error(
                        502, "Invalid upstream Content-Length header."
                    )
                    return
                if response_length is not None and response_length < 0:
                    self._send_proxy_error(
                        502, "Upstream Content-Length must not be negative."
                    )
                    return
                if (
                    response_length is not None
                    and response_length > _MAX_PROXY_RESPONSE_BYTES
                ):
                    self._send_proxy_error(
                        502, "CodeNib API response exceeds the Wiki proxy limit."
                    )
                    return
                payload = response.read(_MAX_PROXY_RESPONSE_BYTES + 1)
                if len(payload) > _MAX_PROXY_RESPONSE_BYTES:
                    self._send_proxy_error(
                        502, "CodeNib API response exceeds the Wiki proxy limit."
                    )
                    return
            self.send_response(response.status)
            for name, value in response.headers.items():
                if name.lower() in {
                    *_HOP_BY_HOP_HEADERS,
                    "content-length",
                    "date",
                    "server",
                }:
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        finally:
            response.close()

    def _send_proxy_error(self, status: int, detail: str) -> None:
        payload = json.dumps({"detail": detail}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

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
    browser_api_base: str = "",
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    root = directory.expanduser().resolve()
    if not (root / "index.html").is_file():
        raise FileNotFoundError(f"prebuilt Wiki index is missing under {root}")
    handler_type = type(
        "ConfiguredWikiStaticHandler",
        (WikiStaticHandler,),
        {
            "api_base": api_base.rstrip("/"),
            "browser_api_base": browser_api_base.rstrip("/"),
        },
    )
    handler = partial(handler_type, directory=str(root))
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--browser-api-base", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args(argv)

    server = build_server(
        args.directory,
        api_base=args.api_base,
        browser_api_base=args.browser_api_base,
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
