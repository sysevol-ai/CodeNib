# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Secret-free provider identities and process-local inference routes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

InferenceOperation = Literal["chat", "embeddings"]

INFERENCE_ROUTE_SCHEMA = "codenib.inference-route.v1"

_PROVIDER_ALIASES = {
    "hugging-face": "huggingface",
    "hugging_face": "huggingface",
}
_RETIRED_PROVIDERS = {"github-models", "github_models"}
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPERATION_PATH_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/responses",
)
_SENSITIVE_OPTION_KEYS = {
    "apikey",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "credential",
    "credentials",
    "headers",
    "password",
    "privatekey",
    "secret",
    "secretkey",
    "token",
}
_ENDPOINT_OPTION_KEYS = {"apibase", "baseurl", "endpoint"}
_RUNTIME_TOP_LEVEL_KEYS = {
    "batchsize",
    "cachefolder",
    "defaultbatchsize",
    "maxretries",
    "retry",
    "retries",
    "showprogressbar",
    "timeout",
}
_RUNTIME_ENCODE_KEYS = {
    "batchsize": "batch_size",
    "converttotensor": "convert_to_tensor",
    "converttonumpy": "convert_to_numpy",
    "showprogressbar": "show_progress_bar",
}
_RUNTIME_MODEL_KEYS = {
    "cachedir": "cache_dir",
    "cachefolder": "cache_folder",
    "localfilesonly": "local_files_only",
}
_REMOTE_RUNTIME_KEYS = {
    "apikey": "api_key",
    "baseurl": "base_url",
    "maxretries": "max_retries",
    "timeout": "timeout",
}
_HUGGINGFACE_RUNTIME_KEYS = {
    "cachefolder": "cache_folder",
    "defaultbatchsize": "default_batch_size",
}
_ROUTE_POLL_BYTES = 64 * 1024


def _require_route_cancellation_check(
    check_cancelled: Callable[[], None] | None,
) -> None:
    if check_cancelled is not None and not callable(check_cancelled):
        raise TypeError("route cancellation check must be callable")


def _trim_text_bounds_interruptibly(
    value: str,
    *,
    check_cancelled: Callable[[], None],
) -> tuple[int, int]:
    start = 0
    end = len(value)
    while start < end:
        window_end = min(end, start + _ROUTE_POLL_BYTES)
        for position in range(start, window_end):
            if not value[position].isspace():
                start = position
                break
        else:
            start = window_end
            if start < end:
                check_cancelled()
            continue
        break
    while end > start:
        window_start = max(start, end - _ROUTE_POLL_BYTES)
        for position in range(end - 1, window_start - 1, -1):
            if not value[position].isspace():
                end = position + 1
                break
        else:
            end = window_start
            if end > start:
                check_cancelled()
            continue
        break
    return start, end


def _lower_text_interruptibly(
    value: str,
    *,
    check_cancelled: Callable[[], None],
) -> str:
    pieces: list[str] = []
    value_length = len(value)
    for offset in range(0, value_length, _ROUTE_POLL_BYTES):
        pieces.append(value[offset : offset + _ROUTE_POLL_BYTES].lower())
        if offset + _ROUTE_POLL_BYTES < value_length:
            check_cancelled()
    return "".join(pieces)


