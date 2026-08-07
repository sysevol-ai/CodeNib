# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Streaming ASGI request-body limits for direct API deployments."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .limits import MAX_REQUEST_BODY_BYTES


class RequestBodyLimitMiddleware:
    """Buffer one bounded HTTP body before request-model parsing begins."""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = MAX_REQUEST_BODY_BYTES,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await self._reject(scope, receive, send, 400, "Ambiguous Content-Length.")
            return
        if content_lengths:
            raw_length = content_lengths[0]
            if not raw_length.isdigit():
                await self._reject(scope, receive, send, 400, "Invalid Content-Length.")
                return
            if int(raw_length) > self.max_bytes:
                await self._reject(
                    scope,
                    receive,
                    send,
                    413,
                    "Request body exceeds the CodeNib API limit.",
                )
                return

        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await self._reject(
                    scope,
                    receive,
                    send,
                    413,
                    "Request body exceeds the CodeNib API limit.",
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        first_receive = True

        async def replay_receive() -> Message:
            nonlocal first_receive
            if first_receive:
                first_receive = False
                if disconnected:
                    return {"type": "http.disconnect"}
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse({"detail": detail}, status_code=status_code)
        await response(scope, receive, send)
