# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only input accounting for captured ContextBench Agent SDK responses.

Assistant content blocks can repeat the same response ID and input usage.
Deduplicate those blocks and require exact agreement with terminal main-model
counters before reporting per-response inputs. Output usage on partial blocks
is deliberately excluded. These are provider-reported tokens, not HTTP request
counts, physical KV residency or subscription cash charges.

Input semantics: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
"""

from collections.abc import Mapping

_INPUT_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_TERMINAL_FIELDS = (
    "inputTokens",
    "cacheCreationInputTokens",
    "cacheReadInputTokens",
)


def _counters(usage, fields):
    if not isinstance(usage, Mapping):
        raise ValueError("Response input usage is unavailable")
    values = tuple(usage.get(field) for field in fields)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Response input counters must be known nonnegative integers")
    return values


def summarize_response_inputs(raw, *, model):
    """Summarize one main model; reject incomplete or conflicting observations.

    Each distinct (session, response ID) contributes input counters exactly once.
    Unknown values never become zero, and partial coverage never becomes an
    all-response maximum. Terminal SDK counters remain unchanged in the raw input.
    """
    groups = {}
    blocks = 0
    for index, event in enumerate(raw["sdk_events"]):
        if event["kind"] != "AssistantMessage":
            continue
        data = event["data"]
        if data.get("model") != model:
            continue
        message_id = data.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Assistant response ID is unavailable")
        identity = (data.get("session_id"), message_id)
        counts = _counters(data.get("usage"), _INPUT_FIELDS)
        if identity in groups:
            group = groups[identity]
            if tuple(group["input_counters"].values()) != counts:
                raise ValueError("Repeated response ID has conflicting input counters")
            group["assistant_blocks"] += 1
        else:
            groups[identity] = {
                "session_id": identity[0],
                "message_id": message_id,
                "first_event_index": index,
                "input_counters": dict(zip(_INPUT_FIELDS, counts, strict=True)),
                "total_input_tokens": sum(counts),
                "assistant_blocks": 1,
            }
        blocks += 1
    if not groups:
        raise ValueError("No main-model response inputs were observed")
    totals = tuple(
        sum(group["input_counters"][field] for group in groups.values())
        for field in _INPUT_FIELDS
    )
    terminal = (raw.get("sdk_result") or {}).get("model_usage") or {}
    expected = _counters(terminal.get(model), _TERMINAL_FIELDS)
    if totals != expected:
        raise ValueError("Observed response inputs do not match terminal SDK counters")
    responses = list(groups.values())
    return {
        "model": model,
        "unique_assistant_responses": len(responses),
        "assistant_blocks": blocks,
        "repeated_blocks_not_added": blocks - len(responses),
        "input_counters": dict(zip(_INPUT_FIELDS, totals, strict=True)),
        "max_response_input_tokens": max(r["total_input_tokens"] for r in responses),
        "mean_response_input_tokens": sum(totals) / len(responses),
        "terminal_input_counters_reconciled": True,
        "responses": responses,
    }