def normalize_provider(
    value: str,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> str:
    """Return the canonical provider identifier used in persisted metadata."""

    _require_route_cancellation_check(check_cancelled)
    if check_cancelled is None:
        provider = str(value or "").strip().lower()
    else:
        raw = str(value or "")
        start, end = _trim_text_bounds_interruptibly(
            raw,
            check_cancelled=check_cancelled,
        )
        pieces: list[str] = []
        for window_start in range(start, end, _ROUTE_POLL_BYTES):
            window_end = min(end, window_start + _ROUTE_POLL_BYTES)
            piece = raw[window_start:window_end].lower()
            for piece_index, character in enumerate(piece):
                absolute_index = window_start - start + piece_index
                valid = character.isascii() and (
                    "a" <= character <= "z"
                    or "0" <= character <= "9"
                    or character in "_.-"
                )
                if absolute_index == 0:
                    valid = valid and "a" <= character <= "z"
                if not valid:
                    raise ValueError(f"invalid inference provider: {value!r}")
            pieces.append(piece)
            if window_end < end:
                check_cancelled()
        provider = "".join(pieces)
        if not provider:
            raise ValueError(f"invalid inference provider: {value!r}")
    if provider in _RETIRED_PROVIDERS:
        raise ValueError(
            "GitHub Models was retired on 2026-07-30; select a current "
            "provider or an OpenAI-compatible BYO endpoint"
        )
    provider = _PROVIDER_ALIASES.get(provider, provider)
    if check_cancelled is None and not _PROVIDER_RE.fullmatch(provider):
        raise ValueError(f"invalid inference provider: {value!r}")
    return provider


def _preflight_long_endpoint(
    raw: str,
    *,
    check_cancelled: Callable[[], None],
) -> None:
    """Attest bounded raw URL units before requesting each future unit."""

    # ``urlsplit`` removes these characters globally and strips leading WHATWG
    # C0 controls.  Track just enough of that view to reject policy violations
    # in the bounded unit that establishes them, before polling for a future
    # unit.  The authoritative parse still follows this interruptible pass.
    header = ""
    header_complete = False
    authority_started = False
    authority_has_character = False
    authority_characters: list[str] = []
    authority_is_bounded = True
    authority_bracketed = False
    authority_bracket_closed = False
    authority_port_started = False
    authority_port_value = 0
    in_path = False
    in_query = False
    in_fragment = False
    query_has_character = False
    fragment_has_character = False
    leading = True
    path_component = ""
    path_component_is_other = False
    path_pending = ""
    path_characters: list[str] = []

    def attest_decoded_path(decoded: str) -> None:
        nonlocal path_component, path_component_is_other
        for decoded_character in decoded:
            if ord(decoded_character) < 32:
                raise ValueError("provider endpoint contains control characters")
            if decoded_character == "/":
                if not path_component_is_other and path_component in {".", ".."}:
                    raise ValueError(
                        "provider endpoint must not contain path traversal"
                    )
                path_component = ""
                path_component_is_other = False
            elif not path_component_is_other:
                if len(path_component) < 2 and decoded_character == ".":
                    path_component += decoded_character
                else:
                    path_component_is_other = True

    def attest_path_window(*, final: bool) -> None:
        nonlocal path_pending
        encoded = path_pending + "".join(path_characters)
        path_characters.clear()
        retained = 0
        if not final and encoded.endswith("%"):
            retained = 1
        elif not final and len(encoded) >= 2 and encoded[-2] == "%":
            retained = 2
        process_end = len(encoded) - retained
        attest_decoded_path(unquote(encoded[:process_end]))
        path_pending = encoded[process_end:]

    def attest_completed_authority() -> None:
        if not authority_has_character:
            raise ValueError("provider endpoint must be an absolute http(s) URL")
        if not authority_is_bounded:
            return
        authority = "".join(authority_characters)
        if "\\" in authority:
            raise ValueError("provider endpoint must be an absolute http(s) URL")
        candidate = urlsplit(header + authority + "/")
        if not candidate.hostname:
            raise ValueError("provider endpoint must be an absolute http(s) URL")
        if candidate.username is not None or candidate.password is not None:
            raise ValueError("provider endpoint must not contain user information")
        try:
            candidate.port
        except ValueError as exc:
            raise ValueError("provider endpoint has an invalid port") from exc

    for window_start in range(0, len(raw), _ROUTE_POLL_BYTES):
        window_end = min(len(raw), window_start + _ROUTE_POLL_BYTES)
        for character in raw[window_start:window_end]:
            if character in "\t\r\n":
                continue
            if leading and ord(character) <= 32:
                continue
            leading = False

            if not header_complete:
                header += character.lower()
                candidates = ("http://", "https://")
                if header in candidates:
                    header_complete = True
                    authority_started = True
                    continue
                if not any(candidate.startswith(header) for candidate in candidates):
                    raise ValueError(
                        "provider endpoint must be an absolute http(s) URL"
                    )
                continue

            if authority_started:
                if character == "/":
                    attest_completed_authority()
                    authority_started = False
                    in_path = True
                    continue
                if character == "?":
                    attest_completed_authority()
                    authority_started = False
                    in_query = True
                    continue
                if character == "#":
                    attest_completed_authority()
                    authority_started = False
                    in_fragment = True
                    continue
                if character == "@":
                    raise ValueError(
                        "provider endpoint must not contain user information"
                    )
                if character == "\\":
                    raise ValueError(
                        "provider endpoint must be an absolute http(s) URL"
                    )
                normalized_character = unicodedata.normalize("NFKC", character)
                if normalized_character != character and any(
                    delimiter in normalized_character for delimiter in "/?#@:"
                ):
                    raise ValueError(
                        "provider endpoint contains invalid authority characters"
                    )
                authority_index = len(authority_characters)
                if not authority_has_character:
                    if character in ":]":
                        raise ValueError(
                            "provider endpoint must be an absolute http(s) URL"
                        )
                    authority_bracketed = character == "["
                elif authority_bracketed and not authority_bracket_closed:
                    if character == "]":
                        authority_bracket_closed = True
                        candidate = urlsplit(
                            header + "".join(authority_characters) + "]"
                        )
                        if not candidate.hostname:
                            raise ValueError(
                                "provider endpoint must be an absolute http(s) URL"
                            )
                    elif authority_index > 64:
                        raise ValueError(
                            "provider endpoint must be an absolute http(s) URL"
                        )
                elif authority_bracketed and authority_bracket_closed:
                    if not authority_port_started:
                        if character != ":":
                            raise ValueError(
                                "provider endpoint must be an absolute http(s) URL"
                            )
                        authority_port_started = True
                    elif not character.isascii() or not character.isdigit():
                        raise ValueError("provider endpoint has an invalid port")
                    else:
                        authority_port_value = authority_port_value * 10 + int(
                            character
                        )
                        if authority_port_value > 65_535:
                            raise ValueError("provider endpoint has an invalid port")
                elif character in "[]":
                    raise ValueError(
                        "provider endpoint must be an absolute http(s) URL"
                    )
                elif not authority_port_started and character == ":":
                    authority_port_started = True
                elif authority_port_started:
                    if not character.isascii() or not character.isdigit():
                        raise ValueError("provider endpoint has an invalid port")
                    authority_port_value = authority_port_value * 10 + int(character)
                    if authority_port_value > 65_535:
                        raise ValueError("provider endpoint has an invalid port")
                authority_has_character = True
                if len(authority_characters) < _ROUTE_POLL_BYTES:
                    authority_characters.append(character)
                else:
                    authority_is_bounded = False
                continue

            if in_fragment:
                fragment_has_character = True
                continue
            if in_query:
                if character == "#":
                    in_query = False
                    in_fragment = True
                else:
                    query_has_character = True
                continue
            if in_path:
                if character == "?":
                    attest_path_window(final=True)
                    in_path = False
                    in_query = True
                elif character == "#":
                    attest_path_window(final=True)
                    in_path = False
                    in_fragment = True
                else:
                    path_characters.append(character)

        if in_path:
            attest_path_window(final=window_end == len(raw))
        if query_has_character or fragment_has_character:
            raise ValueError("provider endpoint must not contain a query or fragment")
        if window_end < len(raw):
            check_cancelled()

    if not header_complete or (authority_started and not authority_has_character):
        raise ValueError("provider endpoint must be an absolute http(s) URL")
    if authority_started:
        attest_completed_authority()
    if not path_component_is_other and path_component in {".", ".."}:
        raise ValueError("provider endpoint must not contain path traversal")


def _validate_decoded_endpoint_path_interruptibly(
    path: str,
    *,
    check_cancelled: Callable[[], None],
) -> str:
    """Validate decoded path policy with bounded percent-decoding calls."""

    component = ""
    component_is_other = False
    suffix_size = max(len(suffix) for suffix in _OPERATION_PATH_SUFFIXES)
    decoded_suffix = ""
    trailing_slashes = 0
    path_length = len(path)
    offset = 0
    while offset < path_length:
        tentative_end = min(path_length, offset + _ROUTE_POLL_BYTES)
        chunk_end = tentative_end
        if chunk_end < path_length:
            # Keep a percent escape wholly in the current or future unit.
            if path[chunk_end - 1] == "%":
                chunk_end -= 1
            elif chunk_end - 2 >= offset and path[chunk_end - 2] == "%":
                chunk_end -= 2
        if chunk_end == offset:  # pragma: no cover - poll size is much larger
            chunk_end = tentative_end
        decoded = unquote(path[offset:chunk_end])
        for character in decoded:
            if ord(character) < 32:
                raise ValueError("provider endpoint contains control characters")
            if character == "/":
                if not component_is_other and component in {".", ".."}:
                    raise ValueError(
                        "provider endpoint must not contain path traversal"
                    )
                component = ""
                component_is_other = False
            elif not component_is_other:
                if len(component) < 2 and character == ".":
                    component += character
                else:
                    component_is_other = True
            if character == "/":
                trailing_slashes = min(suffix_size, trailing_slashes + 1)
            else:
                if trailing_slashes:
                    decoded_suffix = (decoded_suffix + "/" * trailing_slashes)[
                        -suffix_size:
                    ]
                    trailing_slashes = 0
                decoded_suffix = (decoded_suffix + character)[-suffix_size:]
        offset = chunk_end
        if offset < path_length:
            check_cancelled()

    if not component_is_other and component in {".", ".."}:
        raise ValueError("provider endpoint must not contain path traversal")
    return decoded_suffix.lower()


def normalize_endpoint(
    value: str | None,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> str | None:
    """Normalize a provider API base while rejecting credential-bearing URLs."""

    _require_route_cancellation_check(check_cancelled)
    if check_cancelled is None:
        if value is None or not str(value).strip():
            return None
        raw = str(value).strip()
    elif value is None:
        return None
    else:
        text = str(value)
        start, end = _trim_text_bounds_interruptibly(
            text,
            check_cancelled=check_cancelled,
        )
        if start == end:
            return None
        raw = text[start:end]
    interruptible = check_cancelled is not None and len(raw) > _ROUTE_POLL_BYTES
    if interruptible:
        assert check_cancelled is not None
        _preflight_long_endpoint(raw, check_cancelled=check_cancelled)
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("provider endpoint must be an absolute http(s) URL")
    if "\\" in parsed.netloc:
        raise ValueError("provider endpoint must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider endpoint must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("provider endpoint must not contain a query or fragment")

    if not interruptible:
        decoded_path = unquote(parsed.path)
        if any(part in {".", ".."} for part in decoded_path.split("/")):
            raise ValueError("provider endpoint must not contain path traversal")
        if any(ord(character) < 32 for character in decoded_path):
            raise ValueError("provider endpoint contains control characters")
        path = parsed.path.rstrip("/")
        lower_path = path.lower()
        lower_decoded_path = decoded_path.lower().rstrip("/")
    else:
        assert check_cancelled is not None
        lower_decoded_path = _validate_decoded_endpoint_path_interruptibly(
            parsed.path,
            check_cancelled=check_cancelled,
        )
        if lower_decoded_path.endswith(_OPERATION_PATH_SUFFIXES):
            raise ValueError("provider endpoint must not include an operation path")
        path_end = len(parsed.path)
        while path_end and parsed.path[path_end - 1] == "/":
            path_end -= 1
            if path_end and path_end % _ROUTE_POLL_BYTES == 0:
                check_cancelled()
        path = parsed.path[:path_end]
        suffix_size = max(len(suffix) for suffix in _OPERATION_PATH_SUFFIXES)
        lower_path = path[-suffix_size:].lower()
    if lower_path.endswith(_OPERATION_PATH_SUFFIXES) or lower_decoded_path.endswith(
        _OPERATION_PATH_SUFFIXES
    ):
        raise ValueError("provider endpoint must not include an operation path")

    host_value = parsed.hostname
    assert host_value is not None
    host = (
        host_value.lower()
        if not interruptible
        else _lower_text_interruptibly(
            host_value,
            check_cancelled=check_cancelled,
        )
    )
    host = f"[{host}]" if ":" in host else host
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider endpoint has an invalid port") from exc
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


_CANONICAL_KEY_SUFFIXES = ("apikey", "accesstoken", "clientsecret")
_CANONICAL_KEY_LIMIT = max(
    map(
        len,
        (
            *_SENSITIVE_OPTION_KEYS,
            *_ENDPOINT_OPTION_KEYS,
            *_RUNTIME_TOP_LEVEL_KEYS,
            *_RUNTIME_ENCODE_KEYS,
            *_RUNTIME_MODEL_KEYS,
            *_CANONICAL_KEY_SUFFIXES,
            "encodekwargs",
            "modelkwargs",
        ),
    )
)


def _canonical_key_interruptibly(
    value: str,
    *,
    check_cancelled: Callable[[], None],
) -> tuple[str | None, str]:
    """Return a bounded exact key, plus its bounded canonical suffix."""

    exact_parts: list[str] | None = []
    canonical_length = 0
    suffix = ""
    value_length = len(value)
    for offset in range(0, value_length, _ROUTE_POLL_BYTES):
        end = min(value_length, offset + _ROUTE_POLL_BYTES)
        piece = re.sub(r"[^a-z0-9]", "", value[offset:end].lower())
        canonical_length += len(piece)
        if exact_parts is not None:
            if canonical_length <= _CANONICAL_KEY_LIMIT:
                exact_parts.append(piece)
            else:
                exact_parts = None
        suffix = (suffix + piece)[-_CANONICAL_KEY_LIMIT:]
        if end < value_length:
            check_cancelled()
    exact = None if exact_parts is None else "".join(exact_parts)
    return exact, suffix


def _canonical_key_parts(
    value: str,
    *,
    check_cancelled: Callable[[], None] | None,
) -> tuple[str | None, str]:
    if check_cancelled is None:
        canonical = _canonical_key(value)
        return canonical, canonical[-_CANONICAL_KEY_LIMIT:]
    return _canonical_key_interruptibly(
        value,
        check_cancelled=check_cancelled,
    )


def _attest_json_integer(value: int) -> None:
    digit_limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if digit_limit and value.bit_length() > (digit_limit + 1) * 4:
        raise ValueError("JSON integer exceeds the interpreter digit limit")
    # Exercise the same interpreter conversion gate used by ``json.dumps``;
    # the bit-length preflight keeps this current validation bounded.
    str(value)


def _detach_json_object_key(key: Any) -> str:
    if isinstance(key, str):
        return key if type(key) is str else str.__str__(key)
    if key is None:
        return "null"
    if type(key) is bool:
        return "true" if key else "false"
    if isinstance(key, int):
        detached = int.__int__(key)
        _attest_json_integer(detached)
        return str(detached)
    if isinstance(key, float):
        detached_float = float.__float__(key)
        if not math.isfinite(detached_float):
            raise ValueError("JSON numbers must be finite")
        return json.dumps(
            detached_float,
            allow_nan=False,
            separators=(",", ":"),
        )
    raise TypeError("JSON object keys must be strings or scalar JSON keys")


def _json_object_key_category(key: Any) -> str:
    if isinstance(key, str):
        return "text"
    if key is None:
        return "null"
    if isinstance(key, (bool, int, float)):
        return "number"
    raise TypeError("JSON object keys must be strings or scalar JSON keys")


def _compare_json_object_keys_interruptibly(
    left: Any,
    right: Any,
    *,
    check_cancelled: Callable[[], None],
) -> int:
    category = _json_object_key_category(left)
    if category != _json_object_key_category(right):
        raise TypeError("JSON object keys cannot be sorted together")
    if category == "text":
        left_text = left if type(left) is str else str.__str__(left)
        right_text = right if type(right) is str else str.__str__(right)
        shared_length = min(len(left_text), len(right_text))
        for offset in range(0, shared_length, _ROUTE_POLL_BYTES):
            end = min(shared_length, offset + _ROUTE_POLL_BYTES)
            left_piece = left_text[offset:end]
            right_piece = right_text[offset:end]
            if left_piece < right_piece:
                return -1
            if left_piece > right_piece:
                return 1
            if end < shared_length:
                check_cancelled()
        return (len(left_text) > len(right_text)) - (len(left_text) < len(right_text))
    if category == "null":
        return 0
    left_number = (
        float.__float__(left)
        if isinstance(left, float)
        else int.__int__(left) if isinstance(left, int) else left
    )
    right_number = (
        float.__float__(right)
        if isinstance(right, float)
        else int.__int__(right) if isinstance(right, int) else right
    )
    return (left_number > right_number) - (left_number < right_number)


def _sort_json_copied_items_interruptibly(
    values: list[tuple[Any, str, Any]],
    *,
    check_cancelled: Callable[[], None],
) -> list[tuple[Any, str, Any]]:
    def compare(
        left: tuple[Any, str, Any],
        right: tuple[Any, str, Any],
    ) -> int:
        return _compare_json_object_keys_interruptibly(
            left[0],
            right[0],
            check_cancelled=check_cancelled,
        )

    runs: list[list[tuple[Any, str, Any]]] = []
    for start in range(0, len(values), 256):
        end = min(len(values), start + 256)
        run = values[start:end]
        run.sort(key=cmp_to_key(compare))
        runs.append(run)
        if end < len(values):
            check_cancelled()
    while len(runs) > 1:
        check_cancelled()
        merged_runs: list[list[tuple[Any, str, Any]]] = []
        for run_index in range(0, len(runs), 2):
            left = runs[run_index]
            if run_index + 1 == len(runs):
                merged_runs.append(left)
                continue
            right = runs[run_index + 1]
            merged: list[tuple[Any, str, Any]] = []
            left_index = 0
            right_index = 0
            merged_count = len(left) + len(right)
            while left_index < len(left) and right_index < len(right):
                if compare(left[left_index], right[right_index]) <= 0:
                    merged.append(left[left_index])
                    left_index += 1
                else:
                    merged.append(right[right_index])
                    right_index += 1
                if len(merged) < merged_count:
                    check_cancelled()
            for remaining, offset in (
                (left, left_index),
                (right, right_index),
            ):
                for index in range(offset, len(remaining)):
                    merged.append(remaining[index])
                    if len(merged) < merged_count:
                        check_cancelled()
            merged_runs.append(merged)
        runs = merged_runs
    return runs[0] if runs else []


def _interruptible_json_copy(
    value: Any,
    *,
    check_cancelled: Callable[[], None],
    current_key_policy: (
        Callable[[str, tuple[str, ...], Callable[[], None]], None] | None
    ) = None,
    current_item_policy: (
        Callable[[str, tuple[str, ...], Any, Callable[[], None]], None] | None
    ) = None,
    _path: tuple[str, ...] = (),
    _active: set[int] | None = None,
) -> Any:
    if _active is None:
        _active = set()

    def copy_items(items: Any, *, item_count: int | None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        detached_keys: set[str] = set()
        copied_items: list[tuple[Any, str, Any]] = []
        key_category: str | None = None
        iterator = iter(items)
        if item_count is None:
            check_cancelled()
        index = 0
        while True:
            try:
                key, item = next(iterator)
            except StopIteration:
                break
            current_category = _json_object_key_category(key)
            if key_category is not None and current_category != key_category:
                raise TypeError("JSON object keys cannot be sorted together")
            key_category = current_category
            detached_key = _detach_json_object_key(key)
            if detached_key in detached_keys:
                raise TypeError("JSON object keys collide after normalization")
            detached_keys.add(detached_key)
            if current_key_policy is not None:
                current_key_policy(detached_key, _path, check_cancelled)
            detached_item = _interruptible_json_copy(
                item,
                check_cancelled=check_cancelled,
                current_key_policy=current_key_policy,
                current_item_policy=current_item_policy,
                _path=(*_path, detached_key),
                _active=_active,
            )
            if current_item_policy is not None:
                current_item_policy(
                    detached_key,
                    _path,
                    detached_item,
                    check_cancelled,
                )
            copied_items.append((key, detached_key, detached_item))
            index += 1
            if item_count is None or index < item_count:
                check_cancelled()
        if len(copied_items) > 1:
            copied_items = _sort_json_copied_items_interruptibly(
                copied_items,
                check_cancelled=check_cancelled,
            )
        for _key, detached_key, detached_item in copied_items:
            result[detached_key] = detached_item
        return result

    if type(value) is dict:
        identity = id(value)
        if identity in _active:
            raise TypeError("circular JSON value")
        _active.add(identity)
        try:
            return copy_items(value.items(), item_count=len(value))
        finally:
            _active.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in _active:
            raise TypeError("circular JSON value")
        _active.add(identity)
        try:
            check_cancelled()
            items = value.items()
            check_cancelled()
            return copy_items(items, item_count=None)
        finally:
            _active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in _active:
            raise TypeError("circular JSON value")
        _active.add(identity)
        result_items: list[Any] = []
        try:
            if type(value) is list:
                item_count = list.__len__(value)
                for index in range(item_count):
                    result_items.append(
                        _interruptible_json_copy(
                            list.__getitem__(value, index),
                            check_cancelled=check_cancelled,
                            current_key_policy=current_key_policy,
                            current_item_policy=current_item_policy,
                            _path=(*_path, str(index)),
                            _active=_active,
                        )
                    )
                    if index + 1 < item_count:
                        check_cancelled()
            elif type(value) is tuple:
                item_count = tuple.__len__(value)
                for index in range(item_count):
                    result_items.append(
                        _interruptible_json_copy(
                            tuple.__getitem__(value, index),
                            check_cancelled=check_cancelled,
                            current_key_policy=current_key_policy,
                            current_item_policy=current_item_policy,
                            _path=(*_path, str(index)),
                            _active=_active,
                        )
                    )
                    if index + 1 < item_count:
                        check_cancelled()
            else:
                check_cancelled()
                iterator = iter(value)
                check_cancelled()
                while True:
                    try:
                        item = next(iterator)
                    except StopIteration:
                        break
                    result_items.append(
                        _interruptible_json_copy(
                            item,
                            check_cancelled=check_cancelled,
                            current_key_policy=current_key_policy,
                            current_item_policy=current_item_policy,
                            _path=(*_path, str(len(result_items))),
                            _active=_active,
                        )
                    )
                    check_cancelled()
            return result_items
        finally:
            _active.remove(identity)
    if value is None or type(value) in {str, bool}:
        return value
    if type(value) is int:
        _attest_json_integer(value)
        return value
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        detached_int = int.__int__(value)
        _attest_json_integer(detached_int)
        return detached_int
    if isinstance(value, float):
        detached = float.__float__(value)
        if not (float("-inf") < detached < float("inf")):
            raise ValueError("JSON numbers must be finite")
        return detached
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_mapping(
    value: Mapping[str, Any] | None,
    *,
    source: str,
    check_cancelled: Callable[[], None] | None = None,
    current_key_policy: (
        Callable[[str, tuple[str, ...], Callable[[], None]], None] | None
    ) = None,
    current_item_policy: (
        Callable[[str, tuple[str, ...], Any, Callable[[], None]], None] | None
    ) = None,
) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError(f"{source} must be a mapping")
    if check_cancelled is not None:
        if not callable(check_cancelled):
            raise TypeError("route cancellation check must be callable")
        callback_errors: list[BaseException] = []
        policy_errors: list[BaseException] = []

        def poll() -> None:
            try:
                check_cancelled()
            except BaseException as error:  # noqa: B036 - exact provenance
                callback_errors.append(error)
                raise

        def attest_current_key(
            key: str,
            path: tuple[str, ...],
            _current_poll: Callable[[], None] = poll,
        ) -> None:
            if current_key_policy is None:
                return
            try:
                current_key_policy(key, path, poll)
            except BaseException as error:  # noqa: B036 - exact policy result
                policy_errors.append(error)
                raise

        def attest_current_item(
            key: str,
            path: tuple[str, ...],
            item: Any,
            _current_poll: Callable[[], None] = poll,
        ) -> None:
            if current_item_policy is None:
                return
            try:
                current_item_policy(key, path, item, poll)
            except BaseException as error:  # noqa: B036 - exact policy result
                policy_errors.append(error)
                raise

        try:
            if value is None:
                source_value: Any = {}
                copied = {}
            elif type(value) is dict:
                source_value = value
                copied = _interruptible_json_copy(
                    source_value,
                    check_cancelled=poll,
                    current_key_policy=attest_current_key,
                    current_item_policy=attest_current_item,
                )
            else:
                # Mirror ``dict(value or {})``: arbitrary Mapping truthiness is
                # caller-controlled, so poll before it and, when truthy, before
                # its next method.  A falsey current result is terminal and
                # needs no trailing cancellation check.
                poll()
                if not value:
                    copied = {}
                    source_value = {}
                else:
                    poll()
                    source_value = {}
                    copied_items: list[tuple[Any, str, Any]] = []
                    keys_source = value.keys()
                    poll()
                    keys = iter(keys_source)
                    poll()
                    key_category: str | None = None
                    while True:
                        try:
                            key = next(keys)
                        except StopIteration:
                            break
                        current_category = _json_object_key_category(key)
                        if (
                            key_category is not None
                            and current_category != key_category
                        ):
                            raise TypeError(
                                "JSON object keys cannot be sorted together"
                            )
                        key_category = current_category
                        detached_key = _detach_json_object_key(key)
                        if detached_key in source_value:
                            raise TypeError(
                                "JSON object keys collide after normalization"
                            )
                        attest_current_key(detached_key, ())
                        poll()
                        detached_value = _interruptible_json_copy(
                            value[key],
                            check_cancelled=poll,
                            current_key_policy=attest_current_key,
                            current_item_policy=attest_current_item,
                            _path=(detached_key,),
                        )
                        attest_current_item(detached_key, (), detached_value)
                        source_value[detached_key] = detached_value
                        copied_items.append((key, detached_key, detached_value))
                        poll()
                    if len(copied_items) > 1:
                        copied_items = _sort_json_copied_items_interruptibly(
                            copied_items,
                            check_cancelled=poll,
                        )
                    copied = {
                        detached_key: detached_value
                        for _key, detached_key, detached_value in copied_items
                    }
        except (TypeError, ValueError) as exc:
            if any(exc is callback_error for callback_error in callback_errors) or any(
                exc is policy_error for policy_error in policy_errors
            ):
                raise
            raise ValueError(f"{source} must contain JSON-compatible values") from exc
        assert isinstance(copied, dict)
        return copied
    try:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must contain JSON-compatible values") from exc
    return dict(json.loads(encoded))


def _sorted_route_keys(
    mapping: dict[str, Any],
    *,
    check_cancelled: Callable[[], None] | None,
) -> tuple[str, ...]:
    if check_cancelled is None:
        return tuple(sorted(mapping))
    keys: list[str] = []
    key_count = len(mapping)
    for index, key in enumerate(mapping):
        keys.append(key)
        if index + 1 < key_count:
            check_cancelled()

    def compare(left: str, right: str) -> int:
        if left is right:
            return 0
        shared_length = min(len(left), len(right))
        for offset in range(0, shared_length, _ROUTE_POLL_BYTES):
            end = min(shared_length, offset + _ROUTE_POLL_BYTES)
            left_piece = left[offset:end]
            right_piece = right[offset:end]
            if left_piece < right_piece:
                return -1
            if left_piece > right_piece:
                return 1
            if end < shared_length:
                check_cancelled()
        return (len(left) > len(right)) - (len(left) < len(right))

    runs: list[list[str]] = []
    for start in range(0, len(keys), 256):
        end = min(len(keys), start + 256)
        run = keys[start:end]
        run.sort(key=cmp_to_key(compare))
        runs.append(run)
        if end < len(keys):
            check_cancelled()
    while len(runs) > 1:
        check_cancelled()
        merged_runs: list[list[str]] = []
        for run_index in range(0, len(runs), 2):
            left = runs[run_index]
            if run_index + 1 == len(runs):
                merged_runs.append(left)
                continue
            right = runs[run_index + 1]
            merged: list[str] = []
            merged_count = len(left) + len(right)
            left_index = 0
            right_index = 0
            while left_index < len(left) and right_index < len(right):
                if compare(left[left_index], right[right_index]) <= 0:
                    merged.append(left[left_index])
                    left_index += 1
                else:
                    merged.append(right[right_index])
                    right_index += 1
                if len(merged) < merged_count:
                    check_cancelled()
            for remaining, start in (
                (left, left_index),
                (right, right_index),
            ):
                for remaining_index in range(start, len(remaining)):
                    merged.append(remaining[remaining_index])
                    if len(merged) < merged_count:
                        check_cancelled()
            merged_runs.append(merged)
        runs = merged_runs
    return tuple(runs[0]) if runs else ()


def _route_json_pieces_interruptibly(
    value: Any,
    *,
    check_cancelled: Callable[[], None],
) -> list[str]:
    """Encode canonical route JSON pieces without a callback-bearing generator."""

    pieces: list[str] = []

    def append(current: Any) -> None:
        if current is None:
            pieces.append("null")
        elif current is True:
            pieces.append("true")
        elif current is False:
            pieces.append("false")
        elif type(current) is str:
            from json.encoder import encode_basestring_ascii

            pieces.append('"')
            value_length = len(current)
            for offset in range(0, value_length, _ROUTE_POLL_BYTES):
                encoded = encode_basestring_ascii(
                    current[offset : offset + _ROUTE_POLL_BYTES]
                )
                pieces.append(encoded[1:-1])
                if offset + _ROUTE_POLL_BYTES < value_length:
                    check_cancelled()
            pieces.append('"')
        elif type(current) is int:
            pieces.append(str(current))
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError("route JSON number is not finite")
            pieces.append(json.dumps(current, allow_nan=False, separators=(",", ":")))
        elif type(current) is list:
            pieces.append("[")
            item_count = len(current)
            for index, item in enumerate(current):
                if index:
                    pieces.append(",")
                append(item)
                if index + 1 < item_count:
                    check_cancelled()
            pieces.append("]")
        elif type(current) is dict:
            pieces.append("{")
            keys = _sorted_route_keys(
                current,
                check_cancelled=check_cancelled,
            )
            key_count = len(keys)
            for index, key in enumerate(keys):
                if index:
                    pieces.append(",")
                append(key)
                pieces.append(":")
                append(current[key])
                if index + 1 < key_count:
                    check_cancelled()
            pieces.append("}")
        else:  # pragma: no cover - values were detached above
            raise TypeError(f"unsupported route JSON value: {type(current).__name__}")

    append(value)
    return pieces


def _json_dumps_interruptibly(
    value: Any,
    *,
    check_cancelled: Callable[[], None],
) -> str:
    """Encode canonical route JSON with bounded scalar and container polling."""

    pieces = _route_json_pieces_interruptibly(
        value,
        check_cancelled=check_cancelled,
    )
    if len(pieces) > 1:
        check_cancelled()
    return "".join(pieces)


def _freeze_route_json(
    value: Any,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[Any, ...]:
    if type(value) is dict:
        keys = _sorted_route_keys(
            value,
            check_cancelled=check_cancelled,
        )
        frozen_items: list[tuple[str, tuple[Any, ...]]] = []
        key_count = len(keys)
        for index, key in enumerate(keys):
            frozen_items.append(
                (
                    key,
                    _freeze_route_json(
                        value[key],
                        check_cancelled=check_cancelled,
                    ),
                )
            )
            if check_cancelled is not None and index + 1 < key_count:
                check_cancelled()
        return ("object", tuple(frozen_items))
    if type(value) is list:
        frozen_values: list[tuple[Any, ...]] = []
        value_count = len(value)
        for index, item in enumerate(value):
            frozen_values.append(
                _freeze_route_json(item, check_cancelled=check_cancelled)
            )
            if check_cancelled is not None and index + 1 < value_count:
                check_cancelled()
        return ("array", tuple(frozen_values))
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if type(value) is int:
        return ("integer", value)
    if type(value) is float:
        return ("float", repr(value))
    if type(value) is str:
        return ("string", value)
    raise AssertionError(f"unsupported route JSON scalar: {type(value).__name__}")


def _thaw_route_json(
    value: tuple[Any, ...],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> Any:
    kind = value[0]
    if kind == "null":
        return None
    payload = value[1]
    if kind == "object":
        result: dict[str, Any] = {}
        item_count = len(payload)
        for index, (key, item) in enumerate(payload):
            result[key] = _thaw_route_json(
                item,
                check_cancelled=check_cancelled,
            )
            if check_cancelled is not None and index + 1 < item_count:
                check_cancelled()
        return result
    if kind == "array":
        result_items: list[Any] = []
        item_count = len(payload)
        for index, item in enumerate(payload):
            result_items.append(_thaw_route_json(item, check_cancelled=check_cancelled))
            if check_cancelled is not None and index + 1 < item_count:
                check_cancelled()
        return result_items
    if kind in {"boolean", "integer", "string"}:
        return payload
    if kind == "float":
        return float(payload)
    raise AssertionError(f"unsupported frozen route JSON kind: {kind!r}")


def _route_json_equal_interruptibly(
    left: Any,
    right: Any,
    *,
    check_cancelled: Callable[[], None],
) -> bool:
    """Compare detached JSON while attesting each current item before polling."""

    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if len(left) != len(right):
            return False
        item_count = len(left)
        left_keys = _sorted_route_keys(left, check_cancelled=check_cancelled)
        right_keys = _sorted_route_keys(right, check_cancelled=check_cancelled)
        for index, (left_key, right_key) in enumerate(
            zip(left_keys, right_keys, strict=True)
        ):
            if not _route_json_equal_interruptibly(
                left_key,
                right_key,
                check_cancelled=check_cancelled,
            ):
                return False
            if not _route_json_equal_interruptibly(
                dict.__getitem__(left, left_key),
                dict.__getitem__(right, right_key),
                check_cancelled=check_cancelled,
            ):
                return False
            if index + 1 < item_count:
                check_cancelled()
        return True
    if type(left) is list:
        if len(left) != len(right):
            return False
        item_count = len(left)
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            if not _route_json_equal_interruptibly(
                left_item,
                right_item,
                check_cancelled=check_cancelled,
            ):
                return False
            if index + 1 < item_count:
                check_cancelled()
        return True
    if type(left) in {str, bytes}:
        if left is right:
            return True
        if len(left) != len(right):
            return False
        value_length = len(left)
        for offset in range(0, value_length, _ROUTE_POLL_BYTES):
            end = min(value_length, offset + _ROUTE_POLL_BYTES)
            if left[offset:end] != right[offset:end]:
                return False
            if end < value_length:
                check_cancelled()
        return True
    return bool(left == right)


def _reject_private_options(
    value: Any,
    *,
    source: str,
    path: tuple[str, ...] = (),
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    if isinstance(value, Mapping):
        item_count = len(value)
        for index, (key, item) in enumerate(value.items()):
            canonical, canonical_suffix = _canonical_key_parts(
                key,
                check_cancelled=check_cancelled,
            )
            if canonical in _SENSITIVE_OPTION_KEYS or canonical_suffix.endswith(
                _CANONICAL_KEY_SUFFIXES
            ):
                location = ".".join((*path, key))
                raise ValueError(f"{source} must not contain credentials: {location}")
            _reject_private_options(
                item,
                source=source,
                path=(*path, key),
                check_cancelled=check_cancelled,
            )
            if check_cancelled is not None and index + 1 < item_count:
                check_cancelled()
    elif isinstance(value, list):
        item_count = len(value)
        for index, item in enumerate(value):
            _reject_private_options(
                item,
                source=source,
                path=(*path, str(index)),
                check_cancelled=check_cancelled,
            )
            if check_cancelled is not None and index + 1 < item_count:
                check_cancelled()


def _reject_endpoint_options(
    value: Any,
    *,
    source: str,
    path: tuple[str, ...] = (),
    check_cancelled: Callable[[], None] | None = None,
) -> None:
    if isinstance(value, Mapping):
        item_count = len(value)
        for index, (key, item) in enumerate(value.items()):
            canonical, _canonical_suffix = _canonical_key_parts(
                key,
                check_cancelled=check_cancelled,
            )
            if canonical in _ENDPOINT_OPTION_KEYS:
                location = ".".join((*path, key))
                raise ValueError(
                    f"{source} must use the dedicated endpoint field: {location}"
                )
            _reject_endpoint_options(
                item,
                source=source,
                path=(*path, key),
                check_cancelled=check_cancelled,
            )
            if check_cancelled is not None and index + 1 < item_count:
                check_cancelled()
    elif isinstance(value, list):
        item_count = len(value)
        for index, item in enumerate(value):
            _reject_endpoint_options(
                item,
                source=source,
                path=(*path, str(index)),
                check_cancelled=check_cancelled,
            )
            if check_cancelled is not None and index + 1 < item_count:
                check_cancelled()


def _attest_current_compatibility_key(
    key: str,
    path: tuple[str, ...],
    *,
    source: str,
    embedding_endpoint: bool,
    check_cancelled: Callable[[], None],
) -> str | None:
    canonical, canonical_suffix = _canonical_key_parts(
        key,
        check_cancelled=check_cancelled,
    )
    if canonical in _SENSITIVE_OPTION_KEYS or canonical_suffix.endswith(
        _CANONICAL_KEY_SUFFIXES
    ):
        location = ".".join((*path, key))
        raise ValueError(f"{source} must not contain credentials: {location}")
    if canonical in _ENDPOINT_OPTION_KEYS:
        if embedding_endpoint:
            raise ValueError(
                f"embedding endpoint must use the dedicated endpoint field: {key}"
            )
        location = ".".join((*path, key))
        raise ValueError(f"{source} must use the dedicated endpoint field: {location}")
    return canonical


def _prune_embedding_compatibility_value(
    value: Any,
    path: tuple[str, ...],
    *,
    parent_canonical: str | None = None,
    check_cancelled: Callable[[], None] | None,
) -> Any:
    if isinstance(value, list):
        result_items: list[Any] = []
        item_count = len(value)
        for index, item in enumerate(value):
            result_items.append(
                _prune_embedding_compatibility_value(
                    item,
                    (*path, str(index)),
                    check_cancelled=check_cancelled,
                )
            )
            if check_cancelled is not None and index + 1 < item_count:
                check_cancelled()
        return result_items
    if not isinstance(value, Mapping):
        return value

    result: dict[str, Any] = {}
    item_count = len(value)
    for index, (key, item) in enumerate(value.items()):
        canonical, _canonical_suffix = _canonical_key_parts(
            key,
            check_cancelled=check_cancelled,
        )
        if canonical in _ENDPOINT_OPTION_KEYS:
            raise ValueError(
                f"embedding endpoint must use the dedicated endpoint field: {key}"
            )

        operational = not path and canonical in _RUNTIME_TOP_LEVEL_KEYS
        if path and parent_canonical == "encodekwargs":
            operational = operational or canonical in _RUNTIME_ENCODE_KEYS
        if path and parent_canonical == "modelkwargs":
            operational = operational or canonical in _RUNTIME_MODEL_KEYS
        if operational:
            if check_cancelled is not None and index + 1 < item_count:
                check_cancelled()
            continue

        nested = _prune_embedding_compatibility_value(
            item,
            (*path, key),
            parent_canonical=canonical,
            check_cancelled=check_cancelled,
        )
        if nested not in ({}, []):
            result[key] = nested
        if check_cancelled is not None and index + 1 < item_count:
            check_cancelled()
    return result


def embedding_compatibility_options(
    value: Mapping[str, Any] | None,
    *,
    check_cancelled: Callable[[], None] | None = None,
    _openai_dimensions_only: bool = False,
    _openai_dimension: int | None = None,
) -> dict[str, Any]:
    """Return only public options that can affect produced embedding vectors."""

    _require_route_cancellation_check(check_cancelled)
    if check_cancelled is None:
        options = _json_mapping(value, source="embedding options")
        _reject_private_options(options, source="embedding artifact options")
    else:
        attested_canonical_keys: dict[tuple[tuple[str, ...], str], str | None] = {}

        def attest_current_key(
            key: str,
            path: tuple[str, ...],
            poll: Callable[[], None],
        ) -> None:
            canonical = _attest_current_compatibility_key(
                key,
                path,
                source="embedding artifact options",
                embedding_endpoint=True,
                check_cancelled=poll,
            )
            attested_canonical_keys[(path, key)] = canonical

        def attest_current_item(
            key: str,
            path: tuple[str, ...],
            item: Any,
            poll: Callable[[], None],
        ) -> None:
            canonical = attested_canonical_keys.pop((path, key), None)
            if not _openai_dimensions_only or path:
                return
            if canonical in _RUNTIME_TOP_LEVEL_KEYS:
                return
            retained = _prune_embedding_compatibility_value(
                item,
                (key,),
                parent_canonical=canonical,
                check_cancelled=poll,
            )
            if retained in ({}, []):
                return
            if key != "dimensions":
                raise ValueError(
                    "OpenAI-compatible embedding routes support only the "
                    f"dimensions compatibility option, not: {key}"
                )
            if retained is not None and retained != _openai_dimension:
                raise ValueError(
                    "embedding dimensions option must equal the artifact dimension"
                )

        options = _json_mapping(
            value,
            source="embedding options",
            check_cancelled=check_cancelled,
            current_key_policy=attest_current_key,
            current_item_policy=attest_current_item,
        )
        _reject_private_options(
            options,
            source="embedding artifact options",
            check_cancelled=check_cancelled,
        )

    return _prune_embedding_compatibility_value(
        options,
        (),
        check_cancelled=check_cancelled,
    )


def validate_embedding_runtime_options(
    value: Mapping[str, Any] | None,
    *,
    provider: str,
) -> dict[str, Any]:
    """Accept only process-local knobs that cannot change vector semantics."""

    if value is not None and not isinstance(value, Mapping):
        raise ValueError("embedding runtime options must be a mapping")
    canonical_provider = normalize_provider(provider)
    local = canonical_provider == "huggingface"
    allowed = _HUGGINGFACE_RUNTIME_KEYS if local else _REMOTE_RUNTIME_KEYS
    options = dict(value or {})
    validated: dict[str, Any] = {}
    for key, item in options.items():
        canonical = _canonical_key(key)
        if canonical in allowed:
            validated[allowed[canonical]] = item
            continue
        if local and canonical == "encodekwargs" and isinstance(item, Mapping):
            invalid = [
                nested
                for nested in item
                if _canonical_key(nested) not in _RUNTIME_ENCODE_KEYS
            ]
            if invalid:
                raise ValueError(
                    "embedding runtime encode_kwargs may not override vector "
                    f"semantics: {', '.join(sorted(invalid))}"
                )
            validated["encode_kwargs"] = {
                _RUNTIME_ENCODE_KEYS[_canonical_key(nested)]: nested_value
                for nested, nested_value in item.items()
            }
            continue
        if local and canonical == "modelkwargs" and isinstance(item, Mapping):
            invalid = [
                nested
                for nested in item
                if _canonical_key(nested) not in _RUNTIME_MODEL_KEYS
            ]
            if invalid:
                raise ValueError(
                    "embedding runtime model_kwargs may not override vector "
                    f"semantics: {', '.join(sorted(invalid))}"
                )
            validated["model_kwargs"] = {
                _RUNTIME_MODEL_KEYS[_canonical_key(nested)]: nested_value
                for nested, nested_value in item.items()
            }
            continue
        raise ValueError(
            "embedding runtime option may affect vector compatibility; "
            f"declare it in embedding_kwargs instead: {key}"
        )
    return validated


def _normalize_model(
    model: str,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> str:
    if check_cancelled is None:
        normalized = str(model or "").strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError(
                "inference model must be a non-empty id without whitespace"
            )
        if any(ord(character) < 32 for character in normalized):
            raise ValueError("inference model contains control characters")
        return normalized

    raw = str(model or "")
    start, end = _trim_text_bounds_interruptibly(
        raw,
        check_cancelled=check_cancelled,
    )
    if start == end:
        raise ValueError("inference model must be a non-empty id without whitespace")
    for window_start in range(start, end, _ROUTE_POLL_BYTES):
        window_end = min(end, window_start + _ROUTE_POLL_BYTES)
        for character in raw[window_start:window_end]:
            if character.isspace():
                raise ValueError(
                    "inference model must be a non-empty id without whitespace"
                )
            if ord(character) < 32:
                raise ValueError("inference model contains control characters")
        if window_end < end:
            check_cancelled()
    return raw if start == 0 and end == len(raw) else raw[start:end]


def _normalize_credential_env(
    value: str | None,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> str | None:
    if check_cancelled is None:
        if value is None or not str(value).strip():
            return None
        name = str(value).strip()
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid credential environment variable: {value!r}")
        return name
    if value is None:
        return None
    raw = str(value)
    start, end = _trim_text_bounds_interruptibly(
        raw,
        check_cancelled=check_cancelled,
    )
    if start == end:
        return None
    for position in range(start, end):
        character = raw[position]
        valid = (
            character.isascii()
            and (character.isalnum() or character == "_")
            and (position > start or character.isalpha() or character == "_")
        )
        if not valid:
            raise ValueError(f"invalid credential environment variable: {value!r}")
        if position + 1 < end and (position + 1 - start) % _ROUTE_POLL_BYTES == 0:
            check_cancelled()
    return raw[start:end]


@dataclass(frozen=True, slots=True)
class InferenceRoute:
    """A public compatibility identity plus a process-local credential source."""

    operation: InferenceOperation
    provider: str
    model: str
    client_model: str
    endpoint: str | None = None
    dimension: int | None = None
    credential_env: str | None = None
    credential_required: bool = False
    _options_frozen: tuple[Any, ...] = field(
        default=("object", ()),
        repr=False,
    )

    @property
    def compatibility_options(self) -> dict[str, Any]:
        value = _thaw_route_json(self._options_frozen)
        assert isinstance(value, dict)
        return value

    def interruptible_compatibility_options(
        self,
        check_cancelled: Callable[[], None] | None,
    ) -> dict[str, Any]:
        _require_route_cancellation_check(check_cancelled)
        if check_cancelled is None:
            return self.compatibility_options
        value = _thaw_route_json(
            self._options_frozen,
            check_cancelled=check_cancelled,
        )
        assert isinstance(value, dict)
        return value

    def public_identity(
        self,
        *,
        check_cancelled: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Return the complete secret-free identity used for compatibility."""

        _require_route_cancellation_check(check_cancelled)
        identity: dict[str, Any] = {
            "schema": INFERENCE_ROUTE_SCHEMA,
            "operation": self.operation,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "options": (
                self.compatibility_options
                if check_cancelled is None
                else self.interruptible_compatibility_options(check_cancelled)
            ),
        }
        if self.dimension is not None:
            identity["dimension"] = self.dimension
        return identity

    def embedding_backend_kwargs(self) -> dict[str, Any]:
        """Map compatibility options to the selected embedding wrapper."""

        if self.operation != "embeddings":
            raise ValueError("chat routes do not have embedding backend kwargs")
        options = self.compatibility_options
        if self.provider == "huggingface":
            return options
        return {"request_options": options} if options else {}

    @property
    def compatibility_fingerprint(self) -> str:
        payload = json.dumps(
            self.public_identity(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def interruptible_compatibility_fingerprint(
        self,
        check_cancelled: Callable[[], None] | None,
    ) -> str:
        _require_route_cancellation_check(check_cancelled)
        if check_cancelled is None:
            return self.compatibility_fingerprint
        pieces = _route_json_pieces_interruptibly(
            self.public_identity(check_cancelled=check_cancelled),
            check_cancelled=check_cancelled,
        )
        digest = hashlib.sha256()
        piece_count = len(pieces)
        if piece_count:
            check_cancelled()
        for index, piece in enumerate(pieces):
            digest.update(piece.encode("utf-8"))
            if index + 1 < piece_count:
                check_cancelled()
        return "sha256:" + digest.hexdigest()

    def credential(self, environ: Mapping[str, str] | None = None) -> str | None:
        """Resolve the selected credential without storing it on the route."""

        environment = os.environ if environ is None else environ
        value = environment.get(self.credential_env, "") if self.credential_env else ""
        if value:
            return value
        if self.credential_required:
            name = self.credential_env or "an explicit credential variable"
            raise ValueError(
                f"credential environment variable is unset or empty: {name}"
            )
        return None

    def client_kwargs(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Resolve process-local SDK kwargs; callers must never persist this value."""

        result: dict[str, str] = {}
        if self.endpoint:
            key = "api_base" if self.operation == "chat" else "base_url"
            result[key] = self.endpoint
        credential = self.credential(environ)
        if credential:
            result["api_key"] = credential
        return result


def resolve_inference_route(
    *,
    operation: InferenceOperation,
    provider: str,
    model: str,
    endpoint: str | None = None,
    dimension: int | None = None,
    credential_env: str | None = None,
    compatibility_options: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> InferenceRoute:
    """Resolve an explicit provider route without importing an inference SDK."""

    _require_route_cancellation_check(check_cancelled)
    if operation not in {"chat", "embeddings"}:
        raise ValueError(f"unsupported inference operation: {operation!r}")
    if check_cancelled is None:
        canonical_provider = normalize_provider(provider)
        canonical_model = _normalize_model(model)
        normalized_endpoint = normalize_endpoint(endpoint)
    else:
        canonical_provider = normalize_provider(
            provider,
            check_cancelled=check_cancelled,
        )
        canonical_model = _normalize_model(
            model,
            check_cancelled=check_cancelled,
        )
        normalized_endpoint = normalize_endpoint(
            endpoint,
            check_cancelled=check_cancelled,
        )
    environment = os.environ if environ is None else environ
    selected_env = (
        _normalize_credential_env(credential_env)
        if check_cancelled is None
        else _normalize_credential_env(
            credential_env,
            check_cancelled=check_cancelled,
        )
    )
    credential_was_explicit = selected_env is not None

    if canonical_provider == "huggingface":
        if operation != "embeddings":
            raise ValueError("huggingface is supported only for embeddings")
        if normalized_endpoint or selected_env:
            raise ValueError(
                "local Hugging Face embeddings do not accept an endpoint or API key"
            )
        client_model = canonical_model
        credential_required = False
    elif canonical_provider == "openai":
        if selected_env is None and environment.get("OPENAI_API_KEY"):
            selected_env = "OPENAI_API_KEY"
        if selected_env is None and normalized_endpoint is None:
            selected_env = "OPENAI_API_KEY"
        client_model = (
            canonical_model
            if operation == "embeddings" or canonical_model.startswith("openai/")
            else f"openai/{canonical_model}"
        )
        credential_required = normalized_endpoint is None or credential_was_explicit
    else:
        if operation == "embeddings":
            raise ValueError(
                f"unsupported embedding provider: {canonical_provider}; use "
                "huggingface or openai"
            )
        client_model = (
            canonical_model
            if canonical_model.startswith(f"{canonical_provider}/")
            else f"{canonical_provider}/{canonical_model}"
        )
        credential_required = credential_was_explicit

    if operation == "embeddings":
        if (
            not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension <= 0
        ):
            raise ValueError("embedding dimension must be a positive integer")
        options = (
            embedding_compatibility_options(compatibility_options)
            if check_cancelled is None
            else embedding_compatibility_options(
                compatibility_options,
                check_cancelled=check_cancelled,
                _openai_dimensions_only=canonical_provider == "openai",
                _openai_dimension=(
                    dimension if canonical_provider == "openai" else None
                ),
            )
        )
        if canonical_provider == "openai":
            if check_cancelled is None:
                unsupported = sorted(set(options) - {"dimensions"})
            else:
                unsupported = []
                option_count = len(options)
                for index, key in enumerate(options):
                    if key != "dimensions":
                        unsupported.append(key)
                        # A known current incompatibility must not be hidden by
                        # a stop requested before inspecting a possible future
                        # option.
                        break
                    if index + 1 < option_count:
                        check_cancelled()
            if unsupported:
                raise ValueError(
                    "OpenAI-compatible embedding routes support only the dimensions "
                    f"compatibility option, not: {', '.join(unsupported)}"
                )
            requested_dimension = options.get("dimensions")
            if requested_dimension is not None and requested_dimension != dimension:
                raise ValueError(
                    "embedding dimensions option must equal the artifact dimension"
                )
    else:
        if dimension is not None:
            raise ValueError("chat routes do not accept an embedding dimension")
        if check_cancelled is None:
            options = _json_mapping(
                compatibility_options,
                source="chat compatibility options",
            )
            _reject_private_options(options, source="chat compatibility options")
            _reject_endpoint_options(options, source="chat compatibility options")
        else:
            options = _json_mapping(
                compatibility_options,
                source="chat compatibility options",
                check_cancelled=check_cancelled,
                current_key_policy=lambda key, path, poll: (
                    _attest_current_compatibility_key(
                        key,
                        path,
                        source="chat compatibility options",
                        embedding_endpoint=False,
                        check_cancelled=poll,
                    )
                ),
            )
            _reject_private_options(
                options,
                source="chat compatibility options",
                check_cancelled=check_cancelled,
            )
            _reject_endpoint_options(
                options,
                source="chat compatibility options",
                check_cancelled=check_cancelled,
            )
    return InferenceRoute(
        operation=operation,
        provider=canonical_provider,
        model=canonical_model,
        client_model=client_model,
        endpoint=normalized_endpoint,
        dimension=dimension,
        credential_env=selected_env,
        credential_required=credential_required,
        _options_frozen=_freeze_route_json(
            options,
            check_cancelled=check_cancelled,
        ),
    )


def resolve_embedding_artifact_route(
    config: Mapping[str, Any],
    *,
    credential_env: str | None = None,
    environ: Mapping[str, str] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> InferenceRoute:
    """Resolve and validate the immutable embedding route in an artifact.

    Schema-v3 artifacts carry a canonical route and fingerprint. Older
    artifacts are reconstructed from their top-level embedding fields, while
    still rejecting credentials or endpoints hidden in ``embedding_kwargs``.
    """

    _require_route_cancellation_check(check_cancelled)

    def attest_artifact_current_item(
        key: str,
        path: tuple[str, ...],
        item: Any,
        _poll: Callable[[], None],
    ) -> None:
        if (
            not path
            and key == "embedding_route"
            and item is not None
            and not isinstance(item, Mapping)
        ):
            raise ValueError("vector artifact has an invalid embedding route identity")

    artifact = (
        _json_mapping(config, source="vector artifact configuration")
        if check_cancelled is None
        else _json_mapping(
            config,
            source="vector artifact configuration",
            check_cancelled=check_cancelled,
            current_item_policy=attest_artifact_current_item,
        )
    )
    builder_schema = artifact.get("builder_schema")
    route_identity_required = (
        isinstance(builder_schema, int)
        and not isinstance(builder_schema, bool)
        and builder_schema >= 3
    )
    embedded = artifact.get("embedding_route")

    if route_identity_required and not isinstance(embedded, Mapping):
        raise ValueError("vector artifact is missing its embedding route identity")

    if embedded is not None:
        if not isinstance(embedded, Mapping):
            raise ValueError("vector artifact has an invalid embedding route identity")
        embedded_route = (
            _json_mapping(
                embedded,
                source="vector artifact embedding route",
            )
            if check_cancelled is None
            else _json_mapping(
                embedded,
                source="vector artifact embedding route",
                check_cancelled=check_cancelled,
            )
        )
        if embedded_route.get("schema") != INFERENCE_ROUTE_SCHEMA:
            raise ValueError(
                "vector artifact uses an unsupported embedding route schema"
            )
        if embedded_route.get("operation") != "embeddings":
            raise ValueError("vector artifact route is not an embedding route")
        route_kwargs = {
            "operation": "embeddings",
            "provider": embedded_route.get("provider", ""),
            "model": embedded_route.get("model", ""),
            "endpoint": embedded_route.get("endpoint"),
            "dimension": embedded_route.get("dimension"),
            "credential_env": credential_env,
            "compatibility_options": embedded_route.get("options"),
            "environ": environ,
        }
        route = (
            resolve_inference_route(**route_kwargs)
            if check_cancelled is None
            else resolve_inference_route(
                **route_kwargs,
                check_cancelled=check_cancelled,
            )
        )
        public_identity = (
            route.public_identity()
            if check_cancelled is None
            else route.public_identity(check_cancelled=check_cancelled)
        )
        route_identity_matches = (
            embedded_route == public_identity
            if check_cancelled is None
            else _route_json_equal_interruptibly(
                embedded_route,
                public_identity,
                check_cancelled=check_cancelled,
            )
        )
        if not route_identity_matches:
            raise ValueError("vector artifact embedding route is not canonical")
    else:
        route_kwargs = {
            "operation": "embeddings",
            "provider": artifact.get("embedding_provider", ""),
            "model": artifact.get("embedding_model", ""),
            "endpoint": artifact.get("embedding_endpoint"),
            "dimension": artifact.get(
                "dimension",
                artifact.get("embedding_dimension"),
            ),
            "credential_env": credential_env,
            "compatibility_options": artifact.get("embedding_kwargs"),
            "environ": environ,
        }
        route = (
            resolve_inference_route(**route_kwargs)
            if check_cancelled is None
            else resolve_inference_route(
                **route_kwargs,
                check_cancelled=check_cancelled,
            )
        )

    top_level_checks = {
        "embedding_model": route.model,
        "embedding_provider": route.provider,
        "embedding_dimension": route.dimension,
        "dimension": route.dimension,
        "embedding_endpoint": route.endpoint,
    }
    if route_identity_required:
        top_level_checks["embedding_kwargs"] = (
            route.compatibility_options
            if check_cancelled is None
            else route.interruptible_compatibility_options(check_cancelled)
        )
    for key, expected in top_level_checks.items():
        if key not in artifact:
            if route_identity_required:
                raise ValueError(f"vector artifact is missing {key}")
            continue
        actual = artifact[key]
        if key == "embedding_provider" and isinstance(actual, str):
            actual = (
                normalize_provider(actual)
                if check_cancelled is None
                else normalize_provider(
                    actual,
                    check_cancelled=check_cancelled,
                )
            )
        elif key == "embedding_endpoint":
            actual = (
                normalize_endpoint(actual)
                if check_cancelled is None
                else normalize_endpoint(
                    actual,
                    check_cancelled=check_cancelled,
                )
            )
        elif key in {"embedding_dimension", "dimension"} and (
            not isinstance(actual, int) or isinstance(actual, bool)
        ):
            raise ValueError(f"vector artifact {key} is not a positive integer")
        values_match = (
            _route_json_equal_interruptibly(
                actual,
                expected,
                check_cancelled=check_cancelled,
            )
            if check_cancelled is not None
            else actual == expected
        )
        if not values_match:
            raise ValueError(f"vector artifact {key} disagrees with its route identity")

    fingerprint = artifact.get("embedding_fingerprint")
    if route_identity_required and not isinstance(fingerprint, str):
        raise ValueError("vector artifact is missing its embedding fingerprint")
    if fingerprint is not None:
        route_fingerprint = (
            route.compatibility_fingerprint
            if check_cancelled is None
            else route.interruptible_compatibility_fingerprint(check_cancelled)
        )
        if fingerprint != route_fingerprint:
            raise ValueError("vector artifact embedding fingerprint mismatch")
    return route


__all__ = [
    "INFERENCE_ROUTE_SCHEMA",
    "InferenceRoute",
    "embedding_compatibility_options",
    "normalize_endpoint",
    "normalize_provider",
    "resolve_embedding_artifact_route",
    "resolve_inference_route",
    "validate_embedding_runtime_options",
]
