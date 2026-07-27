# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from codenib.llm.options import (
    merge_model_options,
    parse_model_option_assignments,
    parse_model_options_json,
    validate_model_options,
)


def test_cli_assignments_support_nested_json_values():
    options = parse_model_option_assignments(
        [
            "api_version=2025-01-01",
            "timeout=30",
            "extra_body.chat_template_kwargs.enable_thinking=false",
        ]
    )

    assert options == {
        "api_version": "2025-01-01",
        "timeout": 30,
        "extra_body": {
            "chat_template_kwargs": {
                "enable_thinking": False,
            }
        },
    }


def test_model_options_deep_merge_provider_payloads():
    assert merge_model_options(
        {
            "timeout": 20,
            "extra_body": {
                "chat_template_kwargs": {
                    "enable_thinking": True,
                    "reasoning_budget": 100,
                }
            },
        },
        {
            "timeout": 45,
            "extra_body": {
                "chat_template_kwargs": {
                    "enable_thinking": False,
                }
            },
        },
    ) == {
        "timeout": 45,
        "extra_body": {
            "chat_template_kwargs": {
                "enable_thinking": False,
                "reasoning_budget": 100,
            }
        },
    }


@pytest.mark.parametrize("key", ["model", "messages", "tools", "api_key"])
def test_model_options_reject_managed_fields(key):
    with pytest.raises(ValueError, match="cannot override"):
        validate_model_options({key: "unexpected"})


def test_model_options_json_requires_an_object():
    with pytest.raises(ValueError, match="JSON object"):
        parse_model_options_json("[1, 2]", source="MODEL_OPTIONS")
