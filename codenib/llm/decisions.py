# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Typed decisions over OpenRouter's Decisions API, separate from chat completion."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

_Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
_Prompt = Union[_Text, dict[str, Any], list[Any]]
_Probability = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    instructions: _Prompt


class NoulQuestion(_Question):
    """Ask for the probability that a condition holds, without choosing a threshold."""

    type: Literal["noul"] = "noul"
    criteria: dict[Literal["true", "false"], _Prompt] | None = Field(
        default=None, min_length=2, max_length=2
    )


class ChoiceQuestion(_Question):
    """Choose among named alternatives described by their criteria."""

    type: Literal["choice"] = "choice"
    criteria: dict[_Text, _Prompt] = Field(min_length=2)


class ScoreQuestion(_Question):
    """Score on an ordered scale from zero to ``len(criteria) - 1``."""

    type: Literal["score"] = "score"
    criteria: list[_Prompt] = Field(min_length=2)


DecisionQuestion = Annotated[
    Union[NoulQuestion, ChoiceQuestion, ScoreQuestion], Field(discriminator="type")
]
_QUESTIONS = TypeAdapter(dict[_Text, DecisionQuestion])


class NoulAnswer(BaseModel):
    """The probability of yes; workflow thresholds remain the caller's policy."""

    type: Literal["noul"]
    noul: _Probability


class ChoiceAnswer(BaseModel):
    """Selected alternative, its full distribution, and model confidence."""

    type: Literal["choice"]
    choice: str
    probabilities: dict[str, _Probability]
    confidence: _Probability


class ScoreAnswer(BaseModel):
    """Expected position on the question's scale, with its full distribution."""

    type: Literal["score"]
    score: float = Field(strict=True, ge=0, allow_inf_nan=False)
    probabilities: dict[str, _Probability]
    confidence: _Probability
    legend: dict[str, _Prompt]


DecisionAnswer = Annotated[
    Union[NoulAnswer, ChoiceAnswer, ScoreAnswer], Field(discriminator="type")
]


class DecisionResult(BaseModel):
    """Answers and provider metadata, including actual token usage and cost."""

    answers: dict[str, DecisionAnswer]
    model: str
    id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)

    def validate_questions(self, questions: Mapping[str, DecisionQuestion]) -> None:
        """Reject mismatched answers before they can influence application policy."""
        if self.answers.keys() != questions.keys():
            raise ValueError("Decision answer IDs do not match the requested questions")
        for name, question in questions.items():
            answer = self.answers[name]
            if answer.type != question.type:
                raise ValueError(
                    f"Decision answer type does not match question {name!r}"
                )
            if isinstance(answer, NoulAnswer):
                continue
            if isinstance(question, ChoiceQuestion):
                expected = set(question.criteria)
                if answer.choice not in expected:
                    raise ValueError(f"Unknown choice for question {name!r}")
            else:
                expected = {str(i) for i in range(len(question.criteria))}
                if answer.score > len(question.criteria) - 1:
                    raise ValueError(f"Score outside the scale for question {name!r}")
                if set(answer.legend) != expected:
                    raise ValueError(f"Score legend does not match question {name!r}")
            # Jev rounds each probability to two decimal places on the wire.
            # Allow half a rounding unit per entry; retain the original values
            # and score rather than normalizing or recomputing provider output.
            rounding_tolerance = 0.005 * len(expected) + 1e-9
            if set(answer.probabilities) != expected or not math.isclose(
                math.fsum(answer.probabilities.values()),
                1.0,
                rel_tol=0,
                abs_tol=rounding_tolerance,
            ):
                raise ValueError(
                    f"Invalid probability distribution for question {name!r}"
                )


@dataclass
class OpenRouterDecisions:
    """Small synchronous client for Jev's ``state + questions -> answers`` API.

    Reads ``OPENROUTER_API_KEY`` at invocation time unless ``api_key`` is supplied.
    Only transport failures, HTTP 408/429, and HTTP 5xx are retried. This client
    has no chat, tool-calling, or text-generation interface.
    """

    model: str = "~typesafe/jev-latest"
    api_key: str | None = field(default=None, repr=False)
    timeout: float = 30.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        self.model = self.model.strip().removeprefix("openrouter/")
        if not self.model:
            raise ValueError("A Decisions model ID is required")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")

    def decide(
        self,
        *,
        state: Any,
        questions: Mapping[str, DecisionQuestion | dict[str, Any]],
    ) -> DecisionResult:
        """Evaluate a batch of typed questions against the same JSON-compatible state."""
        parsed = _QUESTIONS.validate_python(dict(questions))
        if not parsed:
            raise ValueError("At least one decision question is required")
        payload = {
            "model": self.model,
            "state": state,
            "questions": {
                name: question.model_dump(exclude_none=True)
                for name, question in parsed.items()
            },
        }
        # Fail locally on non-JSON state (including NaN), before sending any data.
        json.dumps(payload, allow_nan=False)
        key = (
            self.api_key
            if self.api_key is not None
            else os.getenv("OPENROUTER_API_KEY")
        )
        if not key or not key.strip():
            raise ValueError("Set OPENROUTER_API_KEY to use OpenRouter Decisions")

        import requests

        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    "https://openrouter.ai/api/alpha/decisions",
                    headers={
                        "Authorization": f"Bearer {key.strip()}",
                        "X-OpenRouter-Title": "CodeNib",
                    },
                    json=payload,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                response.raise_for_status()
                break
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if attempt == self.max_retries or not (
                    status in (408, 429) or 500 <= status < 600
                ):
                    raise
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.max_retries:
                    raise
            time.sleep(min(0.5 * 2**attempt, 8.0))

        result = DecisionResult.model_validate(response.json())
        result.validate_questions(parsed)
        return result
