# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Writing models a visitor may choose for a Wiki run, and what each accepts.

Every entry was checked against OpenRouter's model catalog (2026-09-28) for
JSON-schema structured output, which the chapter planner requires. Requests
set ``provider.require_parameters``, so a parameter a model does not accept
makes the call unroutable; the flags below keep ``temperature`` and
``reasoning`` out of requests for models that reject them.

Prices are the catalog's per-million-token rates on that date. The web route
refreshes them from the public catalog when it can and keeps these otherwise.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class WikiModel:
    id: str
    label: str
    provider: str
    input_usd: float  # per million prompt tokens
    output_usd: float  # per million completion tokens
    context: int
    temperature: bool = True
    reasoning: bool = True
    # A full Wiki has been generated with this model through CodeNib.
    tested: bool = False


WIKI_MODEL_CHOICES: tuple[WikiModel, ...] = (
    WikiModel(
        "deepseek/deepseek-v4.1-flash",
        "DeepSeek V4.1 Flash",
        "DeepSeek",
        0.30,
        1.20,
        1048576,
        tested=True,
    ),
    WikiModel(
        "deepseek/deepseek-v4-pro", "DeepSeek V4 Pro", "DeepSeek", 0.96, 1.91, 1048576
    ),
    WikiModel(
        "anthropic/claude-sonnet-5.5",
        "Claude Sonnet 5.5",
        "Anthropic",
        2.00,
        10.00,
        1000000,
    ),
    WikiModel(
        "anthropic/claude-haiku-4.5",
        "Claude Haiku 4.5",
        "Anthropic",
        1.00,
        5.00,
        200000,
    ),
    WikiModel(
        "anthropic/claude-sonnet-4.6",
        "Claude Sonnet 4.6",
        "Anthropic",
        3.00,
        15.00,
        1000000,
        tested=True,
    ),
    WikiModel(
        "openai/gpt-6-sol",
        "GPT-6 Sol",
        "OpenAI",
        2.00,
        10.00,
        1050000,
        temperature=False,
    ),
    WikiModel(
        "openai/gpt-6-luna",
        "GPT-6 Luna",
        "OpenAI",
        0.10,
        0.50,
        1050000,
        temperature=False,
    ),
    WikiModel(
        "google/gemini-3.8-flash", "Gemini 3.8 Flash", "Google", 0.75, 3.75, 1048576
    ),
    WikiModel("qwen/qwen3.8-max-0902", "Qwen3.8 Max", "Qwen", 2.00, 6.00, 1000000),
    WikiModel("qwen/qwen3.8-flash", "Qwen3.8 Flash", "Qwen", 0.15, 0.47, 1000000),
    WikiModel("moonshotai/kimi-k2.6", "Kimi K2.6", "Moonshot AI", 0.65, 3.41, 262144),
    WikiModel("z-ai/glm-5.3", "GLM 5.3", "Z.ai", 1.40, 4.40, 1310720),
    WikiModel("z-ai/glm-5.3-flash", "GLM 5.3 Flash", "Z.ai", 0.15, 0.50, 1310720),
    WikiModel(
        "mistralai/mistral-medium-3-5",
        "Mistral Medium 3.5",
        "Mistral",
        1.50,
        7.50,
        262144,
    ),
    WikiModel("x-ai/grok-4.3", "Grok 4.3", "xAI", 1.25, 2.50, 1000000),
    WikiModel("minimax/minimax-m3", "MiniMax M3", "MiniMax", 0.30, 1.20, 1048576),
    WikiModel(
        "meta-llama/llama-4-maverick",
        "Llama 4 Maverick",
        "Meta",
        0.19,
        0.65,
        1048576,
        reasoning=False,
    ),
)

WIKI_MODELS: tuple[str, ...] = tuple(model.id for model in WIKI_MODEL_CHOICES)
DEFAULT_WIKI_MODEL = "deepseek/deepseek-v4.1-flash"
_BY_ID = {model.id: model for model in WIKI_MODEL_CHOICES}


def wiki_model(model_id: str) -> Optional[WikiModel]:
    return _BY_ID.get(model_id)


_CATALOG_URL = "https://openrouter.ai/api/v1/models"
_CACHE_SECONDS = 6 * 3600
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "prices": None}


def _fetch_catalog_prices() -> dict[str, tuple[float, float]]:
    """Current per-million rates for the listed models from the public catalog."""
    import requests

    with requests.Session() as session:
        session.trust_env = False
        response = session.get(_CATALOG_URL, timeout=4, allow_redirects=False)
    response.raise_for_status()
    prices: dict[str, tuple[float, float]] = {}
    for item in response.json().get("data", []):
        model_id = item.get("id")
        if model_id not in _BY_ID:
            continue
        pricing = item.get("pricing") or {}
        try:
            prompt = float(pricing["prompt"]) * 1e6
            completion = float(pricing["completion"]) * 1e6
        except (KeyError, TypeError, ValueError):
            continue
        if prompt >= 0 and completion >= 0:
            prices[model_id] = (round(prompt, 4), round(completion, 4))
    return prices


def wiki_model_catalog(
    fetch: Optional[
        Callable[[], dict[str, tuple[float, float]]]
    ] = _fetch_catalog_prices,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The choices for the generation form, with prices refreshed when possible.

    A model the live catalog no longer lists is left out, so the form never
    offers a model OpenRouter cannot route. Without a catalog response every
    choice is kept at its recorded price.
    """
    prices = None
    if fetch is not None:
        with _cache_lock:
            fresh = (
                _cache["prices"] is not None and now() - _cache["at"] < _CACHE_SECONDS
            )
            if fresh:
                prices = _cache["prices"]
        if prices is None:
            try:
                prices = fetch()
            except Exception:  # noqa: BLE001 - any failure keeps recorded prices
                prices = None
            if prices:
                with _cache_lock:
                    _cache.update(at=now(), prices=prices)
    models = []
    for model in WIKI_MODEL_CHOICES:
        row = asdict(model)
        del row["temperature"], row["reasoning"]
        if prices:
            if model.id not in prices:
                continue
            row["input_usd"], row["output_usd"] = prices[model.id]
        models.append(row)
    return {
        "default": DEFAULT_WIKI_MODEL,
        "prices": "live" if prices else "recorded",
        "models": models,
    }
