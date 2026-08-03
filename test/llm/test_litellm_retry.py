# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the LLM-call retry policy (issue #109).

Transient errors (rate-limit / timeout / upstream 5xx / connection blips)
must be retried with exponential backoff up to a configurable max; a single
transient blip followed by success returns the success. Non-transient errors
(auth, bad request, content policy, context-window-exceeded) must NOT be
retried.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import litellm
import pytest

from codenib.llm.litellm_chat import (
    LiteLLMChat,
    RetryConfig,
    _no_thinking_kwargs,
    is_transient_error,
)


def _make_response(content="ok"):
    msg = SimpleNamespace(role="assistant", content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def _no_sleep():
    """Patch out the backoff sleep so tests don't actually wait."""
    return patch("codenib.llm.litellm_chat.time.sleep", return_value=None)


# ---------------------------------------------------------------------------
# Gemini thinking parameter normalization
# ---------------------------------------------------------------------------


class TestNoThinkingKwargs:
    def test_gemini_25_uses_litellm_thinking_zero_budget(self):
        assert _no_thinking_kwargs("vertex_ai/gemini-2.5-pro") == {
            "thinking": {"type": "disabled", "budget_tokens": 0}
        }

    def test_non_thinking_model_has_no_extra_kwargs(self):
        assert _no_thinking_kwargs("openai/gpt-4o-mini") == {}


# ---------------------------------------------------------------------------
# RetryConfig.backoff
# ---------------------------------------------------------------------------


class TestRetryConfig:
    def test_backoff_is_exponential_and_capped(self):
        rc = RetryConfig(base_delay=0.5, max_delay=8.0, jitter=0.0)
        delays = [rc.backoff(i) for i in range(6)]
        assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]

    def test_jitter_stays_within_bounds(self):
        rc = RetryConfig(base_delay=1.0, max_delay=100.0, jitter=0.1)
        # base for attempt 0 is 1.0, jitter adds up to 10% → [1.0, 1.1)
        for _ in range(50):
            d = rc.backoff(0)
            assert 1.0 <= d < 1.1


# ---------------------------------------------------------------------------
# Transient classification
# ---------------------------------------------------------------------------


class TestIsTransientError:
    def test_rate_limit_is_transient(self):
        exc = litellm.RateLimitError("rate limited", "gpt-4o", "openai")
        assert is_transient_error(exc) is True

    def test_timeout_is_transient(self):
        exc = litellm.Timeout("timed out", "gpt-4o", "openai")
        assert is_transient_error(exc) is True

    def test_stdlib_timeout_is_transient(self):
        assert is_transient_error(TimeoutError("slow")) is True

    def test_authentication_is_not_transient(self):
        exc = litellm.AuthenticationError("bad key", "gpt-4o", "openai")
        assert is_transient_error(exc) is False

    def test_bad_request_is_not_transient(self):
        exc = litellm.BadRequestError("nope", "gpt-4o", "openai")
        assert is_transient_error(exc) is False

    def test_context_window_exceeded_is_not_transient(self):
        exc = litellm.ContextWindowExceededError("too big", "gpt-4o", "openai")
        assert is_transient_error(exc) is False

    def test_plain_value_error_is_not_transient(self):
        assert is_transient_error(ValueError("boom")) is False


# ---------------------------------------------------------------------------
# _completion_with_retry / _call_raw integration
# ---------------------------------------------------------------------------


