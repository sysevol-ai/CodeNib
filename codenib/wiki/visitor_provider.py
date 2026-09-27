# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""One explicit visitor credential and cost ledger for a complete Wiki run."""

from __future__ import annotations

import json
import math
from typing import Any, Callable

from ..agent.decision_rerank import RELEVANCE_CRITERIA, decide_code_relevance
from ..agent.runtime.grep_jev import (
    JEV_MODEL,
    PLANNER_MODEL,
    PLANNER_SCHEMA,
    PLANNER_SYSTEM,
    GrepPlan,
    collect_grep_candidates,
    grep_planning_context,
)
from ..llm.decisions import OpenRouterDecisions


class WikiRunStopped(BaseException):
    """Stop a funded run, including AgentWiki's soft-failure/repair paths.

    This deliberately bypasses model-error fallbacks: cancellation, unknown
    charges and exhausted budgets must never initiate another model call.
    The visitor run owner catches it and persists a resumable state.
    """


def provider_json(key: str, path: str, payload: dict | None = None) -> dict:
    """Fixed origin, no redirects/retries, no provider bodies in exceptions."""
    import requests

    try:
        with requests.Session() as session:
            # Ignore netrc and ambient proxy credentials. No operator key is used.
            session.trust_env = False
            with session.request(
                "GET" if payload is None else "POST",
                "https://openrouter.ai" + path,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=(10, 90),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise WikiRunStopped(
                        f"OpenRouter returned HTTP {response.status_code}. "
                        "Completed pages are saved; no automatic retry."
                    )
                data = bytearray()
                for chunk in response.iter_content(8192):
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        raise WikiRunStopped("OpenRouter response exceeded the limit.")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError
                return result
    except (requests.RequestException, ValueError, TypeError):
        raise WikiRunStopped("OpenRouter request failed; no automatic retry.") from None


def verify_key(key: str) -> None:
    data = provider_json(key, "/api/v1/key").get("data")
    if (
        not isinstance(data, dict)
        or data.get("is_management_key") is not False
        or data.get("is_provisioning_key", False) is not False
    ):
        raise WikiRunStopped("Use an OpenRouter inference key, not a management key.")
    remaining = data.get("limit_remaining")
    if isinstance(remaining, (int, float)) and remaining <= 0:
        raise WikiRunStopped("This OpenRouter key has no remaining credit.")


class VisitorProvider:
    """Synchronous AgentWiki client; caller owns lifetime, cancellation and save.

    Only reported cost and aggregate call counts leave this object. The key is
    never serialized. A run budget stops subsequent calls; the provider's key
    credit limit is the hard billing limit for a request already in flight.
    """

    cache_identity = "visitor-openrouter-grep-jev-v1"

    def __init__(
        self,
        key: str,
        budget: float,
        check: Callable[[], None],
        changed: Callable[[dict], None],
    ):
        self._key = key
        self.budget = budget
        self.check = check
        self.changed = changed
        self.cost = 0.0
        self.calls = 0
        self.unknown = False

    def close(self):
        self._key = ""

    def usage(self) -> dict:
        return {
            "reported_cost_usd": self.cost,
            "calls": self.calls,
            "unreported_call_cost": self.unknown,
            "budget_usd": self.budget,
        }

    def before(self):
        self.check()
        if self.unknown or self.cost >= self.budget or self.calls >= 160:
            raise WikiRunStopped("Run budget reached. Completed pages are saved.")
        self.unknown = True
        self.calls += 1
        self.changed(self.usage())

    def record(self, usage: Any):
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
        ):
            raise WikiRunStopped(
                "OpenRouter did not report the charge. Check usage before resuming."
            )
        self.cost += cost
        self.unknown = False
        self.changed(self.usage())
        self.check()

    def complete(self, messages: list[dict], **options) -> str:
        self.before()
        payload = {
            "model": PLANNER_MODEL,
            "messages": messages,
            "temperature": options.get("temperature", 0.2),
            "max_tokens": min(16000, options.get("max_tokens", 4096)),
            "reasoning": {"enabled": False},
        }
        if "response_format" in options:
            payload["response_format"] = options["response_format"]
            payload["provider"] = {"require_parameters": True}
        body = provider_json(self._key, "/api/v1/chat/completions", payload)
        self.record(body.get("usage"))
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(content, str):
                raise ValueError
            return content.strip()
        except (KeyError, IndexError, TypeError, ValueError):
            raise WikiRunStopped(
                "OpenRouter returned an incomplete page response."
            ) from None

    def retrieve(self, source, query: str, limit: int) -> list:
        context = grep_planning_context(source, check_cancelled=self.check)["payload"]
        context["query"] = query
        text = self.complete(
            [
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": json.dumps(context)},
            ],
            max_tokens=1000,
            temperature=0,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "grep_plan",
                    "strict": True,
                    "schema": PLANNER_SCHEMA,
                },
            },
        )
        try:
            plan = GrepPlan.model_validate_json(text)
        except ValueError:
            raise WikiRunStopped(
                "The source search plan was invalid; no retry."
            ) from None
        nodes = collect_grep_candidates(source, plan, check_cancelled=self.check).nodes
        for offset in range(0, len(nodes), 10):
            self.before()
            batch = list(enumerate(nodes[offset : offset + 10], start=offset))
            try:
                scored = decide_code_relevance(
                    OpenRouterDecisions(
                        model=JEV_MODEL,
                        api_key=self._key,
                        timeout=45,
                        max_retries=0,
                        trust_env=False,
                    ),
                    query,
                    batch,
                )
            except Exception:
                raise WikiRunStopped(
                    "OpenRouter source ranking failed; no retry."
                ) from None
            self.record(scored.usage)
            for index, node in batch:
                node.score = scored.answers[f"node_{index}"].score / (
                    len(RELEVANCE_CRITERIA) - 1
                )
        source.authenticated_identity_snapshot(check_cancelled=self.check)
        return sorted(nodes, key=lambda node: -(node.score or 0))[:limit]
