# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Validated LiteLLM request options shared by CLI and web runtimes."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

_RESERVED_OPTIONS = frozenset(
    {
        "api_base",
        "api_key",
        "max_tokens",
        "messages",
        "model",
        "stream",
        "temperature",
        "tool_choice",
        "tools",
        "usage_tracker",
        "usage_turn",
    }
)


def _copy_mapping(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{source} must use non-empty string keys")
        result[key.strip()] = (
            _copy_mapping(item, source=source) if isinstance(item, Mapping) else item
        )
    return result


def validate_model_options(
    value: Mapping[str, Any] | None,
    *,
    source: str = "model options",
) -> dict[str, Any]:
    """Copy and validate provider-specific LiteLLM completion options."""

    if value is not None and not isinstance(value, Mapping):
        raise ValueError(f"{source} must be a mapping")
    options = _copy_mapping(value or {}, source=source)
    reserved = sorted(_RESERVED_OPTIONS.intersection(options))
    if reserved:
        raise ValueError(
            f"{source} cannot override managed option(s): {', '.join(reserved)}"
        )
    try:
        json.dumps(options, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must contain JSON-compatible values") from exc
    return options


def parse_model_options_json(
    value: str | None,
    *,
    source: str,
) -> dict[str, Any]:
    """Parse one JSON object containing provider-specific LiteLLM options."""

    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} must be a JSON object: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{source} must be a JSON object")
    return validate_model_options(parsed, source=source)


def merge_model_options(*values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deep-merge validated option mappings from lowest to highest precedence."""

    def merge_nested(
        base: dict[str, Any],
        overlay: Mapping[str, Any],
    ) -> dict[str, Any]:
        for key, item in overlay.items():
            if isinstance(item, Mapping) and isinstance(base.get(key), dict):
                merge_nested(base[key], item)
            else:
                base[key] = (
                    _copy_mapping(item, source="model options")
                    if isinstance(item, Mapping)
                    else item
                )
        return base

    merged: dict[str, Any] = {}
    for value in values:
        merge_nested(merged, validate_model_options(value))
    return validate_model_options(merged)


def parse_model_option_assignments(values: Iterable[str] | None) -> dict[str, Any]:
    """Parse repeatable ``KEY=JSON`` CLI values, including dotted nested keys."""

    result: dict[str, Any] = {}
    for assignment in values or ():
        key, separator, raw = assignment.partition("=")
        path = [part.strip() for part in key.split(".") if part.strip()]
        if not separator or not path:
            raise ValueError(f"model option must use KEY=VALUE syntax: {assignment!r}")
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw

        cursor = result
        for part in path[:-1]:
            existing = cursor.get(part)
            if existing is None:
                existing = {}
                cursor[part] = existing
            if not isinstance(existing, dict):
                raise ValueError(
                    f"model option path conflicts with an existing value: {key!r}"
                )
            cursor = existing
        cursor[path[-1]] = parsed
    return validate_model_options(result, source="--model-option")


__all__ = [
    "merge_model_options",
    "parse_model_option_assignments",
    "parse_model_options_json",
    "validate_model_options",
]
