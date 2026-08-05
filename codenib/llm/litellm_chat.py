# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Lightweight LLM chat wrapper around LiteLLM.

Replaces the LangChain chat model classes (ChatOpenAI, ChatAnthropic, etc.)
with a single thin wrapper that provides the same two-method interface:

    llm = LiteLLMChat(model="gpt-4o", ...)
    structured = llm.with_structured_output(MyPydanticModel)
    result = structured.invoke([human_message("...")])
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Type

logging.getLogger("LiteLLM").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
warnings.filterwarnings(
    "ignore",
    message=r"Pydantic serializer warnings",
    category=UserWarning,
    module=r"pydantic\.main",
)
with warnings.catch_warnings():
    # Some LiteLLM integrations still attach Pydantic v1-style field metadata
    # during import. It is an upstream compatibility warning, not a CodeNib
    # configuration failure, and otherwise overwhelms one-command CLI output.
    warnings.simplefilter("ignore")
    import litellm
from pydantic import BaseModel

from ..log_utils import get_logger
from .options import validate_model_options

logger = get_logger(__name__)

# Keep provider internals behind CodeNib's concise retry/error reporting.
litellm.suppress_debug_info = True
litellm.turn_off_message_logging = True


# ---------------------------------------------------------------------------
# Retry policy (issue #109)
# ---------------------------------------------------------------------------
#
# litellm normalizes every provider error to an OpenAI-style exception class.
# Only a subset are *transient* -- worth retrying because a later attempt may
# succeed without any change to the request (rate limits, timeouts, upstream
# 5xx / connection blips). Everything else (auth, bad request, content policy,
# context-window-exceeded, ...) is a deterministic failure of *this* request
# and retrying just wastes the budget, so we re-raise immediately.


def _transient_exception_types() -> Tuple[type, ...]:
    """litellm exception classes that warrant a retry.

    Resolved at call time (not import time) so the tuple reflects whatever
    litellm version is installed; unknown names degrade to ``()`` gracefully.
    """
    names = (
        "RateLimitError",
        "Timeout",
        "APIConnectionError",
        "ServiceUnavailableError",
        "InternalServerError",
        "BadGatewayError",
        "APIError",  # generic upstream error; kept last (broadest)
    )
    types: List[type] = []
    for name in names:
        exc = getattr(litellm, name, None)
        if isinstance(exc, type) and issubclass(exc, BaseException):
            types.append(exc)
    # Network-level timeouts can surface as the stdlib TimeoutError too.
    types.append(TimeoutError)
    return tuple(types)


def is_transient_error(exc: BaseException) -> bool:
    """True if *exc* is a transient LLM error worth retrying.

    Non-transient errors (authentication, bad request, content policy,
    context-window-exceeded, ...) return ``False`` so the caller fails fast.
    """
    transient = _transient_exception_types()
    if not isinstance(exc, transient):
        return False
    # ``ContextWindowExceededError`` subclasses ``BadRequestError`` in some
    # litellm versions but ``APIError`` in others; it is never transient
    # (the request is too big and will be on every retry), so exclude it.
    permanent = tuple(
        t
        for name in ("ContextWindowExceededError", "BadRequestError")
        if isinstance((t := getattr(litellm, name, None)), type)
    )
    if permanent and isinstance(exc, permanent):
        return False
    return True


@dataclass(slots=True)
class RetryConfig:
    """Exponential-backoff retry policy for transient LLM errors.

    ``max_retries`` is the number of *additional* attempts after the first,
    so the call is made at most ``max_retries + 1`` times. ``base_delay`` is
    the first backoff in seconds; each subsequent wait is ``base_delay *
    2**attempt`` capped at ``max_delay``. A small random jitter spreads
    retries out so concurrent callers don't synchronize.
    """

    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.1

    def backoff(self, attempt: int) -> float:
        """Seconds to sleep before retry ``attempt`` (0-based)."""
        delay = min(self.base_delay * (2**attempt), self.max_delay)
        if self.jitter:
            delay += random.uniform(0, self.jitter * delay)
        return delay


def _no_thinking_kwargs(model: str) -> Dict[str, Any]:
    """litellm kwargs that disable hidden reasoning for thinking models.

    Gemini 2.5 models spend the ``max_tokens`` budget on hidden reasoning
    tokens before emitting any answer; a tight budget leaves ``content`` empty
    (``finish_reason="length"``). LiteLLM maps Anthropic-style ``thinking`` to
    Gemini's ``thinkingConfig``; a zero budget turns thinking off so the budget
    goes to the answer. No-op for non-thinking providers.

    vLLM-served Qwen thinking models emit a verbose ``Here's a thinking
    process:`` preamble (plain text, not ``<think>`` tags) before the answer,
    which derails the agent's answer extraction and wastes tokens. Disable it
    via the chat template so the model answers directly. Injecting it by model
    name keeps this vLLM-only knob out of ``model_options``, where it would
    leak onto a differently-backed wiki model. (Restored from the closed
    PR #327.)
    """
    m = (model or "").lower()
    if "gemini-2.5" in m or "gemini-2-5" in m:
        return {"thinking": {"type": "disabled", "budget_tokens": 0}}
    if "qwen" in m and "embed" not in m:
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {}


