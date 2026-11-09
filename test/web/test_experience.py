# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""The optional funnel collector never accepts free-form visitor content."""

import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from codenib.web import experience


@pytest.fixture
def collector(monkeypatch, caplog):
    monkeypatch.setenv("CODENIB_EXPERIENCE_EVENTS", "1")
    monkeypatch.setattr(experience.logger, "handlers", [caplog.handler])
    caplog.set_level(logging.INFO)
    experience._recent.clear()
    app = FastAPI()
    app.include_router(experience.router)
    yield TestClient(app)
    experience._recent.clear()


EVENT = dict(event="example_source_open", surface="wiki", visit="a" * 32)


def test_collector_is_explicitly_enabled(collector, monkeypatch, caplog):
    monkeypatch.delenv("CODENIB_EXPERIENCE_EVENTS")
    assert collector.post("/api/experience-events", json=EVENT).status_code == 204
    assert not any("experience_event" in r.message for r in caplog.records)


def test_logs_only_validated_funnel_fields(collector, caplog):
    assert collector.post("/api/experience-events", json=EVENT).status_code == 204
    messages = [r.message for r in caplog.records if r.name == experience.logger.name]
    assert messages == [
        "experience_event event=example_source_open surface=wiki visit=" + "a" * 32
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {**EVENT, "repository": "private/input"},
        {**EVENT, "event": "secret-user-prompt"},
        {**EVENT, "visit": "sk-or-not-a-visit"},
        {**EVENT, "surface": "https://user:secret@example.com"},
    ],
)
def test_rejects_user_content_without_echoing_it(collector, caplog, payload):
    response = collector.post("/api/experience-events", json=payload)
    assert response.status_code == 422
    assert not response.content
    assert not any("experience_event" in r.message for r in caplog.records)


def test_collector_bounds_payload_and_log_volume(collector, monkeypatch, caplog):
    assert (
        collector.post("/api/experience-events", content=b"x" * 513).status_code == 413
    )
    monkeypatch.setattr(experience, "monotonic", lambda: 100)
    for _ in range(120):
        assert collector.post("/api/experience-events", json=EVENT).status_code == 204
    assert collector.post("/api/experience-events", json=EVENT).status_code == 429
    assert len(experience._recent) == 120
    monkeypatch.setattr(experience, "monotonic", lambda: 160)
    assert collector.post("/api/experience-events", json=EVENT).status_code == 204


def test_collector_stops_reading_oversized_stream(collector, caplog):
    chunks = iter([b"x" * 400, b"x" * 200])

    async def receive():
        chunk = next(chunks, None)
        assert chunk is not None, "Oversized uploads must not be drained into memory"
        return {"type": "http.request", "body": chunk, "more_body": True}

    response = asyncio.run(
        experience.experience_event(
            Request({"type": "http", "method": "POST", "headers": []}, receive)
        )
    )
    assert response.status_code == 413
    assert not any("experience_event" in r.message for r in caplog.records)


def test_cli_surface_carries_only_the_guard_first_run(collector, caplog):
    first_run = dict(event="guard_first_run", surface="cli", visit="b" * 32)
    assert collector.post("/api/experience-events", json=first_run).status_code == 204
    for payload in (
        {**first_run, "surface": "landing"},
        {**first_run, "event": "page_view"},
    ):
        assert collector.post("/api/experience-events", json=payload).status_code == 422
    messages = [r.message for r in caplog.records if r.name == experience.logger.name]
    assert messages == [
        "experience_event event=guard_first_run surface=cli visit=" + "b" * 32
    ]


def _messages(caplog):
    return [r.message for r in caplog.records if r.name == experience.logger.name]


def test_page_view_carries_one_arrival_class(collector, caplog):
    view = dict(event="page_view", surface="wiki", visit="c" * 32, arrival="search")
    assert collector.post("/api/experience-events", json=view).status_code == 204
    landing = dict(
        event="page_view",
        surface="landing",
        visit="d" * 32,
        arrival="wiki_link",
        wiki="psf/requests",
    )
    assert collector.post("/api/experience-events", json=landing).status_code == 204
    assert _messages(caplog) == [
        "experience_event event=page_view surface=wiki visit="
        + "c" * 32
        + " arrival=search",
        "experience_event event=page_view surface=landing visit="
        + "d" * 32
        + " arrival=wiki_link wiki=psf/requests",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        # Only a page view says how the tab arrived.
        {**EVENT, "arrival": "search"},
        # Only the landing page names the Wiki that sent the visit.
        dict(
            event="page_view",
            surface="wiki",
            visit="e" * 32,
            arrival="wiki_link",
            wiki="psf/requests",
        ),
        dict(event="page_view", surface="landing", visit="e" * 32, arrival="wiki_link"),
        dict(event="page_view", surface="landing", visit="e" * 32, wiki="psf/requests"),
        dict(
            event="page_view",
            surface="landing",
            visit="e" * 32,
            arrival="search",
            wiki="psf/requests",
        ),
        dict(event="page_view", surface="landing", visit="e" * 32, arrival="email"),
        *(
            dict(
                event="page_view",
                surface="landing",
                visit="e" * 32,
                arrival="wiki_link",
                wiki=name,
            )
            for name in ("..", "owner/..", "a/b/c", "https://x.test/", "a b", "")
        ),
    ],
)
def test_rejects_arrivals_outside_their_contract(collector, caplog, payload):
    response = collector.post("/api/experience-events", json=payload)
    assert response.status_code == 422
    assert not response.content
    assert not _messages(caplog)
