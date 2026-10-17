# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Regression coverage for actual SDK block duplication and cache accounting."""

from copy import deepcopy

import pytest

from scripts.contextbench_usage import summarize_response_inputs


def captured():
    def event(response_id, uncached, creation, read, *, model="main"):
        return {
            "kind": "AssistantMessage",
            "data": {
                "model": model,
                "session_id": "session",
                "message_id": response_id,
                "usage": {
                    "input_tokens": uncached,
                    "cache_creation_input_tokens": creation,
                    "cache_read_input_tokens": read,
                    "cache_creation": {"ephemeral_1h_input_tokens": creation},
                    "output_tokens": 8,
                },
            },
        }

    first = event("response-1", 2, 4140, 0)
    second = event("response-2", 2, 983, 4140)
    return {
        "sdk_events": [
            first,
            second,
            deepcopy(second),
            event("aux-response", 100, 200, 300, model="auxiliary"),
        ],
        "sdk_result": {
            "model_usage": {
                "main": {
                    "inputTokens": 4,
                    "cacheCreationInputTokens": 5123,
                    "cacheReadInputTokens": 4140,
                }
            }
        },
    }


def test_repeated_blocks_do_not_duplicate_inputs_and_cached_tokens_count():
    raw = captured()
    unchanged = deepcopy(raw)
    result = summarize_response_inputs(raw, model="main")

    assert result["unique_assistant_responses"] == 2
    assert result["repeated_blocks_not_added"] == 1
    assert result["max_response_input_tokens"] == 5125
    assert result["mean_response_input_tokens"] == (4142 + 5125) / 2
    assert result["terminal_input_counters_reconciled"] is True
    assert raw == unchanged


@pytest.mark.parametrize("missing", ["usage", "message_id"])
def test_missing_usage_or_identity_cannot_be_reported_as_zero(missing):
    raw = captured()
    del raw["sdk_events"][0]["data"][missing]

    with pytest.raises(ValueError, match="unavailable"):
        summarize_response_inputs(raw, model="main")


def test_conflicting_duplicate_blocks_cannot_silently_pick_a_counter():
    raw = captured()
    raw["sdk_events"][2]["data"]["usage"]["cache_read_input_tokens"] += 1

    with pytest.raises(ValueError, match="conflicting"):
        summarize_response_inputs(raw, model="main")


def test_missing_main_responses_cannot_claim_a_complete_maximum():
    raw = captured()
    raw["sdk_result"]["model_usage"]["main"]["inputTokens"] += 2

    with pytest.raises(ValueError, match="do not match terminal"):
        summarize_response_inputs(raw, model="main")
