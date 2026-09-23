# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Decisions HTTP contract tests. All responses are local; no API calls."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import requests

from codenib.llm import (
    ChoiceAnswer,
    ChoiceQuestion,
    NoulAnswer,
    NoulQuestion,
    OpenRouterDecisions,
    ScoreAnswer,
    ScoreQuestion,
)


def _response(answers=None, status=200):
    response = requests.Response()
    response.status_code = status
    response.url = "https://openrouter.ai/api/alpha/decisions"
    response._content = json.dumps(
        {
            "model": "typesafe/jev-1.13-20260917",
            "answers": answers or {},
            "id": "decision-test",
            "usage": {"input_tokens": 100, "output_tokens": 10, "cost": 0.00001},
        }
    ).encode()
    return response


@pytest.fixture
def http(monkeypatch):
    """Capture prepared HTTP requests and serve a deterministic sequence."""
    calls = []
    responses = []

    def send(_session, request, **kwargs):
        calls.append((request, kwargs))
        assert responses, "Unexpected HTTP request"
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("codenib.llm.decisions.time.sleep", lambda _delay: None)
    return calls, responses


def test_mixed_questions_use_decisions_endpoint_and_preserve_distributions(http):
    calls, responses = http
    responses.append(
        _response(
            {
                "urgent": {"type": "noul", "noul": 0.9},
                "team": {
                    "type": "choice",
                    "choice": "billing",
                    "confidence": 0.6,
                    "probabilities": {"billing": 0.85, "engineering": 0.15},
                },
                "severity": {
                    "type": "score",
                    "score": 1.8,
                    "confidence": 0.8,
                    "probabilities": {"0": 0.0, "1": 0.2, "2": 0.8},
                    "legend": {"0": "low", "1": "medium", "2": "high"},
                },
            }
        )
    )
    result = OpenRouterDecisions(model="openrouter/~typesafe/jev-latest").decide(
        state={"ticket": "Payouts are failing"},
        questions={
            "urgent": NoulQuestion(instructions="Is this urgent?"),
            "team": ChoiceQuestion(
                instructions="Who should handle this?",
                criteria={"billing": "Payments", "engineering": "Bugs"},
            ),
            "severity": {
                "type": "score",
                "instructions": "How severe is it?",
                "criteria": ["low", "medium", "high"],
            },
        },
    )

    request, options = calls[0]
    assert request.method == "POST"
    assert request.url == "https://openrouter.ai/api/alpha/decisions"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert request.headers["Content-Type"] == "application/json"
    assert options["timeout"] == 30.0
    assert options["allow_redirects"] is False
    body = json.loads(request.body)
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == "~typesafe/jev-latest"
    assert "criteria" not in body["questions"]["urgent"]
    assert isinstance(result.answers["urgent"], NoulAnswer)
    assert isinstance(result.answers["team"], ChoiceAnswer)
    assert isinstance(result.answers["severity"], ScoreAnswer)
    assert result.answers["severity"].score == 1.8
    assert result.answers["team"].probabilities["billing"] == 0.85
    assert result.model == "typesafe/jev-1.13-20260917"
    assert result.usage["cost"] == 0.00001
    assert result.id == "decision-test"


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "noul", "noul": True},
        {"type": "noul", "noul": "0.9"},
        {"type": "noul", "noul": -0.1},
        {"type": "noul", "noul": 1.1},
        {"type": "noul", "noul": float("nan")},
        {"type": "noul", "noul": float("inf")},
        {
            "type": "choice",
            "choice": "yes",
            "confidence": 1,
            "probabilities": {"yes": 1},
        },
    ],
)
def test_invalid_or_mismatched_answers_are_not_coerced(http, answer):
    calls, responses = http
    responses.append(_response({"relevant": answer}))
    with pytest.raises(ValueError):
        OpenRouterDecisions().decide(
            state="code", questions={"relevant": NoulQuestion(instructions="Relevant?")}
        )
    assert len(calls) == 1


@pytest.mark.parametrize("answers", [{}, {"wrong_id": {"type": "noul", "noul": 1}}])
def test_missing_or_unknown_question_ids_fail_without_retry(http, answers):
    calls, responses = http
    responses.append(_response(answers))
    with pytest.raises(ValueError, match="IDs"):
        OpenRouterDecisions().decide(
            state="code", questions={"relevant": NoulQuestion(instructions="Relevant?")}
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"score": 1.1},
        {"score": float("nan")},
        {"probabilities": {"0": 0.2, "1": 0.2}},
        {"probabilities": {"0": 0.2, "2": 0.8}},
        {"legend": {"0": "no", "2": "yes"}},
    ],
)
def test_score_is_validated_against_requested_scale(http, changes):
    _calls, responses = http
    answer = {
        "type": "score",
        "score": 0.8,
        "confidence": 0.5,
        "probabilities": {"0": 0.2, "1": 0.8},
        "legend": {"0": "no", "1": "yes"},
        **changes,
    }
    responses.append(_response({"relevant": answer}))
    with pytest.raises(ValueError):
        OpenRouterDecisions().decide(
            state="code",
            questions={
                "relevant": ScoreQuestion(
                    instructions="Relevant?", criteria=["no", "yes"]
                )
            },
        )


