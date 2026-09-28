# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""The optional funnel collector never accepts free-form visitor content."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        collector.post("/api/experience-events", content=b"x" * 257).status_code == 413
    )
    monkeypatch.setattr(experience, "monotonic", lambda: 100)
    for _ in range(120):
        assert collector.post("/api/experience-events", json=EVENT).status_code == 204
    assert collector.post("/api/experience-events", json=EVENT).status_code == 429
    assert len(experience._recent) == 120
    monkeypatch.setattr(experience, "monotonic", lambda: 160)
    assert collector.post("/api/experience-events", json=EVENT).status_code == 204