_JSON_OBJECT_STRUCTURED_OUTPUT_PROVIDERS = frozenset({"deepseek"})


def _uses_json_object_for_structured_output(model: str) -> bool:
    """Return whether *model* needs JSON mode instead of JSON Schema mode.

    DeepSeek's OpenAI-compatible API currently accepts ``text`` and
    ``json_object`` response formats, but not OpenAI's ``json_schema`` format.
    CodeNib requires provider-prefixed LiteLLM model names, so the native
    ``deepseek/...`` route can select the compatible format without first
    sending a request that is guaranteed to fail.
    """

    provider, separator, _ = (model or "").lower().partition("/")
    return bool(separator and provider in _JSON_OBJECT_STRUCTURED_OUTPUT_PROVIDERS)


# ---------------------------------------------------------------------------
# Anthropic prompt caching
# ---------------------------------------------------------------------------


def _is_anthropic(model: str) -> bool:
    m = (model or "").lower()
    return "claude" in m or "anthropic" in m


def _mark_cacheable(message: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of *message* with an ephemeral cache breakpoint on its last block.

    No-op if the content can't carry a block (e.g. an assistant message that is
    only tool_calls with ``content=None``)."""
    content = message.get("content")
    cc = {"type": "ephemeral"}
    if isinstance(content, str) and content:
        return {
            **message,
            "content": [{"type": "text", "text": content, "cache_control": cc}],
        }
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        blocks = list(content)
        blocks[-1] = {**blocks[-1], "cache_control": cc}
        return {**message, "content": blocks}
    return message


def _with_prompt_caching(
    messages: List[Dict[str, Any]], model: str
) -> List[Dict[str, Any]]:
    """Add Anthropic prompt-cache breakpoints for claude models.

    Two ``cache_control`` breakpoints (Anthropic allows up to 4): one on the
    system message — caching the stable tools+system prefix re-sent on every
    turn — and one rolling breakpoint on the last message, so each turn reads
    the prior conversation prefix from cache (~0.1x input price) instead of
    re-billing it at full price. No-op for non-Anthropic models, empty message
    lists, or when ``CODENIB_DISABLE_PROMPT_CACHE`` is set (for A/B runs).
    """
    if not messages or not _is_anthropic(model):
        return messages
    if os.environ.get("CODENIB_DISABLE_PROMPT_CACHE"):
        return messages
    out = [dict(m) for m in messages]
    for i, m in enumerate(out):  # stable breakpoint: tools + system prefix
        if m.get("role") == "system":
            out[i] = _mark_cacheable(m)
            break
    out[-1] = _mark_cacheable(out[-1])  # rolling breakpoint: conversation prefix
    return out


# ---------------------------------------------------------------------------
# Message types (replaces langchain_core.messages.HumanMessage etc.)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatMessage:
    """A single chat message with role and content."""

    role: str  # "system", "user", "assistant"
    content: str


def human_message(content: str) -> ChatMessage:
    """Create a user message."""
    return ChatMessage(role="user", content=content)


def system_message(content: str) -> ChatMessage:
    """Create a system message."""
    return ChatMessage(role="system", content=content)


# ---------------------------------------------------------------------------
# Core wrapper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LiteLLMChat:
    """Thin wrapper around ``litellm.completion``.

    Provides ``.invoke()`` and ``.with_structured_output()`` matching the
    interface that callers already use via LangChain chat models.
    """

    model: str
    temperature: Optional[float] = 0.0
    max_tokens: Optional[int] = 8192
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)
    retry: RetryConfig = field(default_factory=RetryConfig)

    def __post_init__(self) -> None:
        self.extra_kwargs = validate_model_options(
            self.extra_kwargs,
            source="LiteLLMChat.extra_kwargs",
        )

    def invoke(self, messages: List[ChatMessage]) -> str:
        """Send messages and return the assistant content string."""
        response = self._call(messages)
        return response.choices[0].message.content

    def complete(
        self,
        messages: List[Dict[str, Any]],
        **overrides: Any,
    ) -> str:
        """Complete raw LiteLLM message dictionaries and return text content."""

        response = self._call_raw(messages, **overrides)
        return (response.choices[0].message.content or "").strip()

    @property
    def cache_identity(self) -> str:
        """Stable non-secret identity for model-dependent artifact caches."""

        payload = {
            "model": self.model,
            "api_base": self.api_base or "",
            "extra_kwargs": self.extra_kwargs,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def with_structured_output(self, schema: Type[BaseModel]) -> _StructuredLLM:
        """Return a callable that parses LLM responses into *schema*."""
        return _StructuredLLM(chat=self, schema=schema)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call(self, messages: List[ChatMessage], **overrides: Any) -> Any:
        """Build kwargs and call ``litellm.completion``."""
        msg_dicts = [{"role": m.role, "content": m.content} for m in messages]
        return self._call_raw(msg_dicts, **overrides)

    def _call_raw(self, messages: List[Dict[str, Any]], **overrides: Any) -> Any:
        """Like ``_call`` but accepts raw message dicts (for tool-calling flows).

        Accepts two optional control kwargs (not forwarded to litellm):

        - ``usage_tracker``: a :class:`codenib.llm.usage.UsageTracker`. If
          provided, the response's token usage and cost are recorded.
        - ``usage_turn``: turn index attached to the recorded usage entry.
        """
        usage_tracker = overrides.pop("usage_tracker", None)
        usage_turn = overrides.pop("usage_turn", None)

        # Anthropic prompt caching: cache the stable system+tools prefix and the
        # growing conversation so multi-turn agents stop re-billing the prefix.
        messages = _with_prompt_caching(messages, self.model)

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **_no_thinking_kwargs(self.model),
            **self.extra_kwargs,
            **overrides,
        }
        if self.api_key is not None:
            kwargs["api_key"] = self.api_key
        if self.api_base is not None:
            kwargs["api_base"] = self.api_base

        # Drop None values so litellm uses its defaults
        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        start = time.monotonic()
        response = self._completion_with_retry(kwargs)
        duration_ms = (time.monotonic() - start) * 1000

        if usage_tracker is not None:
            usage_tracker.record_response(
                response,
                model=self.model,
                duration_ms=duration_ms,
                turn=usage_turn,
            )

        return response

    def _completion_with_retry(self, kwargs: Dict[str, Any]) -> Any:
        """Call ``litellm.completion`` with exponential-backoff retries.

        Transient errors (rate limit, timeout, upstream 5xx / connection
        blips -- see :func:`is_transient_error`) are retried up to
        ``self.retry.max_retries`` times with backoff. Non-transient errors
        (auth, bad request, content policy, context-window-exceeded) are
        re-raised on the first failure so the caller fails fast.
        """
        max_retries = max(0, self.retry.max_retries)
        last_exc: Optional[BaseException] = None
        for attempt in range(max_retries + 1):
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=r"Pydantic serializer warnings",
                        category=UserWarning,
                    )
                    return litellm.completion(**kwargs)
            except Exception as exc:  # noqa: BLE001 -- classified below
                last_exc = exc
                if attempt >= max_retries or not is_transient_error(exc):
                    raise
                delay = self.retry.backoff(attempt)
                logger.warning(
                    "litellm.completion transient error (attempt %d/%d), "
                    "retrying in %.2fs: %s",
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
        # Unreachable: the loop either returns or raises. Re-raise defensively.
        assert last_exc is not None
        raise last_exc


# ---------------------------------------------------------------------------
# Structured output helper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _StructuredLLM:
    """Wraps a :class:`LiteLLMChat` to parse responses into a Pydantic model.

    Most providers receive the Pydantic model directly through LiteLLM's
    ``response_format`` translation. Providers limited to JSON Object mode
    receive the schema in the prompt and are validated locally in the same way.
    """

    chat: LiteLLMChat
    schema: Type[BaseModel]

    def invoke(self, messages: List[ChatMessage]) -> BaseModel:
        """Call the LLM and return a validated Pydantic instance."""
        messages, response_format = self._prepare_request(messages)
        response = self.chat._call(messages, response_format=response_format)
        content = response.choices[0].message.content

        try:
            return self.schema.model_validate_json(content)
        except Exception:
            # Fallback: try extracting JSON from markdown code blocks
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
            if match:
                return self.schema.model_validate_json(match.group(1).strip())
            raise

    def _prepare_request(
        self,
        messages: List[ChatMessage],
    ) -> Tuple[List[ChatMessage], Any]:
        """Select the provider's structured-output protocol."""

        if not _uses_json_object_for_structured_output(self.chat.model):
            return messages, self.schema
        return self._with_schema_instruction(messages), {"type": "json_object"}

    def _with_schema_instruction(
        self,
        messages: List[ChatMessage],
    ) -> List[ChatMessage]:
        """Add the schema instruction required by JSON Object mode."""

        schema_json = json.dumps(
            self.schema.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        instruction = (
            "Return only a JSON object that validates against the JSON Schema "
            "below. Do not add markdown fences or explanatory text.\n"
            f"JSON Schema:\n{schema_json}"
        )
        prompted = list(messages)
        for index, message in enumerate(prompted):
            if message.role == "system":
                prompted[index] = ChatMessage(
                    role="system",
                    content=f"{message.content}\n\n{instruction}",
                )
                break
        else:
            prompted.insert(0, system_message(instruction))
        return prompted