class TestCompletionWithRetry:
    def test_chat_rejects_extra_kwargs_that_replace_managed_fields(self):
        with pytest.raises(ValueError, match="cannot override.*model"):
            LiteLLMChat(
                model="openai/local",
                extra_kwargs={"model": "anthropic/other"},
            )

    def test_complete_returns_stripped_text_and_forwards_endpoint(self):
        chat = LiteLLMChat(
            model="openai/local",
            api_base="http://localhost:4000/v1",
            api_key="secret",
            extra_kwargs={
                "api_version": "2025-01-01",
                "extra_body": {"reasoning": {"enabled": False}},
            },
            retry=RetryConfig(max_retries=0),
        )
        with patch(
            "codenib.llm.litellm_chat.litellm.completion",
            return_value=_make_response("  answer  "),
        ) as mock_completion:
            text = chat.complete([{"role": "user", "content": "hi"}], max_tokens=12)

        assert text == "answer"
        kwargs = mock_completion.call_args.kwargs
        assert kwargs["api_base"] == "http://localhost:4000/v1"
        assert kwargs["api_key"] == "secret"
        assert kwargs["max_tokens"] == 12
        assert kwargs["api_version"] == "2025-01-01"
        assert kwargs["extra_body"] == {"reasoning": {"enabled": False}}

    def test_cache_identity_excludes_secret_but_tracks_endpoint(self):
        first = LiteLLMChat(
            model="openai/local",
            api_base="http://one/v1",
            api_key="first-secret",
        )
        rotated = LiteLLMChat(
            model="openai/local",
            api_base="http://one/v1",
            api_key="rotated-secret",
        )
        moved = LiteLLMChat(
            model="openai/local",
            api_base="http://two/v1",
            api_key="first-secret",
        )
        configured = LiteLLMChat(
            model="openai/local",
            api_base="http://one/v1",
            extra_kwargs={"api_version": "2025-01-01"},
        )
        reconfigured = LiteLLMChat(
            model="openai/local",
            api_base="http://one/v1",
            extra_kwargs={"api_version": "2026-01-01"},
        )

        assert first.cache_identity == rotated.cache_identity
        assert first.cache_identity != moved.cache_identity
        assert configured.cache_identity != reconfigured.cache_identity
        assert "secret" not in first.cache_identity

    def test_transient_then_success_is_retried(self):
        """A transient error once, then success → retried and returns success."""
        chat = LiteLLMChat(
            model="gpt-4o", retry=RetryConfig(max_retries=2, base_delay=0.01)
        )
        good = _make_response("recovered")
        side_effects = [
            litellm.RateLimitError("rate limited", "gpt-4o", "openai"),
            good,
        ]
        with (
            patch(
                "codenib.llm.litellm_chat.litellm.completion",
                side_effect=side_effects,
            ) as mock_completion,
            _no_sleep(),
        ):
            resp = chat._call_raw([{"role": "user", "content": "hi"}])

        assert resp is good
        assert mock_completion.call_count == 2  # one failure + one success

    def test_non_transient_error_is_not_retried(self):
        """A non-transient error is raised immediately — never retried."""
        chat = LiteLLMChat(
            model="gpt-4o", retry=RetryConfig(max_retries=5, base_delay=0.01)
        )
        with (
            patch(
                "codenib.llm.litellm_chat.litellm.completion",
                side_effect=litellm.AuthenticationError("bad key", "gpt-4o", "openai"),
            ) as mock_completion,
            _no_sleep(),
        ):
            with pytest.raises(litellm.AuthenticationError):
                chat._call_raw([{"role": "user", "content": "hi"}])

        assert mock_completion.call_count == 1  # no retry on permanent error

    def test_exhausts_retries_then_raises(self):
        """Persistent transient errors raise after max_retries + 1 attempts."""
        chat = LiteLLMChat(
            model="gpt-4o", retry=RetryConfig(max_retries=2, base_delay=0.01)
        )
        with (
            patch(
                "codenib.llm.litellm_chat.litellm.completion",
                side_effect=litellm.Timeout("slow", "gpt-4o", "openai"),
            ) as mock_completion,
            _no_sleep(),
        ):
            with pytest.raises(litellm.Timeout):
                chat._call_raw([{"role": "user", "content": "hi"}])

        # 1 initial attempt + 2 retries
        assert mock_completion.call_count == 3

    def test_max_retries_zero_disables_retry(self):
        """max_retries=0 → a single attempt, transient error propagates."""
        chat = LiteLLMChat(model="gpt-4o", retry=RetryConfig(max_retries=0))
        with (
            patch(
                "codenib.llm.litellm_chat.litellm.completion",
                side_effect=litellm.RateLimitError("rl", "gpt-4o", "openai"),
            ) as mock_completion,
            _no_sleep(),
        ):
            with pytest.raises(litellm.RateLimitError):
                chat._call_raw([{"role": "user", "content": "hi"}])

        assert mock_completion.call_count == 1

    def test_backoff_sleep_called_between_retries(self):
        """Backoff sleep is invoked once per retry, with the configured delay."""
        chat = LiteLLMChat(
            model="gpt-4o",
            retry=RetryConfig(max_retries=2, base_delay=0.5, jitter=0.0),
        )
        good = _make_response("ok")
        side_effects = [
            litellm.Timeout("t", "gpt-4o", "openai"),
            litellm.Timeout("t", "gpt-4o", "openai"),
            good,
        ]
        with (
            patch(
                "codenib.llm.litellm_chat.litellm.completion",
                side_effect=side_effects,
            ),
            patch("codenib.llm.litellm_chat.time.sleep") as mock_sleep,
        ):
            resp = chat._call_raw([{"role": "user", "content": "hi"}])

        assert resp is good
        # Two retries → two sleeps with exponential delays 0.5, 1.0.
        assert [c.args[0] for c in mock_sleep.call_args_list] == [0.5, 1.0]

    def test_default_retry_config_present(self):
        """A LiteLLMChat constructed without retry= has a default RetryConfig."""
        chat = LiteLLMChat(model="gpt-4o")
        assert isinstance(chat.retry, RetryConfig)
        assert chat.retry.max_retries == 2
