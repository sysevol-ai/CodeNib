# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""ASGI-level request-body limit contracts."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from codenib.web.app import app
from codenib.web.request_limits import RequestBodyLimitMiddleware


async def _consume_body(scope, receive, send):
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _request(*, headers=(), chunks=()):
    incoming = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ] or [{"type": "http.request", "body": b"", "more_body": False}]
    outgoing = []

    async def receive():
        return incoming.pop(0)

    async def send(message):
        outgoing.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/chat",
        "raw_path": b"/api/chat",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 10000),
        "server": ("127.0.0.1", 8000),
    }
    asyncio.run(
        RequestBodyLimitMiddleware(_consume_body, max_bytes=4)(scope, receive, send)
    )
    status = next(
        message["status"]
        for message in outgoing
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in outgoing
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body) if body else None


def test_rejects_declared_body_before_reading_it() -> None:
    status, body = _request(headers=((b"content-length", b"5"),), chunks=(b"x",))

    assert status == 413
    assert "exceeds" in body["detail"]


def test_rejects_streamed_body_without_content_length() -> None:
    status, body = _request(chunks=(b"abc", b"de"))

    assert status == 413
    assert "exceeds" in body["detail"]


def test_accepts_streamed_body_at_the_limit() -> None:
    status, body = _request(chunks=(b"ab", b"cd"))

    assert status == 204
    assert body is None


def test_rejects_invalid_or_ambiguous_content_length() -> None:
    invalid_status, _ = _request(headers=((b"content-length", b"-1"),))
    duplicate_status, _ = _request(
        headers=((b"content-length", b"1"), (b"content-length", b"1"))
    )

    assert invalid_status == 400
    assert duplicate_status == 400


def test_validation_error_does_not_echo_rejected_prompt() -> None:
    response = TestClient(app).post(
        "/api/chat",
        json={
            "repo_id": "repo",
            "messages": [{"role": "user", "content": "secret" * 11_000}],
        },
    )

    assert response.status_code == 422
    assert len(response.content) < 2_048
    assert "input" not in response.json()["detail"][0]
    assert b"secret" not in response.content
