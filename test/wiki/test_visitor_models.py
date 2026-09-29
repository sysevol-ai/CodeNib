# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

import json

import pytest
import requests
from pydantic import ValidationError

from codenib.web.visitor_wikis import GenerateWiki
from codenib.wiki import visitor_models
from codenib.wiki.visitor_models import (
    DEFAULT_WIKI_MODEL,
    WIKI_MODEL_CHOICES,
    WIKI_MODELS,
    wiki_model_catalog,
)
from codenib.wiki.visitor_provider import VisitorProvider


def capture(monkeypatch):
    sent = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_lines(self, **_):
            yield b"data: " + json.dumps(
                {"choices": [{"delta": {"content": "{}"}}]}
            ).encode()
            yield b"data: " + json.dumps(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"cost": 0.001},
                }
            ).encode()

    class Session(Response):
        def post(self, url, **options):
            sent.append(options["json"])
            return Response()

    monkeypatch.setattr(requests, "Session", Session)
    return sent


def test_choices_are_unique_and_include_the_default_and_tested_models():
    assert len(WIKI_MODELS) == len(set(WIKI_MODELS))
    assert DEFAULT_WIKI_MODEL in WIKI_MODELS
    tested = {model.id for model in WIKI_MODEL_CHOICES if model.tested}
    assert tested == {"deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-4.6"}
    assert len({model.provider for model in WIKI_MODEL_CHOICES}) >= 10


@pytest.mark.parametrize(
    "model, temperature, reasoning",
    [
        ("deepseek/deepseek-v4.1-flash", True, True),
        ("openai/gpt-6-sol", False, True),
        ("meta-llama/llama-4-maverick", True, False),
    ],
)
def test_requests_carry_only_parameters_the_model_accepts(
    monkeypatch, model, temperature, reasoning
):
    sent = capture(monkeypatch)
    provider = VisitorProvider("key", 1, lambda: None, lambda _: None, model=model)
    provider.complete(
        [{"role": "user", "content": "x"}],
        response_format={"type": "json_schema"},
    )
    payload = sent[0]
    assert payload["model"] == model
    assert ("temperature" in payload) is temperature
    assert ("reasoning" in payload) is reasoning
    assert payload["provider"]["require_parameters"] is True


def test_the_route_accepts_listed_models_only():
    assert GenerateWiki(repository="a/b", budget_usd=1.0, model="z-ai/glm-5.3").model
    with pytest.raises(ValidationError):
        GenerateWiki(repository="a/b", budget_usd=1.0, model="vendor/unlisted")


@pytest.fixture
def empty_cache(monkeypatch):
    monkeypatch.setattr(visitor_models, "_cache", {"at": 0.0, "prices": None})


def test_catalog_refreshes_prices_and_drops_models_openrouter_no_longer_lists(
    empty_cache,
):
    live = {model.id: (9.0, 99.0) for model in WIKI_MODEL_CHOICES[1:]}
    catalog = wiki_model_catalog(fetch=lambda: live, now=lambda: 100.0)
    ids = [row["id"] for row in catalog["models"]]
    assert WIKI_MODEL_CHOICES[0].id not in ids
    assert catalog["prices"] == "live"
    assert all(row["input_usd"] == 9.0 for row in catalog["models"])
    assert "temperature" not in catalog["models"][0]


def test_catalog_keeps_recorded_prices_when_the_catalog_is_unreachable(empty_cache):
    def offline():
        raise requests.ConnectionError("offline")

    catalog = wiki_model_catalog(fetch=offline)
    assert catalog["prices"] == "recorded"
    assert [row["id"] for row in catalog["models"]] == list(WIKI_MODELS)
    assert catalog["default"] == DEFAULT_WIKI_MODEL


def test_catalog_reuses_fresh_prices(empty_cache):
    calls = []

    def fetch():
        calls.append(1)
        return {model.id: (1.0, 2.0) for model in WIKI_MODEL_CHOICES}

    wiki_model_catalog(fetch=fetch, now=lambda: 10.0)
    wiki_model_catalog(fetch=fetch, now=lambda: 20.0)
    assert len(calls) == 1
