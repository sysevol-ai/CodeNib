# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json

import pytest
import requests

from codenib.wiki.visitor_provider import FLASH_MODEL, VisitorProvider, WikiRunStopped


def stream(monkeypatch, events, *, status=200):
    sent = []

    class Response:
        status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_lines(self, **_):
            for event in events:
                if isinstance(event, Exception):
                    raise event
                yield (
                    event
                    if isinstance(event, bytes)
                    else b"data: " + json.dumps(event).encode()
                )

    class Session(Response):
        def post(self, url, **options):
            sent.append((url, options))
            return Response()

    monkeypatch.setattr(requests, "Session", Session)
    return sent


def test_stream_reports_receipt_before_completion_without_publishing_draft(monkeypatch):
    updates = []
    sent = stream(
        monkeypatch,
        [
            b": keep-alive",
            {"choices": [{"delta": {"reasoning": "PRIVATE_REASONING"}}]},
            {"choices": [{"delta": {"content": "UNVALIDATED_DRAFT"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"cost": 0.02},
            },
            b"data: [DONE]",
        ],
    )
    provider = VisitorProvider(
        "private-key", 1, lambda: None, updates.append, model=FLASH_MODEL
    )
    assert provider.complete([]) == "UNVALIDATED_DRAFT"
    assert any(
        update["response_chars"] > 0 and update["request_active"] for update in updates
    )
    assert updates[-1]["request_active"] is False
    assert updates[-1]["reported_cost_usd"] == 0.02
    assert updates[-1]["unreported_call_cost"] is False
    assert "UNVALIDATED_DRAFT" not in json.dumps(updates)
    assert "PRIVATE_REASONING" not in json.dumps(updates)
    assert sent[0][1]["json"]["model"] == FLASH_MODEL
    assert sent[0][1]["json"]["provider"]["sort"] == "throughput"
    assert sent[0][1]["allow_redirects"] is False


@pytest.mark.parametrize(
    "tail",
    [
        b"data: [DONE]",
        {"error": {"message": "PRIVATE_PROVIDER_ERROR"}},
        requests.ConnectionError("PRIVATE_NETWORK_ERROR"),
    ],
)
def test_incomplete_stream_stops_without_retries_or_known_charge(monkeypatch, tail):
    sent = stream(monkeypatch, [{"choices": [{"delta": {"content": "draft"}}]}, tail])
    provider = VisitorProvider("private-key", 1, lambda: None, lambda _: None)
    with pytest.raises(WikiRunStopped) as stopped:
        provider.complete([])
    assert "PRIVATE" not in str(stopped.value)
    assert provider.unknown
    with pytest.raises(WikiRunStopped):
        provider.complete([])
    assert len(sent) == 1


def test_stream_cancellation_does_not_wait_for_whole_response(monkeypatch):
    stream(monkeypatch, [{"choices": [{"delta": {"content": "draft"}}]}])
    checks = []

    def check():
        checks.append(1)
        if len(checks) == 2:
            raise WikiRunStopped("Stopped by owner")

    provider = VisitorProvider("private-key", 1, check, lambda _: None)
    with pytest.raises(WikiRunStopped, match="Stopped by owner"):
        provider.complete([])
    assert provider.unknown