@pytest.mark.parametrize(
    "probabilities",
    [
        {"0": 0.54, "1": 0.33, "2": 0.10, "3": 0.02},
        {"0": 0.55, "1": 0.34, "2": 0.10, "3": 0.02},
    ],
)
def test_rounded_distribution_preserves_provider_score_and_probabilities(
    http, probabilities
):
    """Live Jev responses can total 0.99 or 1.01 after wire rounding."""
    _calls, responses = http
    criteria = ["unrelated", "terminology", "supporting", "direct"]
    responses.append(
        _response(
            {
                "relevant": {
                    "type": "score",
                    "score": 0.61,
                    "confidence": 0.2,
                    "probabilities": probabilities,
                    "legend": {str(i): label for i, label in enumerate(criteria)},
                }
            }
        )
    )
    result = OpenRouterDecisions().decide(
        state="code",
        questions={
            "relevant": ScoreQuestion(instructions="Relevant?", criteria=criteria)
        },
    )
    assert result.answers["relevant"].probabilities == probabilities
    assert result.answers["relevant"].score == 0.61


def test_unknown_choice_is_rejected(http):
    _calls, responses = http
    responses.append(
        _response(
            {
                "team": {
                    "type": "choice",
                    "choice": "unknown",
                    "confidence": 1,
                    "probabilities": {"billing": 0.9, "engineering": 0.1},
                }
            }
        )
    )
    with pytest.raises(ValueError, match="Unknown choice"):
        OpenRouterDecisions().decide(
            state="ticket",
            questions={
                "team": ChoiceQuestion(
                    instructions="Team?",
                    criteria={"billing": "Payments", "engineering": "Bugs"},
                )
            },
        )


@pytest.mark.parametrize(
    "questions",
    [
        {},
        {"q": {"type": "score", "instructions": "Rate it", "criteria": ["one"]}},
        {"q": {"type": "noul", "instructions": "   "}},
        {"q": {"type": "noul", "instructions": "Yes?", "criteria": {"yes": "yes"}}},
        {"q": {"type": "noul", "instructions": "Yes?", "temperature": 0.5}},
    ],
)
def test_invalid_questions_fail_before_http(http, questions):
    calls, _responses = http
    with pytest.raises(ValueError):
        OpenRouterDecisions().decide(state="test", questions=questions)
    assert calls == []


def test_missing_key_and_non_json_state_fail_locally(http, monkeypatch):
    calls, _responses = http
    monkeypatch.delenv("OPENROUTER_API_KEY")
    questions = {"q": NoulQuestion(instructions="Yes?")}
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterDecisions().decide(state="test", questions=questions)
    client = OpenRouterDecisions(api_key="private-key")
    assert "private-key" not in repr(client)
    with pytest.raises(ValueError):
        client.decide(state={"x": float("nan")}, questions=questions)
    assert calls == []


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 413])
def test_permanent_http_errors_are_not_retried(http, status):
    calls, responses = http
    responses.append(_response(status=status))
    with pytest.raises(requests.HTTPError):
        OpenRouterDecisions().decide(
            state="test", questions={"q": NoulQuestion(instructions="Yes?")}
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    [408, 429, 500, 502, 503, 524, 529, requests.Timeout(), requests.ConnectionError()],
)
def test_transient_failure_retries_then_returns_answer(http, failure):
    calls, responses = http
    responses.extend(
        [
            _response(status=failure) if isinstance(failure, int) else failure,
            _response({"q": {"type": "noul", "noul": 0.7}}),
        ]
    )
    result = OpenRouterDecisions().decide(
        state="test", questions={"q": NoulQuestion(instructions="Yes?")}
    )
    assert result.answers["q"].noul == 0.7
    assert len(calls) == 2


def test_retry_budget_is_bounded(http):
    calls, responses = http
    responses.extend(_response(status=503) for _ in range(3))
    with pytest.raises(requests.HTTPError):
        OpenRouterDecisions(max_retries=2).decide(
            state="test", questions={"q": NoulQuestion(instructions="Yes?")}
        )
    assert len(calls) == 3


def test_decision_import_does_not_load_chat_or_http_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from codenib.llm import OpenRouterDecisions, ScoreQuestion; "
            "assert 'litellm' not in sys.modules; assert 'requests' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
